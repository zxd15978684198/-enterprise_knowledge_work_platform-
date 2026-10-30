"""进程内轻量指标（无第三方依赖）。

设计取舍：
- 只做「当前进程」的内存计数与时延分位，不落盘、不跨进程聚合；多实例/worker 各自独立。
- 指标内容只含方法、路由模板、状态码与时延，不含请求体、查询串、身份信息，避免把敏感数据带进指标。
- 时延采样按路由模板保留最近 ``_MAX_SAMPLES`` 条，用于 p50/p95；超出后淘汰最旧样本。
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Any

_MAX_SAMPLES = 512

_LOCK = threading.Lock()
_STARTED_AT = time.monotonic()
_requests_total: dict[tuple[str, str, int], int] = {}
_durations_ms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
_counters: dict[str, int] = defaultdict(int)

# 不纳入统计的路径：静态资源与载荷探针会产生噪声且无诊断价值。
_SKIP_PREFIXES = ("/app/", "/docs", "/redoc", "/openapi.json")

# 把高基数片段折成占位符，避免 uuid/数字把指标维度炸开。
_UUID_SEGMENT = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)"
)
_NUMERIC_SEGMENT = re.compile(r"/\d+(?=/|$)")


def route_template(path: str) -> str:
    """把真实请求路径折成低基数模板（``/api/v1/documents/{id}/chunks``）。"""

    template = _UUID_SEGMENT.sub("/{id}", path)
    return _NUMERIC_SEGMENT.sub("/{n}", template)


def _should_observe(route: str) -> bool:
    return not any(route.startswith(prefix) for prefix in _SKIP_PREFIXES)


def observe_request(method: str, route: str, status: int, duration_ms: float) -> None:
    """记录一次请求。``route`` 应为路由模板（如 ``/api/v1/knowledge-bases/{kb_id}``）。"""

    if not _should_observe(route):
        return
    key = (method.upper(), route, int(status))
    with _LOCK:
        _requests_total[key] = _requests_total.get(key, 0) + 1
        _durations_ms[route].append(max(duration_ms, 0.0))


def increment(name: str, amount: int = 1) -> None:
    """自定义计数器（如 ``documents_processed``）。"""

    if amount == 0:
        return
    with _LOCK:
        _counters[name] = _counters.get(name, 0) + int(amount)


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * ratio)))
    return sorted_values[index]


def snapshot() -> dict[str, Any]:
    """返回可 JSON 序列化的指标快照。"""

    with _LOCK:
        requests = [
            {"method": method, "route": route, "status": status, "count": count}
            for (method, route, status), count in sorted(_requests_total.items())
        ]
        latency: dict[str, dict[str, float | int]] = {}
        for route, samples in _durations_ms.items():
            values = sorted(samples)
            if not values:
                continue
            latency[route] = {
                "count": len(values),
                "avg": round(sum(values) / len(values), 2),
                "p50": round(_percentile(values, 0.50), 2),
                "p95": round(_percentile(values, 0.95), 2),
                "max": round(values[-1], 2),
            }
        counters = dict(sorted(_counters.items()))
        total = sum(_requests_total.values())

    return {
        "uptime_seconds": round(time.monotonic() - _STARTED_AT, 3),
        "requests": {"total": total, "by_route": requests},
        "latency_ms": latency,
        "counters": counters,
        "scope": "process",
    }


def reset() -> None:
    """清空所有指标（仅测试使用）。"""

    with _LOCK:
        _requests_total.clear()
        _durations_ms.clear()
        _counters.clear()
