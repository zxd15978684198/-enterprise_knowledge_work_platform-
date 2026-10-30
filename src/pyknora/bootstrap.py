"""启动引导：确保对象存储可用（S3 桶存在），供 api / worker / 部署脚本复用。

用法：

* ``python -m pyknora.bootstrap`` —— 部署前检查并建桶，输出一行 JSON；
* 应用 lifespan 也会调用 :func:`ensure_storage_ready`，配置错误时启动即失败（fail fast）。
"""

from __future__ import annotations

import json
import logging
import sys

from pyknora.config import get_settings
from pyknora.storage import S3ObjectStorage, S3StorageError, get_storage

logger = logging.getLogger(__name__)


def ensure_storage_ready() -> dict[str, str]:
    """确保存储后端就绪，返回描述信息（不含任何凭据）。"""

    settings = get_settings()
    storage = get_storage()
    info = {
        "backend": settings.storage_backend,
        "ingest_mode": settings.ingest_mode,
    }
    if isinstance(storage, S3ObjectStorage):
        storage.ensure_bucket()
        info["endpoint"] = settings.s3_endpoint_url
        info["bucket"] = settings.s3_bucket
    else:
        info["root"] = settings.upload_dir
    return info


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        info = ensure_storage_ready()
    except S3StorageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **info}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - 部署脚本入口
    sys.exit(main())
