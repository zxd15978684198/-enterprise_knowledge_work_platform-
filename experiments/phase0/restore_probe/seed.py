"""确定性种入代表性业务事实与 T05 checkpoint。

种入数据的目的是让「还原前后是否一致」有可比的实体：文档/版本/发布指针、Job 与
Outbox、外部写账本、预算台账，以及 T05 的 run/thread/checkpoint。所有 ID 用固定
命名空间从名字派生（``uuid5``），因此同一份代码总产生同一组事实，便于跨库比对。

checkpoint 部分**复用 T05 的 ``Harness``**（只读 import），不自己实现 checkpoint
存储，也不写 LangGraph 原生表。
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

PHASE0_DIR = Path(__file__).resolve().parents[1]
if str(PHASE0_DIR) not in sys.path:  # 复用 T05 harness（只读）
    sys.path.insert(0, str(PHASE0_DIR))

import checkpoint_probe as probe  # noqa: E402

NAMESPACE = uuid.UUID("5f2b6f0e-9c3a-4c1b-9f57-8f3fbb6c1d21")
WORKSPACES: tuple[str, ...] = ("ws-alpha", "ws-beta")
DOCUMENTS: tuple[str, ...] = ("doc-1", "doc-2", "doc-3", "doc-4", "doc-5")
PUBLISHED_V1: tuple[str, ...] = ("doc-1", "doc-2", "doc-3")
GRAPH_VERSION = "rag.v1"
STATE_SCHEMA_VERSION = 1


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, name)


def hex_digest(name: str) -> str:
    return uuid.uuid5(NAMESPACE, f"digest:{name}").hex * 2  # 64 位十六进制


@dataclass(frozen=True, slots=True)
class SeedReport:
    """种入结果：每张表的行数与关键 ID，供证据引用。"""

    table_rows: tuple[tuple[str, int], ...]
    workspace_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    checkpoint_runs: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_rows": dict(self.table_rows),
            "workspace_ids": list(self.workspace_ids),
            "document_ids": list(self.document_ids),
            "checkpoint_runs": [list(item) for item in self.checkpoint_runs],
        }


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """模拟「应用新版本」后的变化，用于回滚演练的前后对照。"""

    steps: tuple[str, ...]
    documents_promoted: int
    publications_added: int
    schema_marker: str
    pointers_before: tuple[str, ...] = field(default_factory=tuple)
    pointers_after: tuple[str, ...] = field(default_factory=tuple)


def seed_business(conn: psycopg.Connection[Any]) -> SeedReport:
    """种入业务事实；在一个事务内完成，失败整体回滚。"""

    table_rows: list[tuple[str, int]] = []
    with conn.transaction():
        for name in WORKSPACES:
            conn.execute(
                "INSERT INTO p0_t03.phase0_workspaces (workspace_id, name) VALUES (%s, %s)",
                (uid(f"workspace:{name}"), f"Phase 0 {name}"),
            )
        for name in WORKSPACES:
            workspace_id = uid(f"workspace:{name}")
            for index, role in enumerate(("owner", "member")):
                conn.execute(
                    "INSERT INTO p0_t03.phase0_principals "
                    "(workspace_id, principal_id, membership_subject_ref, kind, role) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        workspace_id,
                        uid(f"principal:{name}:{index}"),
                        f"subject:{name}:{index}",
                        "human",
                        role,
                    ),
                )
        for name in DOCUMENTS:
            workspace_id = uid(f"workspace:{WORKSPACES[0]}")
            source_id = uid(f"source:{name}")
            source_version_id = uid(f"source-version:{name}")
            conn.execute(
                "INSERT INTO p0_t12.phase0_sources "
                "(workspace_id, source_id, source_version_id, content_sha256, object_key, "
                " file_name, format) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    source_id,
                    source_version_id,
                    hex_digest(f"content:{name}"),
                    f"objects/{WS_NAME}/{name}.md",
                    f"{name}.md",
                    "md",
                ),
            )
            active = 1 if name in PUBLISHED_V1 else None
            conn.execute(
                "INSERT INTO p0_t12.phase0_documents "
                "(workspace_id, document_id, source_id, source_version_id, "
                " desired_generation, active_version) VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace_id, uid(f"document:{name}"), source_id, source_version_id, 1, active),
            )
            for index_version in (1, 2):
                # v1 必须是 active（约束要求 ready/active/retired 带 attempt 与 checksum），
                # v2 停在 staging，用来证明「未完成索引」也会被备份/还原平平地搬过去。
                status = "active" if index_version == 1 else "staging"
                conn.execute(
                    "INSERT INTO p0_t12.phase0_document_versions "
                    "(workspace_id, document_id, index_version, status, chunk_count, "
                    " winning_attempt_id, manifest_checksum) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        workspace_id,
                        uid(f"document:{name}"),
                        index_version,
                        status,
                        index_version * 7,
                        uid(f"attempt:{name}:v{index_version}") if status == "active" else None,
                        hex_digest(f"manifest:{name}:v{index_version}")
                        if status == "active"
                        else None,
                    ),
                )
        for name in PUBLISHED_V1:
            workspace_id = uid(f"workspace:{WORKSPACES[0]}")
            conn.execute(
                "INSERT INTO p0_t13.phase0_publications "
                "(workspace_id, publication_id, document_id, kind, to_version, from_version, "
                " winning_attempt_id, manifest_checksum, chunk_count, idempotency_key, "
                "committed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (
                    workspace_id,
                    uid(f"publication:{name}:v1"),
                    uid(f"document:{name}"),
                    "publish",
                    1,
                    None,
                    uid(f"attempt:{name}:v1"),
                    hex_digest(f"manifest:{name}:v1"),
                    7,
                    f"publish:{name}:v1",
                ),
            )
        for index in range(6):
            workspace_id = uid(f"workspace:{WORKSPACES[0]}")
            target_id = uid(f"target:{index}")
            job_id = uid(f"job:{index}")
            conn.execute(
                "INSERT INTO p0_t07.phase0_ingest_targets "
                "(workspace_id, target_id, name, desired_version) VALUES (%s, %s, %s, %s)",
                (workspace_id, target_id, f"target-{index}", 1),
            )
            conn.execute(
                "INSERT INTO p0_t07.phase0_jobs "
                "(workspace_id, job_id, target_id, kind, dedup_key, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    job_id,
                    target_id,
                    "ingest",
                    f"job:{index}",
                    "succeeded" if index < 4 else "pending",
                ),
            )
            conn.execute(
                "INSERT INTO p0_t07.phase0_outbox "
                "(workspace_id, outbox_id, job_id, operation, dedup_key, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    uid(f"outbox:{index}"),
                    job_id,
                    "index.publish",
                    f"outbox:{index}",
                    "dispatched" if index < 4 else "pending",
                ),
            )
        for index in range(4):
            # 「外部写已经发生」的最小证据链：响应产物 → 工具调用 → 追加式 receipt。
            workspace_id = uid(f"workspace:{WORKSPACES[0]}")
            run_id = uid(f"external-run:{index}")
            artifact_id = uid(f"artifact:{index}")
            tool_call_id = uid(f"tool-call:{index}")
            intent_key = hex_digest(f"intent:{index}")
            conn.execute(
                "INSERT INTO p0_t06.phase0_response_artifacts "
                "(workspace_id, response_artifact_id, run_id, model_step_key, "
                " schema_version, response_digest) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    artifact_id,
                    run_id,
                    f"step-{index}",
                    1,
                    hex_digest(f"response:{index}"),
                ),
            )
            conn.execute(
                "INSERT INTO p0_t06.phase0_tool_calls "
                "(workspace_id, tool_call_id, run_id, response_artifact_id, call_ordinal, "
                " name, tool_version, arguments_hash, scope_hash, canonicalization_version, "
                " intent_key, status, attempt_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    tool_call_id,
                    run_id,
                    artifact_id,
                    0,
                    "web.search",
                    "1.0.0",
                    hex_digest(f"arguments:{index}"),
                    hex_digest(f"scope:{index}"),
                    "v1",
                    intent_key,
                    "succeeded",
                    1,
                ),
            )
            conn.execute(
                "INSERT INTO p0_t06.phase0_tool_receipts "
                "(workspace_id, receipt_id, tool_call_id, intent_key, attempt_no, "
                " arguments_hash, scope_hash, tool_version, outcome, downstream_sent, "
                " external_idempotency_key, result_summary, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    workspace_id,
                    uid(f"receipt:{index}"),
                    tool_call_id,
                    intent_key,
                    1,
                    hex_digest(f"arguments:{index}"),
                    hex_digest(f"scope:{index}"),
                    "1.0.0",
                    "succeeded",
                    True,
                    f"idem:{index}",
                    '{"status": "ok"}',
                    42 + index,
                ),
            )
        for name in WORKSPACES:
            conn.execute(
                "INSERT INTO p0_t20.phase0_budget_ledgers "
                "(workspace_id, run_id, currency, limit_tokens, limit_cost_microunits, "
                " limit_pages, limit_bytes, limit_concurrency) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uid(f"workspace:{name}"),
                    uid(f"budget-run:{name}"),
                    "USD",
                    200_000,
                    5_000_000,
                    500,
                    100_000_000,
                    4,
                ),
            )
    for table in (
        "p0_t03.phase0_workspaces",
        "p0_t03.phase0_principals",
        "p0_t12.phase0_sources",
        "p0_t12.phase0_documents",
        "p0_t12.phase0_document_versions",
        "p0_t13.phase0_publications",
        "p0_t07.phase0_ingest_targets",
        "p0_t07.phase0_jobs",
        "p0_t07.phase0_outbox",
        "p0_t06.phase0_response_artifacts",
        "p0_t06.phase0_tool_calls",
        "p0_t06.phase0_tool_receipts",
        "p0_t20.phase0_budget_ledgers",
    ):
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        table_rows.append((table, int(row[0]) if row else 0))
    return SeedReport(
        table_rows=tuple(table_rows),
        workspace_ids=tuple(str(uid(f"workspace:{name}")) for name in WORKSPACES),
        document_ids=tuple(str(uid(f"document:{name}")) for name in DOCUMENTS),
        checkpoint_runs=(),
    )


@dataclass(frozen=True, slots=True)
class CheckpointSeed:
    """T05 checkpoint 的种入结果；``expected`` 记录每个 run 的期望最新序列。"""

    bindings: tuple[tuple[uuid.UUID, uuid.UUID, uuid.UUID], ...]  # (workspace, run, thread)
    expected_seq: tuple[int, ...]
    lock_pids: tuple[int, ...]


def seed_checkpoints(dsn: str) -> CheckpointSeed:
    """用 T05 ``Harness`` 在目标库建 ``p0_t05`` schema 并写入两个 run 的 checkpoint。

    T05 判定为 PARTIAL（原生 saver 不可 fence），因此这里只种「业务侧 epoch +
    替身 checkpoint」这一层，用来说明单活条件下的恢复一致性；原生 saver 的
    限制写进证据的残余风险，不在本任务里重新验证。
    """

    bindings: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    expected: list[int] = []
    pids: list[int] = []
    harness = probe.Harness(dsn=dsn)
    try:
        harness.reset_schema()
        for name, writes in (("run-alpha", 3), ("run-beta", 2)):
            binding = harness.create_run(
                workspace_id=uid(f"workspace:{WORKSPACES[0]}"),
                run_id=uid(f"cp-run:{name}"),
                thread_id=uid(f"cp-thread:{name}"),
                graph_version=GRAPH_VERSION,
                state_schema_version=STATE_SCHEMA_VERSION,
            )
            invocation = harness.new_invocation(binding)
            invocation.open_lock()
            if not invocation.try_acquire_lock():
                raise RuntimeError("种入 checkpoint 时锁被占用")
            claim = invocation.claim_owner(0)
            if claim.owner_epoch != 1:
                raise RuntimeError(f"种入 checkpoint 时 epoch 异常: {claim.owner_epoch}")
            seq = 0
            for step in range(1, writes + 1):
                result = invocation.write({"revision": step, "writer": "seed"})
                if not result.accepted:
                    raise RuntimeError(f"种入 checkpoint 被拒: {result.code}")
                seq = int(result.seq)
            expected.append(seq)
            pids.append(int(invocation.lock_pid or 0))
            invocation.close()
            bindings.append((binding.workspace_id, binding.run_id, binding.thread_id))
    finally:
        harness.close()
    return CheckpointSeed(
        bindings=tuple(bindings),
        expected_seq=tuple(expected),
        lock_pids=tuple(pids),
    )


def checkpoint_state(dsn: str) -> tuple[tuple[str, int, str], ...]:
    """读取每个 run 的最新 checkpoint（seq + id），用于还原后逐项比对。"""

    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT run_id::text, last_checkpoint_seq, coalesce(last_checkpoint_id::text, '') "
            "FROM p0_t05.phase0_runs ORDER BY run_id"
        ).fetchall()
    return tuple((str(run), int(seq), str(cid)) for run, seq, cid in rows)


def upgrade_to_v2(conn: psycopg.Connection[Any]) -> UpgradeReport:
    """模拟「应用新版本」：加一列 + 指针前移 + 追加 v2 发布记录。

    这是演练库内的**假升级**，不是编号迁移：文件系统里不会多出任何
    ``db/migrations/*.sql``。它存在的意义是给「升级失败→回滚」制造真实的数据
    差异（schema 一列 + active 指针 + 发布记录）。
    """

    from .inventory import pointer_table

    before = tuple(f"{fact.name}={fact.value}" for fact in pointer_table(conn))
    steps: list[str] = []
    with conn.transaction():
        conn.execute(
            "ALTER TABLE p0_t12.phase0_documents "
            "ADD COLUMN IF NOT EXISTS state_schema_version integer NOT NULL DEFAULT 1"
        )
        steps.append("ALTER TABLE p0_t12.phase0_documents ADD COLUMN state_schema_version")
        promoted = 0
        publications = 0
        for name in PUBLISHED_V1[:2]:
            workspace_id = uid(f"workspace:{WORKSPACES[0]}")
            document_id = uid(f"document:{name}")
            conn.execute(
                "UPDATE p0_t12.phase0_documents SET active_version = 2, "
                "state_schema_version = 2, updated_at = now() WHERE document_id = %s",
                (document_id,),
            )
            promoted += 1
            conn.execute(
                "INSERT INTO p0_t13.phase0_publications "
                "(workspace_id, publication_id, document_id, kind, to_version, from_version, "
                " winning_attempt_id, manifest_checksum, chunk_count, idempotency_key, "
                "committed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (
                    workspace_id,
                    uid(f"publication:{name}:v2"),
                    document_id,
                    "publish",
                    2,
                    1,
                    uid(f"attempt:{name}:v2"),
                    hex_digest(f"manifest:{name}:v2"),
                    14,
                    f"publish:{name}:v2",
                ),
            )
            publications += 1
        steps.append(f"UPDATE documents SET active_version=2 ({promoted} docs)")
        steps.append(f"INSERT publications to_version=2 ({publications} rows)")
    after = tuple(f"{fact.name}={fact.value}" for fact in pointer_table(conn))
    return UpgradeReport(
        steps=tuple(steps),
        documents_promoted=promoted,
        publications_added=publications,
        schema_marker="p0_t12.phase0_documents.state_schema_version=2",
        pointers_before=before,
        pointers_after=after,
    )


def availability_probe(conn: psycopg.Connection[Any]) -> tuple[tuple[str, str], ...]:
    """回滚后「还能用」的最小读路径：文档→最新发布→Job 计数。"""

    checks: list[tuple[str, str]] = []
    row = conn.execute(
        "SELECT count(*) FROM p0_t12.phase0_documents WHERE active_version IS NOT NULL"
    ).fetchone()
    checks.append(("active documents", str(row[0]) if row else "0"))
    row = conn.execute(
        "SELECT count(*) FROM p0_t13.phase0_publications WHERE committed_at IS NOT NULL"
    ).fetchone()
    checks.append(("committed publications", str(row[0]) if row else "0"))
    row = conn.execute(
        "SELECT count(*) FROM p0_t07.phase0_jobs WHERE status = 'succeeded'"
    ).fetchone()
    checks.append(("succeeded jobs", str(row[0]) if row else "0"))
    joined = conn.execute(
        "SELECT d.document_id::text, max(p.to_version) "
        "FROM p0_t12.phase0_documents d JOIN p0_t13.phase0_publications p USING (document_id) "
        "GROUP BY d.document_id ORDER BY 1"
    ).fetchall()
    checks.append(("document->max published version", ";".join(f"{d}:{v}" for d, v in joined)))
    return tuple(checks)


WS_NAME = WORKSPACES[0]
