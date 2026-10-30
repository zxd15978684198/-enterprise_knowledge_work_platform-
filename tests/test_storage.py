"""对象存储（W22）：本地实现与 S3 兼容实现的契约测试。

S3 侧不连真实 MinIO：注入一个记录调用的假 client，只验证「桶自愈、错误包装、
协议方法映射」这些我们自己写的逻辑；真实 MinIO 往返由 E2E 脚本与 Compose 冒烟覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from pyknora.storage import (
    LocalObjectStorage,
    S3ObjectStorage,
    S3StorageError,
    _build_storage,
    checksum,
    get_storage,
    reset_storage,
)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    """只实现被测代码用到的那几个 boto3 方法。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = set()
        self.head_bucket_error: str | None = "404"

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def head_bucket(self, *, Bucket: str) -> None:
        self._record("head_bucket", Bucket=Bucket)
        if self.head_bucket_error is not None:
            raise ClientError({"Error": {"Code": self.head_bucket_error}}, "HeadBucket")

    def create_bucket(self, *, Bucket: str) -> None:
        self._record("create_bucket", Bucket=Bucket)
        self.buckets.add(Bucket)

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self._record("put_object", Bucket=Bucket, Key=Key)
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._record("get_object", Bucket=Bucket, Key=Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self._record("delete_object", Bucket=Bucket, Key=Key)
        self.objects.pop(Key, None)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._record("head_object", Bucket=Bucket, Key=Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}


def _s3(fake: _FakeS3 | None = None) -> tuple[S3ObjectStorage, _FakeS3]:
    client = fake or _FakeS3()
    storage = S3ObjectStorage(
        endpoint_url="http://127.0.0.1:9000",
        access_key="pyknora",
        secret_key="secret",
        bucket="pyknora",
    )
    storage._client = client  # type: ignore[attr-defined]
    return storage, client


def test_local_roundtrip(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    storage.put("workspace/doc.txt", b"hello")
    assert storage.exists("workspace/doc.txt") is True
    assert storage.get("workspace/doc.txt") == b"hello"
    assert storage.size("workspace/doc.txt") == 5
    storage.delete("workspace/doc.txt")
    assert storage.exists("workspace/doc.txt") is False


def test_local_rejects_escape(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path / "root")
    with pytest.raises(ValueError):
        storage.put("../escape.txt", b"x")


def test_reset_storage_forces_local(tmp_path: Path) -> None:
    storage = reset_storage(tmp_path)
    assert isinstance(storage, LocalObjectStorage)
    assert get_storage() is storage


def test_build_storage_selects_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pyknora.config import get_settings

    monkeypatch.setenv("PYKNORA_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("PYKNORA_S3_BUCKET", "kb-bucket")
    get_settings.cache_clear()
    try:
        storage = _build_storage()
        assert isinstance(storage, S3ObjectStorage)
        assert storage.bucket == "kb-bucket"
    finally:
        monkeypatch.delenv("PYKNORA_STORAGE_BACKEND", raising=False)
        monkeypatch.delenv("PYKNORA_S3_BUCKET", raising=False)
        get_settings.cache_clear()


def test_ensure_bucket_creates_when_missing() -> None:
    storage, client = _s3()
    storage.ensure_bucket()
    assert [name for name, _ in client.calls] == ["head_bucket", "create_bucket"]
    assert client.buckets == {"pyknora"}


def test_ensure_bucket_is_noop_when_present() -> None:
    client = _FakeS3()
    client.head_bucket_error = None
    storage, client = _s3(client)
    storage.ensure_bucket()
    assert [name for name, _ in client.calls] == ["head_bucket"]


def test_ensure_bucket_wraps_unexpected_error() -> None:
    client = _FakeS3()
    client.head_bucket_error = "403"
    storage, _ = _s3(client)
    with pytest.raises(S3StorageError):
        storage.ensure_bucket()


def test_s3_roundtrip_maps_to_boto3() -> None:
    storage, client = _s3()
    storage.put("kb/doc.txt", b"payload")
    assert storage.exists("kb/doc.txt") is True
    assert storage.get("kb/doc.txt") == b"payload"
    assert storage.size("kb/doc.txt") == 7
    storage.delete("kb/doc.txt")
    assert storage.exists("kb/doc.txt") is False
    assert [name for name, _ in client.calls] == [
        "put_object",
        "head_object",
        "get_object",
        "head_object",
        "delete_object",
        "head_object",
    ]


def test_s3_get_missing_raises_storage_error() -> None:
    storage, _ = _s3()
    with pytest.raises(S3StorageError):
        storage.get("missing.txt")


def test_checksum_is_stable() -> None:
    assert checksum(b"abc") == checksum(b"abc")
    assert checksum(b"abc") != checksum(b"abd")
