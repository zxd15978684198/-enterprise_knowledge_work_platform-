"""运行配置（环境变量前缀 ``PYKNORA_``，支持根目录 ``.env``）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # —— 静态前端（W22）——
    web_dir: str = Field(
        default="",
        description="前端静态目录；留空表示用包内 src/pyknora/web（镜像里用绝对路径覆盖）",
    )

    # —— 文档入库（W19）——
    upload_dir: str = Field(
        default="data/uploads", description="本地对象存储根目录（W22 换 MinIO 时改为桶名）"
    )
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, description="单文件上限，默认 25 MiB")
    chunk_size: int = Field(default=800, ge=100, le=4000, description="分块目标字符数")
    chunk_overlap: int = Field(default=120, ge=0, le=1000, description="分块重叠字符数")
    max_chunks_per_document: int = Field(default=2000, ge=1, description="单文档分块上限")

    # —— 检索与 RAG（W20）——
    embedding_provider: str = Field(
        default="local", description="local = 字符 n-gram 哈希替身；真实模型适配器尚未接入"
    )
    embedding_dim: int = Field(
        default=1024,
        ge=8,
        description="向量维度，必须与 db/migrations/0003 中 vector(1024) 一致",
    )
    chat_provider: str = Field(
        default="local", description="local = 抽取式替身；真实生成模型适配器尚未接入"
    )
    retrieval_candidate_k: int = Field(
        default=40, ge=1, le=500, description="每路召回候选数（向量 / 词法各取这么多）"
    )
    retrieval_top_k: int = Field(default=8, ge=1, le=100, description="融合后返回条数")
    rrf_k: int = Field(default=60, ge=1, description="RRF 平滑常数 k")
    retrieval_min_similarity: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "向量路候选的余弦相似度下限；低于它的分块不进融合。"
            "0.10 是对本地 n-gram 哈希替身实测出来的经验值（相关 ~0.22–0.25、无关 ~0.00–0.02），"
            "换成真实 embedding 后必须重新标定"
        ),
    )
    rag_max_context_chunks: int = Field(default=6, ge=1, le=50, description="送进模型的片段上限")
    rag_history_turns: int = Field(default=6, ge=0, le=50, description="拼进提示的历史消息条数")
    rag_min_similarity: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "证据充分性阈值（向量余弦相似度）；0 表示关闭。"
            "W20 未校准：真实 embedding 未接入，阈值的生产取值待 ADR-0014 DG-3/DG-4 裁决"
        ),
    )

    # —— 单 URL 受控导入（W21）——
    url_fetch_timeout_seconds: float = Field(
        default=15.0, gt=0, le=120, description="单次抓取总超时（秒）"
    )
    url_max_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1, description="URL 抓取响应字节上限，默认 5 MiB"
    )
    url_max_redirects: int = Field(default=3, ge=0, le=10, description="允许跟随的重定向跳数上限")
    url_allow_private_network: bool = Field(
        default=False,
        description=(
            "是否允许访问回环/私网/链路本地地址。默认 False（生产语义）；"
            "仅测试与本地联调可显式置 True，生产中打开等于关闭 SSRF 防护"
        ),
    )

    # —— 对象存储与队列（W22）——
    storage_backend: Literal["local", "s3"] = Field(
        default="local",
        description="local = 本地目录（开发/测试）；s3 = 任意 S3 兼容端点（MinIO），生产部署用",
    )
    s3_endpoint_url: str = Field(
        default="http://127.0.0.1:9000", description="S3 兼容端点（MinIO 默认 9000 端口）"
    )
    s3_access_key: str = Field(default="pyknora", description="S3 access key；生产必须覆盖")
    s3_secret_key: str = Field(
        default="pyknora-dev-secret", description="S3 secret key；生产必须覆盖，不得提交真实值"
    )
    s3_bucket: str = Field(default="pyknora", description="对象桶名，启动时自动创建")
    s3_region: str = Field(default="us-east-1", description="S3 region（MinIO 不校验，但需给值）")
    s3_presign_seconds: int = Field(
        default=900, ge=30, le=86400, description="预签名 URL 有效期（秒）"
    )
    ingest_mode: Literal["inline", "queue"] = Field(
        default="inline",
        description="inline = 请求内同步入库（开发/测试）；queue = 丢给 Celery worker（部署形态）",
    )
    celery_task_time_limit: int = Field(default=900, ge=30, description="单任务硬超时（秒）")
    celery_task_soft_time_limit: int = Field(default=840, ge=30, description="单任务软超时（秒）")

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
