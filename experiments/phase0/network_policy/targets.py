"""URL → 目标（scheme/host/port/path）的严格解析与归一化。

归一化的目的不是"宽容接受"，而是让**所有等价写法收敛到同一个 host 字符串**，
使白名单/黑名单无法被大小写、尾点、IDN、punycode、百分号编码、整数 IP 等写法绕过；
收敛不了的一律拒绝（``Reason.INVALID_URL`` / ``ENCODED_HOST`` 等）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .errors import PolicyViolation, Reason

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_ASCII_ONLY = re.compile(r"^[a-z0-9._~-]+$")
_HEX_OR_DECIMAL = re.compile(r"^(0x[0-9a-f]+|\d+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Target:
    """解析后的目标；``host`` 是归一化后的 unicode 形式，``host_ascii`` 是 punycode 形式。"""

    raw: str
    scheme: str
    host: str
    host_ascii: str
    port: int
    explicit_port: bool
    is_ip_literal: bool
    path: str
    query: str

    @property
    def authority(self) -> str:
        return f"{self.host_ascii}:{self.port}"


def _violation(reason: Reason, message: str, url: str) -> PolicyViolation:
    # 拒绝信息会进日志/证据：带 userinfo 的 URL 先脱敏，避免把凭据写出去。
    safe_url = redact(url) if "@" in url else url
    return PolicyViolation(reason, message, url=safe_url)


def _loose_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """把浏览器会接受的"简写 IP"（``127.1`` / ``0x7f.0.0.1`` / ``2130706433``）解析出来。

    这类写法能直接绕过只认点分十进制的字符串比较，必须在归一化阶段就消解。
    """

    if _HEX_OR_DECIMAL.match(host):
        value = int(host, 16) if host.lower().startswith("0x") else int(host, 10)
        return ipaddress.IPv4Address(value) if value <= 0xFFFFFFFF else None

    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or any(part == "" for part in parts):
        return None
    numbers: list[int] = []
    for part in parts:
        lowered = part.lower()
        if lowered.startswith("0x"):
            base, text = 16, lowered[2:]
        elif len(part) > 1 and part.startswith("0"):
            base, text = 8, part[1:]
        else:
            base, text = 10, part
        try:
            numbers.append(int(text, base))
        except ValueError:
            return None
    if any(number < 0 or number > 255 for number in numbers[:-1]):
        return None
    tail_limit = 256 ** (4 - len(numbers) + 1)
    if numbers[-1] < 0 or numbers[-1] >= tail_limit:
        return None
    value = 0
    for number in numbers[:-1]:
        value = (value << 8) | number
    value = (value << 8 * (4 - len(numbers) + 1)) | numbers[-1]
    return ipaddress.IPv4Address(value) if value <= 0xFFFFFFFF else None


def canonical_host(raw_host: str) -> tuple[str, str, bool]:
    """返回 ``(unicode host, ascii host, is_ip_literal)``；不可归一化则抛错。"""

    host = unicodedata.normalize("NFKC", raw_host).strip().lower().rstrip(".")
    if not host:
        raise ValueError("empty host")
    if "%" in host:
        # 百分号编码的 host（``exa%6dple.com``）一律拒绝：解码后再判定只会制造歧义。
        raise ValueError("percent-encoded host")
    if " " in host or "/" in host or "@" in host:
        raise ValueError("illegal host characters")

    literal = _loose_ipv4(host)
    if literal is not None:
        return str(literal), str(literal), True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None
    if parsed is not None:
        text = str(parsed)
        return text, text, True

    ascii_host = host
    if not _ASCII_ONLY.match(host):
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("idn encoding failed") from exc
    return host, ascii_host, False


def parse_target(url: str) -> Target:
    """解析并归一化 URL；任何可疑写法都在这里拒绝，不留到后面比较字符串。"""

    text = (url or "").strip()
    if not text:
        raise _violation(Reason.INVALID_URL, "空 URL", url or "")
    if _CONTROL_CHARS.search(text):
        raise _violation(Reason.CONTROL_CHAR_IN_URL, "URL 含控制字符", text)
    if "\\" in text:
        # 浏览器把 ``\`` 当 ``/`` 处理，``http://evil.com\@good.com`` 是最典型的白名单绕过。
        raise _violation(Reason.BACKSLASH_IN_URL, "URL 含反斜杠", text)

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _violation(
            Reason.SCHEME_NOT_ALLOWED, f"只允许 http/https，收到 {scheme or '-'}", text
        )
    if not parts.netloc:
        raise _violation(Reason.HOST_MISSING, "URL 缺少 host", text)
    if "@" in parts.netloc:
        raise _violation(Reason.USERINFO_NOT_ALLOWED, "URL 不得携带 userinfo", text)

    try:
        raw_host = parts.hostname
    except ValueError as exc:
        raise _violation(Reason.INVALID_URL, "host 无法解析", text) from exc
    if not raw_host:
        raise _violation(Reason.HOST_MISSING, "URL 缺少 host", text)
    if "%" in raw_host:
        raise _violation(
            Reason.ENCODED_HOST, "host 含百分号编码（``exa%6dple.com``）", text
        )

    try:
        host, host_ascii, is_ip = canonical_host(raw_host)
    except ValueError as exc:
        raise _violation(Reason.INVALID_URL, f"host 不合法：{exc}", text) from exc

    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise _violation(Reason.INVALID_URL, "端口不合法", text) from exc

    port = explicit_port if explicit_port is not None else DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        raise _violation(Reason.PORT_NOT_ALLOWED, f"端口越界：{port}", text)

    path = parts.path or "/"
    return Target(
        raw=text,
        scheme=scheme,
        host=host,
        host_ascii=host_ascii,
        port=port,
        explicit_port=explicit_port is not None,
        is_ip_literal=is_ip,
        path=path,
        query=parts.query,
    )


def host_variants(host: str) -> frozenset[str]:
    """同一 host 的所有等价写法（unicode / punycode / 尾点），用于名单匹配。"""

    variants = {host.lower().rstrip(".")}
    with contextlib.suppress(UnicodeError):
        variants.add(host.encode("idna").decode("ascii").lower().rstrip("."))
    return frozenset(item for item in variants if item)


def domain_matches(host: str, entry: str) -> bool:
    """``entry`` 命中 ``host`` 或其子域。

    ``example.com`` 命中 ``example.com`` / ``app.example.com``；
    ``*.example.com`` 只命中子域，不命中 apex —— 二者语义不同，测试要分别覆盖。
    """

    entry_norm = entry.strip().lower().rstrip(".")
    suffix_only = entry_norm.startswith("*.")
    if suffix_only:
        entry_norm = entry_norm[2:]
    if not entry_norm:
        return False
    entries = host_variants(entry_norm)
    for variant in host_variants(host):
        for candidate in entries:
            if variant == candidate and not suffix_only:
                return True
            if variant.endswith("." + candidate):
                return True
    return False


def redact(url: str) -> str:
    """脱敏：只留 scheme://host[:port]/path，丢掉 query/fragment/userinfo。"""

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}{parts.path or '/'}"
    except ValueError:
        return "<unparsable-url>"


def decode_for_log(url: str) -> str:
    """仅用于日志的最小解码，避免把 ``%2e`` 之类的编码形式写进证据时难以比对。"""

    return unquote(url)
