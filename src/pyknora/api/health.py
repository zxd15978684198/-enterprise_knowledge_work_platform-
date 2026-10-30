"""健康检查（根路径与 API 前缀各一个入口）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from pyknora.config import get_settings
from pyknora.db import get_session_factory

router = APIRouter(tags=["health"])


async def _check_database() -> tuple[str, str | None]:
    """返回 (状态, 错误信息)；超时 2 秒。"""

    async def probe() -> None:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(probe(), timeout=2.0)
    except TimeoutError:
        return "timeout", "database probe timed out"
    except Exception as exc:
        return "error", f"{type(exc).__name__}: {exc}"
    return "ok", None


async def _health_payload() -> dict[str, Any]:
    settings = get_settings()
    db_status, db_error = await _check_database()
    payload: dict[str, Any] = {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": settings.app_name,
        "version": settings.version,
        "env": settings.env,
        "checks": {
            "database": {"status": db_status},
            "redis": {"status": "not_configured"},
        },
    }
    if db_error:
        payload["checks"]["database"]["error"] = db_error
    return payload


@router.get("/health", summary="健康检查（存活探针）")
async def health() -> dict[str, Any]:
    return await _health_payload()


@router.get("/api/v1/health", summary="健康检查（依赖状态）")
async def api_health() -> dict[str, Any]:
    return await _health_payload()
