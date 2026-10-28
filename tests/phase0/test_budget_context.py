"""P0-T20 验收测试：上下文预算裁剪 + 并发安全的成本账本（真实 PostgreSQL）。

覆盖（对照 Backlog P0-T20 与 TDD02 §6/§7）：
1. 四区段切分与占用；超预算按 retrieval → history → tools 的固定顺序裁剪。
2. 同一输入输出恒定（fingerprint / kept / dropped 全重复），乱序输入等价。
3. 工具 schema 计入预算；工具对（assistant call + tool result）永不拆散，孤立 tool 消息 0。
4. 大检索片段先摘要后丢弃，摘要与丢弃都留痕（计数 + 原因）。
5. 整数 micro-units；取不到价格时 ``cost_source="unavailable"``、按预留最大值保守
   记账、绝不当 0、绝不自动释放。
6. 预算耗尽 → 明确错误码；100 轮最后一格额度双竞争无透支；DB CHECK 兜底防直改透支。
7. 50 次重复结算只记账一次；崩溃（子进程硬退出）后可从库恢复已消耗额度。

隔离：只用 schema ``p0_t20``；DSN 走 ``scope.read_dsn()``（环境变量或 .env.example）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import budget_ledger as bl  # noqa: E402
import context_budget as cb  # noqa: E402
import contracts  # noqa: E402

LEDGER_TABLES = (
    "phase0_budget_ledgers",
    "phase0_budget_reservations",
    "phase0_budget_settlements",
    "phase0_budget_events",
    "phase0_schema_migrations",
)


# ---------------------------------------------------------------------------
# PG fixtures（conftest.py 属 T04，DB fixture 必须写在本文件）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def budget_schema() -> Iterator[str]:
    conn = bl.connect()
    try:
        bl.reset_schema(conn)
        conn.commit()
        digest = bl.apply_migration(conn)
        conn.commit()
    finally:
        conn.close()
    assert len(digest) == 64
    yield bl.SCHEMA
    conn = bl.connect()
    try:
        bl.reset_schema(conn)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def dsn(budget_schema: str) -> str:
    from scope import read_dsn

    return read_dsn()


@pytest.fixture()
def conn(budget_schema: str) -> Iterator[psycopg.Connection[Any]]:
    connection = bl.connect()
    try:
        yield connection
    finally:
        if not connection.closed:
            connection.close()


@pytest.fixture()
def scope() -> contracts.WorkspaceScope:
    principal = contracts.synthesize_principal(
        workspace_id=uuid4(),
        principal_id=uuid4(),
        membership_subject_ref="subject-t20",
        kind="service",
        role="owner",
    )
    return contracts.issue_scope(principal, policy_snapshot_id=uuid4())


@pytest.fixture()
def run_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _limits(**overrides: Any) -> bl.BudgetLimits:
    params: dict[str, Any] = {
        "tokens": 1000,
        "cost_microunits": 1_000_000,
        "pages": 100,
        "bytes": 1_000_000,
        "concurrency": 2,
        "currency": "USD",
    }
    params.update(overrides)
    return bl.BudgetLimits(**params)


def _ledger(
    conn: psycopg.Connection[Any],
    *,
    scope: contracts.WorkspaceScope,
    run_id: UUID,
    limits: bl.BudgetLimits | None = None,
) -> bl.BudgetLedger:
    ledger = bl.BudgetLedger(conn)
    ledger.ensure(scope=scope, run_id=run_id, limits=limits or _limits())
    return ledger


def _row(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row


def _context_input(**overrides: Any) -> cb.ContextInput:
    params: dict[str, Any] = {
        "system": (cb.SystemBlock(key="role", text="你是受控 agent，只做被授权的操作。"),),
        "tools": (
            cb.ToolSchema(
                name="site.read_counter",
                version="1.0.0",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
        "history": (
            cb.HistoryMessage(role="user", text="统计一下控制站的计数。", turn_index=0),
            cb.HistoryMessage(role="assistant", text="好的，我先查询。", turn_index=1),
        ),
        "retrieval": (
            cb.RetrievalChunk(source_ref="doc-a", text="控制站计数说明" * 5, score=9, rank=1),
        ),
    }
    params.update(overrides)
    return cb.ContextInput(**params)


def _context_limits(**overrides: Any) -> cb.ContextLimits:
    params: dict[str, Any] = {
        "max_context_tokens": 4096,
        "reserved_output_tokens": 128,
        "keep_last_turns": 1,
    }
    params.update(overrides)
    return cb.ContextLimits(**params)


# ===========================================================================
# 一、上下文预算
# ===========================================================================
def test_context_sections_are_split_with_per_section_usage() -> None:
    prepared = cb.ContextBuilder(_context_limits()).prepare(_context_input())

    assert [usage.section for usage in prepared.sections] == [
        "system",
        "tools",
        "history",
        "retrieval",
    ]
    usages = {usage.section: usage for usage in prepared.sections}
    assert usages["system"].items == 1
    assert usages["tools"].items == 1
    assert usages["history"].items == 2
    assert usages["retrieval"].items == 1
    assert usages["tools"].tokens > cb.TOOL_OVERHEAD_TOKENS
    assert sum(usage.tokens for usage in prepared.sections) == prepared.total_tokens
    assert prepared.total_tokens <= prepared.limit_tokens
    assert prepared.limit_tokens == 4096 - 128
    assert prepared.reserved_output_tokens == 128


def test_tool_schema_tokens_are_counted_in_budget() -> None:
    without_tools = cb.ContextBuilder(_context_limits()).prepare(
        _context_input(tools=())
    )
    with_tools = cb.ContextBuilder(_context_limits()).prepare(_context_input())
    tools_usage = with_tools.section_usage("tools")

    assert tools_usage.items == 1
    assert tools_usage.tokens > 0
    assert with_tools.total_tokens > without_tools.total_tokens
    tool_item = next(item for item in with_tools.items if item.section == "tools")
    assert "site.read_counter" in tool_item.text
    assert "parameters" in tool_item.text


def test_truncation_drops_retrieval_before_touching_history() -> None:
    retrieval = tuple(
        cb.RetrievalChunk(source_ref=f"doc-{index}", text="检索内容" * 40, score=index, rank=index)
        for index in range(3)
    )
    context = _context_input(retrieval=retrieval)
    full = cb.estimate_context_tokens(context)
    limits = _context_limits(max_context_tokens=full - 60, reserved_output_tokens=0)

    prepared = cb.ContextBuilder(limits).prepare(context)

    assert prepared.total_tokens <= prepared.limit_tokens
    assert prepared.dropped_count("history") == 0
    assert prepared.dropped_count("retrieval") >= 1
    history_items = [item for item in prepared.items if item.section == "history"]
    assert len(history_items) == 2
    # 丢弃按分数从低到高：保留下来的必然是分数更高的片段。
    kept_scores = [
        chunk.score
        for chunk in retrieval
        if f"retrieval/{chunk.source_ref}" in prepared.item_ids("retrieval")
    ]
    dropped_ids = {record.item_id for record in prepared.dropped if record.section == "retrieval"}
    dropped_scores = [chunk.score for chunk in retrieval if chunk.source_ref in dropped_ids]
    if kept_scores and dropped_scores:
        # 先丢最低分：保留片段的最低分不得低于被丢弃片段的最高分。
        assert min(kept_scores) >= max(dropped_scores)


def test_truncation_is_deterministic_and_order_independent_for_retrieval() -> None:
    retrieval = tuple(
        cb.RetrievalChunk(source_ref=f"doc-{index}", text="内容" * (50 + index), score=index % 2, rank=index)
        for index in range(6)
    )
    context = _context_input(retrieval=retrieval)
    full = cb.estimate_context_tokens(context)
    limits = _context_limits(max_context_tokens=full - 120, reserved_output_tokens=0)

    fingerprints = set()
    for _ in range(30):
        prepared = cb.ContextBuilder(limits).prepare(context)
        fingerprints.add(prepared.fingerprint)
    assert len(fingerprints) == 1

    baseline = cb.ContextBuilder(limits).prepare(context)
    shuffled = cb.ContextBuilder(limits).prepare(
        _context_input(retrieval=tuple(reversed(retrieval)))
    )
    assert shuffled.fingerprint == baseline.fingerprint
    assert shuffled.item_ids() == baseline.item_ids()
    assert [record.reason for record in shuffled.dropped] == [
        record.reason for record in baseline.dropped
    ]

    changed = cb.ContextBuilder(limits).prepare(
        _context_input(
            retrieval=tuple(
                cb.RetrievalChunk(
                    source_ref=chunk.source_ref,
                    text=chunk.text + "!",
                    score=chunk.score,
                    rank=chunk.rank,
                )
                for chunk in retrieval
            )
        )
    )
    assert changed.fingerprint != baseline.fingerprint


def test_tool_pairs_are_never_split_and_no_orphan_tool_messages() -> None:
    history: list[cb.HistoryMessage] = []
    for turn in range(10):
        history.append(cb.HistoryMessage(role="user", text=f"第 {turn} 轮问题 " + "x" * 40, turn_index=turn))
        history.append(
            cb.HistoryMessage(
                role="assistant",
                text="调用工具",
                turn_index=turn,
                tool_call_id=f"call-{turn}",
            )
        )
        history.append(
            cb.HistoryMessage(
                role="tool",
                text="工具结果 " + "y" * 40,
                turn_index=turn,
                tool_call_id=f"call-{turn}",
                tool_name="site.read_counter",
            )
        )
    assert len(history) == 30

    context = _context_input(history=tuple(history))
    full = cb.estimate_context_tokens(context)
    limits = _context_limits(max_context_tokens=full // 2, reserved_output_tokens=0, keep_last_turns=2)

    prepared = cb.ContextBuilder(limits).prepare(context)

    assert prepared.dropped_count("history") >= 1
    assert prepared.orphan_tool_messages() == 0
    retained_by_turn: dict[int, set[str]] = {}
    for item_id in prepared.item_ids("history"):
        parts = item_id.split("/")
        turn = int(parts[1])
        retained_by_turn.setdefault(turn, set()).add(parts[2])
    for turn, roles in retained_by_turn.items():
        assert roles == {"user", "assistant", "tool"}, (turn, roles)
    retained_turns = set(retained_by_turn)
    dropped_turns = {
        int(record.item_id.split("/")[1])
        for record in prepared.dropped
        if record.section == "history"
    }
    assert retained_turns.isdisjoint(dropped_turns)
    assert prepared.total_tokens <= prepared.limit_tokens


def test_large_retrieval_is_summarized_then_dropped_with_records() -> None:
    big_text = "检索到的长文档内容。" * 200  # 约 2200 字符，远超摘要阈值
    context = _context_input(
        system=(),
        tools=(),
        history=(),
        retrieval=(cb.RetrievalChunk(source_ref="doc-big", text=big_text, score=1, rank=1),),
    )
    limits = _context_limits(max_context_tokens=30, reserved_output_tokens=0)

    prepared = cb.ContextBuilder(limits).prepare(context)

    assert len(prepared.summaries) == 1
    summary = prepared.summaries[0]
    assert summary.source_ref == "doc-big"
    assert summary.original_tokens > summary.summary_tokens > 0
    assert len(summary.summary_ref) == 16
    assert prepared.section_usage("retrieval").items == 0  # 摘要后仍超预算 → 整段丢弃
    dropped = [record for record in prepared.dropped if record.section == "retrieval"]
    assert len(dropped) == 1
    assert dropped[0].reason == "SUMMARIZED_THEN_DROPPED"
    assert dropped[0].tokens > 0
    assert prepared.dropped_tokens() >= dropped[0].tokens
    assert prepared.total_tokens <= prepared.limit_tokens


def test_summary_failure_degrades_and_drops() -> None:
    def failing_summarizer(text: str, max_tokens: int) -> str:
        raise RuntimeError("summarizer unavailable")

    big = cb.RetrievalChunk(source_ref="doc-fail", text="长内容" * 400, score=1, rank=1)
    context = _context_input(
        system=(), tools=(), history=(), retrieval=(big,)
    )
    builder = cb.ContextBuilder(
        _context_limits(max_context_tokens=20, reserved_output_tokens=0),
        summarizer=failing_summarizer,
    )
    prepared = builder.prepare(context)

    assert prepared.degraded is True
    assert prepared.section_usage("retrieval").items == 0
    dropped = [record for record in prepared.dropped if record.section == "retrieval"]
    assert dropped[0].reason == "SUMMARY_FAILED"
    assert prepared.total_tokens <= prepared.limit_tokens


def test_system_section_is_hard_floor_and_raises_budget_exceeded() -> None:
    huge_system = (cb.SystemBlock(key="role", text="系统约束" * 2000),)
    context = _context_input(system=huge_system, history=(), retrieval=())
    with pytest.raises(cb.ContextBudgetExceededError) as excinfo:
        cb.ContextBuilder(_context_limits(max_context_tokens=64, reserved_output_tokens=0)).prepare(
            context
        )
    assert excinfo.value.code == "CONTEXT_BUDGET_EXCEEDED"
    assert excinfo.value.details["section"] == "system"
    assert excinfo.value.details["over_by"] > 0


def test_reserved_output_reduces_effective_input_budget() -> None:
    context = _context_input()
    full = cb.estimate_context_tokens(context)
    tight = _context_limits(max_context_tokens=full + 4, reserved_output_tokens=0)
    reserved = _context_limits(max_context_tokens=full + 4, reserved_output_tokens=40)

    assert cb.ContextBuilder(tight).prepare(context).dropped == ()
    prepared = cb.ContextBuilder(reserved).prepare(context)
    assert prepared.dropped_count() > 0
    assert prepared.limit_tokens == full + 4 - 40
    assert prepared.total_tokens <= prepared.limit_tokens


def test_optional_tools_are_trimmed_last_and_required_kept() -> None:
    required_tool = cb.ToolSchema(
        name="site.read_counter", version="1.0.0", parameters={"type": "object"}, required=True
    )
    optional_tool = cb.ToolSchema(
        name="site.error_probe",
        version="1.0.0",
        parameters={
            "type": "object",
            "properties": {
                "fault": {"type": "string", "enum": ["http_500", "http_429", "timeout"]},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30000},
                "retry": {"type": "boolean"},
                "note": {"type": "string", "maxLength": 200},
            },
            "required": ["fault"],
            "additionalProperties": False,
        },
        required=False,
    )
    context = _context_input(
        tools=(required_tool, optional_tool),
        history=(),
        retrieval=(cb.RetrievalChunk(source_ref="doc-x", text="内容" * 60, score=1, rank=1),),
    )
    full = cb.estimate_context_tokens(context)
    limits = _context_limits(max_context_tokens=full - 150, reserved_output_tokens=0)

    prepared = cb.ContextBuilder(limits).prepare(context)

    reasons = [record.reason for record in prepared.dropped]
    assert "OPTIONAL_TOOL" in reasons
    assert reasons.index("LOW_SCORE") < reasons.index("OPTIONAL_TOOL")
    assert "site.read_counter" in prepared.item_ids("tools")
    assert "site.error_probe" not in prepared.item_ids("tools")


def test_empty_context_is_stable() -> None:
    empty = cb.ContextInput()
    prepared = cb.ContextBuilder(_context_limits()).prepare(empty)
    again = cb.ContextBuilder(_context_limits()).prepare(empty)

    assert prepared.total_tokens == 0
    assert prepared.items == ()
    assert prepared.dropped == ()
    assert prepared.fingerprint == again.fingerprint
    assert prepared.orphan_tool_messages() == 0


# ===========================================================================
# 二、预算账本：迁移、预留、结算
# ===========================================================================
def test_migration_creates_budget_tables_and_is_not_silently_idempotent(
    budget_schema: str, conn: psycopg.Connection[Any]
) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name",
        (budget_schema,),
    ).fetchall()
    assert {row[0] for row in rows} == set(LEDGER_TABLES)

    ledger_rows = conn.execute(
        f"SELECT migration_id, filename, sha256 FROM {budget_schema}.phase0_schema_migrations"
    ).fetchall()
    assert ledger_rows == [
        (
            bl.MIGRATION_ID,
            bl.MIGRATION_PATH.name,
            bl.migration_sha256(),
        )
    ]
    conn.rollback()

    with bl.connect() as duplicate:
        with pytest.raises(psycopg.errors.DuplicateSchema):
            bl.apply_migration(duplicate)
        duplicate.rollback()


def test_reserve_and_settle_use_integer_micro_units(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id)
    reservation = ledger.reserve(
        scope=scope,
        run_id=run_id,
        purpose_key="step-1",
        maximum=bl.Cost(tokens=200, cost_microunits=1200),
        kind="model",
    )
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.reserved.tokens == 200
    assert state.reserved.cost_microunits == 1200
    assert isinstance(state.reserved.cost_microunits, int)
    conn.rollback()  # 结束只读事务，后续 settle 才会真正提交（不被 savepoint 语义包裹）

    settlement = ledger.settle(
        reservation_id=reservation.reservation_id,
        actual=bl.Cost(tokens=150, cost_microunits=900),
        settlement_key="settle-1",
        cost_source="estimated",
    )
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.spent.tokens == 150
    assert state.spent.cost_microunits == 900
    assert state.reserved.tokens == 0
    assert state.unsettled_cost_microunits == 0
    assert state.frozen is False
    conn.rollback()
    assert settlement.provisional is False
    assert settlement.effective_cost_microunits == 900
    assert isinstance(settlement.effective_cost_microunits, int)

    report = ledger.reconcile(scope=scope, run_id=run_id)
    report.require_match()
    assert report.matches is True
    assert report.settlements == 1
    conn.rollback()


def test_operation_results_match_persisted_rows(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    """调用方拿到的 DTO（内存视角）必须逐字段等于 p0_t20 落库行。"""

    ledger = _ledger(conn, scope=scope, run_id=run_id)
    reservation = ledger.reserve(
        scope=scope,
        run_id=run_id,
        purpose_key="dto-check",
        maximum=bl.Cost(tokens=120, cost_microunits=700),
        kind="model",
    )
    row = _row(
        conn,
        f"SELECT reserved_tokens, reserved_cost_microunits FROM {bl.SCHEMA}.phase0_budget_ledgers "
        "WHERE workspace_id = %s AND run_id = %s",
        (scope.workspace_id, run_id),
    )
    assert row == (reservation.maximum.tokens, reservation.maximum.cost_microunits)
    conn.rollback()

    settlement = ledger.settle(
        reservation_id=reservation.reservation_id,
        actual=bl.Cost(tokens=90, cost_microunits=500),
        settlement_key="dto-settle",
        cost_source="provider_reported",
    )
    row = _row(
        conn,
        f"SELECT spent_tokens, spent_cost_microunits, reserved_tokens "
        f"FROM {bl.SCHEMA}.phase0_budget_ledgers WHERE workspace_id = %s AND run_id = %s",
        (scope.workspace_id, run_id),
    )
    assert row == (settlement.actual.tokens, settlement.actual.cost_microunits, 0)
    status = _row(
        conn,
        f"SELECT status FROM {bl.SCHEMA}.phase0_budget_reservations WHERE reservation_id = %s",
        (reservation.reservation_id,),
    )
    assert status == ("settled",)
    conn.rollback()


def test_budget_exceeded_rejects_with_code_and_dimension(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(
        conn, scope=scope, run_id=run_id, limits=_limits(tokens=100, cost_microunits=1000)
    )
    ledger.reserve(
        scope=scope, run_id=run_id, purpose_key="first", maximum=bl.Cost(tokens=60), kind="step"
    )
    before = _row(
        conn,
        f"SELECT count(*) FROM {bl.SCHEMA}.phase0_budget_reservations",
    )[0]
    conn.rollback()

    with pytest.raises(bl.BudgetExceededError) as excinfo:
        ledger.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key="second",
            maximum=bl.Cost(tokens=60),
            kind="step",
        )
    assert excinfo.value.code == "BUDGET_EXCEEDED"
    assert excinfo.value.dimension == "tokens"

    after = _row(
        conn,
        f"SELECT count(*) FROM {bl.SCHEMA}.phase0_budget_reservations",
    )[0]
    assert after == before  # 失败不留下半行预留
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.reserved.tokens == 60
    conn.rollback()

    with pytest.raises(bl.BudgetExceededError) as cost_exceeded:
        ledger.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key="cost-too-big",
            maximum=bl.Cost(cost_microunits=1001),
            kind="model",
        )
    assert cost_exceeded.value.dimension == "cost_microunits"
    conn.rollback()


def test_fifty_duplicate_settlements_count_once(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id)
    reservation = ledger.reserve(
        scope=scope, run_id=run_id, purpose_key="dup", maximum=bl.Cost(tokens=100), kind="model"
    )
    actual = bl.Cost(tokens=100)
    settlements = [
        ledger.settle(
            reservation_id=reservation.reservation_id,
            actual=actual,
            settlement_key="one-and-only",
            cost_source="provider_reported",
        )
        for _ in range(50)
    ]
    assert all(item.settlement_key == "one-and-only" for item in settlements)

    count = _row(
        conn,
        f"SELECT count(*) FROM {bl.SCHEMA}.phase0_budget_settlements "
        "WHERE workspace_id = %s AND run_id = %s",
        (scope.workspace_id, run_id),
    )[0]
    assert count == 1
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.spent.tokens == 100
    conn.rollback()

    with pytest.raises(bl.SettlementConflictError):
        ledger.settle(
            reservation_id=reservation.reservation_id,
            actual=bl.Cost(tokens=99),
            settlement_key="one-and-only",
            cost_source="provider_reported",
        )
    conn.rollback()


def test_unknown_cost_is_never_counted_as_zero(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    limits = _limits(tokens=1000, cost_microunits=5000)
    ledger = _ledger(conn, scope=scope, run_id=run_id, limits=limits)
    reservation = ledger.reserve(
        scope=scope,
        run_id=run_id,
        purpose_key="model-unknown-cost",
        maximum=bl.Cost(tokens=100, cost_microunits=1000),
        kind="model",
    )

    settlement = ledger.settle_unavailable(
        reservation_id=reservation.reservation_id, settlement_key="settle-unknown"
    )

    assert settlement.cost_source == "unavailable"
    assert settlement.provisional is True
    assert settlement.effective_cost_microunits == 1000  # 预留最大值，不是 0
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.spent.cost_microunits == 1000
    assert state.unsettled_cost_microunits == 1000
    assert state.reserved.cost_microunits == 0
    conn.rollback()

    # 未释放：剩余额度只剩 4000，再要 4001 必须拒绝（证明没有「当 0 免费」）。
    with pytest.raises(bl.BudgetExceededError) as excinfo:
        ledger.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key="after-unknown",
            maximum=bl.Cost(cost_microunits=4001),
            kind="model",
        )
    assert excinfo.value.dimension == "cost_microunits"
    conn.rollback()

    report = ledger.reconcile(scope=scope, run_id=run_id)
    report.require_match()
    assert report.unsettled_cost_microunits == 1000
    conn.rollback()
    print(
        f"[unknown-cost] reserved_max=1000 spent_cost={state.spent.cost_microunits} "
        f"unsettled={state.unsettled_cost_microunits} provisional=true zeroed=false"
    )


def test_release_returns_reservation_and_blocks_late_settlement(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id)
    reservation = ledger.reserve(
        scope=scope, run_id=run_id, purpose_key="cancel-me", maximum=bl.Cost(tokens=100), kind="step"
    )
    released = ledger.release(reservation_id=reservation.reservation_id, reason="CANCELLED_BEFORE_SEND")
    assert released.status == "released"

    state = ledger.state(scope=scope, run_id=run_id)
    assert state.reserved.tokens == 0
    assert state.spent.tokens == 0
    assert ledger.release(reservation_id=reservation.reservation_id).status == "released"
    conn.rollback()

    with pytest.raises(bl.ReservationStateError) as excinfo:
        ledger.settle(
            reservation_id=reservation.reservation_id,
            actual=bl.Cost(tokens=10),
            settlement_key="late",
            cost_source="estimated",
        )
    assert excinfo.value.code == "RESERVATION_STATE_INVALID"
    conn.rollback()


def test_overage_records_real_value_and_freezes_future_reservations(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id, limits=_limits(tokens=1000, cost_microunits=5000))
    reservation = ledger.reserve(
        scope=scope,
        run_id=run_id,
        purpose_key="overrun",
        maximum=bl.Cost(tokens=100, cost_microunits=1000),
        kind="model",
    )
    settlement = ledger.settle(
        reservation_id=reservation.reservation_id,
        actual=bl.Cost(tokens=150, cost_microunits=2500),
        settlement_key="overrun-settle",
        cost_source="provider_reported",
    )
    assert settlement.overage is True
    assert settlement.overage_cost_microunits == 1500

    state = ledger.state(scope=scope, run_id=run_id)
    assert state.spent.tokens == 150
    assert state.spent.cost_microunits == 2500
    assert state.frozen is True
    assert state.freeze_reason == "ACTUAL_EXCEEDS_RESERVATION"
    conn.rollback()

    with pytest.raises(bl.BudgetFrozenError) as excinfo:
        ledger.reserve(
            scope=scope, run_id=run_id, purpose_key="after-freeze", maximum=bl.Cost(tokens=1)
        )
    assert excinfo.value.code == "BUDGET_FROZEN"
    conn.rollback()


def test_deadline_blocks_new_reservations(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(
        conn,
        scope=scope,
        run_id=run_id,
        limits=_limits(deadline_at=datetime.now(UTC) - timedelta(seconds=1)),
    )
    with pytest.raises(bl.DeadlineExceededError) as excinfo:
        ledger.reserve(
            scope=scope, run_id=run_id, purpose_key="too-late", maximum=bl.Cost(tokens=10)
        )
    assert excinfo.value.code == "DEADLINE_EXCEEDED"
    assert excinfo.value.dimension == "deadline"
    conn.rollback()


def test_price_snapshot_gives_integer_estimate_or_explicitly_unavailable() -> None:
    price = bl.PriceSnapshot(
        model_profile_id="profile-a",
        input_microunits_per_million=1_000_000,
        output_microunits_per_million=2_000_000,
    )
    estimate = bl.estimate_cost(input_tokens=3, output_tokens=2, price=price)
    assert estimate.cost_source == "estimated"
    assert estimate.cost_microunits == 3 + 4  # 整数向上取整，无浮点
    assert isinstance(estimate.cost_microunits, int)

    unavailable = bl.estimate_cost(input_tokens=3, output_tokens=2, price=None)
    assert unavailable.cost_microunits is None
    assert unavailable.cost_source == "unavailable"
    with pytest.raises(bl.PricingUnavailableError):
        unavailable.require_cost()


def test_model_and_tool_call_gates_reject_before_any_call(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(
        conn,
        scope=scope,
        run_id=run_id,
        limits=_limits(tokens=100, cost_microunits=1000, concurrency=1),
    )
    calls: list[str] = []  # 假 provider：只有真正发起调用才会追加

    reservation = ledger.reserve_model_call(
        scope=scope,
        run_id=run_id,
        purpose_key="model-1",
        prompt_tokens=30,
        max_output_tokens=30,
        price=None,
        max_cost_microunits=10,
    )
    calls.append("model-1")  # 预留成功后才允许调用
    assert reservation.kind == "model"
    assert reservation.maximum.tokens == 60
    assert reservation.maximum.cost_microunits == 10

    with pytest.raises(bl.BudgetExceededError) as model_exceeded:
        ledger.reserve_model_call(
            scope=scope,
            run_id=run_id,
            purpose_key="model-2",
            prompt_tokens=30,
            max_output_tokens=30,
            price=None,
            max_cost_microunits=10,
        )
    assert model_exceeded.value.dimension == "tokens"
    assert calls == ["model-1"]  # 第二次预算不足 → 没有调用发生

    tool = ledger.reserve_tool_call(
        scope=scope, run_id=run_id, purpose_key="tool-1", concurrency=1, max_bytes=100
    )
    assert tool.kind == "tool"
    with pytest.raises(bl.BudgetExceededError) as tool_exceeded:
        ledger.reserve_tool_call(
            scope=scope, run_id=run_id, purpose_key="tool-2", concurrency=1, max_bytes=100
        )
    assert tool_exceeded.value.dimension == "concurrency"
    assert calls == ["model-1"]

    ledger.release(reservation_id=tool.reservation_id, reason="TOOL_DONE")
    ledger.reserve_tool_call(
        scope=scope, run_id=run_id, purpose_key="tool-3", concurrency=1, max_bytes=100
    )
    conn.rollback()

    with pytest.raises(bl.PricingUnavailableError):
        ledger.reserve_model_call(
            scope=scope,
            run_id=run_id,
            purpose_key="model-no-price",
            prompt_tokens=1,
            max_output_tokens=1,
            price=None,
            max_cost_microunits=None,
        )
    conn.rollback()


def test_concurrent_last_quota_never_overdraws(
    dsn: str, conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id, limits=_limits(tokens=100, concurrency=0))
    local = threading.local()
    opened: list[psycopg.Connection[Any]] = []
    lock = threading.Lock()

    def worker(slot: int, round_index: int, barrier: threading.Barrier) -> tuple[str, str]:
        connection = getattr(local, "conn", None)
        if connection is None or connection.closed:
            connection = bl.connect(dsn=dsn)
            local.conn = connection
            with lock:
                opened.append(connection)
        worker_ledger = bl.BudgetLedger(connection)
        barrier.wait()
        try:
            reservation = worker_ledger.reserve(
                scope=scope,
                run_id=run_id,
                purpose_key=f"race-{round_index}-{slot}",
                maximum=bl.Cost(tokens=60),
            )
        except bl.BudgetExceededError as exc:
            return ("exceeded", exc.dimension or "")
        return ("ok", str(reservation.reservation_id))

    rounds = 100
    max_reserved = 0
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for round_index in range(rounds):
                barrier = threading.Barrier(2)
                futures = [pool.submit(worker, slot, round_index, barrier) for slot in (0, 1)]
                results = [future.result() for future in futures]
                outcomes = [item[0] for item in results]
                assert outcomes.count("ok") == 1, (round_index, results)
                assert outcomes.count("exceeded") == 1
                state = ledger.state(scope=scope, run_id=run_id)
                assert state.reserved.tokens == 60  # 赢家占住 60，输家 0
                assert state.spent.tokens == 0
                max_reserved = max(max_reserved, state.reserved.tokens)
                conn.rollback()
                winner = next(item[1] for item in results if item[0] == "ok")
                ledger.release(reservation_id=UUID(winner), reason="RACE_CLEANUP")
                state = ledger.state(scope=scope, run_id=run_id)
                assert state.reserved.tokens == 0
                assert state.spent.tokens == 0
                assert state.used_concurrency == 0
                conn.rollback()
    finally:
        for connection in opened:
            if not connection.closed:
                connection.close()

    report = ledger.reconcile(scope=scope, run_id=run_id)
    report.require_match()
    assert report.matches is True
    conn.rollback()
    print(
        f"[budget-race] rounds={rounds} winners={rounds} rejected={rounds} "
        f"max_reserved_after_round={max_reserved} overdraft=0 limit_tokens=100 per_attempt=60"
    )


def test_ledger_recovers_spent_state_after_reconnect(
    dsn: str, conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id, limits=_limits(tokens=500, cost_microunits=5000))
    reservation = ledger.reserve(
        scope=scope,
        run_id=run_id,
        purpose_key="recover-1",
        maximum=bl.Cost(tokens=100, cost_microunits=800),
        kind="model",
    )
    ledger.settle(
        reservation_id=reservation.reservation_id,
        actual=bl.Cost(tokens=70, cost_microunits=600),
        settlement_key="recover-settle",
        cost_source="estimated",
    )
    before = ledger.state(scope=scope, run_id=run_id)
    conn.close()

    with bl.connect(dsn=dsn) as fresh:
        fresh_ledger = bl.BudgetLedger(fresh)
        after = fresh_ledger.state(scope=scope, run_id=run_id)
        assert after.spent == before.spent
        assert after.reserved == before.reserved
        assert after.row_version >= before.row_version
        report = fresh_ledger.reconcile(scope=scope, run_id=run_id)
        report.require_match()
        fresh.rollback()
        # 恢复后仍可继续用剩余额度。
        remaining = after.available().tokens
        assert remaining == 500 - 70
        fresh_ledger.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key="recover-2",
            maximum=bl.Cost(tokens=remaining),
        )
        fresh.rollback()


def test_db_backstop_blocks_direct_overdraft(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    _ledger(conn, scope=scope, run_id=run_id, limits=_limits(tokens=100))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {bl.SCHEMA}.phase0_budget_ledgers "
            "SET reserved_tokens = limit_tokens + 1, row_version = row_version + 1 "
            "WHERE workspace_id = %s AND run_id = %s",
            (scope.workspace_id, run_id),
        )
    conn.rollback()


def test_reconcile_detects_tampered_ledger(
    conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    ledger = _ledger(conn, scope=scope, run_id=run_id, limits=_limits(tokens=1000))
    reservation = ledger.reserve(
        scope=scope, run_id=run_id, purpose_key="tamper-1", maximum=bl.Cost(tokens=10)
    )
    ledger.settle(
        reservation_id=reservation.reservation_id,
        actual=bl.Cost(tokens=10),
        settlement_key="tamper-settle",
        cost_source="estimated",
    )
    conn.execute(
        f"UPDATE {bl.SCHEMA}.phase0_budget_ledgers "
        "SET spent_tokens = spent_tokens + 1, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND run_id = %s",
        (scope.workspace_id, run_id),
    )
    conn.commit()

    report = ledger.reconcile(scope=scope, run_id=run_id)
    assert report.matches is False
    with pytest.raises(bl.LedgerMismatchError):
        report.require_match()
    conn.rollback()


_CRASH_CHILD = r"""
import os
import sys
from uuid import UUID

sys.path.insert(0, sys.argv[1])
import budget_ledger as bl
import contracts

scope = contracts.issue_scope(
    contracts.synthesize_principal(
        workspace_id=UUID(sys.argv[2]),
        principal_id=UUID(sys.argv[3]),
        membership_subject_ref="subject-t20-crash",
        kind="service",
        role="owner",
    ),
    policy_snapshot_id=UUID(sys.argv[4]),
)
run_id = UUID(sys.argv[5])
conn = bl.connect()
ledger = bl.BudgetLedger(conn)
ledger.ensure(
    scope=scope,
    run_id=run_id,
    limits=bl.BudgetLimits(tokens=1000, cost_microunits=10000, pages=10, bytes=1000, concurrency=1),
)
reservation = ledger.reserve(
    scope=scope,
    run_id=run_id,
    purpose_key="crash-1",
    maximum=bl.Cost(tokens=100, cost_microunits=500),
    kind="model",
)
ledger.settle(
    reservation_id=reservation.reservation_id,
    actual=bl.Cost(tokens=80, cost_microunits=400),
    settlement_key="crash-settle",
    cost_source="estimated",
)
os._exit(9)  # 已提交的账本事实必须能从库里恢复
"""


def test_crash_after_settle_recovers_from_database(
    dsn: str, conn: psycopg.Connection[Any], scope: contracts.WorkspaceScope, run_id: UUID
) -> None:
    env = dict(os.environ)
    env["PHASE0_PG_DSN"] = dsn
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_CHILD,
            str(PROJECT),
            str(scope.workspace_id),
            str(scope.principal_id),
            str(scope.policy_snapshot_id),
            str(run_id),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 9, (completed.returncode, completed.stderr)

    ledger = bl.BudgetLedger(conn)
    state = ledger.state(scope=scope, run_id=run_id)
    assert state.spent.tokens == 80
    assert state.spent.cost_microunits == 400
    assert state.reserved.tokens == 0
    assert state.frozen is False
    report = ledger.reconcile(scope=scope, run_id=run_id)
    report.require_match()
    assert report.matches is True
    assert report.settlements == 1
    conn.rollback()
    print("[crash-recovery] spent_tokens=80 spent_cost=400 reserved=0 recovered_from_pg=true")
