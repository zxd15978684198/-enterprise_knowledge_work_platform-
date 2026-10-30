"""注册、登录与当前用户。"""

from __future__ import annotations

PASSWORD = "correct-horse-battery"


async def _register(client, email: str, display_name: str = "Tester"):
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": display_name},
    )


async def test_register_returns_token_and_creates_default_workspace(
    client, unique_email: str
) -> None:
    response = await _register(client, unique_email, "张三")

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["email"] == unique_email
    assert body["user"]["display_name"] == "张三"

    workspaces = await client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert workspaces.status_code == 200
    items = workspaces.json()
    assert len(items) == 1
    assert "张三" in items[0]["name"]


async def test_register_rejects_duplicate_email(client, unique_email: str) -> None:
    assert (await _register(client, unique_email)).status_code == 201

    duplicate = await _register(client, unique_email.upper())

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_taken"


async def test_register_rejects_short_password(client, unique_email: str) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": unique_email, "password": "short"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_login_success_and_wrong_password(client, unique_email: str) -> None:
    await _register(client, unique_email)

    ok = await client.post("/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    bad = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": "wrong-password"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"


async def test_me_requires_token_and_returns_profile(client, unique_email: str) -> None:
    token = (await _register(client, unique_email)).json()["access_token"]

    unauthorized = await client.get("/api/v1/auth/me")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthenticated"

    invalid = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_token"

    ok = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.json()["email"] == unique_email
