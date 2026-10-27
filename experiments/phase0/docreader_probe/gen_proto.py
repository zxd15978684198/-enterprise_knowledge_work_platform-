"""P0-T10：从真实 proto 生成 DocReader gRPC stub（可重复、可核验）。

源（只读）：
    references/WeKnora/docreader/proto/docreader.proto
    submodule commit 662abaf9f944e37689d04c42160034b2cdec6829
    sha256 33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0

输出：``experiments/phase0/docreader_probe/generated/``（本包内，不写 references）。
工具链用 ``uv run --with grpcio --with grpcio-tools`` 临时注入；``experiments/phase0``
的 pyproject.toml / uv.lock 属 P0-T02 写权，本任务不改：

    uv run --project experiments/phase0 --with grpcio --with grpcio-tools \
        python experiments/phase0/docreader_probe/gen_proto.py

后处理只有两处，都是确定性的：

1. 生成器在 ``docreader_pb2_grpc.py`` 里写绝对 import ``import docreader_pb2``，
   放进包内后无法导入；改成 ``from . import docreader_pb2``。
2. 生成文件加 ``# ruff: noqa`` 文件级豁免：生成物不参与手写风格检查
   （``experiments/phase0`` 的 ruff 规则仍覆盖本包手写模块）。

同时写 ``generated/PROVENANCE.json`` 记录工具链版本与 proto 哈希，供测试和证据引用。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

#: P0-T09 ``docs/poc/evidence/docreader_manifest.md`` §1.1 锚定的 proto 哈希与字节数。
PROTO_SHA256 = "33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0"
PROTO_BYTES = 4689
SOURCE_COMMIT = "662abaf9f944e37689d04c42160034b2cdec6829"

PROBE_DIR = Path(__file__).resolve().parent
DEFAULT_PROTO = PROBE_DIR / "proto" / "docreader.proto"
DEFAULT_OUT = PROBE_DIR / "generated"

RUFF_NOQA = "# ruff: noqa\n"
ABSOLUTE_IMPORT = "import docreader_pb2 as docreader__pb2\n"
RELATIVE_IMPORT = "from . import docreader_pb2 as docreader__pb2\n"
GENERATED_MODULES = ("docreader_pb2.py", "docreader_pb2_grpc.py")


def sha256_of(path: Path) -> str:
    """返回文件 sha256（十六进制小写）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dist_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 环境缺包时
        return "unknown"


def generate(proto_path: Path, out_dir: Path) -> dict[str, Any]:
    """生成 stub 并写 PROVENANCE.json，返回 provenance 映射。"""
    digest = sha256_of(proto_path)
    if digest != PROTO_SHA256:
        raise SystemExit(
            f"proto 哈希不匹配：{proto_path} = {digest}，锚点为 {PROTO_SHA256}。"
            "拒绝从被改动的 proto 生成 stub。"
        )
    if proto_path.stat().st_size != PROTO_BYTES:
        raise SystemExit(f"proto 字节数 {proto_path.stat().st_size} != {PROTO_BYTES}，拒绝生成。")

    from grpc_tools import protoc  # 延迟导入：缺 grpcio-tools 时报可执行的错误

    out_dir.mkdir(parents=True, exist_ok=True)
    rc = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{proto_path.parent}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(proto_path),
        ]
    )
    if rc != 0:
        raise SystemExit(f"protoc 失败：exit={rc}")

    grpc_module = out_dir / "docreader_pb2_grpc.py"
    text = grpc_module.read_text(encoding="utf-8")
    if ABSOLUTE_IMPORT not in text:
        raise SystemExit("生成器输出已变化：docreader_pb2_grpc.py 未包含预期的绝对 import。")
    grpc_module.write_text(text.replace(ABSOLUTE_IMPORT, RELATIVE_IMPORT, 1), encoding="utf-8")

    for name in GENERATED_MODULES:
        target = out_dir / name
        body = target.read_text(encoding="utf-8")
        if not body.startswith(RUFF_NOQA):
            target.write_text(RUFF_NOQA + body, encoding="utf-8")

    versions = {
        "python": sys.version.split()[0],
        "grpcio": _dist_version("grpcio"),
        "grpcio-tools": _dist_version("grpcio-tools"),
        "protobuf": _dist_version("protobuf"),
    }
    provenance: dict[str, Any] = {
        "task": "P0-T10",
        "source_repo": "references/WeKnora",
        "source_commit": SOURCE_COMMIT,
        "source_path": "docreader/proto/docreader.proto",
        "proto_sha256": digest,
        "proto_bytes": proto_path.stat().st_size,
        "generator": "grpc_tools.protoc",
        "versions": versions,
        "outputs": list(GENERATED_MODULES),
        "note": (
            "stub 是 proto 的派生物；tests/phase0/test_docreader_wire.py 会从副本重新生成"
            "描述符并与之比对。真实 DocReader 服务未运行（T09：镜像 digest/许可 UNKNOWN）。"
        ),
    }
    (out_dir / "PROVENANCE.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从真实 proto 生成 DocReader gRPC stub")
    parser.add_argument("--proto", type=Path, default=DEFAULT_PROTO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    provenance = generate(args.proto, args.out)
    print(json.dumps(provenance, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
