"""P0-T18 证据测试：BrowserSkill owner 唯一性、撤销与迟到结果丢弃。

分两层，与 `tests/phase0/test_bsk_live.py`（T17）保持一致的门控口径：

- **默认单测**（无需 daemon）：`LeaseRegistry` 的 CAS/epoch/撤销/TTL/并发单胜者，
  以及 `BrowserskillRecovery` 用替身 CLI 验证「败者不产生第二次 wire 调用」
  「撤销后不派发」「旧 epoch 结果只进 dropped 账本」。
- **真机用例**：`BSK_LIVE=1` **且** CPython ≥ 3.13 才跑。3.12 的 uv 托管解释器
  派生 `bsk.exe` 时拿不到 daemon 的 Windows 命名管道（`os error 5`），这是 T17
  已记录的运行时差异，不是本任务代码问题；未满足门控时在证据里记 **NOT_RUN**，
  不以 skip 冒充通过。

真机用例的原始输出摘录记录在 `docs/poc/evidence/poc05-recovery.md`。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from browser_contracts import BrowserAction
from browserskill_probe.cli import BskCli, BskCliError
from browserskill_probe.recovery import (
    DEFAULT_LEASE_TTL_SECONDS,
    EPOCH_CONFLICT,
    LEASE_EXPIRED,
    MAX_REQUEST_TTL_MS,
    SESSION_REVOKED,
    STALE_EPOCH,
    Bootstrap,
    BrowserskillRecovery,
    EpochConflict,
    LeaseExpired,
    LeaseRegistry,
    PreparedRequest,
    SessionRevoked,
    StaleEpoch,
    StartRejected,
    new_request_id,
    parse_request_id,
    supported_recovery_actions,
)

LIVE = os.environ.get("BSK_LIVE") == "1"
PY_OK = sys.version_info >= (3, 13)

live = pytest.mark.skipif(
    not (LIVE and PY_OK),
    reason=(
        "NOT_RUN: 需要 BSK_LIVE=1 且 CPython>=3.13；uv 托管的 3.12 派生 bsk.exe 时"
        "拿不到 daemon 命名管道（os error 5），真机取证用 C:\\Python313\\python.exe"
    ),
)


class _StubCli:
    """替身 CLI：按 argv 前缀返回预置 payload 或抛预置异常，并记录全部调用。"""

    def __init__(self, routes: Mapping[tuple[str, ...], Any] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[list[str]] = []

    def _lookup(self, argv: list[str]) -> Any:
        for prefix in sorted(self.routes, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                return self.routes[prefix]
        return {"ok": True}

    def run_json(self, args, *, timeout_seconds: float | None = None) -> Any:
        argv = [str(item) for item in args]
        self.calls.append(list(argv))
        outcome = self._lookup(argv)
        if isinstance(outcome, BaseException):
            raise outcome
        return json.loads(json.dumps(outcome)) if not isinstance(outcome, str) else outcome

    def run(self, args, *, timeout_seconds: float | None = None, check: bool = True):
        argv = [str(item) for item in args]
        self.calls.append(list(argv))
        outcome = self._lookup(argv)
        if isinstance(outcome, BaseException):
            raise outcome
        return 0, json.dumps(outcome, ensure_ascii=False), ""


def _request(expiry_ms: int = 1_700_000_000_000) -> str:
    return f"{expiry_ms}:3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _start_payload(session_id: str = "sess-1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "browser_instance_id": "dc8a279d",
        "agent_window_id": 1,
        "interaction": {"borrow_confirmation": "always", "request_help": "enabled"},
    }


# ================================================================ 请求 ID


def test_new_request_id_encodes_expiry_and_uuid() -> None:
    request_id = new_request_id(ttl_ms=60_000, clock_ms=lambda: 1_700_000_000_000)
    expiry_ms, parsed = parse_request_id(request_id)
    assert expiry_ms == 1_700_000_060_000
    assert str(parsed) == request_id.split(":", 1)[1]


def test_new_request_id_rejects_ttl_above_ten_minutes() -> None:
    with pytest.raises(ValueError, match="ttl_ms"):
        new_request_id(ttl_ms=MAX_REQUEST_TTL_MS + 1, clock_ms=lambda: 0)
    with pytest.raises(ValueError, match="ttl_ms"):
        new_request_id(ttl_ms=0, clock_ms=lambda: 0)


@pytest.mark.parametrize(
    "bad",
    ["", "no-colon", "abc:3f2504e0-4f89-41d3-9a0c-0305e82c3301", "0:3f2504e0-4f89-41d3-9a0c-0305e82c3301"],
)
def test_parse_request_id_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_request_id(bad)


# ================================================================ epoch CAS


def test_first_claim_starts_at_epoch_one_and_is_alive() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    assert lease.epoch == 1
    assert lease.owner_id == "owner-a"
    assert lease.expires_at == 130.0
    assert lease.alive(100.0) is True
    assert registry.epoch("req-1") == 1


def test_claim_with_stale_expected_epoch_is_rejected() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    with pytest.raises(StaleEpoch) as excinfo:
        registry.claim("req-1", expected_epoch=0, owner_id="owner-b", ttl=30.0)
    assert excinfo.value.code == STALE_EPOCH
    assert excinfo.value.details["current_epoch"] == 1


def test_second_owner_cannot_claim_a_live_lease() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    with pytest.raises(EpochConflict) as excinfo:
        registry.claim("req-1", expected_epoch=1, owner_id="owner-b", ttl=30.0)
    assert excinfo.value.code == EPOCH_CONFLICT
    assert excinfo.value.details["holder"] == "owner-a"
    assert registry.epoch("req-1") == 1


def test_same_owner_reclaim_is_idempotent_but_bumps_epoch() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    first = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0, request_id="req-1")
    second = registry.claim("req-1", expected_epoch=1, owner_id="owner-a", ttl=30.0)
    assert (first.epoch, second.epoch) == (1, 2)
    assert second.request_id == "req-1"
    assert len(registry.history()) == 2


def test_claim_with_epoch_ahead_of_current_is_conflict() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    with pytest.raises(EpochConflict) as excinfo:
        registry.claim("req-1", expected_epoch=5, owner_id="owner-a", ttl=30.0)
    assert excinfo.value.code == EPOCH_CONFLICT


def test_assert_current_accepts_live_lease_and_rejects_stale() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    assert registry.assert_current("req-1", lease.epoch).owner_id == "owner-a"
    registry.takeover("req-1", expected_epoch=lease.epoch, owner_id="owner-b", ttl=30.0)
    with pytest.raises(StaleEpoch):
        registry.assert_current("req-1", lease.epoch)


def test_takeover_fences_the_old_owner() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    first = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    second = registry.takeover("req-1", expected_epoch=first.epoch, owner_id="owner-b", ttl=30.0)
    assert second.epoch == first.epoch + 1
    assert registry.assert_current("req-1", second.epoch).owner_id == "owner-b"
    with pytest.raises(StaleEpoch):
        registry.assert_current("req-1", first.epoch)


def test_takeover_requires_cas_hit() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    with pytest.raises(StaleEpoch):
        registry.takeover("req-1", expected_epoch=0, owner_id="owner-b", ttl=30.0)


def test_expired_lease_requires_takeover_not_silent_reuse() -> None:
    now = [100.0]
    registry = LeaseRegistry(clock=lambda: now[0])
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=10.0)
    now[0] = 111.0
    assert lease.alive(now[0]) is False
    assert lease.remaining(now[0]) == 0.0
    with pytest.raises(LeaseExpired) as excinfo:
        registry.assert_current("req-1", lease.epoch)
    assert excinfo.value.code == LEASE_EXPIRED
    taken = registry.takeover("req-1", expected_epoch=lease.epoch, owner_id="owner-b", ttl=10.0)
    assert taken.epoch == lease.epoch + 1


def test_claim_after_expiry_by_another_owner_succeeds_and_old_epoch_is_stale() -> None:
    now = [100.0]
    registry = LeaseRegistry(clock=lambda: now[0])
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=10.0)
    now[0] = 200.0
    taken = registry.claim("req-1", expected_epoch=lease.epoch, owner_id="owner-b", ttl=10.0)
    assert taken.epoch == lease.epoch + 1
    with pytest.raises(StaleEpoch):
        registry.assert_current("req-1", lease.epoch)


def test_claim_snapshot_reports_epoch_lease_and_liveness() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    rows = registry.snapshot()
    assert rows == [
        {
            "subject": "req-1",
            "epoch": 1,
            "revoked": False,
            "revoke_reason": None,
            "lease": rows[0]["lease"],
            "lease_alive": True,
        }
    ]
    assert rows[0]["lease"]["owner_instance"] == "owner-a"
    assert rows[0]["lease"]["lease_epoch"] == 1


# ================================================================ 撤销


def test_revoke_blocks_every_epoch_afterwards() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    record = registry.revoke("req-1", reason="user-revoked")
    assert record == {
        "subject": "req-1",
        "revoked": True,
        "already_revoked": False,
        "reason": "user-revoked",
        "epoch": lease.epoch + 1,
        "previous_owner": "owner-a",
    }
    with pytest.raises(SessionRevoked) as excinfo:
        registry.assert_current("req-1", lease.epoch)
    assert excinfo.value.code == SESSION_REVOKED
    with pytest.raises(SessionRevoked):
        registry.assert_current("req-1", record["epoch"])
    with pytest.raises(SessionRevoked):
        registry.claim("req-1", expected_epoch=record["epoch"], owner_id="owner-b", ttl=30.0)
    with pytest.raises(SessionRevoked):
        registry.takeover("req-1", expected_epoch=record["epoch"], owner_id="owner-b", ttl=30.0)
    assert registry.lease("req-1") is None
    assert registry.is_revoked("req-1") is True
    assert registry.revoke_reason("req-1") == "user-revoked"


def test_revoke_is_idempotent() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    first = registry.revoke("req-1", reason="r1")
    second = registry.revoke("req-1", reason="r2")
    assert first["already_revoked"] is False
    assert second["already_revoked"] is True
    assert second["reason"] == "r1"
    assert second["epoch"] == first["epoch"]


def test_release_requires_current_epoch() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    lease = registry.claim("req-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    registry.release("req-1", epoch=lease.epoch)
    assert registry.lease("req-1") is None
    with pytest.raises(StaleEpoch):
        registry.release("req-1", epoch=lease.epoch)


# ================================================================ 并发单胜者


def test_concurrent_claims_have_exactly_one_winner() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    barrier = threading.Barrier(8)
    winners: list[int] = []
    losers: list[str] = []
    lock = threading.Lock()

    def contend(index: int) -> None:
        barrier.wait()
        try:
            lease = registry.claim(
                "req-race", expected_epoch=0, owner_id=f"owner-{index}", ttl=30.0
            )
        except (EpochConflict, StaleEpoch) as exc:
            with lock:
                losers.append(exc.code)
            return
        with lock:
            winners.append(lease.epoch)

    threads = [threading.Thread(target=contend, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(losers) == 7
    assert set(losers) <= {EPOCH_CONFLICT, STALE_EPOCH}
    assert registry.epoch("req-race") == 1


# ================================================================ 恢复器（替身 CLI）


def _recovery(routes: Mapping[tuple[str, ...], Any] | None = None) -> tuple[BrowserskillRecovery, _StubCli]:
    stub = _StubCli(routes)
    return BrowserskillRecovery(cli=stub, owner_id="owner-a"), stub  # type: ignore[arg-type]


def test_recoverable_start_claims_epoch_one_and_binds_session() -> None:
    recovery, stub = _recovery(
        {
            ("session", "start"): _start_payload("sess-1"),
            ("session", "request"): {"request_id": _request(), "state": "ready",
                                     "session": {"session_id": "sess-1"}, "cleanup_error": None},
        }
    )
    prepared = PreparedRequest(request_id=_request(), state="prepared", expires_at_ms=1_700_000_000_000)
    bootstrap = recovery.recoverable_start(prepared)
    assert isinstance(bootstrap, Bootstrap)
    assert bootstrap.session_id == "sess-1"
    assert bootstrap.lease.epoch == 1
    assert recovery.registry.epoch("sess-1") == 1
    assert recovery.registry.lease("sess-1").request_id == _request()
    assert stub.calls[0] == ["session", "start", "--request-id", _request(), "--no-focus"]
    assert [call[:2] for call in stub.calls] == [
        ["session", "start"],
        ["session", "request"],
    ]


def test_recoverable_start_second_owner_is_rejected_before_wire() -> None:
    registry = LeaseRegistry(clock=lambda: 100.0)
    routes = {
        ("session", "start"): _start_payload("sess-shared"),
        ("session", "request"): {"request_id": _request(), "state": "ready",
                                 "session": {"session_id": "sess-shared"}, "cleanup_error": None},
    }
    stub_a, stub_b = _StubCli(routes), _StubCli(routes)
    owner_a = BrowserskillRecovery(cli=stub_a, registry=registry, owner_id="owner-a")  # type: ignore[arg-type]
    owner_b = BrowserskillRecovery(cli=stub_b, registry=registry, owner_id="owner-b")  # type: ignore[arg-type]
    prepared = PreparedRequest(request_id=_request(), state="prepared", expires_at_ms=1)

    owner_a.recoverable_start(prepared)
    with pytest.raises((EpochConflict, StaleEpoch)) as excinfo:
        owner_b.recoverable_start(prepared)
    assert excinfo.value.code in {EPOCH_CONFLICT, STALE_EPOCH}
    assert stub_b.calls == []


def test_recoverable_start_maps_cancelled_to_start_rejected_and_releases_lease() -> None:
    rejected = BskCliError(
        "CLI 退出码 2",
        exit_code=2,
        stdout=json.dumps({"code": "cancelled", "message": "start request cancelled", "exit_code": 2}),
    )
    recovery, _stub = _recovery({("session", "start"): rejected})
    prepared = PreparedRequest(request_id=_request(), state="prepared", expires_at_ms=1)

    with pytest.raises(StartRejected) as excinfo:
        recovery.recoverable_start(prepared)
    assert excinfo.value.details["wire_code"] == "cancelled"
    assert excinfo.value.details["exit_code"] == 2
    assert recovery.registry.lease(_request()) is None
    assert recovery.registry.epoch(_request()) == 1


def test_execute_is_blocked_after_revoke_and_makes_no_wire_call() -> None:
    recovery, stub = _recovery({("session", "stop"): {"stopped": ["sess-1"]}})
    recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    recovery.revoke(session_id="sess-1", reason="user-revoked")
    stub.calls.clear()

    with pytest.raises(SessionRevoked):
        recovery.execute(
            session_id="sess-1", epoch=1, action=BrowserAction.SNAPSHOT, params={}
        )
    assert stub.calls == []


def test_execute_dispatches_when_epoch_is_current() -> None:
    recovery, stub = _recovery()
    lease = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    payload = recovery.execute(
        session_id="sess-1", epoch=lease.epoch, action=BrowserAction.SNAPSHOT, params={}
    )
    assert payload == {"ok": True}
    assert stub.calls[-1] == ["snapshot", "--session", "sess-1"]
    assert recovery.calls[-1]["epoch"] == lease.epoch


def test_revoke_reports_wire_stop_failure_without_hiding_it() -> None:
    boom = BskCliError("CLI 退出码 1", exit_code=1, stdout='{"code":"not_found"}')
    recovery, _stub = _recovery({("session", "stop"): boom})
    lease = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    record = recovery.revoke(session_id="sess-1", reason="cancel")
    assert record["revoked"] is True
    assert record["wire"] is None
    assert record["wire_error"]["exit_code"] == 1
    assert record["wire_error"]["stdout"] == '{"code":"not_found"}'
    with pytest.raises(SessionRevoked):
        recovery.registry.assert_current("sess-1", lease.epoch)


# ================================================================ 迟到结果丢弃


def test_apply_result_applies_current_epoch() -> None:
    recovery, _stub = _recovery()
    lease = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    outcome = recovery.apply_result(
        session_id="sess-1", epoch=lease.epoch, action=BrowserAction.SNAPSHOT, result={"text": "@1"}
    )
    assert outcome["applied"] is True
    assert outcome["dropped_reason"] is None
    assert outcome["applied_epoch"] == lease.epoch
    assert recovery.applied[-1]["result"] == {"text": "@1"}
    assert recovery.dropped == []


def test_apply_result_drops_stale_epoch() -> None:
    recovery, _stub = _recovery()
    old = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    new = recovery.registry.takeover("sess-1", expected_epoch=old.epoch, owner_id="owner-b", ttl=30.0)
    outcome = recovery.apply_result(
        session_id="sess-1", epoch=old.epoch, action=BrowserAction.NAVIGATE, result={"url": "late"}
    )
    assert outcome == {
        "applied": False,
        "dropped_reason": STALE_EPOCH,
        "session_id": "sess-1",
        "epoch": old.epoch,
        "action": "navigate",
        "detail": outcome["detail"],
    }
    assert recovery.applied == []
    assert recovery.dropped[-1]["dropped_reason"] == STALE_EPOCH
    assert recovery.registry.epoch("sess-1") == new.epoch


def test_apply_result_drops_after_revoke() -> None:
    recovery, _stub = _recovery()
    lease = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    recovery.registry.revoke("sess-1", reason="revoked")
    outcome = recovery.apply_result(
        session_id="sess-1", epoch=lease.epoch, action=BrowserAction.SNAPSHOT, result={"text": "@1"}
    )
    assert outcome["applied"] is False
    assert outcome["dropped_reason"] == SESSION_REVOKED


def test_apply_result_drops_when_lease_expired() -> None:
    now = [100.0]
    registry = LeaseRegistry(clock=lambda: now[0])
    recovery, _stub = _recovery()
    recovery.registry = registry
    lease = registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=10.0)
    now[0] = 120.0
    outcome = recovery.apply_result(
        session_id="sess-1", epoch=lease.epoch, action=BrowserAction.SNAPSHOT, result={}
    )
    assert outcome["applied"] is False
    assert outcome["dropped_reason"] == LEASE_EXPIRED


def test_apply_result_drops_future_epoch_as_conflict() -> None:
    recovery, _stub = _recovery()
    recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    outcome = recovery.apply_result(
        session_id="sess-1", epoch=99, action=BrowserAction.SNAPSHOT, result={}
    )
    assert outcome["applied"] is False
    assert outcome["dropped_reason"] == EPOCH_CONFLICT


# ================================================================ 请求操作


def test_cancel_request_returns_closed_state() -> None:
    recovery, stub = _recovery(
        {("session", "request"): {"request_id": _request(), "state": "closed", "session": None}}
    )
    prepared = recovery.cancel_request(_request())
    assert prepared.state == "closed"
    assert stub.calls[0] == ["session", "request", _request(), "--cancel"]


def test_cancel_request_rejects_unexpected_state() -> None:
    recovery, _stub = _recovery(
        {("session", "request"): {"request_id": _request(), "state": "prepared", "session": None}}
    )
    with pytest.raises(StartRejected, match="closed"):
        recovery.cancel_request(_request())


def test_claim_request_passes_through_exit_code_and_payload() -> None:
    recovery, stub = _recovery(
        {("session", "request"): {"code": "cancelled", "message": "cannot be claimed", "exit_code": 2}}
    )
    stub.calls.clear()  # 只关心 --claim 这一次调用
    result = recovery.claim_request(_request())
    assert result["exit_code"] == 0
    assert result["payload"]["code"] == "cancelled"
    assert stub.calls[0] == ["session", "request", _request(), "--claim"]


def test_status_summarises_lease_and_ledgers() -> None:
    recovery, _stub = _recovery()
    lease = recovery.registry.claim("sess-1", expected_epoch=0, owner_id="owner-a", ttl=30.0)
    recovery.apply_result(session_id="sess-1", epoch=lease.epoch, action=BrowserAction.SNAPSHOT,
                          result={})
    status = recovery.status("sess-1")
    assert status["epoch"] == lease.epoch
    assert status["revoked"] is False
    assert status["lease"]["owner_instance"] == "owner-a"
    assert (status["applied"], status["dropped"]) == (1, 0)


def test_supported_recovery_actions_are_contract_subset() -> None:
    names = set(supported_recovery_actions())
    assert names == {BrowserAction.SNAPSHOT.value, BrowserAction.TAB_LIST.value}
    assert names.issubset({action.value for action in BrowserAction})


def test_default_ttl_is_sane() -> None:
    assert 0 < DEFAULT_LEASE_TTL_SECONDS <= 300


# ================================================================ 真机（BSK_LIVE=1 + 3.13）


def _payload_of(exc: BskCliError) -> dict[str, Any]:
    try:
        parsed = json.loads((exc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@live
def test_live_recoverable_start_executes_and_stop_blocks_old_session() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-live")
    prepared = recovery.prepare_request(name="p0-t18-live")
    assert prepared.state == "prepared"

    bootstrap = recovery.recoverable_start(prepared, ttl=120.0)
    try:
        assert bootstrap.lease.epoch == 1
        assert recovery.inspect_request(prepared.request_id).state in {"ready", "active"}
        snapshot = recovery.execute(
            session_id=bootstrap.session_id,
            epoch=bootstrap.lease.epoch,
            action=BrowserAction.SNAPSHOT,
            params={},
        )
        assert "text" in snapshot
    finally:
        stopped = recovery.stop(session_id=bootstrap.session_id, epoch=bootstrap.lease.epoch)
    assert stopped["stopped"] == [bootstrap.session_id]

    with pytest.raises(BskCliError) as excinfo:
        recovery.cli.run_json(["snapshot", "--session", bootstrap.session_id])
    payload = _payload_of(excinfo.value)
    assert payload.get("code") == "not_found"
    assert excinfo.value.exit_code == 1


@live
def test_live_double_owner_same_request_single_winner() -> None:
    registry = LeaseRegistry()
    stub_cli = BskCli.autodetect()
    owner_a = BrowserskillRecovery(cli=stub_cli, registry=registry, owner_id="t18-owner-a")
    owner_b = BrowserskillRecovery(cli=stub_cli, registry=registry, owner_id="t18-owner-b")

    prepared = owner_a.prepare_request(name="p0-t18-race")
    first = owner_a.recoverable_start(prepared, ttl=120.0)
    try:
        with pytest.raises((EpochConflict, StaleEpoch)):
            owner_b.recoverable_start(prepared, ttl=120.0)

        # wire 层证据：同一 request 再次 start 是幂等的（返回同一 session），
        # daemon 不提供 fencing —— 唯一性只能由本地 CAS 保证。
        _code, stdout, _stderr = stub_cli.run(
            ["session", "start", "--request-id", prepared.request_id, "--no-focus"], check=False
        )
        wire = json.loads(stdout)
        assert wire["session_id"] == first.session_id
        assert registry.epoch(prepared.request_id) == 1
    finally:
        owner_a.stop(session_id=first.session_id, epoch=first.lease.epoch)


@live
def test_live_cancel_then_start_is_rejected() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-cancel")
    prepared = recovery.prepare_request(name="p0-t18-cancel")
    closed = recovery.cancel_request(prepared.request_id)
    assert closed.state == "closed"

    with pytest.raises(StartRejected) as excinfo:
        recovery.recoverable_start(prepared)
    assert excinfo.value.details["wire_code"] == "cancelled"
    assert excinfo.value.details["exit_code"] == 2
    assert recovery.registry.lease(prepared.request_id) is None


@live
def test_live_claim_semantics_are_not_fencing() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-claim")
    prepared = recovery.prepare_request(name="p0-t18-claim")

    not_prepared = recovery.claim_request(prepared.request_id)
    assert not_prepared["exit_code"] == 2
    assert not_prepared["payload"]["code"] == "cancelled"

    missing = recovery.claim_request(f"{prepared.expires_at_ms}:00000000-0000-0000-0000-000000000000")
    assert missing["exit_code"] == 1
    assert missing["payload"]["code"] == "not_found"

    bootstrap = recovery.recoverable_start(prepared, ttl=120.0)
    try:
        claimable = recovery.claim_request(prepared.request_id)
        assert claimable["exit_code"] == 0
        assert claimable["payload"]["session"]["session_id"] == bootstrap.session_id
    finally:
        recovery.stop(session_id=bootstrap.session_id, epoch=bootstrap.lease.epoch)


@live
def test_live_takeover_drops_old_epoch_result() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-owner-a")
    prepared = recovery.prepare_request(name="p0-t18-takeover")
    bootstrap = recovery.recoverable_start(prepared, ttl=120.0)
    try:
        old_epoch = bootstrap.lease.epoch
        taken = recovery.takeover(subject=prepared.request_id, expected_epoch=old_epoch, ttl=120.0)
        assert taken.epoch == old_epoch + 1

        dropped = recovery.apply_result(
            session_id=bootstrap.session_id,
            epoch=old_epoch,
            action=BrowserAction.SNAPSHOT,
            result={"text": "late-result"},
        )
        assert dropped["applied"] is False
        assert dropped["dropped_reason"] == STALE_EPOCH

        applied = recovery.apply_result(
            session_id=bootstrap.session_id,
            epoch=taken.epoch,
            action=BrowserAction.SNAPSHOT,
            result={"text": "current-result"},
        )
        assert applied["applied"] is True
        assert recovery.applied[-1]["result"] == {"text": "current-result"}
    finally:
        recovery.revoke(session_id=bootstrap.session_id, reason="live-cleanup")


@live
def test_live_revoke_makes_actions_impossible() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-revoke")
    prepared = recovery.prepare_request(name="p0-t18-revoke")
    bootstrap = recovery.recoverable_start(prepared, ttl=120.0)
    record = recovery.revoke(session_id=bootstrap.session_id, reason="live-revoked")
    assert record["revoked"] is True
    assert record["wire"]["stopped"] == [bootstrap.session_id]

    with pytest.raises(SessionRevoked):
        recovery.execute(
            session_id=bootstrap.session_id,
            epoch=bootstrap.lease.epoch,
            action=BrowserAction.SNAPSHOT,
            params={},
        )
    with pytest.raises(BskCliError) as excinfo:
        recovery.cli.run_json(["snapshot", "--session", bootstrap.session_id])
    assert _payload_of(excinfo.value).get("code") == "not_found"


@live
def test_live_tab_borrow_and_return() -> None:
    recovery = BrowserskillRecovery(cli=BskCli.autodetect(), owner_id="t18-borrow")
    prepared = recovery.prepare_request(name="p0-t18-borrow")
    bootstrap = recovery.recoverable_start(prepared, ttl=120.0)
    try:
        _code, stdout, _stderr = recovery.cli.run(
            ["tab", "list", "--session", bootstrap.session_id, "--scope", "user"], check=False
        )
        tabs = json.loads(stdout)
        items = tabs.get("tabs", []) if isinstance(tabs, dict) else tabs
        if not items:
            pytest.skip("NOT_RUN: 用户窗口当前没有可借标签页，tab borrow/return 未取证")
        tab_id = str(items[0]["tab_id"])
        borrowed = recovery.cli.run(
            ["tab", "borrow", "--session", bootstrap.session_id, tab_id, "--timeout", "5"],
            check=False,
        )
        if borrowed[0] != 0:
            pytest.skip(f"NOT_RUN: 用户未在 5s 内确认借入，输出={borrowed[1][:200]}")
        payload = json.loads(borrowed[1])
        assert payload.get("tab_id") or payload.get("borrowed") is not None
        returned = recovery.cli.run(
            ["tab", "return", "--session", bootstrap.session_id, tab_id], check=False
        )
        assert returned[0] == 0, returned[1][:400]
    finally:
        recovery.stop(session_id=bootstrap.session_id, epoch=bootstrap.lease.epoch)
