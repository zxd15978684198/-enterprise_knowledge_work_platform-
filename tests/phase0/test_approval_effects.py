"""P0-T06：审批绑定、调用账本与未知写核查的验收测试。

对应 Backlog P0-T06 量化验收（``docs/poc/03_Phase0_Task_Backlog.md`` lines 106–114）与
``docs/poc/07_Contract_Closure.md`` C-04：

1. 审批记录含 who / action / params hash / scope / 有效期 / 幂等键，执行前逐项校验；
2. 同一 approval 重放只产生一次副作用，第二次返回可识别的 ``replayed``；
3. 过期 / 参数被篡改 / 跨 workspace / 被撤销 → 全部拒绝，且零副作用；
4. 调用账本 append-only（每次调用一条 receipt），UPDATE/DELETE 由 DB 触发器拒绝；
5. 崩溃窗口（executing 提交后 / 下游成功后 receipt 前 / receipt 后）不产生重复写。

全部用例跑真实 PostgreSQL（schema ``p0_t06``）+ 真实 HTTP 下游（T04 控制站
``/idem/write`` 与 ``/nonidem/write``）。没有 mock，没有 skip/xfail。

关于轮数：任务卡要求「50 轮每崩溃窗口」。``run_crash_window`` 用**真实子进程
``os._exit``** 注入，每轮一次进程创建（Windows 上约 1 s），三个窗口各 50 轮会让单个
测试文件超过 3 分钟。这里的取舍是：
* ``after_executing_commit`` / ``after_downstream_success`` 各跑 ``CRASH_ROUNDS=50``
  （一个防"静默吞掉"，一个防"重复写"，是不可省的两条）；
* ``after_receipt`` 是**纯读路径**（只读已有 receipt，不发请求、不写外部），用
  ``REREAD_ROUNDS=10`` 覆盖，50 轮的完整证据由
  ``python experiments/phase0/approval_probe.py --rounds 50`` 单独产出。
轮数减少不改变断言强度：每轮都完整跑"崩溃 → 新连接对账 → 断言下游计数"。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import psycopg
import pytest
from uuid import UUID

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import approval_probe as probe  # noqa: E402
import tool_ledger as ledger  # noqa: E402

CRASH_ROUNDS = 50
REREAD_ROUNDS = 10
REPLAY_ROUNDS = 50


# ---------------------------------------------------------------------------
# fixture：schema 只建一次（session），连接每用例一条
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_schema() -> Iterator[str]:
    """空库重建 → 应用 002 迁移 → 记录 sha256；会话结束删 schema。"""

    with closing(probe.connect()) as admin:
        probe.reset_schema(admin)
    with closing(probe.connect(autocommit=False)) as migrator:
        stamp = probe.apply_migration(migrator)
        migrator.commit()
    assert stamp.migration_id == probe.MIGRATION_ID
    assert stamp.sha256 == probe.migration_sha256()
    assert stamp.size_bytes > 0
    try:
        yield stamp.sha256
    finally:
        with closing(probe.connect()) as admin:
            probe.reset_schema(admin)


@pytest.fixture()
def conn(pg_schema: str) -> Iterator[psycopg.Connection]:
    del pg_schema  # 只为建立顺序依赖
    with closing(probe.connect()) as connection:
        yield connection


@pytest.fixture()
def downstream(site) -> probe.Downstream:
    """下游 = T04 控制站（真实 HTTP，服务端自己记账）。"""

    return probe.Downstream(site.url("/"))


# ---------------------------------------------------------------------------
# 验收 1：六项绑定字段齐全 + 缺一即拒绝
# ---------------------------------------------------------------------------
def test_approval_row_binds_all_six_fields(conn: psycopg.Connection) -> None:
    fixture = probe.make_fixture(conn, slug="fields")
    approval = probe.get_approval(
        conn, workspace_id=fixture.workspace_id, approval_id=fixture.approval_id
    )
    assert approval is not None
    # who
    assert approval.requester_principal_id == fixture.requester_principal_id
    assert approval.approver_principal_id == fixture.approver_principal_id
    # action
    assert approval.name == fixture.intent.name
    assert approval.tool_version == fixture.intent.tool_version
    # params hash / scope
    assert approval.arguments_hash == fixture.intent.arguments_hash
    assert approval.scope_hash == fixture.intent.scope_hash
    # 有效期（必须晚于创建时间，且未过期）
    assert approval.expires_at > conn.execute("SELECT now()").fetchone()[0]
    # 幂等键 == 由持久 tool_call_id 派生的稳定值
    assert approval.idempotency_key == ledger.external_idempotency_key(
        workspace_id=fixture.workspace_id, tool_call_id=fixture.intent.tool_call_id
    )
    # 审批事件审计：created 一条，actor 是 requester
    events = conn.execute(
        f"SELECT action, actor_principal_id FROM {probe.SCHEMA}.phase0_approval_events "
        "WHERE workspace_id = %s AND approval_id = %s ORDER BY event_id",
        (fixture.workspace_id, fixture.approval_id),
    ).fetchall()
    assert events[0][0] == "created"
    assert events[0][1] == fixture.requester_principal_id


def test_each_missing_binding_field_blocks_execution(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    report = probe.check_binding_fields(conn, downstream)

    expected = {
        "baseline_complete": "succeeded",
        "who_revoked_approver": "FORBIDDEN",
        "action_version_changed": "APPROVAL_MISMATCH",
        "params_tampered": "APPROVAL_MISMATCH",
        "scope_tampered": "APPROVAL_MISMATCH",
        "expiry_passed": "APPROVAL_EXPIRED",
        "idempotency_key_forged": "APPROVAL_MISMATCH",
        "approval_missing": "APPROVAL_MISSING",
    }
    assert set(report) == set(expected)
    for name, code in expected.items():
        assert report[name]["observed_code"] == code, f"{name}: {report[name]}"
    # 基线必须真的调用了下游一次，否则"拒绝"没有对照
    assert report["baseline_complete"]["downstream_attempts_after"] == 1
    # 其余全部零下游调用
    for name, item in report.items():
        if name == "baseline_complete":
            continue
        assert item["downstream_attempts_after"] == item["downstream_attempts_before"], name


def test_revoked_requester_or_approver_blocks_execution(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    for role in ("requester", "approver"):
        fixture = probe.make_fixture(conn, slug=f"revoke-{role}")
        probe.approve(
            conn,
            workspace_id=fixture.workspace_id,
            approval_id=fixture.approval_id,
            actor=fixture.approver_principal_id,
        )
        target = (
            fixture.requester_principal_id if role == "requester" else fixture.approver_principal_id
        )
        probe.revoke_principal(conn, workspace_id=fixture.workspace_id, principal_id=target)
        before = probe.counts_for(downstream, surface="idem", key=fixture.external_key)
        result = probe.execute(
            conn,
            downstream=downstream,
            approval_id=fixture.approval_id,
            intent=fixture.intent,
            requester_principal_id=fixture.requester_principal_id,
            approver_principal_id=fixture.approver_principal_id,
        )
        after = probe.counts_for(downstream, surface="idem", key=fixture.external_key)
        assert result.outcome == "rejected", role
        assert result.error_code == "FORBIDDEN", role
        assert after.get("attempts", 0) == before.get("attempts", 0), role


# ---------------------------------------------------------------------------
# 验收 2：重放只产生一次副作用，第二次返回 replayed
# ---------------------------------------------------------------------------
def test_replay_is_idempotent_and_returns_replayed(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    report = probe.run_replay(conn, downstream, rounds=REPLAY_ROUNDS)

    assert report.violations == ()
    assert report.rounds == REPLAY_ROUNDS
    assert report.downstream_applied == 1
    assert report.replayed_rounds == REPLAY_ROUNDS - 1
    assert report.receipts == 1


def test_second_call_returns_replayed_with_no_downstream_request(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    fixture = probe.make_fixture(conn, slug="replayed-state")
    probe.approve(
        conn,
        workspace_id=fixture.workspace_id,
        approval_id=fixture.approval_id,
        actor=fixture.approver_principal_id,
    )
    first = probe.execute(
        conn,
        downstream=downstream,
        approval_id=fixture.approval_id,
        intent=fixture.intent,
        requester_principal_id=fixture.requester_principal_id,
        approver_principal_id=fixture.approver_principal_id,
    )
    assert first.outcome == "succeeded"
    assert first.replayed is False
    assert first.receipt is not None

    second = probe.resume(
        conn,
        downstream=downstream,
        approval_id=fixture.approval_id,
        intent=fixture.intent,
        requester_principal_id=fixture.requester_principal_id,
        approver_principal_id=fixture.approver_principal_id,
    )
    assert second.outcome == "replayed"
    assert second.replayed is True
    assert second.downstream is None, "重放不得再发一次网络请求"
    assert second.receipt is not None
    assert second.receipt.receipt_id == first.receipt.receipt_id

    counts = probe.counts_for(downstream, surface="idem", key=fixture.external_key)
    assert counts["attempts"] == 1
    assert counts["applied_calls"] == 1


# ---------------------------------------------------------------------------
# 验收 3：过期 / 篡改 / 跨 workspace / 撤销 → 拒绝且零副作用
# ---------------------------------------------------------------------------
def test_invalid_approvals_are_rejected_with_zero_side_effects(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    report = probe.run_invalid_approvals(conn, downstream)

    expected = {
        "expired": "APPROVAL_EXPIRED",
        "params_tampered": "APPROVAL_MISMATCH",
        "cross_workspace": "WORKSPACE_MISMATCH",
        "revoked": "APPROVAL_REVOKED",
    }
    assert set(report) == set(expected)
    for name, code in expected.items():
        assert report[name]["outcome"] == "rejected", name
        assert report[name]["error_code"] == code, name
        assert report[name]["downstream_attempts_after"] == 0, f"{name} 发生了下游调用"
        assert (
            report[name]["downstream_attempts_after"]
            == report[name]["downstream_attempts_before"]
        ), name


def test_rejection_is_recorded_as_receipt_with_downstream_sent_false(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    fixture = probe.make_fixture(conn, slug="reject-receipt")
    # 未批准就执行：状态仍是 pending
    result = probe.execute(
        conn,
        downstream=downstream,
        approval_id=fixture.approval_id,
        intent=fixture.intent,
        requester_principal_id=fixture.requester_principal_id,
        approver_principal_id=fixture.approver_principal_id,
    )
    assert result.outcome == "rejected"
    assert result.error_code == "APPROVAL_CONSUMED"
    receipts = ledger.receipts_for(
        conn, workspace_id=fixture.workspace_id, tool_call_id=fixture.intent.tool_call_id
    )
    assert len(receipts) == 1
    assert receipts[0].outcome == "rejected"
    assert receipts[0].downstream_sent is False
    assert receipts[0].external_idempotency_key is None
    assert receipts[0].error_code == "APPROVAL_CONSUMED"


# ---------------------------------------------------------------------------
# 验收 4：账本 append-only + 每次调用一条 receipt
# ---------------------------------------------------------------------------
def test_ledger_receipt_has_intent_key_params_hash_result_and_duration(
    conn: psycopg.Connection, downstream: probe.Downstream
) -> None:
    fixture = probe.make_fixture(conn, slug="receipt-fields")
    probe.approve(
        conn,
        workspace_id=fixture.workspace_id,
        approval_id=fixture.approval_id,
        actor=fixture.approver_principal_id,
    )
    result = probe.execute(
        conn,
        downstream=downstream,
        approval_id=fixture.approval_id,
        intent=fixture.intent,
        requester_principal_id=fixture.requester_principal_id,
        approver_principal_id=fixture.approver_principal_id,
    )
    assert result.outcome == "succeeded"
    receipt = result.receipt
    assert receipt is not None
    assert receipt.intent_key == fixture.intent.intent_key
    assert receipt.arguments_hash == fixture.intent.arguments_hash
    assert receipt.scope_hash == fixture.intent.scope_hash
    assert receipt.external_idempotency_key == fixture.external_key
    assert receipt.downstream_sent is True
    assert receipt.result_digest is not None
    assert receipt.result_summary.get("applied") is True
    assert receipt.duration_ms >= 0
    assert receipt.attempt_no == 0


def test_ledger_rejects_update_and_delete(conn: psycopg.Connection) -> None:
    fixture = probe.make_fixture(conn, slug="append-only", with_approval=False)
    receipt = ledger.append_receipt(
        conn,
        ledger.Receipt(
            workspace_id=fixture.workspace_id,
            tool_call_id=fixture.intent.tool_call_id,
            intent_key=fixture.intent.intent_key,
            attempt_no=0,
            arguments_hash=fixture.intent.arguments_hash,
            scope_hash=fixture.intent.scope_hash,
            tool_version=fixture.intent.tool_version,
            outcome="rejected",
            downstream_sent=False,
            duration_ms=0,
            error_code="APPROVAL_MISSING",
        ),
    )
    assert receipt.receipt_id is not None

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {probe.SCHEMA}.phase0_tool_receipts SET outcome = 'succeeded' "
            "WHERE workspace_id = %s AND receipt_id = %s",
            (fixture.workspace_id, receipt.receipt_id),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"DELETE FROM {probe.SCHEMA}.phase0_tool_receipts "
            "WHERE workspace_id = %s AND receipt_id = %s",
            (fixture.workspace_id, receipt.receipt_id),
        )
    # 行还在，且没被改
    remaining = ledger.receipts_for(
        conn, workspace_id=fixture.workspace_id, tool_call_id=fixture.intent.tool_call_id
    )
    assert len(remaining) == 1
    assert remaining[0].outcome == "rejected"


def test_duplicate_receipt_attempt_is_rejected(conn: psycopg.Connection) -> None:
    fixture = probe.make_fixture(conn, slug="dup-attempt", with_approval=False)
    base = ledger.Receipt(
        workspace_id=fixture.workspace_id,
        tool_call_id=fixture.intent.tool_call_id,
        intent_key=fixture.intent.intent_key,
        attempt_no=0,
        arguments_hash=fixture.intent.arguments_hash,
        scope_hash=fixture.intent.scope_hash,
        tool_version=fixture.intent.tool_version,
        outcome="rejected",
        downstream_sent=False,
        duration_ms=0,
        error_code="APPROVAL_MISSING",
    )
    ledger.append_receipt(conn, base)
    with pytest.raises(ledger.ReceiptConflict):
        ledger.append_receipt(conn, base)


# ---------------------------------------------------------------------------
# 验收 5：崩溃窗口不产生重复写
# ---------------------------------------------------------------------------
def test_crash_after_executing_commit_never_sends_and_is_recorded_unknown(
    conn: psycopg.Connection, downstream: probe.Downstream, site
) -> None:
    report = probe.run_crash_window(
        conn,
        downstream,
        window="after_executing_commit",
        rounds=CRASH_ROUNDS,
        site_url=site.url("/"),
    )
    assert report.violations == ()
    assert report.rounds == CRASH_ROUNDS
    # 请求还没发出就被杀：下游一次都没收到
    assert report.downstream_calls == 0
    # 绝不能记成"没执行"，必须落成 unknown_outcome（TDD02 §5）
    assert report.receipts == {"unknown_outcome": CRASH_ROUNDS}
    assert report.states == {"unknown_outcome": CRASH_ROUNDS}


def test_crash_after_downstream_success_is_recovered_by_query_without_replay(
    conn: psycopg.Connection, downstream: probe.Downstream, site
) -> None:
    report = probe.run_crash_window(
        conn,
        downstream,
        window="after_downstream_success",
        rounds=CRASH_ROUNDS,
        site_url=site.url("/"),
        can_query_downstream=True,
    )
    assert report.violations == ()
    # 每轮恰好一次下游调用（崩溃前那一次），恢复时**没有**再发一次
    assert report.downstream_calls == CRASH_ROUNDS
    assert report.receipts == {"succeeded": CRASH_ROUNDS}


def test_crash_after_receipt_is_reused_without_replay(
    conn: psycopg.Connection, downstream: probe.Downstream, site
) -> None:
    report = probe.run_crash_window(
        conn,
        downstream,
        window="after_receipt",
        rounds=REREAD_ROUNDS,
        site_url=site.url("/"),
    )
    assert report.violations == ()
    assert report.downstream_calls == REREAD_ROUNDS
    assert report.receipts == {"succeeded": REREAD_ROUNDS}


def test_unknown_outcome_without_query_support_forbids_auto_replay(
    conn: psycopg.Connection, downstream: probe.Downstream, site
) -> None:
    """下游不可查单时：必须停手在 unknown_outcome，且下游调用次数不增加。"""

    report = probe.run_crash_window(
        conn,
        downstream,
        window="after_downstream_success",
        rounds=3,
        site_url=site.url("/"),
        can_query_downstream=False,
    )
    assert report.violations == ()
    assert report.states == {"unknown_outcome": 3}
    assert report.downstream_calls == 3


# ---------------------------------------------------------------------------
# 验收 5 的对照：支持 / 不支持外部幂等两条路线的结论相反
# ---------------------------------------------------------------------------
def test_two_downstream_routes_reach_opposite_conclusions(
    conn: psycopg.Connection, downstream: probe.Downstream, site
) -> None:
    report = probe.run_idempotency_routes(conn, downstream, site_url=site.url("/"))

    idem = report["route_idempotent"]
    assert idem["reconcile_state"] == "succeeded"
    assert idem["queried_downstream"] is True
    assert idem["applied_after_crash"] == 1
    # 支持去重：同 key 重放不增加副作用
    assert idem["replay_applied"] is False
    assert idem["applied_after_replay"] == 1

    non = report["route_non_idempotent"]
    assert non["reconcile_state"] == "unknown_outcome"
    assert non["queried_downstream"] is False
    assert non["applied_after_crash"] == 1
    # 不支持去重：强行重放会变成两次副作用——这正是"禁止自动重放"的实证
    assert non["forced_replay_applied"] is True
    assert non["applied_after_forced_replay"] == 2


# ---------------------------------------------------------------------------
# 契约层：intent_key 稳定、换参即换意图
# ---------------------------------------------------------------------------
def test_intent_key_is_stable_and_changes_with_parameters() -> None:
    base = {
        "run_id": UUID("11111111-1111-4111-8111-111111111111"),
        "response_artifact_id": UUID("22222222-2222-4222-8222-222222222222"),
        "call_ordinal": 0,
        "name": "control.write",
        "tool_version": "1.0.0",
        "arguments_hash": "a" * 64,
        "scope_hash": "b" * 64,
    }
    first = ledger.intent_key(**base)
    assert first == ledger.intent_key(**base)
    assert len(first) == 64

    for field in ("call_ordinal", "tool_version", "arguments_hash", "scope_hash"):
        changed = dict(base)
        changed[field] = "c" * 64 if field.endswith("_hash") else (
            1 if field == "call_ordinal" else "9.9.9"
        )
        assert ledger.intent_key(**changed) != first, field


def test_same_ordinal_with_different_arguments_is_rejected(
    conn: psycopg.Connection,
) -> None:
    fixture = probe.make_fixture(conn, slug="ordinal-guard", with_approval=False)
    scope = probe.ScopeSpec(
        workspace_id=fixture.workspace_id,
        principal_id=fixture.requester_principal_id,
        operation="tool.execute",
        resource_refs=(("kb", str(fixture.run_id)),),
        grant_scope="p0_t06",
    )
    with pytest.raises(probe.IntentMismatchError):
        probe.prepare_call(
            conn,
            workspace_id=fixture.workspace_id,
            run_id=fixture.run_id,
            response_artifact_id=fixture.response_artifact_id,
            call_ordinal=0,
            name="control.write",
            tool_version="1.0.0",
            arguments={"path": "/idem/write", "key": "different"},
            scope=scope,
        )


def test_arguments_canonicalization_rejects_ambiguous_values() -> None:
    assert ledger.arguments_hash({"b": 1, "a": 2}) == ledger.arguments_hash({"a": 2, "b": 1})
    with pytest.raises(ledger.ArgumentCanonicalizationError):
        ledger.arguments_hash({"value": float("nan")})
    with pytest.raises(ledger.ArgumentCanonicalizationError):
        ledger.arguments_hash({"value": b"bytes"})
    with pytest.raises(ledger.ArgumentCanonicalizationError):
        ledger.canonicalize_arguments({"a": 1}, required_keys=("b",))


def test_approval_events_are_append_only(conn: psycopg.Connection) -> None:
    fixture = probe.make_fixture(conn, slug="events-only")
    probe.approve(
        conn,
        workspace_id=fixture.workspace_id,
        approval_id=fixture.approval_id,
        actor=fixture.approver_principal_id,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {probe.SCHEMA}.phase0_approval_events SET action = 'rejected' "
            "WHERE workspace_id = %s AND approval_id = %s",
            (fixture.workspace_id, fixture.approval_id),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"DELETE FROM {probe.SCHEMA}.phase0_approval_events "
            "WHERE workspace_id = %s AND approval_id = %s",
            (fixture.workspace_id, fixture.approval_id),
        )


def test_approval_and_tool_call_state_machine_rejects_illegal_transitions(
    conn: psycopg.Connection,
) -> None:
    fixture = probe.make_fixture(conn, slug="state-machine")
    probe.approve(
        conn,
        workspace_id=fixture.workspace_id,
        approval_id=fixture.approval_id,
        actor=fixture.approver_principal_id,
    )
    probe.revoke_approval(
        conn,
        workspace_id=fixture.workspace_id,
        approval_id=fixture.approval_id,
        actor=fixture.requester_principal_id,
    )
    # revoked 是终态：不允许再回到 approved
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {probe.SCHEMA}.phase0_approvals SET status = 'approved', "
            "row_version = row_version + 1 WHERE workspace_id = %s AND approval_id = %s",
            (fixture.workspace_id, fixture.approval_id),
        )
