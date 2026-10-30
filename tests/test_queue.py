"""队列形态（W22）：Celery 配置、任务注册与「上传即返回 queued」的分派语义。

不连真实 broker：``enqueue_ingest`` 被替换为记录器，验证 API 侧只落库+投递，
真正的 worker 消费由 Compose 冒烟与 E2E 覆盖。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from pyknora import queue as queue_module
from pyknora.config import get_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _restore_settings(tmp_path: Path):  # type: ignore[no-untyped-def]
    """用例改过 ingest_mode 后清缓存，避免污染后续用例。"""

    from pyknora.storage import reset_storage

    reset_storage(tmp_path)
    yield
    get_settings.cache_clear()


def test_celery_config() -> None:
    app = queue_module.create_celery()
    assert app.conf.task_default_queue == "pyknora"
    assert app.conf.task_acks_late is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.broker_url.startswith("redis://")
    assert app.conf.broker_connection_retry_on_startup is True


def test_task_is_registered_with_stable_name() -> None:
    assert queue_module.ingest_document.name == "pyknora.ingest_document"
    assert "pyknora.ingest_document" in queue_module.celery_app.tasks
    # worker 入口别名必须指向同一 app，否则 `celery -A pyknora.queue` 找不到任务。
    assert queue_module.app is queue_module.celery_app


def test_enqueue_ingest_delegates_to_send_task(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    def fake_send_task(
        name: str,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        recorded.append((name, args or ()))
        return type("Result", (), {"id": "task-123"})()

    # Task.delay → Task.apply_async → app.send_task；patch app 层才能拦住真实投递。
    monkeypatch.setattr(queue_module.celery_app, "send_task", fake_send_task)
    document_id = uuid.uuid4()
    assert queue_module.enqueue_ingest(document_id) == "task-123"
    assert recorded == [("pyknora.ingest_document", (str(document_id),))]


async def test_upload_in_queue_mode_returns_queued(
    client: Any, monkeypatch: pytest.MonkeyPatch, unique_email: str
) -> None:
    monkeypatch.setenv("PYKNORA_INGEST_MODE", "queue")
    get_settings.cache_clear()

    enqueued: list[str] = []

    def fake_enqueue(document_id: uuid.UUID) -> str:
        enqueued.append(str(document_id))
        return "task-queued"

    monkeypatch.setattr(queue_module, "enqueue_ingest", fake_enqueue)

    register = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": "correct-horse-battery", "display_name": "q"},
    )
    assert register.status_code == 201, register.text
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
    workspace = await client.post(
        "/api/v1/workspaces", json={"name": "队列工作区"}, headers=headers
    )
    assert workspace.status_code == 201, workspace.text
    kb = await client.post(
        f"/api/v1/workspaces/{workspace.json()['id']}/knowledge-bases",
        json={"name": "队列库"},
        headers=headers,
    )
    assert kb.status_code == 201, kb.text

    upload = await client.post(
        f"/api/v1/knowledge-bases/{kb.json()['id']}/documents",
        files={"file": ("note.md", "# 队列\\n\\n内容".encode(), "text/markdown")},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    assert body["status"] == "queued"
    assert body["chunk_count"] == 0
    assert enqueued == [body["id"]]


async def test_health_reports_storage_and_queue(client: Any) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["storage"]["status"] == "ok"
    assert checks["storage"]["backend"] in {"local", "s3"}
    assert checks["queue"]["mode"] in {"inline", "queue"}
    assert checks["queue"]["broker"] in {"redis", "in-process"}
