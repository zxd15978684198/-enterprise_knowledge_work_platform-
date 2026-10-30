"""健康检查行为。"""

from __future__ import annotations


async def test_health_reports_database_ok(client) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["service"] == "PyKnora"


async def test_api_health_matches_root_health(client) -> None:
    root = (await client.get("/health")).json()
    prefixed = (await client.get("/api/v1/health")).json()

    assert prefixed["status"] == root["status"]
    assert prefixed["version"] == root["version"]


async def test_response_carries_request_id(client) -> None:
    response = await client.get("/health", headers={"x-request-id": "req-abc"})

    assert response.headers["x-request-id"] == "req-abc"
