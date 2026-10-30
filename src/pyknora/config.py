"""运行配置（环境变量前缀 ``PYKNORA_``，支持根目录 ``.env``）。"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。

    所有字段均可通过 ``PYKNORA_<大写字段名>`` 环境变量覆盖；默认值只用于本地开发，
    生产部署必须显式注入 ``database_url``、``jwt_secret`` 等敏感项。
    """

    model_config = SettingsConfigDict(
        env_prefix="PYKNORA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PyKnora"
    version: str = "0.1.0"
    env: str = Field(default="dev", description="dev / test / prod")
    log_level: str = "INFO"

    database_url: str = Field(
        default=("postgresql+asyncpg://pyknora:phase0-local-only@127.0.0.1:5433/pyknora_dev"),
        description="SQLAlchemy 异步连接串（asyncpg 驱动）",
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5

    jwt_secret: str = Field(default="dev-insecure-change-me", description="生产必须覆盖")
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 720

    redis_url: str = Field(
        default="redis://127.0.0.1:6380/0", description="W19 起用于 Celery 与事件流"
    )

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    @property
    def sync_database_url(self) -> str:
        """迁移与运维脚本使用的同步连接串（psycopg 3）。"""

        url = self.database_url
        for driver in ("+asyncpg", "+psycopg"):
            url = url.replace(driver, "", 1)
        return url

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试通过 ``get_settings.cache_clear()`` 重置。"""

    return Settings()
