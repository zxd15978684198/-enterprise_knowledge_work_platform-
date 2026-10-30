"""W23 指标：采集口径、快照结构、端点与令牌保护。"""

from __future__ import annotations

import pytest

from pyknora import metrics
from pyknora.config import get_settings


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset()
    yield
    metrics.reset()


def test_snapshot_aggregates_requests_and_latency() -> None:
    metrics.observe_request("GET", "/api/v1/things/{thing_id}", 200, 12.0)
    metrics.observe_request("GET", "/api/v1/things/{thing_id}", 200, 20.0)
    metrics.observe_request("POST", "/api/v1/things", 500, 40.0)
    metrics.increment("documents_processed", 3)

    snapshot = metrics.snapshot()

    assert snapshot["scope"] == "process"
    assert snapshot["requests"]["total"] == 3
    assert snapshot["counters"] == {"documents_processed": 3}
    bucket = snapshot["latency_ms"]["/api/v1/things/{thing_id}"]
    assert bucket["count"] == 2
    assert bucket["avg"] == 16.0
    assert bucket["p50"] <= bucket["p95"] <= bucket["max"]


def test_static_and_docs_paths_are_not_tracked() -> None:
    metrics.observe_request("GET", "/app/app.js", 200, 1.0)
    metrics.observe_request("GET", "/docs", 200, 1.0)
    metrics.observe_request("GET", "/openapi.json", 200, 1.0)

    assert metrics.snapshot()["requests"]["total"] == 0


def test_zero_increment_is_ignored() -> None:
    metrics.increment("noop", 0)
    assert "noop" not in metrics.snapshot()["counters"]


async def test_metrics_endpoint_reports_itself(client) -> None:
    first = await client.get("/api/v1/metrics")
    assert first.status_code == 200, first.text
    assert first.json()["scope"] == "process"

    second = await client.get("/api/v1/metrics")
    assert second.status_code == 200
    buckets = {
        (item["method"], item["route"], item["status"]): item["count"]
        for item in second.json()["requests"]["by_route"]
    }
    # 第一次请求自身也应被计入（说明中间件与端点都走到了）
    assert buckets.get(("GET", "/api/v1/metrics", 200), 0) >= 1


async def test_metrics_token_gate(client, monkeypatch) -> None:
    monkeypatch.setenv("PYKNORA_METRICS_TOKEN", "w23-metrics-token")
    get_settings.cache_clear()
    try:
        denied = await client.get("/api/v1/metrics")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "metrics_forbidden"

        wrong = await client.get("/api/v1/metrics", headers={"x-metrics-token": "nope"})
        assert wrong.status_code == 403

        allowed = await client.get(
            "/api/v1/metrics", headers={"x-metrics-token": "w23-metrics-token"}
        )
        assert allowed.status_code == 200
        assert allowed.json()["scope"] == "process"
    finally:
        monkeypatch.delenv("PYKNORA_METRICS_TOKEN", raising=False)
        get_settings.cache_clear()
