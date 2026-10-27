"""地址分类：私网/回环/链路本地/ULA/组播/保留，以及 6to4 内嵌 IPv4 的再分类。

判定顺序刻意"宁可多拦"：先做显式危险网段，再回落到 ``is_global``；
任何一步判定失败都按 blocked 处理（fail closed）。
"""

from __future__ import annotations

import ipaddress

from .errors import PolicyViolation, Reason

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

CLOUD_METADATA: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure IMDS
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # 阿里云 metadata
        "fd00:ec2::254",  # AWS IPv6 IMDS
    }
)

_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_TEREDO = ipaddress.IPv6Network("2001::/32")


def normalize_address(text: str) -> Address:
    """解析地址；IPv4-mapped IPv6 统一折成立即比较的 IPv4。"""

    address: Address = ipaddress.ip_address(text.strip("[]"))
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def address_reason(address: Address) -> str | None:
    """返回拒绝分类名；``None`` 表示公网可达，可以连。"""

    if isinstance(address, ipaddress.IPv6Address):
        if address in _TEREDO:
            return "teredo"
        if address in _SIX_TO_FOUR:
            embedded = ipaddress.IPv4Address(bytes(address.packed[2:6]))
            inner = address_reason(embedded)
            return f"6to4:{inner}" if inner else None
        if address.ipv4_mapped is not None:
            return address_reason(address.ipv4_mapped) or "ipv4_mapped"

    if str(address) in CLOUD_METADATA:
        return "cloud_metadata"
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if address.is_private:
        return "private"
    if not address.is_global:
        return "not_global"
    return None


def require_public_address(address: Address, *, host: str, url: str) -> None:
    """地址不可公网直连时抛 ``ADDRESS_BLOCKED``；``data`` 里带分类，便于审计。"""

    classified = address_reason(address)
    if classified is None:
        return
    raise PolicyViolation(
        Reason.ADDRESS_BLOCKED,
        f"{host} 解析到不可直连地址 {address}（{classified}）",
        url=url,
        data={"ip": str(address), "class": classified, "host": host},
    )
