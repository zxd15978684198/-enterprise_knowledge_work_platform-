"""控制站 HTTP 处理器：计数器端点、页面端点与故障语义。

设计边界（P0-T04 硬约束）：

* 只绑回环、只做测试用计数/页面/故障靶场，不 import 任何业务模块、不写 DB；
* 故障注入入口**默认不存在**：``allow_faults=False`` 时 ``/fault/*`` 与
  ``/_fault/*`` 一律 404，与未知路径无法区分（生产默认不可启用）；
* 所有响应带 ``X-Control-Site-Request-Id``，与到达日志的 seq 对齐，
  让「最终到达端点」这类断言可以精确对上某一次请求；
* 从不记录 Cookie/Authorization 的**值**，只记录它们出现过（见 store）。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import socket
import struct
import sys
import time
import urllib.parse
from typing import Any

from . import payloads
from .store import SESSION_COOKIE, ControlStore, InvalidKeyError, validate_key

__all__ = ["BREAKPOINT_NAMES", "FAULT_PREFIX", "build_handler"]

JSON_CT = "application/json; charset=utf-8"
HTML_CT = "text/html; charset=utf-8"
OCTET_CT = "application/octet-stream"
MAX_BODY_BYTES = 64 * 1024
FAULT_PREFIX = "/fault/"
FAULT_ADMIN_PREFIX = "/_fault/"

#: docs/poc/06 §5 的提交点断点名（与 faults.BREAKPOINT_NAMES 同源，此处只做文档引用）。
BREAKPOINT_NAMES: tuple[str, ...] = (
    "after_business_commit",
    "after_publish_before_mark",
    "after_job_claim",
    "after_effect_before_receipt",
    "after_receipt_before_checkpoint",
    "before_activation_commit",
)


class _HttpError(Exception):
    """处理器内部的一次性响应（状态码 + 错误码 + 附加上下文）。"""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


class _NullWriter:
    """断连后替换掉的写端：让 http.server 的 flush/close 变成空操作。"""

    closed = True

    def write(self, data: Any) -> int:
        return 0

    def writelines(self, lines: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/"): "_get_index",
    ("GET", "/healthz"): "_get_healthz",
    ("GET", "/static/page.html"): "_get_static_page",
    ("GET", "/static/js.html"): "_get_js_page",
    ("GET", "/static/iframe.html"): "_get_iframe_page",
    ("GET", "/static/frame.html"): "_get_frame_page",
    ("GET", "/download/html"): "_get_download_html",
    ("GET", "/download/binary"): "_get_download_binary",
    ("GET", "/redirect/301"): "_get_redirect_301",
    ("GET", "/redirect/302"): "_get_redirect_302",
    ("GET", "/login"): "_get_login",
    ("POST", "/login"): "_post_login",
    ("GET", "/protected"): "_get_protected",
    ("POST", "/idem/write"): "_post_idem_write",
    ("POST", "/nonidem/write"): "_post_nonidem_write",
    ("GET", "/idem/count"): "_get_idem_count",
    ("GET", "/nonidem/count"): "_get_nonidem_count",
    ("GET", "/js/beacon"): "_get_js_beacon",
    ("POST", "/js/beacon"): "_post_js_beacon",
    ("GET", "/_control/state"): "_get_control_state",
    ("GET", "/_control/requests"): "_get_control_requests",
    ("POST", "/_control/reset"): "_post_control_reset",
}

_FAULT_ADMIN_ROUTES: dict[tuple[str, str], str] = {
    ("POST", f"{FAULT_ADMIN_PREFIX}arm"): "_post_fault_arm",
    ("POST", f"{FAULT_ADMIN_PREFIX}release"): "_post_fault_release",
    ("POST", f"{FAULT_ADMIN_PREFIX}wait"): "_post_fault_wait",
    ("GET", f"{FAULT_ADMIN_PREFIX}state"): "_get_fault_state",
}


def _known_methods(path: str) -> list[str]:
    return sorted(method for method, route_path in _ROUTES if route_path == path)


def _abort_socket(connection: socket.socket) -> None:
    """尽量用 RST 主动断连（平台结构体不同）；失败则退回普通 close。"""
    try:
        if sys.platform == "win32":
            linger = struct.pack("HH", 1, 0)  # Winsock: struct linger 为两个 u_short
        else:
            linger = struct.pack("ii", 1, 0)  # BSD/Linux: int + int
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        connection.close()


def build_handler(
    *,
    store: ControlStore,
    barrier: Any | None,
    allow_faults: bool,
) -> type[http.server.BaseHTTPRequestHandler]:
    """按站点配置构造处理器类（闭包持有 store / barrier）。"""

    class ControlSiteHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PyKnoraControlSite/0.1"
        sys_version = ""

        # -- 基础设施 -----------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:
            # 故意静默：证据走到达日志，不往 stderr 打请求行（避免噪声与泄漏面）。
            return

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = {key: value for key, value in urllib.parse.parse_qsl(parsed.query)}
            self._record = store.begin_request(
                method=method, path=path, query=query, headers=self.headers
            )
            self._finished = False
            self._fault_gate: str | None = None
            try:
                self._body = self._read_body()
                if path.startswith(FAULT_PREFIX):
                    self._handle_fault(name=path[len(FAULT_PREFIX) :], query=query)
                    return
                if path.startswith(FAULT_ADMIN_PREFIX):
                    self._handle_fault_admin(path=path, method=method, query=query)
                    return
                route = _ROUTES.get((method, path))
                if route is not None:
                    getattr(self, route)(query=query)
                    return
                allowed = _known_methods(path)
                if allowed:
                    self._send_json(
                        405,
                        {
                            "error": "method_not_allowed",
                            "path": path,
                            "allowed": allowed,
                        },
                        headers=(("Allow", ", ".join(allowed)),),
                    )
                    return
                self._send_json(404, {"error": "not_found", "path": path})
            except _HttpError as exc:
                payload: dict[str, Any] = {"error": exc.code}
                payload.update(exc.extra)
                self._send_json(exc.status, payload)
            except (BrokenPipeError, ConnectionResetError):
                self._finish(aborted=True)
            finally:
                if not self._finished:
                    self._finish(aborted=True)

        # -- 响应写出 -----------------------------------------------------
        def _finish(
            self,
            *,
            status: int | None = None,
            key: str | None = None,
            aborted: bool = False,
        ) -> None:
            if self._finished:
                return
            self._finished = True
            store.finish(
                self._record,
                status=status,
                key=key,
                fault_gate=self._fault_gate,
                aborted=aborted,
            )

        def _send(
            self,
            status: int,
            body: bytes,
            *,
            content_type: str = JSON_CT,
            headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Control-Site-Request-Id", str(self._record.seq))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self._finish(aborted=True)
                return
            self._finish(status=status)

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send(status, body, headers=headers)

        def _send_html(self, status: int, html: str, **kwargs: Any) -> None:
            self._send(status, html.encode("utf-8"), content_type=HTML_CT, **kwargs)

        # -- 请求解析 -----------------------------------------------------
        def _read_body(self) -> bytes:
            raw_length = self.headers.get("Content-Length")
            if not raw_length:
                return b""
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise _HttpError(400, "invalid_content_length") from exc
            if length < 0:
                raise _HttpError(400, "invalid_content_length")
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                raise _HttpError(413, "body_too_large", limit=MAX_BODY_BYTES)
            return self.rfile.read(length)

        def _payload(self, query: dict[str, str]) -> dict[str, Any]:
            payload: dict[str, Any] = dict(query)
            if not self._body:
                return payload
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                text = self._body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _HttpError(400, "invalid_utf8") from exc
            if content_type == "application/json" or text.lstrip().startswith("{"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise _HttpError(400, "invalid_json") from exc
                if not isinstance(data, dict):
                    raise _HttpError(400, "json_object_required")
                payload.update(data)
                return payload
            payload.update(
                {key: value for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True)}
            )
            return payload

        def _key_from(self, payload: dict[str, Any], *, default: str | None = None) -> str:
            raw = payload.get("key", default)
            try:
                return validate_key(raw)
            except InvalidKeyError as exc:
                raise _HttpError(400, "invalid_key", reason=str(exc)) from exc

        # -- 页面与健康 ---------------------------------------------------
        def _get_index(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.INDEX_PAGE_HTML)

        def _get_healthz(self, *, query: dict[str, str]) -> None:
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "pyknora-phase0-control-site",
                    "fault_injection": bool(allow_faults),
                    "request_id": self._record.seq,
                    "uptime_s": round(store.uptime_s, 3),
                },
            )

        def _get_static_page(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.STATIC_PAGE_HTML)

        def _get_js_page(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.JS_PAGE_HTML)

        def _get_iframe_page(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.IFRAME_PAGE_HTML)

        def _get_frame_page(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.FRAME_PAGE_HTML)

        def _get_download_html(self, *, query: dict[str, str]) -> None:
            self._send(
                200,
                payloads.DOWNLOAD_HTML.encode("utf-8"),
                content_type=HTML_CT,
                headers=(("Content-Disposition", 'attachment; filename="control-download.html"'),),
            )

        def _get_download_binary(self, *, query: dict[str, str]) -> None:
            self._send(
                200,
                payloads.BINARY_PAYLOAD,
                content_type=OCTET_CT,
                headers=(("Content-Disposition", 'attachment; filename="control-download.bin"'),),
            )

        def _get_redirect_301(self, *, query: dict[str, str]) -> None:
            self._send(301, b"", headers=(("Location", "/static/page.html"),))

        def _get_redirect_302(self, *, query: dict[str, str]) -> None:
            self._send(302, b"", headers=(("Location", "/static/page.html"),))

        # -- 人工登录 -----------------------------------------------------
        def _session_token(self) -> str | None:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return value
            return None

        def _get_login(self, *, query: dict[str, str]) -> None:
            self._send_html(200, payloads.LOGIN_PAGE_HTML)

        def _post_login(self, *, query: dict[str, str]) -> None:
            payload = self._payload(query)
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            if username != payloads.TEST_USER or password != payloads.TEST_PASSWORD:
                self._send_json(
                    401,
                    {
                        "error": "invalid_credentials",
                        "note": "合成测试账号；自动化不得猜测或绕过登录",
                    },
                )
                return
            token = store.create_session()
            cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
            self._send(303, b"", headers=(("Location", "/protected"), ("Set-Cookie", cookie)))

        def _get_protected(self, *, query: dict[str, str]) -> None:
            if not store.session_valid(self._session_token()):
                self._send_json(
                    401,
                    {
                        "authenticated": False,
                        "error": "login_required",
                        "login_url": "/login",
                    },
                )
                return
            self._send_json(
                200,
                {
                    "authenticated": True,
                    "user": payloads.TEST_USER,
                    "session": SESSION_COOKIE,
                },
            )

        # -- 计数端点 -----------------------------------------------------
        def _post_idem_write(self, *, query: dict[str, str]) -> None:
            key = self._key_from(self._payload(query))
            result = store.idempotent_write(key)
            self._record.key = key
            self._send_json(
                200,
                {**result, "request_id": self._record.seq},
                headers=(("X-Idempotent-Replayed", "false" if result["applied"] else "true"),),
            )

        def _post_nonidem_write(self, *, query: dict[str, str]) -> None:
            key = self._key_from(self._payload(query))
            result = store.non_idempotent_write(key)
            self._record.key = key
            self._send_json(200, {**result, "request_id": self._record.seq})

        def _get_idem_count(self, *, query: dict[str, str]) -> None:
            key = self._key_from(query)
            self._record.key = key
            self._send_json(200, store.counts("idem", key))

        def _get_nonidem_count(self, *, query: dict[str, str]) -> None:
            key = self._key_from(query)
            self._record.key = key
            self._send_json(200, store.counts("nonidem", key))

        def _get_js_beacon(self, *, query: dict[str, str]) -> None:
            key = self._key_from(query, default="js-page")
            result = store.non_idempotent_write(key)
            self._record.key = key
            self._send_json(200, {**result, "source": "js-page"})

        def _post_js_beacon(self, *, query: dict[str, str]) -> None:
            payload = self._payload(query)
            key = self._key_from(payload, default="js-page")
            result = store.non_idempotent_write(key)
            self._record.key = key
            self._send_json(200, {**result, "source": "js-page", "vector": payload.get("vector")})

        # -- 控制面（只读观测 + 复位）-------------------------------------
        def _get_control_state(self, *, query: dict[str, str]) -> None:
            snapshot = store.snapshot()
            snapshot["client_errors"] = getattr(self.server, "client_errors", 0)
            snapshot["fault_injection"] = bool(allow_faults)
            if allow_faults and barrier is not None:
                snapshot["barrier"] = barrier.snapshot()
            self._send_json(200, snapshot)

        def _get_control_requests(self, *, query: dict[str, str]) -> None:
            try:
                after = int(query.get("after", "0"))
                limit = int(query.get("limit", "100"))
            except ValueError as exc:
                raise _HttpError(400, "invalid_number") from exc
            if after < 0 or limit <= 0 or limit > 1000:
                raise _HttpError(400, "out_of_range", after=after, limit=limit)
            self._send_json(200, store.arrivals(after=after, limit=limit))

        def _post_control_reset(self, *, query: dict[str, str]) -> None:
            result = store.reset()
            if allow_faults and barrier is not None:
                released = barrier.release_all()
                result["barrier_released"] = released
            self._send_json(200, result)

        # -- 故障注入（默认不存在）----------------------------------------
        def _handle_fault(self, *, name: str, query: dict[str, str]) -> None:
            if not allow_faults or barrier is None or name not in barrier.fault_names():
                self._send_json(404, {"error": "not_found", "path": self.path})
                return
            params = barrier.params_of(name)
            if name == "disconnect_after_success":
                key = self._key_from(self._payload(query), default=f"fault-{name}")
                _effect = store.non_idempotent_write(key)  # 副作用先落地，再断连（F3 窗口）
                self._record.key = key
                self._gate(name)
                self._finish(status=None, aborted=True)
                self._abort()
                return
            if name == "delay":
                self._gate(name)
                seconds = _clamp(params.get("delay_seconds", 0.0), 0.0, 10.0)
                time.sleep(seconds)
                self._send_json(
                    200,
                    {
                        "fault": name,
                        "gate": self._fault_gate,
                        "delay_seconds": seconds,
                        "request_id": self._record.seq,
                    },
                    headers=(("X-Phase0-Fault", name),),
                )
                return
            if name == "timeout":
                # 服务端挂住连接直到测试 release / 屏障 deadline；随后无响应断连，
                # 客户端侧表现为 socket 超时或连接被关闭（两种都在断言容忍范围内）。
                self._gate(name)
                self._finish(status=None, aborted=True)
                self._abort()
                return
            if name == "server_error":
                self._gate(name)
                status = int(_clamp(params.get("status", 503), 500, 599))
                self._send_json(
                    status,
                    {
                        "error": "injected_server_error",
                        "fault": name,
                        "request_id": self._record.seq,
                    },
                    headers=(("X-Phase0-Fault", name),),
                )
                return
            if name == "truncate":
                self._gate(name)
                declared = int(_clamp(params.get("declared_bytes", 4096), 64, 1_048_576))
                sent = int(_clamp(params.get("sent_bytes", 17), 0, max(declared - 1, 0)))
                self._truncate(declared=declared, sent=sent)
                return
            if name == "login_required":
                self._gate(name)
                if params.get("mode") == "wall":
                    self._send_html(
                        401,
                        payloads.LOGIN_PAGE_HTML,
                        headers=(
                            ("X-Phase0-Fault", name),
                            ("WWW-Authenticate", 'FormBased realm="phase0-manual"'),
                        ),
                    )
                    return
                self._send_html(
                    200,
                    payloads.LOGIN_PAGE_HTML,
                    headers=(("X-Phase0-Fault", name),),
                )
                return
            self._send_json(404, {"error": "not_found", "path": self.path})

        def _gate(self, name: str) -> str:
            gate = barrier.hit(name)  # type: ignore[union-attr]
            self._fault_gate = gate
            self._record.fault_gate = gate
            return gate

        def _truncate(self, *, declared: int, sent: int) -> None:
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {declared}\r\n"
                "Connection: close\r\n"
                f"X-Control-Site-Request-Id: {self._record.seq}\r\n"
                "X-Phase0-Fault: truncate\r\n"
                "\r\n"
            ).encode("ascii")
            try:
                self.wfile.write(head)
                if sent:
                    self.wfile.write(b"X" * sent)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self._finish(status=None, aborted=True)
                self.wfile = _NullWriter()  # type: ignore[assignment]
                return
            self.close_connection = True
            self._finish(status=200)
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_WR)
            self.wfile = _NullWriter()  # type: ignore[assignment]

        def _abort(self) -> None:
            self.close_connection = True
            _abort_socket(self.connection)
            self.wfile = _NullWriter()  # type: ignore[assignment]

        # -- 故障控制面（默认不存在）--------------------------------------
        def _handle_fault_admin(self, *, path: str, method: str, query: dict[str, str]) -> None:
            if not allow_faults or barrier is None:
                self._send_json(404, {"error": "not_found", "path": path})
                return
            route = _FAULT_ADMIN_ROUTES.get((method, path))
            if route is None:
                allowed = sorted(
                    allowed_method
                    for allowed_method, admin_path in _FAULT_ADMIN_ROUTES
                    if admin_path == path
                )
                if allowed:
                    self._send_json(
                        405,
                        {"error": "method_not_allowed", "path": path, "allowed": allowed},
                        headers=(("Allow", ", ".join(allowed)),),
                    )
                    return
                self._send_json(404, {"error": "not_found", "path": path})
                return
            getattr(self, route)(query=query)

        def _get_fault_state(self, *, query: dict[str, str]) -> None:
            self._send_json(200, barrier.snapshot())  # type: ignore[union-attr]

        def _post_fault_arm(self, *, query: dict[str, str]) -> None:
            payload = self._payload(query)
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                raise _HttpError(400, "name_required")
            armable = barrier.fault_names() | barrier.breakpoint_names()  # type: ignore[union-attr]
            if name not in armable:
                # 只允许本任务声明的故障与提交点名称，避免控制面被当成任意开关。
                raise _HttpError(
                    400,
                    "unknown_fault",
                    allowed=sorted(armable),
                )
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise _HttpError(400, "params_must_be_object")
            timeout = payload.get("timeout")
            try:
                state = barrier.arm(  # type: ignore[union-attr]
                    name,
                    timeout=float(timeout) if timeout is not None else None,
                    params=params,
                )
            except (ValueError, TypeError) as exc:
                raise _HttpError(400, "arm_rejected", reason=str(exc)) from exc
            self._send_json(200, state.as_dict())

        def _post_fault_release(self, *, query: dict[str, str]) -> None:
            payload = self._payload(query)
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                raise _HttpError(400, "name_required")
            try:
                released = barrier.release(name)  # type: ignore[union-attr]
            except ValueError as exc:
                raise _HttpError(400, "release_rejected", reason=str(exc)) from exc
            self._send_json(
                200, {"released": released, "state": barrier.state(name)}  # type: ignore[union-attr]
            )

        def _post_fault_wait(self, *, query: dict[str, str]) -> None:
            payload = self._payload(query)
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                raise _HttpError(400, "name_required")
            timeout = payload.get("timeout")
            try:
                hit_seen = barrier.wait_armed(  # type: ignore[union-attr]
                    name, timeout=float(timeout) if timeout is not None else None
                )
            except (ValueError, TypeError) as exc:
                raise _HttpError(400, "wait_rejected", reason=str(exc)) from exc
            self._send_json(
                200,
                {
                    "hit": hit_seen,
                    "state": barrier.state(name),  # type: ignore[union-attr]
                },
            )

    return ControlSiteHandler


def _clamp(value: Any, low: float, high: float) -> float:
    """把故障参数夹到安全区间，避免测试参数写出一个挂死/超长的服务。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(number, high))
