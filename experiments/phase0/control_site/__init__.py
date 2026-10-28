"""PyKnora Phase 0 控制站（纯标准库本地测试靶场，P0-T04）。

对外只暴露三件事：

* :class:`ControlSite` —— 可重复起停的 HTTP 服务（只绑 127.0.0.1）；
* :mod:`control_site.client` —— 绕过代理的最小 HTTP 客户端（测试侧断言用）；
* :mod:`control_site.payloads` —— 确定性载荷与 sha256（manifest 校验用）。

本包不写任何业务表、不读凭据、不落盘；故障注入入口默认不存在，
只有显式 ``allow_fault_injection=True`` 才会挂载。
"""

from __future__ import annotations

from .client import HttpResult, request, wait_for_healthz
from .handlers import FAULT_PREFIX, build_handler
from .payloads import PAYLOAD_ID
from .server import LOOPBACK_HOSTS, ControlSite
from .store import SESSION_COOKIE, ControlStore, InvalidKeyError, validate_key

__all__ = [
    "FAULT_PREFIX",
    "LOOPBACK_HOSTS",
    "PAYLOAD_ID",
    "SESSION_COOKIE",
    "ControlSite",
    "ControlStore",
    "HttpResult",
    "InvalidKeyError",
    "build_handler",
    "request",
    "validate_key",
    "wait_for_healthz",
]

__version__ = "0.1.0"
