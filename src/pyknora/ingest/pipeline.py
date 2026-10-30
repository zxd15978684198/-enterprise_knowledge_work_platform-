"""入库流水线：对象存储 → 解析 → 分块 → 落库。

当前为**同步内联**执行（W19）：上传接口在返回前完成解析与切块，小文件可控、
测试确定性高。W22 引入 Celery 后只需把 :func:`process_document` 丢进 worker，
调用方与状态机不变。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, select

from pyknora.config import get_settings
from pyknora.db import session_scope
from pyknora.ingest.chunker import split_text
from pyknora.ingest.parsers import ParseError, parse_bytes
from pyknora.models import Chunk, Document
from pyknora.storage import get_storage

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 500


async def process_document(document_id: uuid.UUID) -> str:
    """执行一次入库；返回最终状态（``ready`` / ``failed``）。

    * 重复调用是幂等的：先删旧分块再写新分块，同一事务内完成，读者只会看到
      旧版本或新版本，不会看到半成品；
    * 任何异常都会落到 ``failed`` 并把原因写进 ``documents.error``，不向上抛，
      避免后台任务把进程带崩。
    """

    settings = get_settings()
    storage = get_storage()

    async with session_scope() as session:
        document = await session.get(Document, document_id)
        if document is None:
            logger.warning("入库跳过：文档不存在 document_id=%s", document_id)
            return "failed"

        document.status = "processing"
        document.error = None
        await session.commit()

        try:
            if not document.storage_key:
                raise ParseError("文档缺少对象存储 key")
            data = storage.get(document.storage_key)
            parsed = parse_bytes(data, document.filename or document.title, document.mime_type)
            chunks = split_text(
                parsed.text,
                size=settings.chunk_size,
                overlap=settings.chunk_overlap,
                max_chunks=settings.max_chunks_per_document,
            )
            if not chunks:
                raise ParseError("分块结果为空")

            await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
            session.add_all(
                [
                    Chunk(
                        workspace_id=document.workspace_id,
                        document_id=document.id,
                        ordinal=chunk.ordinal,
                        content=chunk.content,
                        char_count=chunk.char_count,
                        page=chunk.page,
                    )
                    for chunk in chunks
                ]
            )
            document.mime_type = parsed.mime_type
            document.chunk_count = len(chunks)
            document.status = "ready"
            await session.commit()
            logger.info(
                "入库完成 document_id=%s chunks=%d pages=%d warnings=%d",
                document.id,
                len(chunks),
                parsed.pages,
                len(parsed.warnings),
            )
            return "ready"
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("入库失败 document_id=%s error=%s", document_id, message)
            await session.rollback()
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = "failed"
                document.error = message[:_MAX_ERROR_CHARS]
                await session.commit()
            return "failed"


async def recount_chunks(document_id: uuid.UUID) -> int:
    """统计分块数（诊断与测试用）。"""

    async with session_scope() as session:
        rows = await session.scalars(select(Chunk.id).where(Chunk.document_id == document_id))
        return len(rows.all())
