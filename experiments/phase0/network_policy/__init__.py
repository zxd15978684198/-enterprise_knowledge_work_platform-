"""P0-T16 网络出口策略包（纯 Python，可脱离浏览器单测）。

分层：

- ``targets``：URL → 归一化目标（大小写/尾点/IDN/punycode/整数 IP/反斜杠/控制字符）
- ``addresses``：IP 分类（私网/回环/链路本地/ULA/组播/保留/云 metadata/6to4 内嵌）
- ``dns``：可注入的解析器 + rebinding 防护（同一次授权内两次采样必须一致）
- ``limits``：令牌桶、并发闸门、计数器（QPS/并发上限可观测）
- ``engine``：``PolicyConfig`` / ``NetworkPolicyEngine`` / ``HopGuard``（同时实现 T15 的
  ``NetworkPolicy`` 协议）
- ``flow``：连接层受控取回（固定 IP、逐跳授权、重定向、字节与时长预算）

刻意不 import 任何浏览器库：Playwright 侧只在本包之外（``playwright_probe.py``）薄薄一层。
"""

from __future__ import annotations

from .addresses import CLOUD_METADATA, address_reason, normalize_address, require_public_address
from .dns import DnsError, DnsResolver, RebindingGuard, StaticResolver, SystemResolver
from .engine import HopDecision, HopGuard, NetworkPolicyEngine, PolicyConfig
from .errors import PolicyViolation, Reason
from .flow import FetchResult, RawGetter, SafeFetcher
from .limits import ConcurrencyGate, Metrics, TokenBucket
from .targets import Target, domain_matches, host_variants, parse_target, redact

__all__ = [
    "CLOUD_METADATA",
    "ConcurrencyGate",
    "DnsError",
    "DnsResolver",
    "FetchResult",
    "HopDecision",
    "HopGuard",
    "Metrics",
    "NetworkPolicyEngine",
    "PolicyConfig",
    "PolicyViolation",
    "RawGetter",
    "Reason",
    "RebindingGuard",
    "SafeFetcher",
    "StaticResolver",
    "SystemResolver",
    "Target",
    "TokenBucket",
    "address_reason",
    "domain_matches",
    "host_variants",
    "normalize_address",
    "parse_target",
    "redact",
    "require_public_address",
]
