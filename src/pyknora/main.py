"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from pyknora.api import api_router
from pyknora.api import health as health_module
from pyknora.bootstrap import ensure_storage_ready
from pyknora.config import get_settings
from pyknora.db import dispose_engine
from pyknora.errors import install_error_handlers
from pyknora.metrics import observe_request, route_template
from pyknora.storage import S3StorageError

logger = logging.getLogger("pyknora")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("starting %s %s (env=%s)", settings.app_name, settings.version, settings.env)
    try:
        info = ensure_storage_ready()
        logger.info("存储就绪 backend=%s ingest_mode=%s", info["backend"], info["ingest_mode"])
    except S3StorageError as exc:
        # 部署形态下存储不可用不应「带病启动」：健康检查会一直 degraded，直接失败更快暴露。
        logger.error("对象存储不可用：%s", exc)
        raise RuntimeError(f"对象存储不可用：{exc}") from exc
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("stopped %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        summary="PyKnora 企业知识工作平台（P0 MVP）",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        # 用「折成低基数的真实路径」而不是路由对象：FastAPI 的嵌套 include_router
        # 会让 route.path 只保留内层前缀（如 /metrics 而非 /api/v1/metrics）。
        template = route_template(request.url.path) if request.scope.get("route") else "unmatched"
        observe_request(request.method, template, response.status_code, elapsed_ms)
        logger.info(
            "%s %s -> %s %.1fms request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    install_error_handlers(app)
    app.include_router(health_module.router)
    app.include_router(api_router)

    web_dir = Path(settings.web_dir) if settings.web_dir else Path(__file__).parent / "web"
    if web_dir.is_dir():
        app.mount("/app", StaticFiles(directory=web_dir, html=True), name="web")

        @app.get("/", include_in_schema=False)
        async def index_redirect() -> RedirectResponse:
            return RedirectResponse(url="/app/")

    return app


app = create_app()
