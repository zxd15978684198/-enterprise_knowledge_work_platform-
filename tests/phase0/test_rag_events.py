"""P0-T26 验收测试：LangGraph RAG、可核验引用、终态一次事务提交与事件一致性。

覆盖任务卡（docs/poc/03 P0-T26）与提示词注入项：
* 授权检索（跨 workspace / 旧版本不可见）、无证据拒答；
* Message + Run + terminal 同一事务（回滚后三者都不在）；
* 50 次重复 finalize 只产生一个 terminal；迟到/旧 generation 写入被拒；
* 部分 token 后 DB 失败不出现伪 completed；模型中断 → 新 generation；
* 取消、游标过期/越权、P1 续传 CAPABILITY_UNSUPPORTED；
* 伪引用/越权引用被拒并落审计；Rerank 故障降级；
* 100 例质量实测（引用准确率、延迟）。
"""

from __future__ import annotations

import sys
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHASE0 = ROOT / "experiments" / "phase0"
if str(PHASE0) not in sys.path:
    sys.path.insert(0, str(PHASE0))

import events_probe as ep  # noqa: E402
import rag_probe as rp  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402


@pytest.fixture(scope="session")
def installed() -> str:
    admin = ep.connect(autocommit=True)
    ep.reset_schema(admin)
    conn = ep.connect()
    try:
        sha = ep.apply_schema(conn)
        conn.commit()
        yield sha
    finally:
        conn.close()
        ep.reset_schema(admin)
        admin.close()


@dataclass(frozen=True, slots=True)
class Fixture:
    workspace: uuid.UUID
    principal: uuid.UUID
    member: uuid.UUID
    outsider: uuid.UUID
    kb: uuid.UUID
    kb_private: uuid.UUID
    document: uuid.UUID
    other_workspace: uuid.UUID
    other_principal: uuid.UUID
    other_kb: uuid.UUID


@pytest.fixture
def seeded(installed: str) -> Fixture:
    with closing(ep.connect()) as conn, conn.transaction():
        rp.reset_corpus(conn)
        workspace = rp.create_workspace(conn, slug="t26-main")
        principal = rp.add_principal(conn, workspace_id=workspace, role="owner")
        member = rp.add_principal(conn, workspace_id=workspace)
        outsider = rp.add_principal(conn, workspace_id=workspace)
        kb = rp.add_kb(conn, workspace_id=workspace, name="handbook")
        kb_private = rp.add_kb(conn, workspace_id=workspace, name="private")
        rp.grant_read(conn, workspace_id=workspace, kb_id=kb, principal_id=principal)
        rp.grant_read(conn, workspace_id=workspace, kb_id=kb, principal_id=member)
        rp.grant_read(
            conn, workspace_id=workspace, kb_id=kb_private, principal_id=principal
        )
        other_workspace = rp.create_workspace(conn, slug="t26-other")
        other_principal = rp.add_principal(
            conn, workspace_id=other_workspace, role="owner"
        )
        other_kb = rp.add_kb(conn, workspace_id=other_workspace, name="handbook")
        rp.grant_read(
            conn,
            workspace_id=other_workspace,
            kb_id=other_kb,
            principal_id=other_principal,
        )
        document = rp.add_document(
            conn,
            workspace_id=workspace,
            kb_id=kb,
            texts=(
                "报销流程规定三十日内提交电子发票，财务五个工作日内审核。",
                "差旅住宿标准为一线城市每晚六百元。",
            ),
        )
        rp.add_document(
            conn,
            workspace_id=other_workspace,
            kb_id=other_kb,
            texts=("外部合作方结算周期为验收后十个工作日。",),
        )
    return Fixture(
        workspace=workspace,
        principal=principal,
        member=member,
        outsider=outsider,
        kb=kb,
        kb_private=kb_private,
        document=document.document_id,
        other_workspace=other_workspace,
        other_principal=other_principal,
        other_kb=other_kb,
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _dsn() -> str:
    from scope import read_dsn

    return read_dsn()


def _conversation(workspace: uuid.UUID, principal: uuid.UUID) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    with closing(ep.connect()) as conn, conn.transaction():
        ep.ensure_conversation(
            conn,
            workspace_id=workspace,
            principal_id=principal,
            conversation_id=conversation_id,
        )
    return conversation_id


def _run_rag(
    fx: Fixture,
    *,
    query: str = "报销流程多少日内提交电子发票？",
    kb_ids: list[uuid.UUID] | None = None,
    principal: uuid.UUID | None = None,
    model: object | None = None,
    sink_factory: object | None = None,
    reranker: object | None = None,
) -> rp.RagRunResult:
    return rp.run_rag(
        _dsn(),
        workspace_id=fx.workspace if principal is None else fx.workspace,
        principal_id=principal if principal is not None else fx.principal,
        conversation_id=_conversation(fx.workspace, fx.principal),
        query=query,
        kb_ids=kb_ids if kb_ids is not None else [fx.kb],
        model=model,  # type: ignore[arg-type]
        sink_factory=sink_factory,
        reranker=reranker,  # type: ignore[arg-type]
    )


def _read_run(run: ep.RunState) -> ep.RunState:
    with closing(ep.connect()) as conn, conn.transaction():
        return ep.read_run(conn, run.workspace_id, run.run_id)


def _events(run: ep.RunState, *, event_type: str | None = None) -> list[ep.RunEvent]:
    with closing(ep.connect()) as conn, conn.transaction():
        return ep.list_events(
            conn, run.workspace_id, run.run_id, event_type=event_type
        )


def _message(run: ep.RunState, *, role: str = "assistant") -> ep.MessageState | None:
    with closing(ep.connect()) as conn, conn.transaction():
        messages = ep.list_messages(conn, run.workspace_id, run.run_id)
    return next((item for item in messages if item.role == role), None)


# ---------------------------------------------------------------------------
# 迁移与台账
# ---------------------------------------------------------------------------
def test_schema_ledger_and_reapply(installed: str) -> None:
    assert len(installed) == 64
    with closing(ep.connect()) as conn:
        with conn.transaction():
            row = conn.execute(
                f"SELECT migration_id, sha256 FROM {ep.SCHEMA}.phase0_schema_migrations"
            ).fetchall()
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (ep.SCHEMA,),
        ).fetchall()
    assert [item["migration_id"] for item in row] == [ep.MIGRATION_ID]
    assert row[0]["sha256"] == installed == ep.schema_sha256()
    names = {item["table_name"] for item in tables}
    required = {
        "phase0_runs",
        "phase0_messages",
        "phase0_run_events",
        "phase0_message_citations",
        "phase0_citation_rejections",
        "phase0_event_cursors",
        "phase0_chunks",
    }
    assert required <= names
    with closing(ep.connect()) as conn, pytest.raises(ep.SchemaAlreadyApplied):
        ep.apply_schema(conn)


# ---------------------------------------------------------------------------
# 一次事务：Message + Run + terminal
# ---------------------------------------------------------------------------
def test_completed_commits_message_run_and_one_terminal(seeded: Fixture) -> None:
    result = _run_rag(seeded)
    assert result.status == "completed"
    assert result.answer_kind in ("grounded", "grounded_partial")
    assert result.citations, "可答问题必须带证据引用"
    assert result.terminal is not None and result.terminal.event_type == "completed"
    message = _message(result.run)
    assert message is not None
    assert message.status == "final"
    assert message.finalized_at is not None
    assert result.run.terminal_at is not None
    assert message.content == result.answer_text
    assert (message.result_hash or "") == (result.run.result_hash or "") != ""
    completed = _events(result.run, event_type="completed")
    assert len(completed) == 1
    assert completed[0].generation == 1
    assert completed[0].payload["message_id"] == str(message.message_id)
    tokens = _events(result.run, event_type="token")
    assert len(tokens) == result.persisted_tokens > 0
    offsets = [int(item.payload["offset"]) for item in tokens]
    assert offsets == sorted(offsets)
    assert "".join(str(item.payload["text"]) for item in tokens) == message.content


def test_finalize_is_single_transaction_until_commit(seeded: Fixture) -> None:
    """回滚最终事务后：Run 仍在运行、草稿仍 partial、无 terminal。"""

    conversation = _conversation(seeded.workspace, seeded.principal)
    with closing(ep.connect()) as conn:
        with conn.transaction():
            run, _user = ep.create_run(
                conn,
                workspace_id=seeded.workspace,
                principal_id=seeded.principal,
                conversation_id=conversation,
                query="报销流程多少日内提交？",
            )
            message = ep.ensure_assistant_message(
                conn, workspace_id=seeded.workspace, run_id=run.run_id, generation=1
            )
        with closing(ep.connect()) as stream_conn:
            stream_conn.execute("BEGIN")
            message = ep.persist_tokens(
                stream_conn,
                run=run,
                message=message,
                generation=1,
                texts=["根据资料[[1]]，三十日内提交。"],
            )
            stream_conn.commit()
        with closing(ep.connect()) as lookup, lookup.transaction():
            real_chunk = lookup.execute(
                f"SELECT chunk_id FROM {ep.SCHEMA}.phase0_chunks "
                "WHERE workspace_id = %s AND document_id = %s LIMIT 1",
                (seeded.workspace, seeded.document),
            ).fetchone()
        # 模拟最终事务开始后回滚
        final = ep.connect()
        final.execute("BEGIN")
        ep.finalize(
            final,
            run=run,
            message=message,
            generation=1,
            content=message.content,
            citations=(
                ep.CitationSpec(
                    chunk_id=real_chunk["chunk_id"],
                    document_id=seeded.document,
                    index_version=1,
                    rank=1,
                    quote_hash="0" * 64,
                ),
            ),
            answer_kind="grounded",
        )
        final.rollback()
        final.close()
    with closing(ep.connect()) as conn, conn.transaction():
        stored_run = ep.read_run(conn, run.workspace_id, run.run_id)
        stored_message = ep.read_message(conn, run.workspace_id, message.message_id)
        terminal = ep.read_terminal(conn, run.workspace_id, run.run_id)
        completed_count = ep.count_events(
            conn, run.workspace_id, run.run_id, event_type="completed"
        )
    assert stored_run.status == "running"
    assert stored_run.terminal_at is None
    assert stored_message.status == "partial"
    assert stored_message.finalized_at is None
    assert terminal is None
    assert completed_count == 0


def test_finalize_is_committed_atomically(seeded: Fixture) -> None:
    """提交后：Run 终态、Message final、terminal 事件、引用一次性都在。"""

    result = _run_rag(seeded)
    with closing(ep.connect()) as conn, conn.transaction():
        run = ep.read_run(conn, seeded.workspace, result.run.run_id)
        messages = ep.list_messages(conn, seeded.workspace, result.run.run_id)
        terminal = ep.read_terminal(conn, seeded.workspace, result.run.run_id)
        event_count = ep.count_events(conn, seeded.workspace, result.run.run_id)
    assert run.status == "completed"
    assert terminal is not None and terminal.event_type == "completed"
    assert len(messages) == 2
    assert event_count >= 3


# ---------------------------------------------------------------------------
# 幂等与终态唯一
# ---------------------------------------------------------------------------
def test_fifty_duplicate_finalize_produce_single_terminal(seeded: Fixture) -> None:
    result = _run_rag(seeded)
    run = result.run
    message = _message(run)
    assert message is not None
    assert result.terminal is not None
    terminal_id = result.terminal.event_id
    assert message.content
    replay_citations = tuple(
        ep.CitationSpec(
            chunk_id=item["chunk_id"],
            document_id=item["document_id"],
            index_version=item["index_version"],
            rank=item["rank"],
            quote_hash=item["quote_hash"],
        )
        for item in message.citations
    )
    for index in range(50):
        with closing(ep.connect()) as conn:
            conn.execute("BEGIN")
            replay = ep.finalize(
                conn,
                run=run,
                message=message,
                generation=1,
                content=message.content,
                citations=replay_citations,
                answer_kind="grounded",
            )
            conn.commit()
        assert replay.replayed is True, f"第 {index} 次重复 finalize 应走复放"
        assert replay.terminal_event.event_id == terminal_id
    with closing(ep.connect()) as conn, conn.transaction():
        terminal_count = ep.count_terminal(conn, seeded.workspace, run.run_id)
        completed = ep.count_events(
            conn, seeded.workspace, run.run_id, event_type="completed"
        )
    assert terminal_count == 1
    assert completed == 1


def test_finalize_conflict_and_database_terminal_uniqueness(seeded: Fixture) -> None:
    result = _run_rag(seeded)
    run = result.run
    message = _message(run)
    assert message is not None
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        with pytest.raises(ep.RunAlreadyTerminal) as conflicted:
            ep.finalize(
                conn,
                run=run,
                message=message,
                generation=1,
                content="不同内容",
                citations=tuple(
                    ep.CitationSpec(
                        chunk_id=item["chunk_id"],
                        document_id=item["document_id"],
                        index_version=item["index_version"],
                        rank=item["rank"],
                        quote_hash=item["quote_hash"],
                    )
                    for item in message.citations
                ),
                answer_kind="grounded",
            )
        conn.rollback()
    assert conflicted.value.code == "RUN_ALREADY_TERMINAL"
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        with pytest.raises(pg_errors.UniqueViolation):
            conn.execute(
                f"INSERT INTO {ep.SCHEMA}.phase0_run_events "
                "(workspace_id, run_id, event_seq, event_id, generation, event_type, payload) "
                "VALUES (%s, %s, %s, %s, 1, 'cancelled', '{}'::jsonb)",
                (
                    seeded.workspace,
                    run.run_id,
                    ep.allocate_event_seq(conn, seeded.workspace, run.run_id),
                    uuid.uuid4(),
                ),
            )
        conn.rollback()


def test_stale_generation_write_is_rejected(seeded: Fixture) -> None:
    conversation = _conversation(seeded.workspace, seeded.principal)
    with closing(ep.connect()) as conn:
        with conn.transaction():
            run, _user = ep.create_run(
                conn,
                workspace_id=seeded.workspace,
                principal_id=seeded.principal,
                conversation_id=conversation,
                query="报销流程多少日内提交？",
            )
            first = ep.ensure_assistant_message(
                conn, workspace_id=seeded.workspace, run_id=run.run_id, generation=1
            )
        conn.execute("BEGIN")
        first = ep.persist_tokens(
            conn, run=run, message=first, generation=1, texts=["旧回答草稿"]
        )
        conn.commit()
        conn.execute("BEGIN")
        new_run, second = ep.bump_generation(
            conn, run=run, expected_generation=1, reason="model_crash"
        )
        conn.commit()
    assert new_run.generation == 2
    assert second.generation == 2
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        with pytest.raises(ep.StaleGeneration) as stale:
            ep.persist_tokens(
                conn,
                run=new_run,
                message=second,
                generation=1,
                texts=["迟到写入"],
            )
        conn.rollback()
    assert stale.value.code == "STALE_GENERATION"
    with closing(ep.connect()) as conn, conn.transaction():
        old = ep.read_message(conn, seeded.workspace, first.message_id)
        reset_events = ep.list_events(
            conn, seeded.workspace, run.run_id, event_type="generation_reset"
        )
    assert old.status == "incomplete"
    assert old.content == "旧回答草稿"
    assert len(reset_events) == 1
    assert reset_events[0].payload["from_generation"] == 1
    assert reset_events[0].payload["to_generation"] == 2


# ---------------------------------------------------------------------------
# 故障注入：模型中断、DB 失败、取消
# ---------------------------------------------------------------------------
class _CrashOnceModel:
    """第一次生成中途崩溃，第二次成功（验证 generation reset 后重跑）。"""

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, prompt: rp.ModelPrompt) -> object:
        self.calls += 1
        if self.calls == 1:
            yield "第一次生成的半截内容"
            raise rp.ModelCrashError("模型进程中断（模拟 kill）")
        yield from rp.DeterministicModel().stream(prompt)


class _AlwaysCrashModel:
    def stream(self, prompt: rp.ModelPrompt) -> object:
        yield "半截"
        raise rp.ModelCrashError("模型进程中断（模拟 kill）")


def test_model_crash_resets_generation_and_old_tokens_stay_out(seeded: Fixture) -> None:
    result = _run_rag(seeded, model=_CrashOnceModel())
    assert result.status == "completed"
    assert result.generation == 2
    assert result.terminal is not None and result.terminal.event_type == "completed"
    assert "半截" not in result.answer_text
    with closing(ep.connect()) as conn, conn.transaction():
        reset_events = ep.list_events(
            conn, seeded.workspace, result.run.run_id, event_type="generation_reset"
        )
        messages = ep.list_messages(conn, seeded.workspace, result.run.run_id)
    assert len(reset_events) == 1
    drafts = [item for item in messages if item.role == "assistant"]
    assert len(drafts) == 2
    assert drafts[0].status == "incomplete"
    assert drafts[1].status == "final"
    assert "半截" not in drafts[1].content


def test_model_crash_twice_ends_failed_without_completed(seeded: Fixture) -> None:
    result = _run_rag(seeded, model=_AlwaysCrashModel())
    assert result.status == "failed"
    assert result.terminal is not None and result.terminal.event_type == "failed"
    with closing(ep.connect()) as conn, conn.transaction():
        completed = ep.count_events(
            conn, seeded.workspace, result.run.run_id, event_type="completed"
        )
        terminal_count = ep.count_terminal(conn, seeded.workspace, result.run.run_id)
    assert completed == 0
    assert terminal_count == 1


def test_db_failure_after_partial_tokens_leaves_no_terminal(seeded: Fixture) -> None:
    result = rp.run_rag(
        _dsn(),
        workspace_id=seeded.workspace,
        principal_id=seeded.principal,
        conversation_id=_conversation(seeded.workspace, seeded.principal),
        query="报销流程多少日内提交电子发票？",
        kb_ids=[seeded.kb],
        sink_factory=lambda dsn, **kwargs: rp.FailingTokenSink(  # type: ignore[arg-type]
            dsn, fail_after=2, **kwargs
        ),
    )
    assert result.status == "running"
    assert result.terminal is None
    assert result.persisted_tokens == 2
    with closing(ep.connect()) as conn, conn.transaction():
        completed = ep.count_events(
            conn, seeded.workspace, result.run.run_id, event_type="completed"
        )
        terminal_count = ep.count_terminal(conn, seeded.workspace, result.run.run_id)
    assert completed == 0
    assert terminal_count == 0
    message = _message(result.run)
    assert message is not None and message.status == "partial"


def test_cancel_run_keeps_terminal_and_blocks_late_finalize(seeded: Fixture) -> None:
    conversation = _conversation(seeded.workspace, seeded.principal)
    with closing(ep.connect()) as conn:
        with conn.transaction():
            run, _user = ep.create_run(
                conn,
                workspace_id=seeded.workspace,
                principal_id=seeded.principal,
                conversation_id=conversation,
                query="报销流程？",
            )
            message = ep.ensure_assistant_message(
                conn, workspace_id=seeded.workspace, run_id=run.run_id, generation=1
            )
        conn.execute("BEGIN")
        message = ep.persist_tokens(
            conn, run=run, message=message, generation=1, texts=["半截回答"]
        )
        conn.commit()
        conn.execute("BEGIN")
        cancelled_run = ep.cancel_run(conn, run=run, reason="用户取消").run
        conn.commit()
    assert cancelled_run.status == "cancelled"
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        with pytest.raises(ep.RunAlreadyTerminal) as terminal_error:
            ep.finalize(
                conn,
                run=cancelled_run,
                message=message,
                generation=1,
                content="迟到完成",
                answer_kind="grounded",
            )
        conn.rollback()
        conn.execute("BEGIN")
        with pytest.raises(ep.RunAlreadyTerminal):
            ep.persist_tokens(
                conn,
                run=cancelled_run,
                message=message,
                generation=1,
                texts=["迟到 token"],
            )
        conn.rollback()
    assert terminal_error.value.code == "RUN_ALREADY_TERMINAL"
    with closing(ep.connect()) as conn, conn.transaction():
        terminal = ep.read_terminal(conn, seeded.workspace, run.run_id)
        stored = ep.read_message(conn, seeded.workspace, message.message_id)
        completed = ep.count_events(
            conn, seeded.workspace, run.run_id, event_type="completed"
        )
    assert terminal is not None and terminal.event_type == "cancelled"
    assert stored.status == "incomplete"
    assert completed == 0


# ---------------------------------------------------------------------------
# 引用：伪引用、越权、旧版本
# ---------------------------------------------------------------------------
def test_bad_handle_is_rejected_and_audited(seeded: Fixture) -> None:
    result = _run_rag(seeded, model=rp.DeterministicModel(inject_bad_handle=True))
    assert result.status == "completed"
    assert result.rejections, "伪引用必须产生拒绝记录"
    fake = [item for item in result.rejections if item.get("handle") == 99]
    assert fake and fake[0]["reason"] == "not_in_evidence"
    with closing(ep.connect()) as conn, conn.transaction():
        rejected = ep.count_rejections(
            conn, seeded.workspace, result.run.run_id, reason="not_in_evidence"
        )
        rows = conn.execute(
            f"SELECT chunk_id, quote_hash FROM {ep.SCHEMA}.phase0_message_citations "
            "WHERE workspace_id = %s AND message_id = %s",
            (seeded.workspace, _message(result.run).message_id),
        ).fetchall()
    assert rejected >= 1
    assert rows, "真实引用仍应写入"
    assert all(item["quote_hash"] for item in rows)


def test_cross_workspace_citation_is_rejected(seeded: Fixture) -> None:
    with closing(ep.connect()) as conn:
        with conn.transaction():
            foreign = conn.execute(
                f"SELECT chunk_id, document_id, index_version FROM {ep.SCHEMA}.phase0_chunks "
                "WHERE workspace_id = %s LIMIT 1",
                (seeded.other_workspace,),
            ).fetchone()
            run, _user = ep.create_run(
                conn,
                workspace_id=seeded.workspace,
                principal_id=seeded.principal,
                conversation_id=_conversation(seeded.workspace, seeded.principal),
                query="跨空间引用测试",
            )
        evidence = (
            rp.Evidence(
                handle=1,
                chunk_id=foreign["chunk_id"],
                document_id=foreign["document_id"],
                index_version=foreign["index_version"],
                chunk_ordinal=0,
                kind="body",
                text="外部合作方结算周期",
                score=0.5,
                rank=1,
            ),
        )
        conn.execute("BEGIN")
        check = rp.validate_citations(
            conn,
            workspace_id=seeded.workspace,
            principal_id=seeded.principal,
            run_id=run.run_id,
            evidence=evidence,
            answer_text="据资料[[1]]，结算周期为十天。",
        )
        conn.commit()
    assert check.citations == ()
    assert check.rejections[0]["reason"] == "cross_workspace"


def test_stale_version_citation_is_rejected(seeded: Fixture) -> None:
    """旧版本 chunk 即使出现在 evidence 里也必须被拒绝（活动指针已前进）。"""

    with closing(ep.connect()) as conn:
        with conn.transaction():
            rp.add_document(
                conn,
                workspace_id=seeded.workspace,
                kb_id=seeded.kb,
                document_id=seeded.document,
                index_version=2,
                texts=("新版报销流程：线上提交电子发票。",),
            )
        with conn.transaction():
            old_version = conn.execute(
                f"SELECT c.chunk_id, c.document_id, c.index_version "
                f"FROM {ep.SCHEMA}.phase0_chunks AS c "
                f"JOIN {ep.SCHEMA}.phase0_documents AS d USING (workspace_id, document_id) "
                "WHERE d.active_version <> c.index_version LIMIT 1"
            ).fetchone()
        assert old_version is not None, "语料必须含一个非活动的旧版本 chunk"
        conn.execute("BEGIN")
        evidence = (
            rp.Evidence(
                handle=1,
                chunk_id=old_version["chunk_id"],
                document_id=old_version["document_id"],
                index_version=old_version["index_version"],
                chunk_ordinal=0,
                kind="body",
                text="旧版内容",
                score=0.4,
                rank=1,
            ),
        )
        check = rp.validate_citations(
            conn,
            workspace_id=seeded.workspace,
            principal_id=seeded.principal,
            run_id=None,  # type: ignore[arg-type]
            evidence=evidence,
            answer_text="[[1]]",
            record=False,
        )
        conn.rollback()
    assert check.citations == ()
    assert check.rejections[0]["reason"] in ("stale_version", "unknown_chunk")


# ---------------------------------------------------------------------------
# 检索授权：跨空间/旧版本/无授权不可见
# ---------------------------------------------------------------------------
def test_retrieval_is_scoped_by_membership_grants_and_active_version(
    seeded: Fixture,
) -> None:
    with closing(ep.connect()) as conn:
        for principal, _expected_min in ((seeded.principal, 1), (seeded.member, 1)):
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            hits = rp.retrieve(
                conn,
                workspace_id=seeded.workspace,
                principal_id=principal,
                kb_ids=[seeded.kb],
                query="报销流程 电子发票",
            )
            conn.rollback()
            assert hits, "有读权限的成员应能检索到 chunk"
            assert all(item.handle >= 1 for item in hits)
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        outsider_hits = rp.retrieve(
            conn,
            workspace_id=seeded.workspace,
            principal_id=seeded.outsider,
            kb_ids=[seeded.kb],
            query="报销流程 电子发票",
        )
        conn.rollback()
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        private_hits = rp.retrieve(
            conn,
            workspace_id=seeded.workspace,
            principal_id=seeded.member,
            kb_ids=[seeded.kb_private],
            query="任何内容",
        )
        conn.rollback()
    assert outsider_hits == (), "无 grant 的主体不能检索到任何 chunk"
    assert private_hits == (), "未授权的 KB 不能被检索（filter-in-query）"


def test_old_version_chunks_are_invisible(seeded: Fixture) -> None:
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        rp.add_document(
            conn,
            workspace_id=seeded.workspace,
            kb_id=seeded.kb,
            document_id=seeded.document,
            index_version=2,
            texts=("新版报销流程：线上提交电子发票，三十日内完成。",),
        )
        conn.commit()
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        chunk_ids = {
            str(item["chunk_id"])
            for item in conn.execute(
                f"SELECT chunk_id FROM {ep.SCHEMA}.phase0_chunks WHERE workspace_id = %s "
                "AND document_id = %s",
                (seeded.workspace, seeded.document),
            ).fetchall()
        }
        hits = rp.retrieve(
            conn,
            workspace_id=seeded.workspace,
            principal_id=seeded.principal,
            kb_ids=[seeded.kb],
            query="报销流程 电子发票",
        )
        conn.rollback()
    hit_ids = {str(item.chunk_id) for item in hits}
    assert hit_ids, "新活动版本应可检索"
    old_ids = chunk_ids - hit_ids
    assert old_ids, "旧版本 chunk 存在但不可见"
    with closing(ep.connect()) as conn, conn.transaction():
        old_versions = conn.execute(
            f"SELECT index_version, status FROM {ep.SCHEMA}.phase0_document_versions "
            "WHERE workspace_id = %s AND document_id = %s ORDER BY index_version",
            (seeded.workspace, seeded.document),
        ).fetchall()
    assert [item["status"] for item in old_versions] == ["retired", "active"]


# ---------------------------------------------------------------------------
# 拒答、Rerank 降级
# ---------------------------------------------------------------------------
def test_no_evidence_refuses_without_citations(seeded: Fixture) -> None:
    result = _run_rag(seeded, query="火星殖民地的退税政策如何计算？", kb_ids=[seeded.kb_private])
    assert result.status == "completed"
    assert result.refused is True
    assert result.answer_kind == "refused"
    assert result.citations == ()
    assert result.terminal is not None and result.terminal.event_type == "completed"
    with closing(ep.connect()) as conn, conn.transaction():
        citations = conn.execute(
            f"SELECT count(*) AS n FROM {ep.SCHEMA}.phase0_message_citations "
            "WHERE workspace_id = %s AND message_id = %s",
            (seeded.workspace, _message(result.run).message_id),
        ).fetchone()
    assert citations["n"] == 0


def test_rerank_failure_degrades_with_diagnostic(seeded: Fixture) -> None:
    result = _run_rag(seeded, reranker=rp.FailingReranker())
    assert result.status == "completed"
    assert any(item.startswith("rerank_degraded") for item in result.diagnostics)
    assert result.terminal is not None and result.terminal.event_type == "completed"


# ---------------------------------------------------------------------------
# 事件游标与续传能力
# ---------------------------------------------------------------------------
def test_cursor_roundtrip_expiry_and_scope(seeded: Fixture) -> None:
    result = _run_rag(seeded)
    with closing(ep.connect()) as conn:
        with conn.transaction():
            token, state = ep.issue_cursor(
                conn, run=result.run, principal_id=seeded.principal, after_seq=0, ttl_seconds=60
            )
        assert state.after_seq == 0
        with conn.transaction():
            events, next_token = ep.read_events_with_cursor(
                conn, cursor_token=token, principal_id=seeded.principal
            )
        assert events and next_token != token
        tail, after_tail = ep.read_events_with_cursor(
            conn, cursor_token=next_token, principal_id=seeded.principal
        )
        assert list(tail) == []
        assert after_tail is not None
        with conn.transaction(), pytest.raises(ep.CursorExpired) as expired:
            ep.read_events_with_cursor(
                conn,
                cursor_token=next_token,
                principal_id=seeded.principal,
                now=state.expires_at + ep.timedelta(seconds=1),
            )
        assert expired.value.code == "EVENT_CURSOR_EXPIRED"
        assert expired.value.run_status == "completed"
        with conn.transaction():
            other_principal = rp.add_principal(conn, workspace_id=seeded.workspace)
        with conn.transaction(), pytest.raises(ep.CursorScopeMismatch):
            ep.read_events_with_cursor(
                conn, cursor_token=next_token, principal_id=other_principal
            )
        with conn.transaction(), pytest.raises(ep.EventValidationError):
            ep.read_events_with_cursor(
                conn,
                cursor_token=next_token[:-4] + "0000",
                principal_id=seeded.principal,
            )
    with (
        closing(ep.connect()) as conn,
        conn.transaction(),
        pytest.raises(ep.CapabilityUnsupported),
    ):
        ep.resume_events(conn, run=result.run, principal_id=seeded.principal)


def test_events_are_append_only(seeded: Fixture) -> None:
    result = _run_rag(seeded)
    with closing(ep.connect()) as conn:
        conn.execute("BEGIN")
        with pytest.raises(pg_errors.CheckViolation):
            conn.execute(
                f"UPDATE {ep.SCHEMA}.phase0_run_events SET payload = '{{}}'::jsonb "
                "WHERE workspace_id = %s AND run_id = %s",
                (seeded.workspace, result.run.run_id),
            )
        conn.rollback()
        conn.execute("BEGIN")
        with pytest.raises(pg_errors.CheckViolation):
            conn.execute(
                f"DELETE FROM {ep.SCHEMA}.phase0_run_events "
                "WHERE workspace_id = %s AND run_id = %s",
                (seeded.workspace, result.run.run_id),
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# 100 例质量实测（写入 docs/poc/evidence/rag-citations.json）
# ---------------------------------------------------------------------------
def test_quality_sample_metrics(installed: str) -> None:
    started = time.perf_counter()
    payload = rp.evaluate_quality(_dsn(), cases=100)
    elapsed = time.perf_counter() - started
    assert payload["sample_count"] == 100
    assert payload["fabricated_citations"] == 0, payload["fabricated_citations"]
    assert payload["unauthorized_citations"] == 0
    assert payload["citation_accuracy"] >= 0.85
    assert payload["behaviour_match_rate"] >= 0.9
    assert payload["completed_runs"] >= 95
    assert payload["latency_ms"]["p50"] > 0
    path = rp.write_evidence(payload)
    assert path.exists() and path.suffix == ".json"
    assert elapsed < 600
