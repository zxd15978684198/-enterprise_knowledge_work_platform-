"""知识库与文档：CRUD、上传入库、分块查询、重处理。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from sqlalchemy import delete, func, select

from pyknora.config import get_settings
from pyknora.deps import CurrentUser, SessionDep, WorkspaceContext
from pyknora.errors import ApiError
from pyknora.ingest.parsers import is_supported
from pyknora.ingest.pipeline import process_document
from pyknora.models import (
    Chunk,
    Document,
    KnowledgeBase,
    Membership,
    User,
    Workspace,
)
from pyknora.schemas import (
    ChunkOut,
    ChunkPage,
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from pyknora.storage import checksum, get_storage

router = APIRouter(tags=["knowledge"])

_MAX_TITLE = 300


async def _membership(session: SessionDep, user: User, workspace_id: uuid.UUID) -> WorkspaceContext:
    stmt = (
        select(Workspace, Membership)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
            Membership.user_id == user.id,
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        # 非成员与不存在同返回 404，避免泄漏工作区是否存在
        raise ApiError("not_found", "工作区不存在", status_code=status.HTTP_404_NOT_FOUND)
    workspace, membership = row
    return WorkspaceContext(workspace=workspace, membership=membership)


async def _load_kb(
    session: SessionDep, user: User, kb_id: uuid.UUID
) -> tuple[KnowledgeBase, WorkspaceContext]:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise ApiError("not_found", "知识库不存在", status_code=status.HTTP_404_NOT_FOUND)
    ctx = await _membership(session, user, kb.workspace_id)
    return kb, ctx


async def _load_document(
    session: SessionDep, user: User, document_id: uuid.UUID
) -> tuple[Document, WorkspaceContext]:
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise ApiError("not_found", "文档不存在", status_code=status.HTTP_404_NOT_FOUND)
    ctx = await _membership(session, user, document.workspace_id)
    return document, ctx


async def _kb_with_counts(session: SessionDep, kb: KnowledgeBase) -> KnowledgeBaseOut:
    documents = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
    )
    chunks = await session.scalar(
        select(func.count())
        .select_from(Chunk)
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
    )
    out = KnowledgeBaseOut.model_validate(kb)
    return out.model_copy(update={"document_count": documents or 0, "chunk_count": chunks or 0})


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


@router.get(
    "/workspaces/{workspace_id}/knowledge-bases",
    response_model=list[KnowledgeBaseOut],
    summary="知识库列表",
)
async def list_knowledge_bases(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[KnowledgeBaseOut]:
    await _membership(session, user, workspace_id)
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.deleted_at.is_(None))
        .order_by(KnowledgeBase.created_at)
    )
    rows = (await session.scalars(stmt)).all()
    return [await _kb_with_counts(session, kb) for kb in rows]


@router.post(
    "/workspaces/{workspace_id}/knowledge-bases",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
)
async def create_knowledge_base(
    workspace_id: uuid.UUID,
    payload: KnowledgeBaseCreate,
    user: CurrentUser,
    session: SessionDep,
) -> KnowledgeBaseOut:
    await _membership(session, user, workspace_id)
    duplicated = await session.scalar(
        select(func.count())
        .select_from(KnowledgeBase)
        .where(
            KnowledgeBase.workspace_id == workspace_id,
            KnowledgeBase.name == payload.name,
            KnowledgeBase.deleted_at.is_(None),
        )
    )
    if duplicated:
        raise ApiError(
            "name_taken", "同一工作区内知识库名称重复", status_code=status.HTTP_409_CONFLICT
        )
    kb = KnowledgeBase(
        workspace_id=workspace_id,
        name=payload.name,
        description=payload.description,
        created_by=user.id,
    )
    session.add(kb)
    await session.commit()
    return await _kb_with_counts(session, kb)


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut, summary="知识库详情")
async def get_knowledge_base(
    kb_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> KnowledgeBaseOut:
    kb, _ = await _load_kb(session, user, kb_id)
    return await _kb_with_counts(session, kb)


@router.patch("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut, summary="修改知识库")
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    user: CurrentUser,
    session: SessionDep,
) -> KnowledgeBaseOut:
    kb, _ = await _load_kb(session, user, kb_id)
    if payload.name is not None and payload.name != kb.name:
        taken = await session.scalar(
            select(func.count())
            .select_from(KnowledgeBase)
            .where(
                KnowledgeBase.workspace_id == kb.workspace_id,
                KnowledgeBase.name == payload.name,
                KnowledgeBase.id != kb.id,
                KnowledgeBase.deleted_at.is_(None),
            )
        )
        if taken:
            raise ApiError(
                "name_taken", "同一工作区内知识库名称重复", status_code=status.HTTP_409_CONFLICT
            )
        kb.name = payload.name
    if payload.description is not None:
        kb.description = payload.description
    await session.commit()
    return await _kb_with_counts(session, kb)


@router.delete(
    "/knowledge-bases/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除知识库（软删除，连带其文档）",
)
async def delete_knowledge_base(
    kb_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    kb, _ = await _load_kb(session, user, kb_id)
    kb.deleted_at = func.now()
    await session.execute(
        Document.__table__.update()
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=list[DocumentOut],
    summary="知识库下文档列表",
)
async def list_documents(
    kb_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentOut]:
    kb, _ = await _load_kb(session, user, kb_id)
    stmt = (
        select(Document)
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.scalars(stmt)).all()
    return [DocumentOut.model_validate(row) for row in rows]


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传文档并入库（同步解析 + 分块）",
)
async def upload_document(
    kb_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    file: Annotated[UploadFile, File(description="待入库文件")],
    title: Annotated[str | None, Form()] = None,
) -> DocumentOut:
    kb, _ = await _load_kb(session, user, kb_id)
    settings = get_settings()

    filename = file.filename or "upload"
    if not is_supported(filename):
        raise ApiError(
            "unsupported_format",
            "不支持的格式；支持 txt/md/html/pdf/docx",
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    data = await file.read()
    if not data:
        raise ApiError("empty_file", "文件内容为空", status_code=status.HTTP_400_BAD_REQUEST)
    if len(data) > settings.max_upload_bytes:
        raise ApiError(
            "file_too_large",
            f"单文件上限 {settings.max_upload_bytes} 字节",
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )

    digest = checksum(data)
    existing = await session.scalar(
        select(Document).where(
            Document.knowledge_base_id == kb.id,
            Document.checksum == digest,
            Document.deleted_at.is_(None),
        )
    )
    if existing is not None:
        # 幂等：同知识库内相同内容不重复入库，直接返回既有记录
        response.status_code = status.HTTP_200_OK
        return DocumentOut.model_validate(existing)

    document_id = uuid.uuid4()
    storage_key = f"{kb.workspace_id}/{document_id}/{filename}"
    get_storage().put(storage_key, data)

    document = Document(
        id=document_id,
        workspace_id=kb.workspace_id,
        knowledge_base_id=kb.id,
        title=(title or filename.rsplit(".", 1)[0])[:_MAX_TITLE],
        source_type="upload",
        filename=filename,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        checksum=digest,
        storage_key=storage_key,
        status="pending",
        created_by=user.id,
    )
    session.add(document)
    await session.commit()

    await process_document(document.id)
    refreshed = await session.get(Document, document.id)
    assert refreshed is not None
    await session.refresh(refreshed)
    return DocumentOut.model_validate(refreshed)


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="文档详情")
async def get_document(
    document_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> DocumentOut:
    document, _ = await _load_document(session, user, document_id)
    return DocumentOut.model_validate(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档（软删除并清空分块）",
)
async def delete_document(
    document_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    document, _ = await _load_document(session, user, document_id)
    document.deleted_at = func.now()
    document.chunk_count = 0
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    if document.storage_key:
        get_storage().delete(document.storage_key)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/documents/{document_id}/chunks",
    response_model=ChunkPage,
    summary="文档分块（分页）",
)
async def list_chunks(
    document_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChunkPage:
    document, _ = await _load_document(session, user, document_id)
    total = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document.id)
    )
    stmt = (
        select(Chunk)
        .where(Chunk.document_id == document.id)
        .order_by(Chunk.ordinal)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.scalars(stmt)).all()
    return ChunkPage(
        items=[ChunkOut.model_validate(row) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentOut,
    summary="重新解析入库（版本 +1）",
)
async def reprocess_document(
    document_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> DocumentOut:
    document, _ = await _load_document(session, user, document_id)
    document.version += 1
    document.status = "pending"
    document.error = None
    await session.commit()

    await process_document(document.id)
    refreshed = await session.get(Document, document.id)
    assert refreshed is not None
    await session.refresh(refreshed)
    return DocumentOut.model_validate(refreshed)
