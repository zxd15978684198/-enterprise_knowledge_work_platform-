"""网络出口策略引擎（P0-T16）：域名/端口/协议、每跳 DNS 与 IP、预算、限流。

它同时实现 T15 在 ``browser_contracts.py`` 里预留的 ``NetworkPolicy`` 协议
（``check_navigation(workspace_id=..., url=...) -> PolicyDecision``），因此可以直接作为
``BrowserPort(network_policy=engine)`` 传入；更细的每跳/预算能力通过 ``open_guard()`` 使用。

职责边界（TDD05 §6）：
- **不**在浏览器 route hook 里做唯一拦截；真正的出口控制在连接层（``flow.RawGetter``）；
- 子资源、iframe、WebSocket、下载同样走 ``HopGuard.check()``（T16 的 probe 用它包住每次请求）；
- 一切判定失败即拒绝，绝不因为"没证据"而放行。
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Final
from uuid import UUID

from browser_contracts import PolicyDecision

from .addresses import normalize_address, require_public_address
from .dns import DnsResolver, RebindingGuard, SystemResolver
from .errors import PolicyViolation, Reason
from .limits import ConcurrencyGate, Metrics, TokenBucket
from .targets import ALLOWED_SCHEMES, Target, domain_matches, parse_target, redact

_LOOPBACK_LITERALS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """策略配置。默认值刻意保守：只允许 80/443，白名单为空时按"不限制域名"处理。"""

    allow_domains: frozenset[str] = frozenset()
    deny_domains: frozenset[str] = frozenset()
    allow_ports: frozenset[int] = frozenset({80, 443})
    allow_loopback_endpoints: frozenset[tuple[str, int]] = frozenset()
    max_redirects: int = 5
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_ms: int = 15_000
    per_domain_qps: float = 5.0
    per_domain_burst: int = 5
    max_concurrent_per_domain: int = 2

    def __post_init__(self) -> None:
        for port in self.allow_ports:
            if not 1 <= port <= 65535:
                raise ValueError(f"allow_ports 含越界端口 {port}")
        if not 0 <= self.max_redirects <= 20:
            raise ValueError("max_redirects 必须在 [0, 20]")
        if self.max_response_bytes < 1 or self.max_total_ms < 1:
            raise ValueError("预算必须为正")
        if self.per_domain_qps <= 0 or self.per_domain_burst < 1:
            raise ValueError("限流参数必须为正")
        if self.max_concurrent_per_domain < 1:
            raise ValueError("并发上限必须 >= 1")
        for host, port in self.allow_loopback_endpoints:
            self._validate_loopback_endpoint(host, port)

    @staticmethod
    def _validate_loopback_endpoint(host: str, port: int) -> None:
        """放行清单只接受**回环字面量**：写错域名就等于开了后门，构造期直接失败。"""

        if not 1 <= port <= 65535:
            raise ValueError(f"allow_loopback_endpoints 端口越界：{port}")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError as exc:
            raise ValueError(f"allow_loopback_endpoints 只接受 IP 字面量：{host!r}") from exc
        if str(address) not in _LOOPBACK_LITERALS:
            raise ValueError(
                f"allow_loopback_endpoints 只允许回环地址 {sorted(_LOOPBACK_LITERALS)}，"
                f"收到 {host!r}"
            )


@dataclass(frozen=True, slots=True)
class HopDecision:
    """一次被批准的跳转；``connect_ip`` 是**唯一**允许连接的目的地址（防 rebinding）。"""

    url: str
    scheme: str
    host: str
    port: int
    connect_ip: str
    addresses: tuple[str, ...]
    hop_index: int
    trusted_endpoint: bool = False

    @property
    def host_header(self) -> str:
        default = 80 if self.scheme == "http" else 443
        return self.host if self.port == default else f"{self.host}:{self.port}"

    @property
    def request_target(self) -> str:
        parsed = parse_target(self.url)
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.path}{query}"


@dataclass
class _Lease:
    leases: dict[str, int] = field(default_factory=dict)


class HopGuard:
    """一次导航的预算与限流上下文：每跳都调用 ``check()``，重定向必须 ``note_redirect()``。"""

    def __init__(
        self,
        engine: NetworkPolicyEngine,
        workspace_id: UUID,
        *,
        max_redirects: int,
        max_response_bytes: int,
        total_budget_ms: int,
    ) -> None:
        self._engine = engine
        self._workspace_id = workspace_id
        self._max_redirects = max_redirects
        self._max_bytes = max_response_bytes
        self._deadline = time.monotonic() + total_budget_ms / 1000.0
        self._redirects = 0
        self._bytes = 0
        self._hop = 0
        self._lease = _Lease()
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> HopGuard:
        return self

    @property
    def engine(self) -> NetworkPolicyEngine:
        return self._engine

    @property
    def workspace_id(self) -> UUID:
        return self._workspace_id

    @property
    def max_response_bytes(self) -> int:
        return self._max_bytes

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for host, count in self._lease.leases.items():
            for _ in range(count):
                self._engine.concurrency.release(host)
        self._lease.leases.clear()

    # -- 预算 -------------------------------------------------------------
    @property
    def remaining_ms(self) -> float:
        return max(0.0, (self._deadline - time.monotonic()) * 1000.0)

    @property
    def bytes_read(self) -> int:
        return self._bytes

    @property
    def redirects(self) -> int:
        return self._redirects

    def check_deadline(self) -> None:
        if self.remaining_ms <= 0:
            self._engine.metrics.incr(f"deny.{Reason.DEADLINE_EXCEEDED}")
            raise PolicyViolation(
                Reason.DEADLINE_EXCEEDED,
                f"总时长预算已耗尽（{self._redirects} 跳，{self._bytes} 字节）",
                data={"redirects": self._redirects, "bytes_read": self._bytes},
            )

    def note_bytes(self, amount: int) -> None:
        self._bytes += amount
        if self._bytes > self._max_bytes:
            self._engine.metrics.incr(f"deny.{Reason.BODY_TOO_LARGE}")
            raise PolicyViolation(
                Reason.BODY_TOO_LARGE,
                f"响应体超过上限 {self._max_bytes} 字节（已读 {self._bytes}）",
                data={"limit": self._max_bytes, "bytes_read": self._bytes},
            )

    def note_redirect(self, *, from_url: str, to_url: str) -> None:
        self._redirects += 1
        self._engine.metrics.incr("redirect.hop")
        if self._redirects > self._max_redirects:
            self._engine.metrics.incr(f"deny.{Reason.TOO_MANY_REDIRECTS}")
            raise PolicyViolation(
                Reason.TOO_MANY_REDIRECTS,
                f"重定向次数超过上限 {self._max_redirects}",
                url=from_url,
                data={"limit": self._max_redirects, "next": redact(to_url)},
            )

    # -- 每跳授权 ---------------------------------------------------------
    def check(self, url: str) -> HopDecision:
        self.check_deadline()
        decision = self._engine.authorize(url, workspace_id=self._workspace_id)
        if decision.trusted_endpoint:
            self._hop += 1
            self._engine.metrics.incr("allow.hop")
            self._engine.metrics.incr("bypass.trusted_endpoint")
            return replace(decision, hop_index=self._hop)

        key = decision.host
        bucket = self._engine.bucket_for(key)
        if not bucket.try_acquire():
            self._engine.metrics.incr(f"deny.{Reason.RATE_LIMITED}")
            raise PolicyViolation(
                Reason.RATE_LIMITED,
                f"{key} 超过 {self._engine.config.per_domain_qps}/s 的速率上限",
                url=url,
                data={"host": key},
            )
        if not self._engine.concurrency.try_acquire(key):
            self._engine.metrics.incr(f"deny.{Reason.CONCURRENCY_LIMITED}")
            raise PolicyViolation(
                Reason.CONCURRENCY_LIMITED,
                f"{key} 并发连接数超过上限 "
                f"{self._engine.config.max_concurrent_per_domain}",
                url=url,
                data={"host": key},
            )
        self._lease.leases[key] = self._lease.leases.get(key, 0) + 1
        self._hop += 1
        self._engine.metrics.incr("allow.hop")
        return HopDecision(
            url=decision.url,
            scheme=decision.scheme,
            host=decision.host,
            port=decision.port,
            connect_ip=decision.connect_ip,
            addresses=decision.addresses,
            hop_index=self._hop,
            trusted_endpoint=False,
        )


class NetworkPolicyEngine:
    """策略入口。同一个实例可被多个会话共享；每次导航用独立 ``HopGuard`` 隔离预算。"""

    def __init__(
        self,
        config: PolicyConfig | None = None,
        *,
        resolver: DnsResolver | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or PolicyConfig()
        self._metrics = Metrics()
        self._clock = clock or time.monotonic
        self._rebinding = RebindingGuard(resolver or SystemResolver())
        self._buckets: dict[str, TokenBucket] = {}
        self._concurrency = ConcurrencyGate(self._config.max_concurrent_per_domain)

    # -- 只读属性 ---------------------------------------------------------
    @property
    def config(self) -> PolicyConfig:
        return self._config

    @property
    def metrics(self) -> Metrics:
        return self._metrics

    @property
    def concurrency(self) -> ConcurrencyGate:
        return self._concurrency

    def bucket_for(self, host: str) -> TokenBucket:
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = TokenBucket(
                rate=self._config.per_domain_qps,
                burst=self._config.per_domain_burst,
                clock=self._clock,
            )
            self._buckets[host] = bucket
        return bucket

    def snapshot(self) -> dict[str, int]:
        return self._metrics.snapshot()

    # -- T15 NetworkPolicy 协议 -------------------------------------------
    def check_navigation(self, *, workspace_id: UUID, url: str) -> PolicyDecision:
        """T15 预留在 ``BrowserPort`` 里的唯一入口；这里只做无副作用的静态+解析判定。"""

        try:
            self.authorize(url, workspace_id=workspace_id)
        except PolicyViolation as violation:
            return PolicyDecision(
                allowed=False,
                reason=f"{violation.reason}: {violation.message}",
            )
        return PolicyDecision(allowed=True)

    # -- 单次授权（无预算/限流；由 HopGuard 复用） --------------------------
    def authorize(self, url: str, *, workspace_id: UUID) -> HopDecision:
        target = parse_target(url)
        self._check_scheme(target)
        self._check_domain(target)
        trusted = self._is_trusted_endpoint(target)
        if not trusted:
            self._check_port(target)

        if trusted:
            self._metrics.incr("allow.trusted_endpoint")
            self._metrics.incr("allow.authorize")
            return HopDecision(
                url=url,
                scheme=target.scheme,
                host=target.host,
                port=target.port,
                connect_ip=target.host,
                addresses=(target.host,),
                hop_index=0,
                trusted_endpoint=True,
            )

        addresses = self._resolve_and_classify(target, url=url)
        self._metrics.incr("allow.authorize")
        return HopDecision(
            url=url,
            scheme=target.scheme,
            host=target.host,
            port=target.port,
            connect_ip=addresses[0],
            addresses=addresses,
            hop_index=0,
            trusted_endpoint=False,
        )

    # -- 每跳组合 ---------------------------------------------------------
    def open_guard(
        self,
        *,
        workspace_id: UUID,
        max_redirects: int | None = None,
        max_response_bytes: int | None = None,
        total_budget_ms: int | None = None,
    ) -> HopGuard:
        return HopGuard(
            self,
            workspace_id,
            max_redirects=(
                self._config.max_redirects if max_redirects is None else max_redirects
            ),
            max_response_bytes=(
                self._config.max_response_bytes
                if max_response_bytes is None
                else max_response_bytes
            ),
            total_budget_ms=(
                self._config.max_total_ms if total_budget_ms is None else total_budget_ms
            ),
        )

    # -- 分步判定 ---------------------------------------------------------
    def _check_scheme(self, target: Target) -> None:
        if target.scheme not in ALLOWED_SCHEMES:
            self._deny(Reason.SCHEME_NOT_ALLOWED, f"协议 {target.scheme} 不允许", target.raw)

    def _check_domain(self, target: Target) -> None:
        for entry in sorted(self._config.deny_domains):
            if domain_matches(target.host, entry):
                self._deny(
                    Reason.DOMAIN_DENIED,
                    f"域名命中 denylist 条目 {entry!r}",
                    target.raw,
                    data={"host": target.host, "entry": entry},
                )
        if self._config.allow_domains and not any(
            domain_matches(target.host, entry)
            for entry in sorted(self._config.allow_domains)
        ):
            self._deny(
                Reason.DOMAIN_NOT_ALLOWED,
                f"域名 {target.host!r} 不在 allowlist 内",
                target.raw,
                data={"host": target.host},
            )

    def _check_port(self, target: Target) -> None:
        if target.port not in self._config.allow_ports:
            self._deny(
                Reason.PORT_NOT_ALLOWED,
                f"端口 {target.port} 不在允许列表 {sorted(self._config.allow_ports)}",
                target.raw,
                data={"port": target.port},
            )

    def _is_trusted_endpoint(self, target: Target) -> bool:
        if target.is_ip_literal:
            return (target.host, target.port) in self._config.allow_loopback_endpoints
        return False

    def _resolve_and_classify(self, target: Target, *, url: str) -> tuple[str, ...]:
        if target.is_ip_literal:
            addresses = (target.host,)
        else:
            addresses = self._rebinding.resolve_stable(
                target.host, port=target.port, url=url
            )
            self._metrics.incr("dns.resolve")
        classified: list[str] = []
        for text in addresses:
            address = normalize_address(text)
            try:
                require_public_address(address, host=target.host, url=url)
            except PolicyViolation:
                self._metrics.incr(f"deny.{Reason.ADDRESS_BLOCKED}")
                raise
            classified.append(str(address))
        # 任一记录落在禁区就整体拒绝：多 A 记录时只挑"看起来干净"的那条等于没拦。
        return tuple(classified)

    def _deny(
        self,
        reason: Reason,
        message: str,
        url: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        self._metrics.incr(f"deny.{reason}")
        raise PolicyViolation(reason, message, url=redact(url), data=data)
