"""测试侧最小 HTTP 客户端（标准库），强制绕过代理并显式区分失败类型。

为什么不直接用第三方客户端：本任务硬约束「控制站只用标准库、不新增依赖」，
而实验环境当前只装了 psycopg / sqlalchemy / redis / pyyaml / pytest 系列。
本模块因此只包一层 ``urllib``：

* 构造 opener 时传入空 ``ProxyHandler`` —— 机器上的 ``HTTP_PROXY`` 不会把
  回环请求劫走（否则「到达了控制站」的证据会失真）；
* 连接失败/超时/断流都归类成 :class:`HttpResult.error`，不抛异常，
  让测试可以断言「客户端**没有**拿到完整响应」这类事实；
* ``follow_redirects=False`` 用于断言 301/302 的原始 ``Location``。
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = ["HttpResult", "request", "wait_for_healthz"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """关闭自动跟随，便于断言原始状态码与 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - 基类签名
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


@dataclass
class HttpResult:
    """一次 HTTP 往返的归约结果；``error`` 非空表示客户端侧失败。"""

    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        """是否拿到了完整成功响应（有状态码且无客户端错误）。"""
        return self.status is not None and self.error is None

    @property
    def complete(self) -> bool:
        return self.ok

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def location(self) -> str | None:
        return self.headers.get("location") or self.headers.get("Location")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


def request(
    method: str,
    url: str,
    *,
    body: bytes | str | dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    follow_redirects: bool = True,
) -> HttpResult:
    """发一次请求；任何网络/协议失败都转成 ``HttpResult.error``，不抛异常。

    ``body`` 支持 bytes/str 原样发送，也支持 dict —— 后者按 JSON 编码并自动补
    ``Content-Type: application/json``，避免每个用例自己序列化。
    """
    extra_headers = dict(headers or {})
    if isinstance(body, dict):
        data: bytes | None = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if not any(name.lower() == "content-type" for name in extra_headers):
            extra_headers["Content-Type"] = "application/json"
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = body
    req = urllib.request.Request(url, data=data, method=method.upper())
    for name, value in extra_headers.items():
        req.add_header(name, value)
    opener = _OPENER if follow_redirects else _NO_REDIRECT_OPENER
    started = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        response = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        payload = b""
        try:
            payload = exc.read()
        except OSError:
            payload = b""
        return HttpResult(
            status=exc.code,
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=payload,
            elapsed_s=elapsed(),
        )
    except urllib.error.URLError as exc:
        return HttpResult(
            status=None,
            error=f"URLError:{type(exc.reason).__name__}:{exc.reason}",
            elapsed_s=elapsed(),
        )
    except (http.client.HTTPException, OSError) as exc:
        return HttpResult(
            status=None,
            error=f"{type(exc).__name__}:{exc}",
            elapsed_s=elapsed(),
        )

    status = response.status
    response_headers = dict(response.headers.items())
    try:
        payload = response.read()
        return HttpResult(
            status=status,
            headers=response_headers,
            body=payload,
            elapsed_s=elapsed(),
        )
    except http.client.IncompleteRead as exc:
        # 断流故障：状态行与头已到手，但正文长度不足。保留已读部分作为证据。
        partial = bytes(getattr(exc, "partial", b"") or b"")
        return HttpResult(
            status=status,
            headers=response_headers,
            body=partial,
            error=f"IncompleteRead:{exc!s}",
            elapsed_s=elapsed(),
        )
    except (http.client.HTTPException, OSError) as exc:
        return HttpResult(
            status=status,
            headers=response_headers,
            error=f"{type(exc).__name__}:{exc}",
            elapsed_s=elapsed(),
        )
    finally:
        response.close()


def wait_for_healthz(url: str, *, timeout: float = 10.0, max_interval: float = 0.05) -> float:
    """轮询 ``/healthz`` 直到 200，返回耗时；超时抛 :class:`TimeoutError`。

    这是「健康等待用 healthz 轮询而不是 sleep」的落地：轮询间隔从 5ms 指数退避到
    ``max_interval``，整体受 ``timeout`` 约束，失败时把最后一次错误带进异常。
    """
    started = time.monotonic()
    interval = 0.005
    last_error = "未发起请求"
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError(f"{url} 未在 {timeout:.1f}s 内就绪（最后错误：{last_error}）")
        result = request("GET", url, timeout=min(1.0, max(remaining, 0.1)))
        if result.status == 200:
            return time.monotonic() - started
        last_error = result.error or f"status={result.status}"
        time.sleep(min(interval, max(remaining, 0.001)))
        interval = min(interval * 1.7, max_interval)
