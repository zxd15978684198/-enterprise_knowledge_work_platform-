"""DNS 解析与 rebinding 防护。

关键约束：**解析结果要用于"连哪个 IP"**，所以必须在授权时固定一次结果；
授权与连接之间再次解析、结果不同，就是 rebinding —— 这里用"同一次授权内两次采样不一致即拒绝"表达。
"""

from __future__ import annotations

import socket
from collections.abc import Iterable, Mapping
from typing import Protocol

from .errors import PolicyViolation, Reason


class DnsError(Exception):
    """解析失败（NXDOMAIN、超时、空结果）；调用方一律按拒绝处理。"""


class DnsResolver(Protocol):
    """``host -> IP 文本列表``。端口只用于 ``getaddrinfo`` 语义，不作为缓存键。"""

    def resolve(self, host: str, *, port: int) -> tuple[str, ...]: ...


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


class StaticResolver:
    """测试与离线场景用：固定映射，未登记的 host 视为 ``DnsError``。"""

    def __init__(self, mapping: Mapping[str, Iterable[str]]) -> None:
        self._mapping = {
            key.strip().lower().rstrip("."): tuple(value) for key, value in mapping.items()
        }
        self.calls: list[str] = []

    def resolve(self, host: str, *, port: int) -> tuple[str, ...]:
        key = host.strip().lower().rstrip(".")
        self.calls.append(key)
        try:
            answers = self._mapping[key]
        except KeyError as exc:
            raise DnsError(f"no static answer for {host!r}") from exc
        if not answers:
            raise DnsError(f"empty static answer for {host!r}")
        return _dedupe(answers)


class SystemResolver:
    """真实解析：只取 IPv4/IPv6 的 ``SOCK_STREAM`` 结果，忽略别名。"""

    def resolve(self, host: str, *, port: int) -> tuple[str, ...]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:  # noqa: BLE001 - 统一转成 DnsError，调用方按拒绝处理
            raise DnsError(f"getaddrinfo failed for {host!r}: {exc}") from exc
        answers = _dedupe(info[4][0] for info in infos if info[4])
        if not answers:
            raise DnsError(f"no address for {host!r}")
        return answers


class RebindingGuard:
    """固定解析：同一 host 连续采样，结果集合不一致即 ``DNS_REBINDING``。

    ``samples`` 默认 2：一次给授权用，一次给"授权到连接之间是否被换过"用；
    测试可通过自定义 resolver 让两次返回不同结果来复现 rebinding。
    """

    def __init__(self, resolver: DnsResolver, *, samples: int = 2) -> None:
        if samples < 2:
            raise ValueError("samples must be >= 2")
        self._resolver = resolver
        self._samples = samples

    @property
    def resolver(self) -> DnsResolver:
        return self._resolver

    def resolve_stable(self, host: str, *, port: int, url: str) -> tuple[str, ...]:
        samples: list[frozenset[str]] = []
        for _ in range(self._samples):
            try:
                answers = self._resolver.resolve(host, port=port)
            except DnsError as exc:
                raise PolicyViolation(
                    Reason.DNS_FAILURE, f"解析 {host!r} 失败：{exc}", url=url, data={"host": host}
                ) from exc
            if not answers:
                raise PolicyViolation(
                    Reason.DNS_FAILURE, f"{host!r} 解析为空", url=url, data={"host": host}
                )
            samples.append(frozenset(answers))

        baseline = samples[0]
        for sample in samples[1:]:
            if sample != baseline:
                raise PolicyViolation(
                    Reason.DNS_REBINDING,
                    f"{host!r} 两次解析结果不一致：{sorted(baseline)} vs {sorted(sample)}",
                    url=url,
                    data={
                        "host": host,
                        "first": sorted(baseline),
                        "second": sorted(sample),
                    },
                )
        return tuple(sorted(baseline))
