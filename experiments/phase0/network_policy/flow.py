"""受控 HTTP 取回：**连接固定已授权 IP**，逐跳过策略，重定向由我们自己跟。

为什么不用 ``requests``/``urllib`` 直接取：它们会按 URL 再解析一次 DNS，等于给了 rebinding
第二次机会。这里用 ``HopGuard`` 授权出来的 ``connect_ip`` 建连接，Host 头仍写域名，
TLS 用 ``server_hostname`` 指定 SNI —— 连接目的与证书校验都锚定在授权时的解析结果上。

下载/子资源同样走 ``guard.note_bytes()``：超过上限立即抛错，**不会**继续读。
"""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin
from uuid import UUID

from .engine import HopDecision, HopGuard, NetworkPolicyEngine
from .errors import PolicyViolation, Reason

READ_CHUNK: Final = 64 * 1024
MAX_HEADER_BYTES: Final = 64 * 1024
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
USER_AGENT: Final = "pyknora-phase0-probe/1 (+p0-t16)"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次取回的**事实**：状态、头、体、最终 URL、跳数、实际读取字节数。"""

    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str
    hops: int
    bytes_read: int

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _PinnedConnection(http.client.HTTPConnection):
    """与授权结果绑定的连接：TCP 连 ``connect_ip``，Host/SNI 仍是域名。"""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_ip: str,
        timeout: float,
        tls: ssl.SSLContext | None,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._connect_ip = connect_ip
        self._tls = tls

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        if self._tls is not None:
            sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


class RawGetter:
    """单跳 GET，不跟随重定向。TLS 证书校验默认开启且不可关。"""

    def __init__(self, *, tls_context: ssl.SSLContext | None = None) -> None:
        self._tls_context = tls_context or ssl.create_default_context()

    def get(
        self, decision: HopDecision, *, guard: HopGuard
    ) -> tuple[int, dict[str, str], bytes]:
        guard.check_deadline()
        timeout = max(0.001, guard.remaining_ms / 1000.0)
        tls = self._tls_context if decision.scheme == "https" else None
        connection = _PinnedConnection(
            decision.host,
            decision.port,
            connect_ip=decision.connect_ip,
            timeout=timeout,
            tls=tls,
        )
        try:
            connection.request(
                "GET",
                decision.request_target,
                headers={
                    "Host": decision.host_header,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": USER_AGENT,
                },
            )
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            header_bytes = sum(len(key) + len(value) for key, value in response.getheaders())
            if header_bytes > MAX_HEADER_BYTES:
                guard.engine.metrics.incr(f"deny.{Reason.HEADERS_TOO_LARGE}")
                raise PolicyViolation(
                    Reason.HEADERS_TOO_LARGE,
                    f"响应头 {header_bytes} 字节超过上限 {MAX_HEADER_BYTES}",
                    url=decision.url,
                )
            body = self._read_body(response, guard=guard, url=decision.url)
            return response.status, headers, body
        except TimeoutError as exc:
            guard.engine.metrics.incr(f"deny.{Reason.DEADLINE_EXCEEDED}")
            raise PolicyViolation(
                Reason.DEADLINE_EXCEEDED,
                "读取响应超时（总时长预算耗尽）",
                url=decision.url,
                data={"bytes_read": guard.bytes_read},
            ) from exc
        except OSError as exc:
            raise PolicyViolation(
                Reason.UNSAFE_REDIRECT,
                f"连接 {decision.connect_ip}:{decision.port} 失败：{exc}",
                url=decision.url,
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _read_body(
        response: http.client.HTTPResponse, *, guard: HopGuard, url: str
    ) -> bytes:
        chunks: list[bytes] = []
        while True:
            guard.check_deadline()
            chunk = response.read(READ_CHUNK)
            if not chunk:
                return b"".join(chunks)
            guard.note_bytes(len(chunk))
            chunks.append(chunk)


class SafeFetcher:
    """带策略的取回器：每个 URL（含每跳重定向）都重新过 ``HopGuard.check``。"""

    def __init__(
        self,
        engine: NetworkPolicyEngine,
        *,
        getter: RawGetter | None = None,
    ) -> None:
        self._engine = engine
        self._getter = getter or RawGetter()

    def fetch(
        self,
        url: str,
        *,
        workspace_id: UUID,
        max_redirects: int | None = None,
        max_response_bytes: int | None = None,
        total_budget_ms: int | None = None,
    ) -> FetchResult:
        with self._engine.open_guard(
            workspace_id=workspace_id,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
            total_budget_ms=total_budget_ms,
        ) as guard:
            current = url
            while True:
                decision = guard.check(current)
                status, headers, body = self._getter.get(decision, guard=guard)
                location = headers.get("location")
                if status in REDIRECT_STATUSES and location:
                    target = urljoin(current, location)
                    guard.note_redirect(from_url=current, to_url=target)
                    current = target
                    continue
                return FetchResult(
                    status=status,
                    headers=headers,
                    body=body,
                    final_url=current,
                    hops=guard.redirects + 1,
                    bytes_read=guard.bytes_read,
                )
