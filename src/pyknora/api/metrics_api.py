"""指标端点。

只暴露聚合计数与时延分位，不含请求体、查询串与身份信息。
`PYKNORA_METRICS_TOKEN` 非空时要求 `x-metrics-token` 头，便于把端点只开放给内网探针。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request

from pyknora.config import get_settings
from pyknora.errors import ApiError
from pyknora.metrics import snapshot

router = APIRouter(tags=["metrics"])


@router.get("/metrics", summary="进程内指标快照（运维用）")
async def metrics(request: Request) -> dict[str, Any]:
    settings = get_settings()
    expected = settings.metrics_token
    if expected:
        provided = request.headers.get("x-metrics-token", "")
        if not provided or not secrets.compare_digest(provided, expected):
            raise ApiError(
                "metrics_forbidden",
                "指标端点需要有效的 x-metrics-token",
                status_code=403,
            )
    return snapshot()
