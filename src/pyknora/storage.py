"""本地对象存储（W19）。W22 引入 MinIO/S3 时实现同一接口即可替换。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pyknora.config import get_settings


class ObjectStorage(Protocol):
    """对象存储最小接口：按 key 存取字节。"""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...


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


_storage: LocalObjectStorage | None = None


def get_storage() -> LocalObjectStorage:
    """进程内单例存储；测试通过 ``reset_storage()`` 指到临时目录。"""

    global _storage
    if _storage is None:
        _storage = LocalObjectStorage(root=Path(get_settings().upload_dir))
    return _storage


def reset_storage(root: Path | None = None) -> LocalObjectStorage:
    global _storage
    _storage = LocalObjectStorage(root=root or Path(get_settings().upload_dir))
    return _storage


def checksum(data: bytes) -> str:
    """内容指纹：用于同知识库内幂等去重。"""

    return hashlib.sha256(data).hexdigest()
