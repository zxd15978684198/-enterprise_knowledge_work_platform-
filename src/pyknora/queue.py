"""Celery 队列（W22）：把文档入库从请求线程搬到 worker。

设计要点：

* **开关在配置**：``PYKNORA_INGEST_MODE`` 为 ``inline``（默认，开发/测试）时完全不碰
  Celery；为 ``queue`` 时 API 只落库并投递任务，worker 消费。
* **worker 侧自建引擎**：任务里 ``asyncio.run`` 一个新事件循环，结束后
  ``dispose_engine()``，不与 API 进程的连接池共享（跨进程本来也不共享，这里防的是
  同一 worker 进程内多次任务复用脏连接）。
* **幂等**：任务重复投递时 ``process_document`` 本身幂等（先删旧分块再写），
  与 W19 语义一致；``task_acks_late`` + 单并发预取降低「投递丢失但已确认」的概率。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from celery import Celery

from pyknora.config import get_settings

logger = logging.getLogger(__name__)


def create_celery() -> Celery:
    settings = get_settings()
    app = Celery("pyknora", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        task_time_limit=settings.celery_task_time_limit,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        broker_connection_retry_on_startup=True,
        task_default_queue="pyknora",
    )
    return app


celery_app = create_celery()
#: ``celery -A pyknora.queue worker`` 的入口别名
app = celery_app


@celery_app.task(name="pyknora.ingest_document")
def ingest_document(document_id: str) -> str:
    """worker 任务：执行一次入库，返回最终状态（``ready`` / ``failed``）。"""

    from pyknora.db import dispose_engine
    from pyknora.ingest.pipeline import process_document

    async def _run() -> str:
        try:
            return await process_document(uuid.UUID(document_id))
        finally:
            await dispose_engine()

    return asyncio.run(_run())


def enqueue_ingest(document_id: uuid.UUID) -> str:
    """投递入库任务，返回 Celery task id（日志与排障用）。"""

    result = ingest_document.delay(str(document_id))
    logger.info("已投递入库任务 document_id=%s task_id=%s", document_id, result.id)
    return result.id
