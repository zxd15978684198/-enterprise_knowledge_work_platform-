"""pytest 基建：独立测试库 + 每例清表 + ASGI 客户端。"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试库与开发库隔离；必须在导入 pyknora 配置前写入环境变量。
TEST_DB_NAME = "pyknora_test"
_PG_USER = os.environ.get("PYKNORA_TEST_PG_USER", "pyknora")
_PG_PASSWORD = os.environ.get("PYKNORA_TEST_PG_PASSWORD", "phase0-local-only")
_PG_HOST = os.environ.get("PYKNORA_TEST_PG_HOST", "127.0.0.1")
_PG_PORT = os.environ.get("PYKNORA_TEST_PG_PORT", "5433")
TEST_ASYNC_URL = (
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/{TEST_DB_NAME}"
)
TEST_SYNC_URL = f"postgresql://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/{TEST_DB_NAME}"
ADMIN_SYNC_URL = f"postgresql://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}/postgres"

os.environ["PYKNORA_DATABASE_URL"] = TEST_ASYNC_URL
os.environ.setdefault("PYKNORA_JWT_SECRET", "test-only-secret-0123456789abcdef0123456789abcdef")


def _ensure_database() -> None:
    """测试库不存在则创建（幂等）。"""

    with psycopg.connect(ADMIN_SYNC_URL, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    _ensure_database()
    from pyknora.config import get_settings

    get_settings.cache_clear()
    from pyknora.migrate import run

    run(TEST_SYNC_URL)
    yield


@pytest.fixture(autouse=True)
async def _clean_tables(database: None) -> AsyncIterator[None]:
    """每个用例前清空业务表，保证隔离。"""

    from sqlalchemy import text

    from pyknora.db import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(
            text("TRUNCATE app.memberships, app.workspaces, app.users RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
async def client() -> AsyncIterator[object]:
    """进程内 ASGI 客户端（不占端口）。"""

    from httpx import ASGITransport, AsyncClient

    from pyknora.db import dispose_engine
    from pyknora.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await dispose_engine()


@pytest.fixture
def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"
