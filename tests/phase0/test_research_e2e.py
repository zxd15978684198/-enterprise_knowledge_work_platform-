"""P0-T28：Research 全链（目标→DAG→三通道→审核→发布→引用）端到端与负例。

覆盖提示词验收：
* 30 个研究任务覆盖正路径与审批拒绝/预算/浏览器断线/非法计划；
* 三读取通道（search/http/js）与用户浏览器快照；
* 已发布候选 100% 有快照/时间/通道/许可/证据/审核；
* 禁止/越权/无证据发布 0；精确重复不重索引；两轮变化/未变化状态正确；
* 审批后撤权、批准后 DB 故障、取消、重复恢复、秘密 canary。

真实限域：使用可注入 ``ModelPort`` 替身，真实 chat 模型链路 NOT_RUN；BSK 真机 NOT_RUN。
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

PHASE0 = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

import budget_ledger  # noqa: E402
import contracts  # noqa: E402
import research_flow as rf  # noqa: E402
import research_probe as rp  # noqa: E402
import task_scheduler as ts  # noqa: E402
from control_site import ControlSite  # noqa: E402

# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _dsn() -> str:
    return rp.read_dsn()


def _schema_exists(conn: psycopg.Connection[Any], name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (name,)
    ).fetchone()
    conn.commit()
    return row is not None


def _ensure_module_migration(dsn: str, module: Any) -> None:
    conn = module.connect(dsn=dsn)
    try:
        if _schema_exists(conn, module.SCHEMA):
            return
        module.apply_migration(conn)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def installed() -> Iterator[str]:
    dsn = _dsn()
    admin = rp.connect(dsn=dsn, autocommit=True)
    rp.reset_schema(admin)
    admin.close()
    conn = rp.connect(dsn=dsn)
    digest = rp.apply_schema(conn)
    conn.close()
    rp.ensure_checkpoint_schema(dsn=dsn)
    _ensure_module_migration(dsn, ts)
    _ensure_module_migration(dsn, budget_ledger)
    yield digest
    admin = rp.connect(dsn=dsn, autocommit=True)
    rp.reset_schema(admin)
    admin.close()


@pytest.fixture()
def site(installed: str) -> Iterator[ControlSite]:
    control = ControlSite(host="127.0.0.1", port=0, allow_fault_injection=True)
    control.start()
    try:
        yield control
    finally:
        control.stop()


@pytest.fixture()
def workspace(installed: str) -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def principal(workspace: uuid.UUID) -> contracts.Principal:
    return contracts.Principal(
        workspace_id=workspace,
        principal_id=uuid.uuid4(),
        membership_subject_ref="user:t28",
        kind="human",
        role="owner",
        status="active",
        membership_revision=1,
        _trust=contracts.backend_constructor_token(),
    )


def _context(
    *,
    workspace: uuid.UUID,
    principal: contracts.Principal,
    site: ControlSite,
    goal: str,
    model: Any = None,
    budget_mode: str = "normal",
) -> rf.ResearchContext:
    return rf.make_context(
        dsn=_dsn(),
        workspace_id=workspace,
        run_id=uuid.uuid4(),
        goal=goal,
        principal=principal,
        site=site,
        model=model,
        budget_mode=budget_mode,
    )


def _approve(ctx: rf.ResearchContext) -> rf.ResearchOutcome:
    return rf.resume_research(
        ctx,
        {
            "decision": "approve",
            "approver_principal_id": str(ctx.principal.principal_id),
            "reason": "tests",
        },
    )


def _read(ctx: rf.ResearchContext) -> rp.RunState:
    with rp.connect(dsn=ctx.dsn) as conn:
        return rp.read_run(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)


def _candidates(ctx: rf.ResearchContext) -> list[rp.CandidateRecord]:
    with rp.connect(dsn=ctx.dsn) as conn:
        return rp.list_candidates(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)


def _job_rows(workspace: uuid.UUID) -> list[dict[str, Any]]:
    with rp.connect(dsn=_dsn()) as conn:
        rows = conn.execute(
            "SELECT * FROM p0_t28.phase0_jobs WHERE workspace_id = %s ORDER BY job_id",
            (workspace,),
        ).fetchall()
        conn.commit()
        return [dict(row) for row in rows]


def _events(ctx: rf.ResearchContext) -> list[rp.EventRecord]:
    with rp.connect(dsn=ctx.dsn) as conn:
        return rp.list_events(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)


def _sources(ctx: rf.ResearchContext) -> list[rp.SourceRecord]:
    with rp.connect(dsn=ctx.dsn) as conn:
        return rp.list_sources(conn, workspace_id=ctx.workspace_id, run_id=ctx.run_id)


def _citations(ctx: rf.ResearchContext) -> list[dict[str, Any]]:
    with rp.connect(dsn=ctx.dsn) as conn:
        rows = conn.execute(
            "SELECT c.candidate_id, c.evidence_id, c.ordinal, e.quote, e.quote_sha256,"
            " e.source_id FROM p0_t28.phase0_citations c"
            " JOIN p0_t28.phase0_evidence e"
            " ON e.workspace_id = c.workspace_id AND e.evidence_id = c.evidence_id"
            " WHERE c.workspace_id = %s AND c.run_id = %s ORDER BY c.ordinal",
            (ctx.workspace_id, ctx.run_id),
        ).fetchall()
        conn.commit()
        return [dict(row) for row in rows]


class _ChangedModel(rf.DeterministicResearchModel):
    """第二轮内容有变化：结论末尾追加补充说明（引用标记保持不变）。"""

    def compose_answer(self, goal: str, quotes: Any) -> str:
        return super().compose_answer(goal, quotes) + "（第二轮补充：以最新流程为准。）"


# ---------------------------------------------------------------------------
# 1. schema 与迁移台账
# ---------------------------------------------------------------------------


def test_schema_ledger_ddl_digest_and_reapply(installed: str) -> None:
    with rp.connect(dsn=_dsn()) as conn:
        rows = conn.execute(
            "SELECT migration_id, sha256 FROM p0_t28.phase0_t28_migrations"
        ).fetchall()
        conn.commit()
    assert len(rows) == 1
    assert rows[0]["migration_id"] == rp.MIGRATION_ID
    assert rows[0]["sha256"] == installed == rp.migration_sha256()
    with rp.connect(dsn=_dsn()) as conn:
        with pytest.raises(rp.SchemaAlreadyApplied):
            rp.apply_schema(conn)
        conn.rollback()


# ---------------------------------------------------------------------------
# 2. 正路径：DAG → 三通道 → 中断 → 批准 → 发布
# ---------------------------------------------------------------------------


def test_run_executes_three_channels_and_waits_for_approval(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="差旅报销上限与发票要求是什么",
    )
    outcome = rf.run_research(ctx)
    assert outcome.interrupted is True
    assert outcome.status == "waiting_approval"
    assert outcome.published == 0
    assert outcome.candidates == 1
    assert set(outcome.channels) == {"search", "http", "js", "user_browser"}
    run = _read(ctx)
    assert run.status == "waiting_approval"
    assert run.terminal_at is None
    sources = _sources(ctx)
    assert {source.channel for source in sources} == {
        "search", "http", "js", "user_browser"
    }
    for source in sources:
        assert source.license
        assert source.body_sha256 and len(source.body_sha256) == 64
        assert isinstance(source.snapshot, dict) and source.snapshot
    assert len(_events(ctx)) >= 8


def test_resume_approves_and_publishes_in_one_transaction(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="费用报销流程需要几级审批",
    )
    assert rf.run_research(ctx).interrupted is True
    outcome = _approve(ctx)
    assert outcome.status == "succeeded"
    assert outcome.published == 1
    assert outcome.terminal_count == 1
    assert outcome.citations == 3
    run = _read(ctx)
    assert run.status == "succeeded"
    assert run.answer_kind in rp.ANSWER_KINDS
    assert run.result_hash and len(run.result_hash) == 64
    candidates = _candidates(ctx)
    assert len(candidates) == 1
    assert candidates[0].status == "published"
    assert candidates[0].published_at is not None
    assert candidates[0].job_id is not None
    jobs = _job_rows(workspace)
    assert [job["status"] for job in jobs] == ["pending"]
    assert jobs[0]["candidate_id"] == candidates[0].candidate_id
    terminal = [e for e in _events(ctx) if e.event_type in rp.TERMINAL_EVENT_TYPES]
    assert [e.event_type for e in terminal] == ["completed"]
    assert terminal[0].payload["result_hash"] == run.result_hash


def test_repeated_run_and_resume_are_idempotent(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="外部链接访问的安全策略要求",
    )
    rf.run_research(ctx)
    _approve(ctx)
    events_after_first = len(_events(ctx))
    again = rf.run_research(ctx)
    assert again.replayed is True
    assert again.terminal_count == 1
    again2 = rf.resume_research(
        ctx, {"decision": "approve", "approver_principal_id": str(principal.principal_id)}
    )
    assert again2.replayed is True
    assert len(_events(ctx)) == events_after_first
    counters = rf.read_external_writes(site)
    prefix = f"{rf.EXTERNAL_IDEM_PREFIX}:{ctx.run_id}:"
    ours = [key for key in counters if key.startswith(prefix)]
    assert len(ours) == 1
    assert counters[ours[0]]["applied_calls"] == 1
    assert counters[ours[0]]["attempts"] <= 2


# ---------------------------------------------------------------------------
# 3. 审批负例：拒绝、撤权、未审批
# ---------------------------------------------------------------------------


def test_rejected_approval_keeps_research_out_of_business_facts(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="审计日志与原始文档快照保留多久",
    )
    assert rf.run_research(ctx).interrupted is True
    outcome = rf.resume_research(
        ctx,
        {"decision": "reject", "approver_principal_id": str(principal.principal_id),
         "reason": "policy:no_external_write"},
    )
    assert outcome.status == "succeeded"
    assert outcome.published == 0
    candidates = _candidates(ctx)
    assert [candidate.status for candidate in candidates] == ["rejected"]
    assert candidates[0].rejection_reason == "policy:no_external_write"
    assert candidates[0].job_id is None
    assert len(_job_rows(workspace)) == 0
    assert rf.read_external_writes(site) == {}


def test_approval_revoked_after_decision_blocks_publish(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="知识库导入失败时活动版本是否受影响",
    )
    first = rf.run_research(ctx)
    assert first.interrupted is True
    approval_id = uuid.UUID(str(first.interactions[0]["approval_id"]))
    with rp.connect(dsn=_dsn()) as conn:
        rp.decide_approval(
            conn, workspace_id=workspace, approval_id=approval_id, decision="approved",
            approver_principal_id=principal.principal_id, reason="initial",
        )
        conn.commit()
    with rp.connect(dsn=_dsn()) as conn:
        rp.revoke_approval(
            conn, workspace_id=workspace, approval_id=approval_id, reason="withdrawn"
        )
        conn.commit()
    blocked: rf.ResearchOutcome | None = None
    try:
        blocked = rf.resume_research(
            ctx,
            {"decision": "approve",
             "approver_principal_id": str(principal.principal_id)},
        )
    except rp.ResearchError:
        blocked = None
    assert blocked is None or blocked.published == 0
    run = _read(ctx)
    assert run.status in {"succeeded", "failed"}
    assert [candidate.status for candidate in _candidates(ctx)] != ["published"]
    assert "indexed" not in [event.event_type for event in _events(ctx)]
    assert len(_job_rows(workspace)) == 0
    assert rf.read_external_writes(site) == {}


def test_publish_requires_approval_reference() -> None:
    workspace = uuid.uuid4()
    run_id = uuid.uuid4()
    with rp.connect(dsn=_dsn()) as conn:
        rp.create_run(
            conn, workspace_id=workspace, run_id=run_id, goal="手工构造的发布负例"
        )
        source = rp.record_source(
            conn, workspace_id=workspace, run_id=run_id, task_id="t2-http", channel="http",
            url="http://127.0.0.1/static/page.html", body="手工来源正文",
            license_name="CC-BY-4.0", snapshot={"channel": "http"},
        )
        evidence = rp.record_evidence(
            conn, workspace_id=workspace, run_id=run_id, source_id=source.source_id,
            quote="手工来源正文", locator="p:1",
        )
        candidate = rp.build_candidate(
            conn, workspace_id=workspace, run_id=run_id, task_id="t5-analyze",
            title="负例候选", body="负例候选正文", evidence_ids=[evidence.evidence_id],
            requires_approval=True,
        )
        conn.commit()
    with rp.connect(dsn=_dsn()) as conn:
        with pytest.raises(rp.ApprovalRequired):
            rp.publish_candidate(
                conn, workspace_id=workspace, run_id=run_id,
                candidate_id=candidate.candidate_id,
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# 4. 预算、计划、通道失败与取消
# ---------------------------------------------------------------------------


def test_tight_budget_stops_before_any_publication(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="差旅与费用报销的区别", budget_mode="tight",
    )
    outcome = rf.run_research(ctx)
    assert outcome.status == "failed"
    assert outcome.error_code == "BUDGET_EXCEEDED"
    assert outcome.published == 0
    assert len(_job_rows(workspace)) == 0
    assert rf.read_external_writes(site) == {}
    run = _read(ctx)
    assert run.status == "failed"
    assert [e.event_type for e in _events(ctx) if e.event_type in rp.TERMINAL_EVENT_TYPES] == [
        "failed"
    ]


def test_invalid_plan_fails_before_side_effects(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    model = rf.ScriptedResearchModel(
        plan={"task_graph_revision": 1, "nodes": "not-a-list"}
    )
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="安全白名单是否允许携带凭据", model=model,
    )
    outcome = rf.run_research(ctx)
    assert outcome.status == "failed"
    assert outcome.error_code == "PLAN_INVALID"
    assert len(_sources(ctx)) == 0
    assert outcome.published == 0
    assert rf.read_external_writes(site) == {}


def test_broken_js_channel_fails_without_publication(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="原始文档快照的归档周期",
    )
    ctx.js = rf._BrokenChannel(site)  # type: ignore[assignment]
    outcome = rf.run_research(ctx)
    assert outcome.status == "failed"
    assert outcome.error_code == "SOURCE_UNAVAILABLE"
    assert outcome.published == 0
    assert len(_job_rows(workspace)) == 0


def test_cancel_after_interrupt_keeps_single_terminal(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="差旅住宿标准是多少",
    )
    assert rf.run_research(ctx).interrupted is True
    result = rf.cancel_research(ctx, reason="user_cancelled")
    assert result.run.status == "cancelled"
    assert result.replayed is False
    after = rf.resume_research(
        ctx, {"decision": "approve", "approver_principal_id": str(principal.principal_id)}
    )
    assert after.replayed is True
    assert after.published == 0
    terminal = [e for e in _events(ctx) if e.event_type in rp.TERMINAL_EVENT_TYPES]
    assert [e.event_type for e in terminal] == ["cancelled"]


# ---------------------------------------------------------------------------
# 5. 事务原子性与数据库不变式
# ---------------------------------------------------------------------------


def test_finalize_writes_terminal_only_on_commit() -> None:
    workspace = uuid.uuid4()
    run_id = uuid.uuid4()
    with rp.connect(dsn=_dsn()) as conn:
        rp.create_run(conn, workspace_id=workspace, run_id=run_id, goal="终态原子性负例")
        rp.set_run_status(conn, workspace_id=workspace, run_id=run_id, status="running")
        conn.commit()
    with rp.connect(dsn=_dsn()) as conn:
        rp.finalize_run(
            conn, workspace_id=workspace, run_id=run_id,
            answer_kind="insufficient_evidence", content="未提交的终态",
        )
        inside = rp.read_run(conn, workspace_id=workspace, run_id=run_id)
        assert inside.status == "succeeded"
        conn.rollback()
    with rp.connect(dsn=_dsn()) as conn:
        rolled_back = rp.read_run(conn, workspace_id=workspace, run_id=run_id)
        assert rolled_back.status == "running"
        assert rp.count_events(
            conn, workspace_id=workspace, run_id=run_id, event_type="completed"
        ) == 0
        rp.finalize_run(
            conn, workspace_id=workspace, run_id=run_id,
            answer_kind="insufficient_evidence", content="已提交的终态",
        )
        conn.commit()
    with rp.connect(dsn=_dsn()) as conn:
        committed = rp.read_run(conn, workspace_id=workspace, run_id=run_id)
        assert committed.status == "succeeded"
        assert rp.count_events(
            conn, workspace_id=workspace, run_id=run_id, event_type="completed"
        ) == 1
        with pytest.raises(psycopg.errors.UniqueViolation):
            rp.append_event(
                conn, workspace_id=workspace, run_id=run_id, event_type="completed",
                payload={"duplicate": True},
            )
        conn.rollback()


def test_candidate_without_citation_cannot_commit() -> None:
    workspace = uuid.uuid4()
    run_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    with rp.connect(dsn=_dsn()) as conn:
        rp.create_run(conn, workspace_id=workspace, run_id=run_id, goal="无引用候选负例")
        rp.set_run_status(conn, workspace_id=workspace, run_id=run_id, status="running")
        conn.execute(
            "INSERT INTO p0_t28.phase0_candidates (workspace_id, run_id, candidate_id,"
            " task_id, dedupe_key, content_sha256, title, body, requires_approval, status)"
            " VALUES (%s, %s, %s, 't5-analyze', 'manual:nocite', %s, '无引用候选',"
            " '无引用正文', false, 'pending')",
            (workspace, run_id, candidate_id, hashlib.sha256(b"nocite").hexdigest()),
        )
        conn.commit()
    with rp.connect(dsn=_dsn()) as conn:
        result = rp.publish_candidate(
            conn, workspace_id=workspace, run_id=run_id, candidate_id=candidate_id,
            require_approval=False,
        )
        assert result.candidate.status == "published"
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.commit()
        conn.rollback()
    with rp.connect(dsn=_dsn()) as conn:
        rows = conn.execute(
            "SELECT status FROM p0_t28.phase0_candidates WHERE workspace_id = %s"
            " AND candidate_id = %s",
            (workspace, candidate_id),
        ).fetchall()
        conn.commit()
    assert rows[0]["status"] == "pending"


def test_events_and_citations_are_append_only(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="差旅报销上限与发票要求是什么",
    )
    rf.run_research(ctx)
    _approve(ctx)
    with rp.connect(dsn=_dsn()) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE p0_t28.phase0_events SET payload = '{}'::jsonb"
                " WHERE workspace_id = %s AND run_id = %s",
                (workspace, ctx.run_id),
            )
        conn.rollback()
    with rp.connect(dsn=_dsn()) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "DELETE FROM p0_t28.phase0_citations WHERE workspace_id = %s AND run_id = %s",
                (workspace, ctx.run_id),
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# 6. 精确重复不重索引 / 两轮变化
# ---------------------------------------------------------------------------


def test_exact_duplicate_content_reuses_index_job(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    goal = "差旅住宿标准是多少"
    first = _context(workspace=workspace, principal=principal, site=site, goal=goal)
    rf.run_research(first)
    _approve(first)
    first_jobs = _job_rows(workspace)
    assert len(first_jobs) == 1
    second = _context(workspace=workspace, principal=principal, site=site, goal=goal)
    rf.run_research(second)
    _approve(second)
    jobs = _job_rows(workspace)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == first_jobs[0]["job_id"]
    assert jobs[0]["dedupe_key"] == first_jobs[0]["dedupe_key"]
    assert len(rf.read_external_writes(site)) == 2  # 两个 run 各自一次外部写，Job 仍只有一条


def test_changed_content_creates_new_job(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    goal = "原始文档快照的归档周期"
    first = _context(workspace=workspace, principal=principal, site=site, goal=goal)
    rf.run_research(first)
    _approve(first)
    second = _context(
        workspace=workspace, principal=principal, site=site, goal=goal,
        model=_ChangedModel(),
    )
    rf.run_research(second)
    _approve(second)
    jobs = _job_rows(workspace)
    assert len(jobs) == 2
    assert len({job["dedupe_key"] for job in jobs}) == 2


# ---------------------------------------------------------------------------
# 7. 引用回源与可追溯性
# ---------------------------------------------------------------------------


def test_citations_trace_back_to_authorized_sources(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="差旅报销上限与发票要求是什么",
    )
    rf.run_research(ctx)
    _approve(ctx)
    citations = _citations(ctx)
    assert len(citations) == 3
    sources = {source.source_id: source for source in _sources(ctx)}
    for citation in citations:
        quote = citation["quote"]
        assert hashlib.sha256(quote.encode("utf-8")).hexdigest() == citation["quote_sha256"]
        source = sources[citation["source_id"]]
        assert source.license
        assert citation["quote"] in json.dumps(source.snapshot, ensure_ascii=False) or any(
            quote in candidate.body for candidate in _candidates(ctx)
        )


def test_traceability_report_is_complete(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="外部链接访问的安全策略要求",
    )
    rf.run_research(ctx)
    _approve(ctx)
    report = rf.verify_traceability(_dsn(), workspace_id=workspace, run_id=ctx.run_id)
    assert report["published_candidates"] == 1
    assert report["complete_chains"] == 1
    assert report["incomplete"] == 0
    row = report["rows"][0]
    assert row["problems"] == []
    assert row["evidence_count"] == 3
    assert row["approval_id"] and row["job_id"] and row["job_dedupe_key"]
    assert set(row["channels"]) <= {"search", "http", "js", "user_browser"}
    assert row["licenses"]


def test_event_payloads_contain_no_credentials(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    ctx = _context(
        workspace=workspace, principal=principal, site=site,
        goal="费用报销流程需要几级审批",
    )
    rf.run_research(ctx)
    _approve(ctx)
    blob = json.dumps([event.payload for event in _events(ctx)], ensure_ascii=False)
    forbidden = ("phase0-local-only", "PHASE0_PG_DSN", "password=", "Authorization")
    assert not [token for token in forbidden if token in blob]


# ---------------------------------------------------------------------------
# 8. 30 任务批量分类
# ---------------------------------------------------------------------------


def test_batch_thirty_tasks_classifies_all_outcomes(
    site: ControlSite, workspace: uuid.UUID, principal: contracts.Principal
) -> None:
    report = rf.run_batch(
        dsn=_dsn(), site=site, workspace_id=workspace, principal=principal
    )
    summary = report["summary"]
    assert summary == {
        "succeeded": 12,
        "approval_rejected": 6,
        "budget_exceeded": 4,
        "browser_down": 4,
        "plan_invalid": 4,
        "unexpected": 0,
    }
    assert len(report["rows"]) == 30
    assert {row["actual"] for row in report["rows"]} == {
        "succeeded",
        "approval_rejected",
        "budget_exceeded",
        "browser_down",
        "plan_invalid",
    }
    assert all(row["expect"] == row["actual"] for row in report["rows"])
    approved = [row for row in report["rows"] if row["actual"] == "succeeded"]
    assert all(row["published"] == 1 for row in approved)
    assert all(row["external_applied"] == 1 for row in approved)
    assert all(
        row["external_applied"] == 0
        for row in report["rows"]
        if row["actual"] not in ("succeeded",)
    )
    chains = []
    for row in approved:
        chain = rf.verify_traceability(
            _dsn(), workspace_id=workspace, run_id=uuid.UUID(str(row["run_id"]))
        )
        assert chain["published_candidates"] == 1
        assert chain["complete_chains"] == 1
        assert chain["incomplete"] == 0
        assert chain["rows"][0]["problems"] == []
        chains.append(chain)
    assert len(chains) == 12
    evidence_path = rp.EVIDENCE_PATH.parent / "research-batch.json"
    written = rf.write_evidence(report, path=evidence_path)
    assert written.exists()
