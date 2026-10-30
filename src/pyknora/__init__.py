"""PyKnora：企业知识工作平台（P0 MVP）。"""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> None:
    """``pyknora`` 命令行入口：启动 API 服务。"""

    from pyknora.serve import main as serve

    serve()


__all__ = ["__version__", "main"]
