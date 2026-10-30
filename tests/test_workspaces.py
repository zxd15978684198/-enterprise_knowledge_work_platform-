"""工作区列表、创建、详情与跨工作区隔离。"""

from __future__ import annotations

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


async def test_create_workspace_assigns_owner(client, unique_email: str) -> None:
    token = await _register(client, unique_email)

    response = await client.post(
        "/api/v1/workspaces", json={"name": "知识中心"}, headers=_auth(token)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "知识中心"
    assert body["role"] == "owner"
    assert len(body["members"]) == 1
    assert body["members"][0]["role"] == "owner"


async def test_slug_is_generated_and_unique(client, unique_email: str) -> None:
    token = await _register(client, unique_email)

    first = await client.post("/api/v1/workspaces", json={"name": "Foo Bar"}, headers=_auth(token))
    second = await client.post("/api/v1/workspaces", json={"name": "Foo Bar"}, headers=_auth(token))

    assert first.json()["slug"] == "foo-bar"
    assert second.json()["slug"] == "foo-bar-2"


async def test_explicit_slug_conflict_is_409(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    payload = {"name": "One", "slug": "fixed-slug"}

    assert (
        await client.post("/api/v1/workspaces", json=payload, headers=_auth(token))
    ).status_code == 201
    conflict = await client.post("/api/v1/workspaces", json=payload, headers=_auth(token))

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "slug_taken"


async def test_workspace_detail_lists_members(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    created = await client.post("/api/v1/workspaces", json={"name": "Team"}, headers=_auth(token))
    workspace_id = created.json()["id"]

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=_auth(token))

    assert detail.status_code == 200
    assert detail.json()["id"] == workspace_id
    assert [m["role"] for m in detail.json()["members"]] == ["owner"]


async def test_cross_workspace_access_is_denied(client, unique_email: str) -> None:
    owner_token = await _register(client, unique_email)
    created = await client.post(
        "/api/v1/workspaces", json={"name": "Private"}, headers=_auth(owner_token)
    )
    workspace_id = created.json()["id"]

    outsider_token = await _register(client, f"out-{unique_email}")
    denied = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=_auth(outsider_token))

    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"

    outsider_list = await client.get("/api/v1/workspaces", headers=_auth(outsider_token))
    assert [w["id"] for w in outsider_list.json()].count(workspace_id) == 0


async def test_workspaces_require_authentication(client) -> None:
    assert (await client.get("/api/v1/workspaces")).status_code == 401
    assert (await client.post("/api/v1/workspaces", json={"name": "x"})).status_code == 401
