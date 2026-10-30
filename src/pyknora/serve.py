"""开发服务器入口。

用法::

    uv run python -m pyknora.serve --port 8000 --reload

Linux 容器内直接 ``uvicorn pyknora.main:app`` 即可；本模块只是给本地开发加一层
参数默认值（Windows 下与 Docker 内行为一致）。
"""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pyknora.serve", description="PyKnora 开发服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="文件变更自动重启")
    args = parser.parse_args(argv)

    uvicorn.run(
        "pyknora.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
