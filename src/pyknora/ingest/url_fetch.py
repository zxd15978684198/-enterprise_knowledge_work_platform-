"""受控 URL 抓取：SSRF 防护 + 大小/类型/超时/重定向策略。

为什么要有这一层：URL 由客户端提交，属于不可信输入。若直接 ``httpx.get(url)``，
攻击者可以用 ``http://169.254.169.254/latest/meta-data/`` 之类的地址把云元数据、
内网服务、本机端口读进知识库。因此这里做五件事：

1. **只接受 http/https**，且拒绝带 userinfo 的 URL（``http://user:pass@host/``）；
2. **每一次 DNS 解析结果都要过私网判定**——不是看 URL 里写的字符串，而是看真正
   解析出的 IP（含 IPv4-mapped-IPv6 形式），避免 ``127.0.0.1.nip.io`` 这类绕过；
3. **手动跟随重定向**（默认最多 3 跳），每一跳都重新做第 1/2 步，
   防止「公网 URL 302 到内网」；
4. **流式读取并硬截断** ``max_bytes``，避免超大响应打爆内存；
5. **内容类型白名单**，只接受可解析为文本的 html/xhtml/plain/markdown。

已知限制（记为债务，见 ``docs/phase1/wave-w21-record.md`` §5）：本实现是「解析时
校验 + 逐跳复校验」，仍存在 DNS rebinding 的窗口（解析与连接之间 TTL 内换 IP）。
生产级做法是 egress proxy 或 DNS pinning，本阶段不做。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from pyknora.config import get_settings
from pyknora.ingest.parsers import is_supported

#: 只允许这两种 scheme；``file:``/``ftp:``/``gopher:`` 等一律拒绝。
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 可接受的响应内容类型（只取 ``;`` 前的主类型）。
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }
)

#: 非公网网段：回环、私网、链路本地、CGNAT、文档/基准测试段、组播、保留段。
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
    )
)

_CONTENT_TYPE_SUFFIX = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
}

_MAX_URL_LENGTH = 2048
_MAX_FILENAME_LENGTH = 180
_USER_AGENT = "PyKnora-URLImporter/0.1 (+https://example.invalid/pyknora)"


class UrlFetchError(Exception):
    """抓取失败。``code`` 供 API 层映射为统一错误体，不要直接回显内部细节。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """一次成功抓取的结果；``final_url`` 是重定向后的最终地址。"""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    data: bytes

    @property
    def filename(self) -> str:
        return derive_filename(self.final_url, self.content_type)


def normalize_url(raw: str) -> str:
    """校验并规范化 URL；失败抛 :class:`UrlFetchError`。"""

    text = (raw or "").strip()
    if not text:
        raise UrlFetchError("invalid_url", "URL 不能为空")
    if len(text) > _MAX_URL_LENGTH:
        raise UrlFetchError("invalid_url", f"URL 长度上限 {_MAX_URL_LENGTH} 字符")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlFetchError(
            "unsupported_scheme",
            f"只支持 http/https，收到 {scheme or '（空）'}",
            status_code=422,
        )
    if parts.username or parts.password:
        raise UrlFetchError(
            "url_credentials_not_allowed",
            "URL 不允许携带用户名或口令",
            status_code=422,
        )
    host = parts.hostname
    if not host:
        raise UrlFetchError("invalid_url", "URL 缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:  # 端口越界（如 :99999）
        raise UrlFetchError("invalid_url", f"端口非法：{exc}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise UrlFetchError("invalid_url", "端口超出 1-65535")

    netloc = host if port is None else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    # 去掉 fragment；保留 query，很多文档站靠 ?id= 定位
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def derive_filename(url: str, content_type: str) -> str:
    """从 URL 路径推导落库文件名；路径无可用后缀时按内容类型补。"""

    path = unquote(urlsplit(url).path or "")
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if base and is_supported(base):
        name = base
    else:
        suffix = _CONTENT_TYPE_SUFFIX.get(content_type, ".html")
        stem = base or "page"
        name = f"{stem}{suffix}"
    name = name.replace("\\", "_").replace("/", "_")
    return name[:_MAX_FILENAME_LENGTH] or "page.html"


async def _resolve_ips(host: str, port: int) -> list[str]:
    """解析主机名（测试可替换此函数以注入固定 IP）。"""

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlFetchError("dns_resolution_failed", f"域名解析失败：{host}") from exc
    return sorted({str(info[4][0]) for info in infos})


def _blocked_reason(ip_text: str) -> str | None:
    """返回被拦截的原因；公网地址返回 ``None``。"""

    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return f"无法识别的地址 {ip_text}"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        ip = ipaddress.IPv6Address(str(ip).split("%", 1)[0])
    for network in _BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return f"{ip} 属于非公网网段 {network}"
    return None


async def assert_public_target(host: str, port: int) -> None:
    """解析并校验目标主机；任何一个解析结果落在非公网段即拒绝（fail-closed）。"""

    ips = await _resolve_ips(host, port)
    if not ips:
        raise UrlFetchError("dns_resolution_failed", f"域名无解析结果：{host}")
    for ip_text in ips:
        reason = _blocked_reason(ip_text)
        if reason is not None:
            raise UrlFetchError(
                "blocked_target",
                f"目标地址被拒绝：{reason}",
                status_code=403,
            )


async def fetch_url(
    url: str,
    *,
    timeout_seconds: float | None = None,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    allow_private_network: bool | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedPage:
    """抓取 URL 并返回受限字节。

    参数默认值取自 :class:`pyknora.config.Settings`；``transport`` 仅供测试注入。
    """

    settings = get_settings()
    if timeout_seconds is None:
        timeout_seconds = settings.url_fetch_timeout_seconds
    max_bytes = settings.url_max_bytes if max_bytes is None else max_bytes
    max_redirects = settings.url_max_redirects if max_redirects is None else max_redirects
    if allow_private_network is None:
        allow_private_network = settings.url_allow_private_network

    current = normalize_url(url)
    requested = current

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.1"},
    ) as client:
        for hop in range(max_redirects + 1):
            parts = urlsplit(current)
            host = parts.hostname or ""
            port = parts.port or (443 if parts.scheme == "https" else 80)
            if not allow_private_network:
                await assert_public_target(host, port)

            try:
                async with client.stream("GET", current) as response:
                    status_code = response.status_code
                    if status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise UrlFetchError(
                                "redirect_without_location",
                                "上游返回重定向但缺少 Location",
                                status_code=502,
                            )
                        if hop >= max_redirects:
                            raise UrlFetchError(
                                "too_many_redirects",
                                f"重定向超过 {max_redirects} 跳",
                                status_code=502,
                            )
                        current = urljoin(current, location)
                        continue
                    if status_code >= 400:
                        raise UrlFetchError(
                            "upstream_http_error",
                            f"上游返回 HTTP {status_code}",
                            status_code=502,
                        )

                    content_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if content_type not in ALLOWED_CONTENT_TYPES:
                        raise UrlFetchError(
                            "unsupported_content_type",
                            f"不支持的内容类型 {content_type or '（缺失）'}；"
                            "支持 html/plain/markdown",
                            status_code=415,
                        )

                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > max_bytes:
                            raise UrlFetchError(
                                "response_too_large",
                                f"响应超过上限 {max_bytes} 字节",
                                status_code=413,
                            )
                    if not buffer:
                        raise UrlFetchError("empty_response", "上游响应为空", status_code=422)
                    return FetchedPage(
                        requested_url=requested,
                        final_url=current,
                        status_code=status_code,
                        content_type=content_type,
                        data=bytes(buffer),
                    )
            except httpx.TimeoutException as exc:
                raise UrlFetchError(
                    "upstream_timeout", f"抓取超时（{timeout_seconds} 秒）", status_code=504
                ) from exc
            except httpx.HTTPError as exc:
                raise UrlFetchError(
                    "upstream_unreachable", f"抓取失败：{type(exc).__name__}", status_code=502
                ) from exc

    raise UrlFetchError("too_many_redirects", "重定向次数用尽", status_code=502)
