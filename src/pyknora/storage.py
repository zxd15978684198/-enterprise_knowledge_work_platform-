"""对象存储：本地目录（开发/测试）与 S3 兼容端点（MinIO，部署形态）两个实现。

W19–W21 只有 :class:`LocalObjectStorage`；W22 按 ``PYKNORA_STORAGE_BACKEND`` 选择实现。
两者都满足 :class:`ObjectStorage` 协议，上层（入库流水线、上传接口）无需感知差异。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pyknora.config import get_settings

logger = logging.getLogger(__name__)


class ObjectStorage(Protocol):
    """对象存储最小接口：按 key 存取字节。"""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int: ...


@dataclass(slots=True)
class LocalObjectStorage:
    """把对象落在本地目录（默认 ``data/uploads``），key 即相对路径。"""

    root: Path

    def _resolve(self, key: str) -> Path:
        # key 由服务端生成（workspace_id/filename），仍做一次越界校验以防回归。
        root = self.root.resolve()
        target = (self.root / key).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"非法的存储 key：{key}")
        return target

    def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def size(self, key: str) -> int:
        return self._resolve(key).stat().st_size


class S3StorageError(RuntimeError):
    """S3 后端不可用或返回了非预期错误。"""


@dataclass(slots=True)
class S3ObjectStorage:
    """S3 兼容对象存储（MinIO / AWS S3）。

    客户端惰性创建，构造本身不触网，便于测试与配置校验。
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "us-east-1"
    _client: Any = field(default=None, init=False, repr=False)

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    def ensure_bucket(self) -> None:
        """确保桶存在；已存在则静默返回。启动与迁移前调用。"""

        from botocore.exceptions import BotoCoreError, ClientError

        client = self._get_client()
        try:
            client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise S3StorageError(f"检查桶 {self.bucket} 失败：{exc}") from exc
        try:
            client.create_bucket(Bucket=self.bucket)
            logger.info("已创建对象桶 bucket=%s endpoint=%s", self.bucket, self.endpoint_url)
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"创建桶 {self.bucket} 失败：{exc}") from exc

    def put(self, key: str, data: bytes) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._get_client().put_object(Bucket=self.bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"上传对象 {key} 失败：{exc}") from exc

    def get(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            body = self._get_client().get_object(Bucket=self.bucket, Key=key)["Body"]
            return body.read()
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"读取对象 {key} 失败：{exc}") from exc

    def delete(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._get_client().delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"删除对象 {key} 失败：{exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._get_client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise S3StorageError(f"检查对象 {key} 失败：{exc}") from exc
        except BotoCoreError as exc:
            raise S3StorageError(f"检查对象 {key} 失败：{exc}") from exc

    def size(self, key: str) -> int:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            head = self._get_client().head_object(Bucket=self.bucket, Key=key)
            return int(head.get("ContentLength", 0))
        except (BotoCoreError, ClientError) as exc:
            raise S3StorageError(f"读取对象大小 {key} 失败：{exc}") from exc


_storage: LocalObjectStorage | S3ObjectStorage | None = None


def _build_storage() -> LocalObjectStorage | S3ObjectStorage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return S3ObjectStorage(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
        )
    return LocalObjectStorage(root=Path(settings.upload_dir))


def get_storage() -> LocalObjectStorage | S3ObjectStorage:
    """进程内单例存储；测试通过 ``reset_storage()`` 指定本地目录。"""

    global _storage
    if _storage is None:
        _storage = _build_storage()
    return _storage


def reset_storage(root: Path | None = None) -> LocalObjectStorage | S3ObjectStorage:
    """重建存储单例。传 ``root`` 表示强制本地后端（测试用）。"""

    global _storage
    _storage = (
        LocalObjectStorage(root=root or Path(get_settings().upload_dir))
        if root
        else _build_storage()
    )
    return _storage


def checksum(data: bytes) -> str:
    """内容指纹：用于同知识库内幂等去重。"""

    return hashlib.sha256(data).hexdigest()
