"""P0-T15 Browser port 的契约与校验层测试。

验收依据（Backlog P0-T15 卡 + ``docs/poc/evidence/browserskill_protocol_manifest.md``）：

- 单入口 ``execute(action, params, expected_epoch, timeout_ms) -> result | RPCError``，
  ``action`` 用枚举，未知动作拒绝；
- 能力协商 ``capabilities(actions, ui_channel, protocol_version)``，U1–U9 显式落成能力位；
- 错误映射与 T14 §6.6 一致，超时不得自动重试；
- Scope：``workspace_id`` / ``session_id`` 只能服务端注入，传入即拒绝；
- 网关硬约束：``request_help`` 拒绝 ``completion_criteria``、``wait_until`` 默认
  ``domcontentloaded``；
- 网络策略留前置拦截点（T16 落地），默认 fail-closed。

本文件是纯契约层用例：不启动浏览器、不连 daemon、不做网络请求。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from browser_contracts import (
    ACTION_SPECS,
    ADAPTER_API_VERSION,
    AUTOMATIC_RETRY,
    HUMAN_TIMEOUT_MAX_MS,
    MAX_TIMEOUT_MS,
    Approval,
    BrowserAction,
    BrowserCapabilityError,
    BrowserErrorCode,
    BrowserPort,
    BrowserResult,
    BrowserSession,
    BrowserSessionState,
    CapabilityId,
    CapabilityStatus,
    Capabilities,
    DenyAllNetworkPolicy,
    PolicyDecision,
    RPCError,
    SideEffect,
    TransportFailure,
    action_spec,
    compute_action_hash,
    create_browser_session,
    map_wire_error,
    negotiate_capabilities,
)

# ---------------------------------------------------------------------------
# 固定事实
# ---------------------------------------------------------------------------
WORKSPACE = UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORKSPACE = UUID("99999999-9999-4999-8999-999999999999")
PRINCIPAL = UUID("22222222-2222-4222-8222-222222222222")
TOOL_CALL = UUID("33333333-3333-4333-8333-333333333333")
PROTOCOL_VERSION = "bsk-wire/1"

ALL_ACTIONS: frozenset[BrowserAction] = frozenset(BrowserAction)
TESTED_ACTIONS: frozenset[BrowserAction] = ALL_ACTIONS - {BrowserAction.EVALUATE}

MANIFEST_ACTIONS: tuple[str, ...] = (
    "observe",
    "snapshot",
    "screenshot",
    "navigate",
    "navigate_back",
    "navigate_forward",
    "reload",
    "click",
    "fill",
    "press",
    "hover",
    "wheel",
    "scroll_to",
    "focus",
    "blur",
    "select",
    "tab_list",
    "tab_create",
    "tab_select",
    "tab_close",
    "tab_borrow",
    "tab_return",
    "get_html",
    "evaluate",
    "console",
    "network",
    "wait_for_navigation",
    "wait_ms",
    "window_resize",
    "emulate",
    "request_help",
)


# ---------------------------------------------------------------------------
# 测试替身（只实现窄接口，不触网）
# ---------------------------------------------------------------------------
class FakeTransport:
    """记录 dispatch 调用；可配置返回体或抛 ``TransportFailure``。"""

    def __init__(
        self,
        reply: Mapping[str, object] | None = None,
        failure: TransportFailure | None = None,
        crash: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.reply = dict(reply or {})
        self.failure = failure
        self.crash = crash

    def dispatch(
        self, method: str, params: Mapping[str, object], *, timeout_ms: int
    ) -> Mapping[str, object]:
        self.calls.append((method, dict(params), timeout_ms))
        if self.crash is not None:
            raise self.crash
        if self.failure is not None:
            raise self.failure
        return dict(self.reply)


@dataclass(frozen=True, slots=True)
class AllowAllPolicy:
    """T16 的真实策略占位：只用于证明 port 的拦截点可被替换，不是安全策略。"""

    def check_navigation(self, *, workspace_id: UUID, url: str) -> PolicyDecision:
        return PolicyDecision(allowed=True, reason="test-allow")


def make_caps(
    *,
    platform: str = "linux",
    declared: frozenset[BrowserAction] = TESTED_ACTIONS,
    protocol_version: str = PROTOCOL_VERSION,
    ui_channel: bool | None = True,
) -> Capabilities:
    return negotiate_capabilities(
        declared_actions=declared,
        ui_channel=ui_channel,
        protocol_version=protocol_version,
        backend="browserskill",
        backend_version="0.3.1",
        platform=platform,
        tested_protocol_versions=frozenset({PROTOCOL_VERSION}),
    )


def make_session(
    caps: Capabilities,
    *,
    state: BrowserSessionState = BrowserSessionState.CONNECTED,
    lease_epoch: int = 1,
    workspace_id: UUID = WORKSPACE,
) -> BrowserSession:
    return create_browser_session(
        workspace_id=workspace_id,
        principal_id=PRINCIPAL,
        capabilities=caps,
        state=state,
        lease_epoch=lease_epoch,
    )


def make_approval(
    session: BrowserSession,
    action: BrowserAction,
    params: Mapping[str, object],
    *,
    expires_in: float = 300.0,
    workspace_id: UUID | None = None,
    browser_session_id: UUID | None = None,
    override_hash: str | None = None,
    granted_action: BrowserAction | None = None,
) -> Approval:
    normalized: dict[str, object] = dict(params)
    if action_spec(action).navigational:
        normalized.setdefault("wait_until", "domcontentloaded")
    digest = override_hash or compute_action_hash(
        workspace_id=session.workspace_id,
        browser_session_id=session.browser_session_id,
        action=action,
        params=normalized,
    )
    return Approval(
        approval_id=uuid4(),
        workspace_id=workspace_id or session.workspace_id,
        browser_session_id=browser_session_id or session.browser_session_id,
        action=granted_action or action,
        params_hash=digest,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        granted_by=PRINCIPAL,
    )


def error_of(result: object) -> RPCError:
    assert isinstance(result, RPCError), f"期望 RPCError，实际收到 {result!r}"
    return result


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def caps() -> Capabilities:
    return make_caps()


@pytest.fixture()
def session(caps: Capabilities) -> BrowserSession:
    return make_session(caps)


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture()
def port(transport: FakeTransport) -> BrowserPort:
    """默认策略 fail-closed（DenyAllNetworkPolicy）：不做任何网络放行。"""

    return BrowserPort(transport)


@pytest.fixture()
def allow_port(transport: FakeTransport) -> BrowserPort:
    return BrowserPort(transport, network_policy=AllowAllPolicy())


# ---------------------------------------------------------------------------
# 1. 单入口契约与 31 动作白名单
# ---------------------------------------------------------------------------
def test_action_enum_matches_manifest_whitelist() -> None:
    assert tuple(action.value for action in BrowserAction) == MANIFEST_ACTIONS
    assert len(BrowserAction) == 31
    assert set(ACTION_SPECS) == set(BrowserAction)


def test_every_action_has_spec_with_evidence_and_params() -> None:
    for action, spec in ACTION_SPECS.items():
        assert spec.action is action
        assert spec.evidence, f"{action} 缺少证据出处"
        assert re.match(r"^(manager|daemon|schema)\.(go|ts)", spec.evidence), spec.evidence
        assert spec.params, f"{action} 未声明参数白名单（至少要为空集语义）"
        assert set(spec.required) <= set(spec.params)


def test_only_write_effects_require_approval() -> None:
    for action, spec in ACTION_SPECS.items():
        assert spec.requires_approval is (spec.effect is SideEffect.WRITE), action


def test_human_steps_and_arbitrary_js_flags() -> None:
    human = {action for action, spec in ACTION_SPECS.items() if spec.human_step}
    assert human == {BrowserAction.REQUEST_HELP, BrowserAction.TAB_BORROW}
    js = {action for action, spec in ACTION_SPECS.items() if spec.arbitrary_js}
    assert js == {BrowserAction.EVALUATE}


def test_unknown_action_rejected_without_dispatch(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session, action="hoverr", params={}, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.INVALID_BROWSER_ACTION
    assert transport.calls == []


def test_single_entry_dispatches_tool_prefixed_method(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session,
        action=BrowserAction.OBSERVE,
        params={"max_text_chars": 2000},
        expected_epoch=1,
        timeout_ms=5000,
        tool_call_id=TOOL_CALL,
    )
    assert isinstance(result, BrowserResult)
    assert result.action is BrowserAction.OBSERVE
    assert result.status == "succeeded"
    assert result.tool_call_id == TOOL_CALL
    assert len(transport.calls) == 1
    method, wire_params, timeout_ms = transport.calls[0]
    assert method == "tool.observe"
    assert timeout_ms == 5000
    assert wire_params["max_text_chars"] == 2000


# ---------------------------------------------------------------------------
# 2. Scope 只能服务端注入
# ---------------------------------------------------------------------------
def test_session_id_is_injected_by_server_not_caller(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session, action=BrowserAction.TAB_LIST, params={}, expected_epoch=1, timeout_ms=5000
    )
    assert isinstance(result, BrowserResult)
    wire_params = transport.calls[0][1]
    assert wire_params["session_id"] == str(session.browser_session_id)
    assert wire_params["session_id"] != str(session.workspace_id)


@pytest.mark.parametrize(
    "key",
    [
        "session_id",
        "browser_session_id",
        "browser_instance_id",
        "workspace_id",
        "principal_id",
        "expected_epoch",
        "timeout_ms",
        "tool_call_id",
    ],
)
def test_reserved_scope_keys_are_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport, key: str
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.OBSERVE,
        params={key: "20202020-2020-4020-8020-202020202020"},
        expected_epoch=1,
        timeout_ms=5000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.SCOPE_INJECTION_REJECTED
    assert err.data["param"] == key
    assert transport.calls == []


def test_from_client_payload_is_denied() -> None:
    with pytest.raises(RPCError) as excinfo:
        BrowserSession.from_client_payload(workspace_id=WORKSPACE)
    assert excinfo.value.code is BrowserErrorCode.SCOPE_INJECTION_REJECTED


def test_direct_session_construction_is_denied(caps: Capabilities) -> None:
    with pytest.raises(RPCError) as excinfo:
        BrowserSession(
            workspace_id=WORKSPACE,
            principal_id=PRINCIPAL,
            browser_session_id=uuid4(),
            backend="browserskill",
            state=BrowserSessionState.CONNECTED,
            capabilities=caps,
            lease_epoch=1,
        )
    assert excinfo.value.code is BrowserErrorCode.UNTRUSTED_BROWSER_CONSTRUCTION


def test_session_capabilities_must_be_negotiated() -> None:
    with pytest.raises(RPCError) as excinfo:
        create_browser_session(
            workspace_id=WORKSPACE,
            principal_id=PRINCIPAL,
            capabilities={"actions": ["click"]},  # type: ignore[arg-type]
        )
    assert excinfo.value.code is BrowserErrorCode.UNTRUSTED_BROWSER_CONSTRUCTION


# ---------------------------------------------------------------------------
# 3. 能力协商：U1–U9 显式能力位
# ---------------------------------------------------------------------------
def test_negotiation_marks_only_declared_actions_supported() -> None:
    declared = frozenset({BrowserAction.OBSERVE, BrowserAction.TAB_LIST})
    negotiated = make_caps(declared=declared)
    assert negotiated.adapter_api_version == ADAPTER_API_VERSION
    assert negotiated.supported_actions == declared
    assert BrowserAction.CLICK in negotiated.unsupported_actions
    assert negotiated.require_capability is not None
    negotiated.require_action(BrowserAction.OBSERVE)
    with pytest.raises(BrowserCapabilityError) as excinfo:
        negotiated.require_action(BrowserAction.CLICK)
    assert excinfo.value.code is BrowserErrorCode.CAPABILITY_UNSUPPORTED


def test_evaluate_is_never_supported(caps: Capabilities) -> None:
    assert BrowserAction.EVALUATE not in caps.supported_actions
    assert caps.supports("evaluate") is False
    with pytest.raises(BrowserCapabilityError) as excinfo:
        caps.require_action(BrowserAction.EVALUATE)
    assert excinfo.value.code is BrowserErrorCode.CAPABILITY_UNSUPPORTED
    assert excinfo.value.data["reason"] == "arbitrary_js_forbidden"


def test_all_unknown_bits_are_recorded_explicitly(caps: Capabilities) -> None:
    assert set(caps.capabilities) == set(CapabilityId)
    assert caps.status_of(CapabilityId.U1_ACTION_PARAM_SCHEMA) is CapabilityStatus.UNKNOWN
    assert caps.status_of(CapabilityId.U2_PROTOCOL_VERSION) is CapabilityStatus.SUPPORTED
    assert caps.status_of(CapabilityId.U5_FULL_REQUEST_NETWORK_CONTROL) is (
        CapabilityStatus.UNKNOWN
    )
    assert caps.status_of(CapabilityId.U9_WINDOW_CLOSED_DETECTION) is CapabilityStatus.UNKNOWN


def test_windows_runtime_bit_is_unknown_not_unsupported() -> None:
    linux = make_caps(platform="linux")
    windows = make_caps(platform="windows")
    assert linux.status_of(CapabilityId.U7_WINDOWS_RUNTIME) is CapabilityStatus.UNSUPPORTED
    assert windows.status_of(CapabilityId.U7_WINDOWS_RUNTIME) is CapabilityStatus.UNKNOWN


@pytest.mark.parametrize(
    ("ui_channel", "expected"),
    [
        (True, CapabilityStatus.SUPPORTED),
        (False, CapabilityStatus.UNSUPPORTED),
        (None, CapabilityStatus.UNKNOWN),
    ],
)
def test_ui_channel_three_state(ui_channel: bool | None, expected: CapabilityStatus) -> None:
    assert make_caps(ui_channel=ui_channel).ui_channel is expected


def test_unknown_declared_action_is_recorded_not_dropped() -> None:
    negotiated = negotiate_capabilities(
        declared_actions=[BrowserAction.OBSERVE, "frobnicate"],  # type: ignore[list-item]
        ui_channel=True,
        protocol_version=PROTOCOL_VERSION,
        tested_protocol_versions=frozenset({PROTOCOL_VERSION}),
    )
    assert negotiated.unknown_actions == frozenset({"frobnicate"})


def test_missing_protocol_version_is_incompatible() -> None:
    with pytest.raises(BrowserCapabilityError) as excinfo:
        negotiate_capabilities(
            declared_actions=[BrowserAction.OBSERVE],
            ui_channel=True,
            protocol_version=None,
        )
    assert excinfo.value.code is BrowserErrorCode.BROWSER_INCOMPATIBLE


def test_untested_protocol_version_is_incompatible() -> None:
    with pytest.raises(BrowserCapabilityError) as excinfo:
        negotiate_capabilities(
            declared_actions=[BrowserAction.OBSERVE],
            ui_channel=True,
            protocol_version="bsk-wire/999",
            tested_protocol_versions=frozenset({PROTOCOL_VERSION}),
        )
    assert excinfo.value.code is BrowserErrorCode.BROWSER_INCOMPATIBLE
    assert excinfo.value.data["protocol_version"] == "bsk-wire/999"


def test_empty_tested_versions_denies_every_backend() -> None:
    """Phase 0 的真实状态（U2）：没有已测版本 ⇒ 真实后端一律 BROWSER_INCOMPATIBLE。"""

    with pytest.raises(BrowserCapabilityError) as excinfo:
        negotiate_capabilities(
            declared_actions=[BrowserAction.OBSERVE],
            ui_channel=True,
            protocol_version="bsk-wire/1",
        )
    assert excinfo.value.code is BrowserErrorCode.BROWSER_INCOMPATIBLE


def test_require_unknown_capability_raises(caps: Capabilities) -> None:
    with pytest.raises(BrowserCapabilityError) as excinfo:
        caps.require_capability(CapabilityId.U5_FULL_REQUEST_NETWORK_CONTROL)
    assert excinfo.value.code is BrowserErrorCode.CAPABILITY_UNSUPPORTED
    assert excinfo.value.data["status"] == "unknown"


def test_port_negotiate_returns_error_instead_of_raising() -> None:
    port = BrowserPort(FakeTransport())
    result = port.negotiate(
        declared_actions=[BrowserAction.OBSERVE],
        ui_channel=True,
        protocol_version=None,
    )
    assert error_of(result).code is BrowserErrorCode.BROWSER_INCOMPATIBLE


def test_capabilities_hash_is_stable_and_version_sensitive() -> None:
    first = make_caps()
    second = make_caps()
    assert first.capabilities_hash == second.capabilities_hash
    other = negotiate_capabilities(
        declared_actions=[BrowserAction.OBSERVE],
        ui_channel=True,
        protocol_version="bsk-wire/2",
        tested_protocol_versions=frozenset({"bsk-wire/2"}),
    )
    assert other.capabilities_hash != first.capabilities_hash


# ---------------------------------------------------------------------------
# 4. 未协商动作与参数白名单
# ---------------------------------------------------------------------------
def test_unsupported_action_is_not_dispatched(
    session: BrowserSession, transport: FakeTransport
) -> None:
    narrow = make_caps(declared=frozenset({BrowserAction.OBSERVE}))
    narrow_session = make_session(narrow)
    port = BrowserPort(transport, network_policy=AllowAllPolicy())
    result = port.execute(
        narrow_session,
        action=BrowserAction.CLICK,
        params={"ref": "r1"},
        expected_epoch=1,
        timeout_ms=5000,
    )
    assert error_of(result).code is BrowserErrorCode.CAPABILITY_UNSUPPORTED
    assert transport.calls == []
    assert session is not narrow_session


def test_unknown_param_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.OBSERVE,
        params={"bogus": 1},
        expected_epoch=1,
        timeout_ms=5000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED
    assert err.data["unsupported"] == ["bogus"]
    assert transport.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"cookie": "sid=abc"},
        {"authorization": "Bearer abcdefghijklmnop"},
        {"device_token": "t"},
        {"sessionKey": "x"},
    ],
)
def test_secret_keys_are_rejected(
    port: BrowserPort,
    session: BrowserSession,
    transport: FakeTransport,
    params: dict[str, str],
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.OBSERVE,
        params=params,
        expected_epoch=1,
        timeout_ms=5000,
    )
    assert error_of(result).code is BrowserErrorCode.SECRET_IN_PARAMS
    assert transport.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "Bearer abcdefghijklmnop",
        "Kx8vQ2mN4pR7sT1uW3yZ6bC9dF2gH5jL8mN1pQ4R6tYv",
    ],
)
def test_secret_looking_values_are_rejected(
    port: BrowserPort,
    session: BrowserSession,
    transport: FakeTransport,
    value: str,
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.FILL,
        params={"ref": "r1", "value": value},
        expected_epoch=1,
        timeout_ms=5000,
    )
    assert error_of(result).code is BrowserErrorCode.SECRET_IN_PARAMS
    assert transport.calls == []


def test_arbitrary_js_action_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.EVALUATE,
        params={"expression": "document.cookie"},
        expected_epoch=1,
        timeout_ms=5000,
    )
    assert error_of(result).code is BrowserErrorCode.ARBITRARY_JS_REJECTED
    assert transport.calls == []


def test_arbitrary_js_param_on_other_action_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.GET_HTML,
        params={"expression": "document.body.innerHTML"},
        expected_epoch=1,
        timeout_ms=5000,
    )
    assert error_of(result).code is BrowserErrorCode.ARBITRARY_JS_REJECTED
    assert transport.calls == []


def test_required_params_are_enforced(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={},
        expected_epoch=1,
        timeout_ms=5000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.INVALID_BROWSER_PARAM
    assert err.data["missing"] == ["url"]
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 5. 网关硬约束：默认 wait_until 与 request_help
# ---------------------------------------------------------------------------
def test_navigate_defaults_to_domcontentloaded(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/docs"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert isinstance(result, BrowserResult)
    assert transport.calls[0][1]["wait_until"] == "domcontentloaded"


def test_navigate_explicit_wait_until_is_preserved(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/docs", "wait_until": "networkidle"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert transport.calls[0][1]["wait_until"] == "networkidle"


def test_invalid_wait_until_is_rejected(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/docs", "wait_until": "later"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert error_of(result).code is BrowserErrorCode.INVALID_BROWSER_PARAM
    assert transport.calls == []


def test_request_help_rejects_completion_criteria(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.REQUEST_HELP,
        params={"prompt": "please log in", "completion_criteria": "done"},
        expected_epoch=1,
        timeout_ms=300000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED
    assert err.data["param"] == "completion_criteria"
    assert transport.calls == []


def test_request_help_is_dispatched_without_criteria(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.REQUEST_HELP,
        params={"prompt": "please log in"},
        expected_epoch=1,
        timeout_ms=HUMAN_TIMEOUT_MAX_MS,
    )
    assert isinstance(result, BrowserResult)
    assert transport.calls[0][0] == "tool.request_help"
    assert "completion_criteria" not in transport.calls[0][1]


@pytest.mark.parametrize(
    ("action", "params", "timeout_ms", "ok"),
    [
        (BrowserAction.OBSERVE, {}, MAX_TIMEOUT_MS, True),
        (BrowserAction.OBSERVE, {}, MAX_TIMEOUT_MS + 1, False),
        (BrowserAction.OBSERVE, {}, 0, False),
        (BrowserAction.REQUEST_HELP, {"prompt": "hi"}, HUMAN_TIMEOUT_MAX_MS, True),
        (BrowserAction.REQUEST_HELP, {"prompt": "hi"}, HUMAN_TIMEOUT_MAX_MS + 1, False),
    ],
)
def test_timeout_bounds(
    port: BrowserPort,
    session: BrowserSession,
    action: BrowserAction,
    params: dict[str, object],
    timeout_ms: int,
    ok: bool,
) -> None:
    result = port.execute(
        session, action=action, params=params, expected_epoch=1, timeout_ms=timeout_ms
    )
    if ok:
        assert isinstance(result, BrowserResult)
    else:
        assert error_of(result).code is BrowserErrorCode.INVALID_BROWSER_PARAM


# ---------------------------------------------------------------------------
# 6. epoch / 状态 / 控制
# ---------------------------------------------------------------------------
def test_stale_epoch_is_rejected(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session, action=BrowserAction.OBSERVE, params={}, expected_epoch=99, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.STALE_EPOCH
    assert err.data["current"] == 1
    assert transport.calls == []


def test_paused_session_requires_resume(caps: Capabilities) -> None:
    paused = make_session(caps, state=BrowserSessionState.PAUSED)
    transport = FakeTransport()
    port = BrowserPort(transport, network_policy=AllowAllPolicy())
    rejected = port.execute(
        paused, action=BrowserAction.OBSERVE, params={}, expected_epoch=1, timeout_ms=5000
    )
    assert error_of(rejected).code is BrowserErrorCode.BROWSER_PAUSED
    assert transport.calls == []

    resumed = port.control(paused, "resume", expected_epoch=1)
    assert isinstance(resumed, BrowserSession)
    assert resumed.state is BrowserSessionState.CONNECTED
    assert resumed.lease_epoch == 2

    still_stale = port.execute(
        paused, action=BrowserAction.OBSERVE, params={}, expected_epoch=1, timeout_ms=5000
    )
    assert error_of(still_stale).code is BrowserErrorCode.BROWSER_PAUSED

    accepted = port.execute(
        resumed, action=BrowserAction.OBSERVE, params={}, expected_epoch=2, timeout_ms=5000
    )
    assert isinstance(accepted, BrowserResult)
    assert accepted.observed_epoch == 2


def test_control_unknown_command_is_rejected(port: BrowserPort, session: BrowserSession) -> None:
    result = port.control(session, "explode", expected_epoch=1)  # type: ignore[arg-type]
    assert error_of(result).code is BrowserErrorCode.INVALID_BROWSER_ACTION


def test_control_requires_current_epoch(port: BrowserPort, session: BrowserSession) -> None:
    result = port.control(session, "pause", expected_epoch=7)
    assert error_of(result).code is BrowserErrorCode.STALE_EPOCH


def test_control_pause_then_double_pause(port: BrowserPort, session: BrowserSession) -> None:
    paused = port.control(session, "pause", expected_epoch=1)
    assert isinstance(paused, BrowserSession)
    assert paused.state is BrowserSessionState.PAUSED
    assert paused.lease_epoch == 1
    again = port.control(paused, "pause", expected_epoch=1)
    assert error_of(again).code is BrowserErrorCode.BROWSER_PAUSED


def test_revoke_is_idempotent_and_terminal(port: BrowserPort, session: BrowserSession) -> None:
    revoked = port.control(session, "revoke", expected_epoch=1)
    assert isinstance(revoked, BrowserSession)
    assert revoked.state is BrowserSessionState.REVOKED
    assert revoked.lease_epoch == 2

    again = port.control(revoked, "revoke", expected_epoch=999)
    assert isinstance(again, BrowserSession)
    assert again.state is BrowserSessionState.REVOKED
    assert again.lease_epoch == 2

    closed = port.control(revoked, "close", expected_epoch=999)
    assert isinstance(closed, BrowserSession)
    assert closed.state is BrowserSessionState.REVOKED


def test_execute_after_revoke_is_rejected(port: BrowserPort, session: BrowserSession) -> None:
    revoked = port.control(session, "revoke", expected_epoch=1)
    assert isinstance(revoked, BrowserSession)
    result = port.execute(
        revoked, action=BrowserAction.OBSERVE, params={}, expected_epoch=2, timeout_ms=5000
    )
    assert error_of(result).code is BrowserErrorCode.SESSION_REVOKED


# ---------------------------------------------------------------------------
# 7. 审批绑定（写动作）
# ---------------------------------------------------------------------------
def test_write_action_without_approval_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params={"ref": "r1"},
        expected_epoch=1,
        timeout_ms=5000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.APPROVAL_REQUIRED
    assert err.data["params_hash"]
    assert transport.calls == []


def test_write_action_with_valid_approval_dispatches(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    params = {"ref": "r1"}
    approval = make_approval(session, BrowserAction.CLICK, params)
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params=params,
        expected_epoch=1,
        timeout_ms=5000,
        approval=approval,
    )
    assert isinstance(result, BrowserResult)
    assert transport.calls[0][0] == "tool.click"
    assert transport.calls[0][1]["ref"] == "r1"


def test_approval_params_hash_mismatch_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    approval = make_approval(session, BrowserAction.CLICK, {"ref": "r1"})
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params={"ref": "r2"},  # 参数被改写 ⇒ 指纹不一致
        expected_epoch=1,
        timeout_ms=5000,
        approval=approval,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.APPROVAL_MISMATCH
    assert err.data["reason"] == "params_mismatch"
    assert transport.calls == []


def test_approval_expired_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    approval = make_approval(session, BrowserAction.CLICK, {"ref": "r1"}, expires_in=-1)
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params={"ref": "r1"},
        expected_epoch=1,
        timeout_ms=5000,
        approval=approval,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.APPROVAL_MISMATCH
    assert err.data["reason"] == "expired"
    assert transport.calls == []


def test_approval_from_other_workspace_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    approval = make_approval(
        session, BrowserAction.CLICK, {"ref": "r1"}, workspace_id=OTHER_WORKSPACE
    )
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params={"ref": "r1"},
        expected_epoch=1,
        timeout_ms=5000,
        approval=approval,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.APPROVAL_MISMATCH
    assert err.data["reason"] == "workspace_mismatch"


def test_approval_for_other_action_is_rejected(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    approval = make_approval(
        session,
        BrowserAction.CLICK,
        {"ref": "r1"},
        granted_action=BrowserAction.FILL,
    )
    result = port.execute(
        session,
        action=BrowserAction.CLICK,
        params={"ref": "r1"},
        expected_epoch=1,
        timeout_ms=5000,
        approval=approval,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.APPROVAL_MISMATCH
    assert err.data["reason"] == "action_mismatch"


# ---------------------------------------------------------------------------
# 8. 网络策略前置拦截点（T16 落地；这里只验证 fail-closed 接口）
# ---------------------------------------------------------------------------
def test_navigation_is_denied_by_default(
    port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    assert isinstance(port.network_policy, DenyAllNetworkPolicy)
    result = port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/docs"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.SSRF_BLOCKED
    assert err.data["policy"] == "T16"
    assert transport.calls == []


def test_navigation_dispatches_when_policy_allows(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    result = allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/docs"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert isinstance(result, BrowserResult)
    assert transport.calls[0][0] == "tool.navigate"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "https://user:pass@example.com/docs",
        "file:///etc/passwd",
    ],
)
def test_url_shape_guards(
    allow_port: BrowserPort,
    session: BrowserSession,
    transport: FakeTransport,
    url: str,
) -> None:
    result = allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": url},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert error_of(result).code is BrowserErrorCode.SSRF_BLOCKED
    assert transport.calls == []


def test_final_url_is_redacted(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.reply = {"final_url": "https://example.com/a?token=secret#frag"}
    result = allow_port.execute(
        session,
        action=BrowserAction.NAVIGATE,
        params={"url": "https://example.com/a"},
        expected_epoch=1,
        timeout_ms=8000,
    )
    assert isinstance(result, BrowserResult)
    assert result.final_url == "https://example.com/a"


# ---------------------------------------------------------------------------
# 9. 错误映射与超时语义（禁止自动重试）
# ---------------------------------------------------------------------------
def test_automatic_retry_is_disabled() -> None:
    assert AUTOMATIC_RETRY is False


def test_timeout_before_send_is_retryable(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.failure = TransportFailure("timeout", "dial failed", action_sent=False)
    result = allow_port.execute(
        session, action=BrowserAction.OBSERVE, params={}, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_TIMEOUT
    assert err.action_sent is False
    assert err.retryable is True
    assert err.replay_safe is True


def test_timeout_after_send_is_unknown_outcome(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.failure = TransportFailure("timeout", "no reply", action_sent=True)
    result = allow_port.execute(
        session, action=BrowserAction.CLICK, params={"ref": "r1"}, expected_epoch=1,
        timeout_ms=5000, approval=make_approval(session, BrowserAction.CLICK, {"ref": "r1"}),
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.TOOL_UNKNOWN_OUTCOME
    assert err.action_sent is True
    assert err.retryable is False
    assert err.replay_safe is False


def test_cancel_before_send_maps_to_browser_cancelled() -> None:
    mapped = map_wire_error(TransportFailure("cancelled", "user cancelled", action_sent=False))
    assert mapped.code is BrowserErrorCode.BROWSER_CANCELLED
    assert mapped.retryable is False


@pytest.mark.parametrize(
    ("wire_code", "message", "expected"),
    [
        ("daemon_incompatible", "protocol mismatch", BrowserErrorCode.BROWSER_INCOMPATIBLE),
        ("protocol_error", "bad frame", BrowserErrorCode.BROWSER_INCOMPATIBLE),
        ("unknown_method", "tool.frobnicate", BrowserErrorCode.CAPABILITY_UNSUPPORTED),
        ("gateway_ui_unsupported", "no ui channel", BrowserErrorCode.CAPABILITY_UNSUPPORTED),
        ("task_paused", "paused by user", BrowserErrorCode.BROWSER_PAUSED),
        ("renderer_read_timeout", "renderer stuck", BrowserErrorCode.TOOL_UNKNOWN_OUTCOME),
        ("not_found", "session is not registered", BrowserErrorCode.BROWSER_OFFLINE),
        ("owner_lost", "owner disconnected", BrowserErrorCode.OWNER_LOST),
        ("mystery_code", "nobody knows", BrowserErrorCode.BROWSER_WIRE_ERROR),
    ],
)
def test_wire_error_mapping_table(wire_code: str, message: str, expected: BrowserErrorCode) -> None:
    mapped = map_wire_error(TransportFailure(wire_code, message, action_sent=True))
    assert mapped.code is expected
    assert mapped.data["wire_code"] == wire_code


def test_unregistered_wire_error_is_not_success(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.reply = {"error": {"code": "mystery_code", "message": "boom"}}
    result = allow_port.execute(
        session, action=BrowserAction.OBSERVE, params={}, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_WIRE_ERROR
    assert err.action_sent is True


def test_transport_crash_never_looks_like_success(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.crash = RuntimeError("socket exploded")
    result = allow_port.execute(
        session, action=BrowserAction.OBSERVE, params={}, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_WIRE_ERROR
    assert err.action_sent is True


# ---------------------------------------------------------------------------
# 10. 结果不变量与模型可见信封
# ---------------------------------------------------------------------------
def test_artifact_refs_must_be_uuid_references(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    transport.reply = {"artifact_refs": ["https://cdn.example.com/shot.png"]}
    result = allow_port.execute(
        session, action=BrowserAction.SCREENSHOT, params={}, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is BrowserErrorCode.BROWSER_WIRE_ERROR
    assert "UUID" in err.message


def test_artifact_refs_are_parsed_as_uuid(
    allow_port: BrowserPort, session: BrowserSession, transport: FakeTransport
) -> None:
    artifact = uuid4()
    transport.reply = {"artifact_refs": [str(artifact)], "observed_epoch": 3}
    result = allow_port.execute(
        session, action=BrowserAction.SCREENSHOT, params={}, expected_epoch=1, timeout_ms=5000
    )
    assert isinstance(result, BrowserResult)
    assert result.artifact_refs == (artifact,)
    assert result.observed_epoch == 3


def test_result_invariants_reject_contradiction() -> None:
    with pytest.raises(RPCError) as excinfo:
        BrowserResult(
            tool_call_id=TOOL_CALL,
            action=BrowserAction.OBSERVE,
            status="succeeded",
            final_url=None,
            artifact_refs=(),
            error_code="BROWSER_OFFLINE",
            action_sent=True,
            observed_epoch=1,
        )
    assert excinfo.value.code is BrowserErrorCode.BROWSER_WIRE_ERROR


def test_error_envelope_shape_matches_result_go() -> None:
    err = RPCError(
        BrowserErrorCode.SSRF_BLOCKED,
        "blocked",
        data={"url": "https://example.com", "policy": "T16"},
    )
    envelope = err.to_envelope("tool.navigate")
    assert envelope["method"] == "tool.navigate"
    error = envelope["error"]
    assert isinstance(error, Mapping)
    assert error["code"] == "SSRF_BLOCKED"
    assert error["data"]["policy"] == "T16"
    assert isinstance(envelope["recovery_hint"], str) and envelope["recovery_hint"]


# ---------------------------------------------------------------------------
# 11. 正负契约 fixture 总表（≥20 条，全部判定正确且不触达传输层）
# ---------------------------------------------------------------------------
NEGATIVE_FIXTURES: tuple[tuple[str, dict[str, Any], BrowserErrorCode, str], ...] = (
    ("hoverr", {}, BrowserErrorCode.INVALID_BROWSER_ACTION, "未知动作"),
    ("observe", {"session_id": "x"}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入 session"),
    ("observe", {"workspace_id": "x"}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入空间"),
    ("observe", {"principal_id": "x"}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入主体"),
    ("observe", {"expected_epoch": 9}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入 epoch"),
    ("observe", {"timeout_ms": 1}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入超时"),
    ("observe", {"tool_call_id": "x"}, BrowserErrorCode.SCOPE_INJECTION_REJECTED, "注入调用id"),
    ("observe", {"cookie": "a=b"}, BrowserErrorCode.SECRET_IN_PARAMS, "cookie 键"),
    ("observe", {"authorization": "Bearer abc"}, BrowserErrorCode.SECRET_IN_PARAMS, "auth 键"),
    ("navigate", {"url": "https://e.com", "device_token": "t"},
     BrowserErrorCode.SECRET_IN_PARAMS, "设备 token"),
    ("observe", {"bogus": 1}, BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED, "未知参数"),
    ("evaluate", {"expression": "1+1"}, BrowserErrorCode.ARBITRARY_JS_REJECTED, "任意 JS"),
    ("get_html", {"expression": "document.body"}, BrowserErrorCode.ARBITRARY_JS_REJECTED,
     "借参数夹带 JS"),
    ("observe", {"wait_until": "load"}, BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED,
     "read 动作无 wait_until"),
    ("request_help", {"prompt": "log in", "completion_criteria": "done"},
     BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED, "模型自证完成"),
    ("observe", {"max_depth": "deep"}, BrowserErrorCode.INVALID_BROWSER_PARAM, "类型不符"),
    ("click", {"ref": "r1"}, BrowserErrorCode.APPROVAL_REQUIRED, "写动作无审批"),
    ("navigate", {}, BrowserErrorCode.INVALID_BROWSER_PARAM, "缺 url"),
    ("navigate", {"url": "ftp://e.com/a"}, BrowserErrorCode.SSRF_BLOCKED, "scheme 拒绝"),
    ("press", {"key": "Enter", "path": "/tmp/x"},
     BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED, "未声明参数"),
    ("tab_select", {}, BrowserErrorCode.INVALID_BROWSER_PARAM, "缺 tab_id"),
    ("wait_ms", {"duration_ms": -1}, BrowserErrorCode.INVALID_BROWSER_PARAM, "负时长"),
    ("screenshot", {"path": "/tmp/a.png"}, BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED,
     "本地路径泄漏"),
    ("fill", {"ref": "r1", "value": "Bearer abcdefghijkl"}, BrowserErrorCode.SECRET_IN_PARAMS,
     "值形态像凭据"),
)


def test_negative_fixture_matrix_has_at_least_twenty_cases() -> None:
    assert len(NEGATIVE_FIXTURES) >= 20


@pytest.mark.parametrize(("action", "params", "expected", "note"), NEGATIVE_FIXTURES)
def test_negative_fixture_matrix(
    port: BrowserPort,
    session: BrowserSession,
    transport: FakeTransport,
    action: str,
    params: dict[str, Any],
    expected: BrowserErrorCode,
    note: str,
) -> None:
    result = port.execute(
        session, action=action, params=params, expected_epoch=1, timeout_ms=5000
    )
    err = error_of(result)
    assert err.code is expected, f"{note}: 期望 {expected} 实际 {err.code} ({err.message})"
    assert transport.calls == [], f"{note}: 被拒绝的调用不得触达传输层"
