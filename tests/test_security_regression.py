"""W23 安全回归批次：五类不变量。

只调用既有实现与既有策略，不为通过测试放宽任何开关（例如不打开
``PYKNORA_URL_ALLOW_PRIVATE_NETWORK``），否则回归批次本身就成了风险来源。
"""

from __future__ import annotations

import pytest

from pyknora.ingest.url_fetch import UrlFetchError, assert_public_target

PASSWORD = "correct-horse-battery"


async def _register(client, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": email.split("@")[0]},
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _workspace(client, token: str, name: str) -> str:
    response = await client.post("/api/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _kb(client, token: str, workspace_id: str) -> str:
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": "回归知识库"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_protected_endpoint_requires_token(client) -> None:
    response = await client.get("/api/v1/workspaces")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_malformed_bearer_token_rejected(client) -> None:
    response = await client.get("/api/v1/workspaces", headers=_auth("not-a-real-jwt"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_tampered_token_signature_rejected(client, unique_email) -> None:
    token = await _register(client, unique_email)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{'A' * len(signature)}"

    response = await client.get("/api/v1/workspaces", headers=_auth(tampered))
    assert response.status_code == 401


async def test_cross_workspace_resource_is_not_found(client, unique_email) -> None:
    token = await _register(client, unique_email)
    workspace_a = await _workspace(client, token, "工作区 A")
    workspace_b = await _workspace(client, token, "工作区 B")
    kb_id = await _kb(client, token, workspace_a)

    # 用 B 的作用域访问 A 的知识库：必须是 404，不能泄漏「存在但无权」。
    response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
    assert response.status_code == 200  # 同用户、同工作区成员可见

    cross = await client.get(
        f"/api/v1/workspaces/{workspace_b}/knowledge-bases",
        headers=_auth(token),
    )
    assert cross.status_code == 200
    assert all(item["id"] != kb_id for item in cross.json())

    other_user_token = await _register(client, f"other-{unique_email}")
    denied = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(other_user_token))
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "target",
    ["169.254.169.254", "127.0.0.1", "10.0.0.1", "192.168.1.1", "::1"],
)
async def test_ssrf_private_and_metadata_targets_blocked(target: str) -> None:
    with pytest.raises(UrlFetchError) as excinfo:
        await assert_public_target(target, 80)
    assert excinfo.value.code == "blocked_target"


async def test_cors_only_allows_configured_origin(client) -> None:
    allowed = await client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"

    denied = await client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert denied.status_code == 200  # 简单请求照常返回，但不应带放行头
    assert "access-control-allow-origin" not in denied.headers
