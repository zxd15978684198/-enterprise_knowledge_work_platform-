"""模型配置 API 测试：CRUD、默认唯一、连接测试如实报告、跨工作区隔离。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient


async def _register(client: AsyncClient, email: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Passw0rd!2026", "display_name": "模型用户"},
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    workspace = await client.post("/api/v1/workspaces", json={"name": "模型空间"}, headers=headers)
    assert workspace.status_code == 201, workspace.text
    return {"headers": headers, "workspace_id": workspace.json()["id"]}


def _model_payload(workspace_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": "主对话模型",
        "kind": "chat",
        "provider": "local",
        "model_name": "extractive-local",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_and_list_model_configs(client: AsyncClient, unique_email: str) -> None:
    ctx = await _register(client, unique_email)

    created = await client.post(
        "/api/v1/models", json=_model_payload(ctx["workspace_id"]), headers=ctx["headers"]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "chat"
    assert body["is_default"] is False
    # 凭据只存引用，不回显任何密钥字段
    assert "api_key" not in body

    listed = await client.get(
        "/api/v1/models",
        params={"workspace_id": ctx["workspace_id"]},
        headers=ctx["headers"],
    )
    assert listed.status_code == 200, listed.text
    assert [item["name"] for item in listed.json()] == ["主对话模型"]

    filtered = await client.get(
        "/api/v1/models",
        params={"workspace_id": ctx["workspace_id"], "kind": "embedding"},
        headers=ctx["headers"],
    )
    assert filtered.status_code == 200
    assert filtered.json() == []


@pytest.mark.asyncio
async def test_duplicate_name_conflicts(client: AsyncClient, unique_email: str) -> None:
    ctx = await _register(client, unique_email)
    first = await client.post(
        "/api/v1/models", json=_model_payload(ctx["workspace_id"]), headers=ctx["headers"]
    )
    assert first.status_code == 201

    duplicate = await client.post(
        "/api/v1/models", json=_model_payload(ctx["workspace_id"]), headers=ctx["headers"]
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_default_is_exclusive_per_kind(client: AsyncClient, unique_email: str) -> None:
    ctx = await _register(client, unique_email)
    first = await client.post(
        "/api/v1/models",
        json=_model_payload(ctx["workspace_id"], is_default=True),
        headers=ctx["headers"],
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/v1/models",
        json=_model_payload(ctx["workspace_id"], name="备选对话模型", is_default=True),
        headers=ctx["headers"],
    )
    assert second.status_code == 201, second.text

    listed = await client.get(
        "/api/v1/models", params={"workspace_id": ctx["workspace_id"]}, headers=ctx["headers"]
    )
    defaults = [item for item in listed.json() if item["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "备选对话模型"

    # 不同 kind 的默认互不影响
    embedding = await client.post(
        "/api/v1/models",
        json=_model_payload(
            ctx["workspace_id"],
            name="默认向量模型",
            kind="embedding",
            model_name="hashing-local",
            is_default=True,
        ),
        headers=ctx["headers"],
    )
    assert embedding.status_code == 201, embedding.text
    listed = await client.get(
        "/api/v1/models", params={"workspace_id": ctx["workspace_id"]}, headers=ctx["headers"]
    )
    assert len([item for item in listed.json() if item["is_default"]]) == 2


@pytest.mark.asyncio
async def test_update_and_delete_model_config(client: AsyncClient, unique_email: str) -> None:
    ctx = await _register(client, unique_email)
    created = await client.post(
        "/api/v1/models", json=_model_payload(ctx["workspace_id"]), headers=ctx["headers"]
    )
    model_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/models/{model_id}",
        params={"workspace_id": ctx["workspace_id"]},
        json={"name": "改名后的模型", "is_default": True},
        headers=ctx["headers"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "改名后的模型"
    assert patched.json()["is_default"] is True

    deleted = await client.delete(
        f"/api/v1/models/{model_id}",
        params={"workspace_id": ctx["workspace_id"]},
        headers=ctx["headers"],
    )
    assert deleted.status_code == 204

    missing = await client.get(
        "/api/v1/models", params={"workspace_id": ctx["workspace_id"]}, headers=ctx["headers"]
    )
    assert missing.json() == []


@pytest.mark.asyncio
async def test_connection_test_reports_honestly(client: AsyncClient, unique_email: str) -> None:
    ctx = await _register(client, unique_email)

    local = await client.post(
        "/api/v1/models", json=_model_payload(ctx["workspace_id"]), headers=ctx["headers"]
    )
    ok_result = await client.post(
        f"/api/v1/models/{local.json()['id']}/test",
        params={"workspace_id": ctx["workspace_id"]},
        headers=ctx["headers"],
    )
    assert ok_result.status_code == 200, ok_result.text
    body = ok_result.json()
    assert body["ok"] is True
    assert body["matches_runtime"] is True
    assert body["adapter"] == "extractive-local"
    assert body["latency_ms"] is not None

    remote = await client.post(
        "/api/v1/models",
        json=_model_payload(
            ctx["workspace_id"], name="远程模型", provider="openai", model_name="gpt-4o-mini"
        ),
        headers=ctx["headers"],
    )
    failed = await client.post(
        f"/api/v1/models/{remote.json()['id']}/test",
        params={"workspace_id": ctx["workspace_id"]},
        headers=ctx["headers"],
    )
    assert failed.status_code == 200
    failed_body = failed.json()
    # 没装适配器就要如实说失败，不能伪造 ok=true
    assert failed_body["ok"] is False
    assert failed_body["matches_runtime"] is False
    assert "未接入" in failed_body["detail"]


@pytest.mark.asyncio
async def test_models_isolated_across_workspaces(client: AsyncClient, unique_email: str) -> None:
    ctx_a = await _register(client, unique_email)
    ctx_b = await _register(client, f"other-{unique_email}")

    created = await client.post(
        "/api/v1/models", json=_model_payload(ctx_a["workspace_id"]), headers=ctx_a["headers"]
    )
    assert created.status_code == 201
    model_id = created.json()["id"]

    # B 用自己的工作区 id 去读 A 的模型 → 404（不泄露存在性）
    cross = await client.patch(
        f"/api/v1/models/{model_id}",
        params={"workspace_id": ctx_b["workspace_id"]},
        json={"name": "越权改名"},
        headers=ctx_b["headers"],
    )
    assert cross.status_code == 404, cross.text

    deleted = await client.delete(
        f"/api/v1/models/{model_id}",
        params={"workspace_id": ctx_b["workspace_id"]},
        headers=ctx_b["headers"],
    )
    assert deleted.status_code == 404

    own = await client.get(
        "/api/v1/models", params={"workspace_id": ctx_a["workspace_id"]}, headers=ctx_a["headers"]
    )
    assert len(own.json()) == 1

    unknown_workspace = await client.get(
        "/api/v1/models", params={"workspace_id": str(uuid.uuid4())}, headers=ctx_b["headers"]
    )
    assert unknown_workspace.status_code == 404
