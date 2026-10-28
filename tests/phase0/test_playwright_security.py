"""P0-T16 Playwright 适配与 SSRF/网络策略的真实测试。

验收依据（``docs/poc/03_Phase0_Task_Backlog.md`` P0-T16 + TDD05 §6）：

* 纯 Python 可测的出口策略：每跳（含重定向链）校验私网/回环/链路本地/IPv6 ULA、
  IP 字面量绕过、DNS rebinding、协议与端口白名单、响应体上限、总时长预算、重定向次数；
* 域名 allowlist/denylist，大小写、尾点、IDN/punycode、userinfo 等绕过形式都有负例；
* T15 ``browser_contracts.BrowserPort`` 的 ``navigate_back/forward/reload`` 也要过策略；
* 真浏览器不可用时记 **NOT_RUN** 并给出原因与替代验证，**不**用 skip/xfail 冒充通过；
* 每域 QPS 与并发上限可观测，并有超限拒绝用例。

网络：唯一本地服务监听 ``127.0.0.1:5610``，teardown 断言端口已释放（无监听残留）。
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
import urllib.parse
from collections.abc import Iterator
from typing import Any, Final
from uuid import UUID

import pytest

from browser_contracts import (
    BrowserAction,
    BrowserErrorCode,
    BrowserPort,
    BrowserResult,
    BrowserSession,
    BrowserSessionState,
    RPCError,
    create_browser_session,
    negotiate_capabilities,
)
from network_policy import (
    ConcurrencyGate,
    NetworkPolicyEngine,
    PolicyConfig,
    PolicyViolation,
    Reason,
    SafeFetcher,
    StaticResolver,
    address_reason,
    domain_matches,
    normalize_address,
    parse_target,
)
from playwright_probe import (
    PageContext,
    PlaywrightNavigator,
    PlaywrightUnavailable,
    PolicyGuardedBrowserPort,
    detect_playwright,
    substitute_http_probe,
)

PORT: Final = 5610
HOST: Final = "127.0.0.1"
BASE_URL: Final = f"http://{HOST}:{PORT}"
WORKSPACE: Final = UUID("11111111-1111-4111-8111-111111111111")
PRINCIPAL: Final = UUID("22222222-2222-4222-8222-222222222222")
PUBLIC_IP: Final = "93.184.216.34"


# ---------------------------------------------------------------------------
# 本地 HTTP 服务（唯一监听端口 5610）
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # 静默：测试输出只留断言
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if path == "/ok":
            self._send(200, b"hello p0-t16")
            return
        if path == "/big":
            size = int(query.get("n", ["524288"])[0])
            self._send(200, b"x" * size)
            return
        if path == "/slow":
            delay_ms = int(query.get("ms", ["1500"])[0])
            time.sleep(delay_ms / 1000.0)
            self._send(200, b"slow")
            return
        if path.startswith("/hop/"):
            remaining = int(path.rsplit("/", 1)[1])
            target = "/ok" if remaining <= 0 else f"/hop/{remaining - 1}"
            self._send(302, b"", {"Location": target})
            return
        if path == "/to-private":
            self._send(302, b"", {"Location": "http://10.9.9.9/ok"})
            return
        if path == "/to-loopback":
            self._send(302, b"", {"Location": f"http://127.0.0.1:{PORT}/ok"})
            return
        if path == "/to-file":
            self._send(302, b"", {"Location": "file:///etc/passwd"})
            return
        if path == "/loop":
            self._send(302, b"", {"Location": "/loop"})
            return
        self._send(404, b"not found")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _port_open(host: str = HOST, port: int = PORT, *, timeout: float = 0.2) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


@pytest.fixture()
def local_server() -> Iterator[str]:
    """起 5610 上的测试服务；teardown 断言确实没有监听残留。"""

    try:
        server = _Server((HOST, PORT), _Handler)
    except OSError as exc:  # 端口被占不是可跳过项：直接暴露环境冲突
        raise AssertionError(
            f"{HOST}:{PORT} 无法监听（{exc}）；本任务固定只用 5610，请先释放该端口"
        ) from exc
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        assert _port_open(), "测试服务启动后端口未监听"
        yield BASE_URL
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not _port_open(), f"{HOST}:{PORT} 在 teardown 后仍有监听残留"


# ---------------------------------------------------------------------------
# 解析器与引擎
# ---------------------------------------------------------------------------
def _static_resolver() -> StaticResolver:
    return StaticResolver(
        {
            "public.test": [PUBLIC_IP],
            "app.public.test": [PUBLIC_IP],
            "evil.test": ["127.0.0.1"],
            "internal.test": ["10.1.2.3"],
            "localhost": ["127.0.0.1"],
            "example.com": [PUBLIC_IP],
            "münchen.example": [PUBLIC_IP],
            "xn--mnchen-3ya.example": [PUBLIC_IP],
        }
    )


class FlakyResolver:
    """第一次返回公网、第二次返回回环：模拟 DNS rebinding 的时间差。"""

    def __init__(self, first: list[str], second: list[str]) -> None:
        self._answers = [first, second]
        self.calls = 0

    def resolve(self, host: str, *, port: int) -> tuple[str, ...]:
        index = 0 if self.calls == 0 else 1
        self.calls += 1
        return tuple(self._answers[index])


def strict_engine(**overrides: Any) -> NetworkPolicyEngine:
    """默认引擎：无任何放行例外。"""

    return NetworkPolicyEngine(PolicyConfig(**overrides), resolver=_static_resolver())


def local_engine(**overrides: Any) -> NetworkPolicyEngine:
    """只为了能连本地 5610 的测试引擎；放行项本身有独立用例验证不可滥用。"""

    config = PolicyConfig(
        allow_loopback_endpoints=frozenset({(HOST, PORT)}), **overrides
    )
    return NetworkPolicyEngine(config, resolver=_static_resolver())


# ---------------------------------------------------------------------------
# 1. URL / host 归一化
# ---------------------------------------------------------------------------
def test_parse_target_normalizes_case_and_trailing_dot() -> None:
    target = parse_target("HTTP://Example.COM./path?q=1")
    assert target.scheme == "http"
    assert target.host == "example.com"
    assert target.path == "/path"
    assert target.query == "q=1"


def test_parse_target_punycode_and_unicode_are_same_host() -> None:
    unicode_target = parse_target("http://münchen.example/")
    ascii_target = parse_target("http://xn--mnchen-3ya.example/")
    assert unicode_target.host_ascii == "xn--mnchen-3ya.example"
    assert unicode_target.host_ascii == ascii_target.host_ascii


def test_parse_target_default_ports() -> None:
    assert parse_target("http://example.com/").port == 80
    assert parse_target("https://example.com/").port == 443
    assert parse_target("http://example.com:8443/").port == 8443


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://user:pass@example.com/", Reason.USERINFO_NOT_ALLOWED),
        ("http://user@example.com/", Reason.USERINFO_NOT_ALLOWED),
        ("http://evil.com\\@good.com/", Reason.BACKSLASH_IN_URL),
        ("http://exa%6dple.com/", Reason.ENCODED_HOST),
        ("ftp://example.com/", Reason.SCHEME_NOT_ALLOWED),
        ("file:///etc/passwd", Reason.SCHEME_NOT_ALLOWED),
        ("javascript:alert(1)", Reason.SCHEME_NOT_ALLOWED),
        ("http://exa\nmple.com/", Reason.CONTROL_CHAR_IN_URL),
        ("http:///path-without-host", Reason.HOST_MISSING),
    ],
)
def test_parse_target_rejects_bypass_forms(url: str, reason: Reason) -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        parse_target(url)
    assert excinfo.value.reason is reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("10.1", "10.0.0.1"),
    ],
)
def test_loose_ipv4_forms_are_canonicalized(raw: str, expected: str) -> None:
    assert parse_target(f"http://{raw}/").host == expected


# ---------------------------------------------------------------------------
# 2. 地址分类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (PUBLIC_IP, None),
        ("2606:2800:220:1::1", None),
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("172.16.5.4", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "cloud_metadata"),
        ("100.100.100.200", "cloud_metadata"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("198.18.0.1", "private"),
        ("::1", "loopback"),
        ("fd00::1", "private"),
        ("fe80::1", "link_local"),
        ("::ffff:127.0.0.1", "loopback"),
        ("2002:7f00:1::", "6to4:loopback"),
        ("2001::1", "teredo"),
    ],
)
def test_address_classification(text: str, expected: str | None) -> None:
    assert address_reason(normalize_address(text)) == expected


# ---------------------------------------------------------------------------
# 3. 引擎级 SSRF 向量（≥20 条）
# ---------------------------------------------------------------------------
SSRF_VECTORS: tuple[tuple[str, Reason], ...] = (
    ("http://127.0.0.1/", Reason.ADDRESS_BLOCKED),
    ("http://127.1/", Reason.ADDRESS_BLOCKED),
    ("http://0x7f000001/", Reason.ADDRESS_BLOCKED),
    ("http://2130706433/", Reason.ADDRESS_BLOCKED),
    ("http://0.0.0.0/", Reason.ADDRESS_BLOCKED),
    ("http://10.0.0.1/", Reason.ADDRESS_BLOCKED),
    ("http://172.16.5.4/", Reason.ADDRESS_BLOCKED),
    ("http://192.168.0.1/", Reason.ADDRESS_BLOCKED),
    ("http://169.254.169.254/latest/meta-data", Reason.ADDRESS_BLOCKED),
    ("http://100.100.100.200/", Reason.ADDRESS_BLOCKED),
    ("http://198.18.0.1/", Reason.ADDRESS_BLOCKED),
    ("http://224.0.0.1/", Reason.ADDRESS_BLOCKED),
    ("http://[::1]/", Reason.ADDRESS_BLOCKED),
    ("http://[fd00::1]/", Reason.ADDRESS_BLOCKED),
    ("http://[fe80::1]/", Reason.ADDRESS_BLOCKED),
    ("http://[::ffff:127.0.0.1]/", Reason.ADDRESS_BLOCKED),
    ("http://[2002:7f00:1::]/", Reason.ADDRESS_BLOCKED),
    ("http://internal.test/", Reason.ADDRESS_BLOCKED),
    ("http://evil.test/", Reason.ADDRESS_BLOCKED),
    ("http://localhost/", Reason.ADDRESS_BLOCKED),
    ("http://user@example.com/", Reason.USERINFO_NOT_ALLOWED),
    ("ftp://example.com/x", Reason.SCHEME_NOT_ALLOWED),
    ("http://exa%6dple.com/", Reason.ENCODED_HOST),
    ("http://evil.com\\@example.com/", Reason.BACKSLASH_IN_URL),
    ("http://example.com:8080/", Reason.PORT_NOT_ALLOWED),
    ("http://unknown.test/", Reason.DNS_FAILURE),
)


@pytest.mark.parametrize(("url", "reason"), SSRF_VECTORS)
def test_engine_blocks_ssrf_vector(url: str, reason: Reason) -> None:
    engine = strict_engine()
    with pytest.raises(PolicyViolation) as excinfo:
        engine.authorize(url, workspace_id=WORKSPACE)
    assert excinfo.value.reason is reason, f"{url} 应以 {reason} 拒绝，实际 {excinfo.value}"
    assert excinfo.value.code == "SSRF_BLOCKED"


def test_ssrf_vector_count_is_at_least_twenty() -> None:
    assert len(SSRF_VECTORS) >= 20


def test_public_host_is_allowed_with_pinned_ip() -> None:
    engine = strict_engine()
    decision = engine.authorize("http://public.test/ok", workspace_id=WORKSPACE)
    assert decision.connect_ip == PUBLIC_IP
    assert decision.trusted_endpoint is False


# ---------------------------------------------------------------------------
# 4. DNS rebinding 与解析失败
# ---------------------------------------------------------------------------
def test_engine_rejects_dns_rebinding() -> None:
    engine = NetworkPolicyEngine(
        PolicyConfig(), resolver=FlakyResolver([PUBLIC_IP], ["127.0.0.1"])
    )
    with pytest.raises(PolicyViolation) as excinfo:
        engine.authorize("http://rebind.test/", workspace_id=WORKSPACE)
    assert excinfo.value.reason is Reason.DNS_REBINDING
    assert excinfo.value.data["first"] == [PUBLIC_IP]
    assert excinfo.value.data["second"] == ["127.0.0.1"]


def test_engine_denies_when_any_record_is_private() -> None:
    engine = NetworkPolicyEngine(
        PolicyConfig(),
        resolver=StaticResolver({"mixed.test": [PUBLIC_IP, "10.0.0.7"]}),
    )
    with pytest.raises(PolicyViolation) as excinfo:
        engine.authorize("http://mixed.test/", workspace_id=WORKSPACE)
    assert excinfo.value.reason is Reason.ADDRESS_BLOCKED
    assert excinfo.value.data["ip"] == "10.0.0.7"


def test_engine_fails_closed_on_dns_failure() -> None:
    engine = NetworkPolicyEngine(
        PolicyConfig(), resolver=StaticResolver({})
    )
    with pytest.raises(PolicyViolation) as excinfo:
        engine.authorize("http://missing.test/", workspace_id=WORKSPACE)
    assert excinfo.value.reason is Reason.DNS_FAILURE


# ---------------------------------------------------------------------------
# 5. 域名 allowlist / denylist
# ---------------------------------------------------------------------------
def test_allowlist_matches_apex_and_subdomain() -> None:
    engine = strict_engine(allow_domains=frozenset({"public.test"}))
    assert engine.check_navigation(
        workspace_id=WORKSPACE, url="http://public.test/"
    ).allowed
    assert engine.check_navigation(
        workspace_id=WORKSPACE, url="http://app.public.test/"
    ).allowed
    denied = engine.check_navigation(workspace_id=WORKSPACE, url="http://example.com/")
    assert denied.allowed is False
    assert denied.reason.startswith(str(Reason.DOMAIN_NOT_ALLOWED))


def test_allowlist_wildcard_excludes_apex() -> None:
    assert domain_matches("app.public.test", "*.public.test") is True
    assert domain_matches("public.test", "*.public.test") is False
    assert domain_matches("public.test", "public.test") is True


def test_allowlist_ignores_case_and_trailing_dot() -> None:
    engine = strict_engine(allow_domains=frozenset({"Example.COM."}))
    assert engine.check_navigation(workspace_id=WORKSPACE, url="http://example.com/").allowed
    assert engine.check_navigation(workspace_id=WORKSPACE, url="http://EXAMPLE.com./").allowed


def test_allowlist_handles_punycode_equivalence() -> None:
    engine = strict_engine(allow_domains=frozenset({"xn--mnchen-3ya.example"}))
    assert engine.check_navigation(
        workspace_id=WORKSPACE, url="http://münchen.example/"
    ).allowed
    assert engine.check_navigation(
        workspace_id=WORKSPACE, url="http://xn--mnchen-3ya.example/"
    ).allowed


def test_denylist_wins_over_allowlist() -> None:
    engine = strict_engine(
        allow_domains=frozenset({"public.test"}),
        deny_domains=frozenset({"secret.public.test"}),
    )
    assert engine.check_navigation(workspace_id=WORKSPACE, url="http://public.test/").allowed
    denied = engine.check_navigation(
        workspace_id=WORKSPACE, url="http://secret.public.test/"
    )
    assert denied.allowed is False
    assert Reason.DOMAIN_DENIED in denied.reason


def test_denylist_matches_subdomains_of_entry() -> None:
    engine = strict_engine(deny_domains=frozenset({"public.test"}))
    denied = engine.check_navigation(workspace_id=WORKSPACE, url="http://app.public.test/")
    assert denied.allowed is False


@pytest.mark.parametrize(
    "url",
    [
        "http://PUBLIC.TEST/",
        "http://public.test./",
        "http://public.test:80/",
    ],
)
def test_denylist_cannot_be_bypassed_by_case_or_trailing_dot(url: str) -> None:
    engine = strict_engine(deny_domains=frozenset({"public.test"}))
    denied = engine.check_navigation(workspace_id=WORKSPACE, url=url)
    assert denied.allowed is False, f"{url} 绕过了 denylist"
    assert Reason.DOMAIN_DENIED in denied.reason or Reason.PORT_NOT_ALLOWED in denied.reason


def test_denylist_cannot_be_bypassed_by_idn_or_punycode() -> None:
    engine = strict_engine(deny_domains=frozenset({"xn--mnchen-3ya.example"}))
    assert engine.check_navigation(workspace_id=WORKSPACE, url="http://münchen.example/").allowed is False
    engine2 = strict_engine(deny_domains=frozenset({"münchen.example"}))
    assert engine2.check_navigation(
        workspace_id=WORKSPACE, url="http://xn--mnchen-3ya.example/"
    ).allowed is False


def test_fullwidth_idn_is_normalized_before_matching() -> None:
    # 全角字母经 NFKC 归一化后与 ASCII 同名：既不能绕过 denylist，也不会被误判成另一个域。
    engine = strict_engine(deny_domains=frozenset({"example.com"}))
    denied = engine.check_navigation(workspace_id=WORKSPACE, url="http://ｅｘａｍｐｌｅ.com/")
    assert denied.allowed is False


def test_userinfo_credentials_are_not_echoed_in_violation() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        parse_target("http://user:s3cr3t@example.com/")
    assert excinfo.value.reason is Reason.USERINFO_NOT_ALLOWED
    assert "s3cr3t" not in str(excinfo.value)
    assert excinfo.value.url is not None and "s3cr3t" not in excinfo.value.url


# ---------------------------------------------------------------------------
# 6. 每跳重定向（真实本地服务）
# ---------------------------------------------------------------------------
def test_fetch_follows_allowed_redirect_chain(local_server: str) -> None:
    engine = local_engine()
    result = substitute_http_probe(
        f"{local_server}/hop/1", workspace_id=WORKSPACE, engine=engine
    )
    assert result.status == 200
    assert result.body == b"hello p0-t16"
    assert result.final_url.endswith("/ok")
    assert result.hops == 3
    assert engine.snapshot()["redirect.hop"] == 2


def test_fetch_blocks_redirect_to_private_ip(local_server: str) -> None:
    engine = local_engine()
    with pytest.raises(PolicyViolation) as excinfo:
        substitute_http_probe(
            f"{local_server}/to-private", workspace_id=WORKSPACE, engine=engine
        )
    assert excinfo.value.reason is Reason.ADDRESS_BLOCKED
    assert excinfo.value.data["ip"] == "10.9.9.9"
    # 第一跳被放行、第二跳被拒：证明校验发生在**每一跳**而不是只在入口。
    assert engine.snapshot()["allow.hop"] == 1


def test_fetch_blocks_redirect_to_disallowed_scheme(local_server: str) -> None:
    engine = local_engine()
    with pytest.raises(PolicyViolation) as excinfo:
        substitute_http_probe(
            f"{local_server}/to-file", workspace_id=WORKSPACE, engine=engine
        )
    assert excinfo.value.reason is Reason.SCHEME_NOT_ALLOWED


def test_fetch_enforces_redirect_limit(local_server: str) -> None:
    engine = local_engine()
    with pytest.raises(PolicyViolation) as excinfo:
        SafeFetcher(engine).fetch(
            f"{local_server}/loop", workspace_id=WORKSPACE, max_redirects=2
        )
    assert excinfo.value.reason is Reason.TOO_MANY_REDIRECTS
    assert excinfo.value.data["limit"] == 2


# ---------------------------------------------------------------------------
# 7. 响应体上限与总时长预算
# ---------------------------------------------------------------------------
def test_fetch_enforces_response_size_limit(local_server: str) -> None:
    engine = local_engine()
    with pytest.raises(PolicyViolation) as excinfo:
        SafeFetcher(engine).fetch(
            f"{local_server}/big?n=524288",
            workspace_id=WORKSPACE,
            max_response_bytes=32 * 1024,
        )
    assert excinfo.value.reason is Reason.BODY_TOO_LARGE
    read = excinfo.value.data["bytes_read"]
    assert 32 * 1024 < read <= 32 * 1024 + 64 * 1024, "超限后必须尽快停止读取"


def test_fetch_enforces_total_time_budget(local_server: str) -> None:
    engine = local_engine()
    started = time.monotonic()
    with pytest.raises(PolicyViolation) as excinfo:
        SafeFetcher(engine).fetch(
            f"{local_server}/slow?ms=1500",
            workspace_id=WORKSPACE,
            total_budget_ms=300,
        )
    elapsed = time.monotonic() - started
    assert excinfo.value.reason is Reason.DEADLINE_EXCEEDED
    assert elapsed < 1.4, f"预算 300ms 但耗时 {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 8. 限流与并发（可观测计数）
# ---------------------------------------------------------------------------
def test_rate_limit_denies_burst_and_counts() -> None:
    engine = strict_engine(per_domain_qps=0.001, per_domain_burst=1)
    with engine.open_guard(workspace_id=WORKSPACE) as guard:
        guard.check("http://public.test/a")
        with pytest.raises(PolicyViolation) as excinfo:
            guard.check("http://public.test/b")
    assert excinfo.value.reason is Reason.RATE_LIMITED
    snapshot = engine.snapshot()
    assert snapshot["deny.rate_limited"] == 1
    assert snapshot["allow.hop"] == 1


def test_concurrency_limit_denies_second_inflight() -> None:
    engine = strict_engine(max_concurrent_per_domain=1)
    first = engine.open_guard(workspace_id=WORKSPACE)
    try:
        first.check("http://public.test/a")
        with pytest.raises(PolicyViolation) as excinfo:
            second = engine.open_guard(workspace_id=WORKSPACE)
            try:
                second.check("http://public.test/b")
            finally:
                second.close()
    finally:
        first.close()
    assert excinfo.value.reason is Reason.CONCURRENCY_LIMITED
    assert engine.concurrency.active("public.test") == 0
    assert engine.snapshot()["deny.concurrency_limited"] == 1


def test_concurrency_gate_release_is_reentrant_safe() -> None:
    gate = ConcurrencyGate(limit=1)
    assert gate.try_acquire("a") is True
    assert gate.try_acquire("a") is False
    gate.release("a")
    gate.release("a")  # 多释放不得把计数压成负数
    assert gate.active("a") == 0
    assert gate.try_acquire("a") is True


def test_metrics_snapshot_is_observable() -> None:
    engine = strict_engine()
    with pytest.raises(PolicyViolation):
        engine.authorize("http://127.0.0.1/", workspace_id=WORKSPACE)
    engine.authorize("http://public.test/", workspace_id=WORKSPACE)
    snapshot = engine.snapshot()
    assert snapshot["deny.address_blocked"] == 1
    assert snapshot["allow.authorize"] == 1
    assert snapshot["dns.resolve"] == 1
    assert all(isinstance(value, int) for value in snapshot.values())


# ---------------------------------------------------------------------------
# 9. 端口：启动/释放与环境隔离
# ---------------------------------------------------------------------------
def test_local_port_is_released_after_stop() -> None:
    server = _Server((HOST, PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _port_open() is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert _port_open() is False, "stop 后 5610 不应仍有监听"


# ---------------------------------------------------------------------------
# 10. T15 接线：navigate_back / navigate_forward / reload 也必须过策略
# ---------------------------------------------------------------------------
class FakeTransport:
    """记录 dispatch；返回固定 final_url，供 PageContext 推进。"""

    def __init__(self, final_url: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.final_url = final_url

    def dispatch(
        self, method: str, params: dict[str, object], *, timeout_ms: int
    ) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if self.final_url is not None:
            return {"final_url": self.final_url}
        return {}


def _capabilities():
    return negotiate_capabilities(
        declared_actions=list(BrowserAction),
        ui_channel=False,
        protocol_version="p0-t15/1",
        tested_protocol_versions=frozenset({"p0-t15/1"}),
    )


def _session() -> BrowserSession:
    return create_browser_session(
        workspace_id=WORKSPACE,
        principal_id=PRINCIPAL,
        capabilities=_capabilities(),
        state=BrowserSessionState.CONNECTED,
        lease_epoch=1,
    )


def _guarded_port(
    engine: NetworkPolicyEngine,
    *,
    final_url: str | None = None,
    context: PageContext | None = None,
) -> tuple[PolicyGuardedBrowserPort, FakeTransport]:
    transport = FakeTransport(final_url=final_url)
    port = BrowserPort(transport, network_policy=engine)  # type: ignore[arg-type]
    guarded = PolicyGuardedBrowserPort(
        port, engine, workspace_id=WORKSPACE, context=context
    )
    return guarded, transport


def _execute(
    guarded: PolicyGuardedBrowserPort,
    action: BrowserAction,
    params: dict[str, object],
) -> BrowserResult | RPCError:
    return guarded.execute(
        _session(),
        action=action,
        params=params,
        expected_epoch=1,
        timeout_ms=5000,
    )


@pytest.mark.parametrize(
    "action", [BrowserAction.NAVIGATE_BACK, BrowserAction.NAVIGATE_FORWARD, BrowserAction.RELOAD]
)
def test_history_actions_require_page_context(action: BrowserAction) -> None:
    engine = strict_engine()
    guarded, transport = _guarded_port(engine)
    result = _execute(guarded, action, {})
    assert isinstance(result, RPCError)
    assert result.code is BrowserErrorCode.SSRF_BLOCKED
    assert result.data["policy_reason"] == str(Reason.NO_PAGE_CONTEXT)
    assert transport.calls == []


def test_poisoned_history_is_revalidated_on_back() -> None:
    """历史里被塞进私网 URL（模拟被投毒/伪造）时，back 仍必须被策略拦下。"""

    engine = strict_engine()
    context = PageContext(history=["http://127.0.0.1/admin", "http://public.test/a"], cursor=1)
    guarded, transport = _guarded_port(engine, context=context)
    result = _execute(guarded, BrowserAction.NAVIGATE_BACK, {})
    assert isinstance(result, RPCError)
    assert result.code is BrowserErrorCode.SSRF_BLOCKED
    assert result.data["policy_reason"] == str(Reason.ADDRESS_BLOCKED)
    assert transport.calls == []


@pytest.mark.parametrize(
    ("action", "cursor"),
    [
        (BrowserAction.NAVIGATE_BACK, 1),
        (BrowserAction.NAVIGATE_FORWARD, 0),
        (BrowserAction.RELOAD, 1),
    ],
)
def test_history_actions_dispatch_when_target_is_allowed(
    action: BrowserAction, cursor: int
) -> None:
    engine = strict_engine()
    context = PageContext(
        history=["http://public.test/a", "http://example.com/b"], cursor=cursor
    )
    guarded, transport = _guarded_port(engine, context=context)
    result = _execute(guarded, action, {})
    assert isinstance(result, BrowserResult)
    assert transport.calls, f"{action} 应被转发"
    assert transport.calls[0][0] in {"tool.navigate_back", "tool.navigate_forward", "tool.reload"}


def test_navigate_passes_contract_and_policy_gates() -> None:
    engine = strict_engine()
    guarded, transport = _guarded_port(engine, final_url="http://public.test/landed")
    result = _execute(guarded, BrowserAction.NAVIGATE, {"url": "http://public.test/start"})
    assert isinstance(result, BrowserResult)
    assert guarded.context.current_url == "http://public.test/landed"
    assert transport.calls[0][0] == "tool.navigate"


def test_navigate_to_private_target_is_blocked_before_transport() -> None:
    engine = strict_engine()
    guarded, transport = _guarded_port(engine)
    result = _execute(guarded, BrowserAction.NAVIGATE, {"url": "http://10.0.0.5/"})
    assert isinstance(result, RPCError)
    assert result.code is BrowserErrorCode.SSRF_BLOCKED
    assert transport.calls == []


def test_non_navigational_action_is_not_policy_gated() -> None:
    engine = strict_engine()
    guarded, transport = _guarded_port(engine)
    result = _execute(guarded, BrowserAction.OBSERVE, {})
    assert isinstance(result, BrowserResult)
    assert transport.calls[0][0] == "tool.observe"


# ---------------------------------------------------------------------------
# 11. 配置自证：放行项不得被滥用
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("host", "port"),
    [("example.com", 80), ("10.0.0.1", 8080), ("0.0.0.0", 80), ("::", 80)],
)
def test_trusted_loopback_endpoint_rejects_non_loopback(host: str, port: int) -> None:
    with pytest.raises(ValueError):
        PolicyConfig(allow_loopback_endpoints=frozenset({(host, port)}))


@pytest.mark.parametrize("bad_port", [0, 70000])
def test_policy_config_rejects_bad_ports(bad_port: int) -> None:
    with pytest.raises(ValueError):
        PolicyConfig(allow_ports=frozenset({bad_port}))


def test_trusted_endpoint_bypass_is_counted() -> None:
    engine = local_engine()
    with engine.open_guard(workspace_id=WORKSPACE) as guard:
        decision = guard.check(f"http://{HOST}:{PORT}/ok")
    assert decision.trusted_endpoint is True
    assert engine.snapshot()["bypass.trusted_endpoint"] == 1


# ---------------------------------------------------------------------------
# 12. Playwright：可用就跑真实用例，不可用就显式 NOT_RUN
# ---------------------------------------------------------------------------
PROBE_CASES: tuple[str, ...] = (
    f"{BASE_URL}/ok",
    f"{BASE_URL}/hop/1",
    f"{BASE_URL}/big?n=2048",
)


def _browser_record(availability) -> dict[str, object]:
    return {
        "task": "P0-T16",
        "component": "playwright",
        "status": "RUN" if availability.available else "NOT_RUN",
        "reason": availability.reason,
        "executable": availability.executable,
        "planned_cases": list(PROBE_CASES),
        "substitute_verification": [
            "network_policy: 26 条 SSRF/绕过向量 + 域名/端口/协议白名单",
            "flow: 每跳重定向校验、响应体上限、总时长预算",
            "limits: 每域 QPS 与并发上限（计数可观测）",
            "T15 接线: navigate_back/forward/reload 均过策略",
        ],
    }


def test_playwright_availability_is_recorded_not_faked() -> None:
    availability = detect_playwright()
    record = _browser_record(availability)
    print("P0-T16_BROWSER_EVIDENCE=" + json.dumps(record, ensure_ascii=False))
    if not availability.available:
        assert record["status"] == "NOT_RUN"
        assert isinstance(availability.reason, str) and availability.reason
        assert availability.executable is None
    else:
        assert record["status"] == "RUN"
        assert availability.executable


def test_navigator_fails_closed_when_browser_is_missing(local_server: str) -> None:
    availability = detect_playwright()
    engine = local_engine()
    if not availability.available:
        with pytest.raises(PlaywrightUnavailable) as excinfo:
            PlaywrightNavigator(engine)
        assert availability.reason in str(excinfo.value)
        return
    navigator = PlaywrightNavigator(engine)
    snapshot = navigator.navigate(f"{local_server}/ok", workspace_id=WORKSPACE)
    assert "hello p0-t16" in snapshot.text


def test_real_browser_cases_or_explicit_not_run(local_server: str) -> None:
    """可用时跑 ≥3 条真实导航；不可用时必须留下带原因的 NOT_RUN，而不是 skip。"""

    availability = detect_playwright()
    if not availability.available:
        record = _browser_record(availability)
        assert record["status"] == "NOT_RUN"
        assert len(record["planned_cases"]) >= 3
        assert record["substitute_verification"]
        return

    engine = local_engine()
    navigator = PlaywrightNavigator(engine)
    snapshots = [navigator.navigate(url, workspace_id=WORKSPACE) for url in PROBE_CASES]
    assert len(snapshots) == 3
    assert snapshots[0].title is not None
    assert snapshots[0].screenshot_bytes > 0
    assert all(snapshot.blocked_requests == () for snapshot in snapshots)


def test_probe_cases_use_only_local_server() -> None:
    assert all(case.startswith(BASE_URL) for case in PROBE_CASES)
    assert len(PROBE_CASES) >= 3
