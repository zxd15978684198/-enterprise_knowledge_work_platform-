"""P0-T25 · 授权检索与三路对照验收（真实 PostgreSQL，禁 skip/xfail/mock）。

覆盖 Backlog P0-T25 量化验收：
1. 100 条 QA 三路对照（vector / fts / bm25，另加 rrf 融合）真实指标：Recall@5、
   MRR@10、NDCG@5、延迟、no-answer 假阳性、superseded 命中。
2. 未授权 / 未发布 / 旧版本 chunk 命中 = 0（旧版本 chunk 仍在表里，靠可见性三连隐藏）。
3. 跨空间探测 ≥24 次：RLS + 可见性 CTE 双重过滤，命中 0；自报 workspace 扩展被拒。
4. 撤销成员 / 切换 workspace 后同一连接立即可见性变化。
5. 单快照：REPEATABLE READ 内并发切换 active 指针，不读混版本。
6. 布尔组合与权限过滤组合不越权（3 类实测）。
7. 证据 JSON 落盘并脱敏（无口令、无密钥）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import IsolationLevel
from psycopg.pq import TransactionStatus

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import retrieval_probe as probe  # noqa: E402
import scope  # noqa: E402

TENANT_TABLES = (
    "phase0_principals",
    "phase0_documents",
    "phase0_document_versions",
    "phase0_chunks",
)


# ---------------------------------------------------------------------------
# fixtures（真实 PG；本文件自建，不依赖 conftest 的站点 fixture）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def admin_dsn() -> str:
    return scope.read_dsn()


@pytest.fixture(scope="session")
def admin(admin_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = probe.connect(admin_dsn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def schema(admin: psycopg.Connection[Any]) -> str:
    probe.reset_schema(admin)
    admin.commit()
    digest = probe.apply_schema(admin)
    admin.commit()
    return digest


@pytest.fixture(scope="session")
def app_dsn(admin: psycopg.Connection[Any], schema: str) -> str:
    created = probe.create_app_role(admin)
    admin.commit()
    return str(created["dsn"])


@pytest.fixture(scope="session")
def app(app_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = probe.connect(app_dsn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def seed(admin: psycopg.Connection[Any], schema: str) -> probe.SeedData:
    data = probe.seed_corpus(admin)
    admin.commit()
    return data


@pytest.fixture(scope="session", autouse=True)
def cleanup_retrieval_artifacts(admin_dsn: str) -> Iterator[None]:
    """跑完删除 p0_t25 schema 与 p0_t25_app 角色（只碰本槽）。"""

    yield
    conn = probe.connect(admin_dsn)
    try:
        probe.reset_schema(conn)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def permissions(app: psycopg.Connection[Any], seed: probe.SeedData) -> dict[str, Any]:
    result = probe.probe_permissions(app, seed, probes=24)
    print(f"[permissions] {json.dumps(probe._fmt(result), ensure_ascii=False)}")
    return result


@pytest.fixture(scope="session")
def snapshot(app_dsn: str, admin_dsn: str, seed: probe.SeedData) -> dict[str, Any]:
    result = probe.probe_snapshot(admin_dsn, app_dsn, seed)
    print(f"[snapshot] {json.dumps(probe._fmt(result), ensure_ascii=False)}")
    return result


@pytest.fixture(scope="session")
def metrics(app: psycopg.Connection[Any], seed: probe.SeedData) -> dict[str, Any]:
    result = probe.evaluate_retrieval(app, seed)
    print(f"[metrics] {json.dumps(probe._fmt(result), ensure_ascii=False)}")
    return result


@pytest.fixture(scope="session")
def boolean_combos(
    app: psycopg.Connection[Any], seed: probe.SeedData
) -> list[dict[str, Any]]:
    combos: list[dict[str, Any]] = []
    workspace_id = seed.workspaces["A"]
    principal_id = seed.principals["A"]
    with probe.scoped_transaction(
        app, workspace_id=workspace_id, principal_id=principal_id
    ):
        anchor_only = probe.search(app, "aa04v1c1", mode="fts", top_k=probe.TOP_K)
        assert len(anchor_only.hits) == 1
        gold_id = anchor_only.hits[0].chunk_id

        # 组合 1：同空间两个 token（唯一 anchor + 共享 topic）。FTS 是 AND 语义，
        # 只命中 gold；BM25 是 OR 语义，会带回同 topic 的邻居，但**必须全部落在当前空间**。
        for mode in ("fts", "bm25"):
            result = probe.search(app, "aa04v1c1 topic01", mode=mode, top_k=probe.TOP_K)
            local = all(hit.workspace_id == workspace_id for hit in result.hits)
            combos.append(
                {
                    "name": "local_anchor_AND_shared_topic",
                    "mode": mode,
                    "query": "aa04v1c1 topic01",
                    "hits": len(result.hits),
                    "workspaces": sorted({str(hit.workspace_id) for hit in result.hits}),
                    "gold_first": bool(result.hits and result.hits[0].chunk_id == gold_id),
                    "all_local": local,
                }
            )
            assert result.hits
            assert local
            if mode == "fts":
                assert len(result.hits) == 1
                assert result.hits[0].chunk_id == gold_id
            else:
                assert any(hit.chunk_id == gold_id for hit in result.hits)

        # 组合 2：其他空间 anchor + 本空间 token → 不得因“跨空间词”放大召回。
        for mode in ("fts", "bm25"):
            result = probe.search(app, "ab00v1c1 topic01", mode=mode, top_k=probe.TOP_K)
            local = all(hit.workspace_id == workspace_id for hit in result.hits)
            combos.append(
                {
                    "name": "foreign_anchor_AND_local_topic",
                    "mode": mode,
                    "query": "ab00v1c1 topic01",
                    "hits": len(result.hits),
                    "workspaces": sorted({str(hit.workspace_id) for hit in result.hits}),
                    "gold_first": False,
                    "all_local": local,
                }
            )
            assert local
            if mode == "fts":
                assert result.hits == ()

        # 组合 3：不存在 token + 有效 token → 不得静默丢弃未命中词。
        for mode in ("fts", "bm25"):
            result = probe.search(app, "zz-absent-99 topic01", mode=mode, top_k=probe.TOP_K)
            local = all(hit.workspace_id == workspace_id for hit in result.hits)
            combos.append(
                {
                    "name": "absent_token_AND_valid_topic",
                    "mode": mode,
                    "query": "zz-absent-99 topic01",
                    "hits": len(result.hits),
                    "workspaces": sorted({str(hit.workspace_id) for hit in result.hits}),
                    "gold_first": False,
                    "all_local": local,
                }
            )
            assert local
            if mode == "fts":
                assert result.hits == ()

        # 权限过滤组合：vector 结果必须全部落在当前空间，且 profile 不会被静默混算。
        vector = probe.search(app, "aa04v1c1 topic01", mode="vector", top_k=probe.TOP_K)
        combos.append(
            {
                "name": "vector_scope_filter",
                "mode": "vector",
                "query": "aa04v1c1 topic01",
                "hits": len(vector.hits),
                "workspaces": sorted({str(hit.workspace_id) for hit in vector.hits}),
                "gold_first": bool(vector.hits and vector.hits[0].chunk_id == gold_id),
                "all_local": all(hit.workspace_id == workspace_id for hit in vector.hits),
            }
        )
        assert vector.hits
        assert {hit.workspace_id for hit in vector.hits} == {workspace_id}
    return combos


# ---------------------------------------------------------------------------
# 1) schema 镜像 / RLS actor / deny 默认
# ---------------------------------------------------------------------------
def test_schema_mirror_records_digest_and_forces_rls(
    admin: psycopg.Connection[Any],
    app: psycopg.Connection[Any],
    seed: probe.SeedData,
    schema: str,
) -> None:
    ledger = admin.execute(
        f"SELECT migration_id, sha256 FROM {probe.SCHEMA}.phase0_schema_migrations"
    ).fetchone()
    assert ledger is not None
    assert ledger["migration_id"] == probe.MIGRATION_ID
    assert ledger["sha256"] == schema == probe.schema_sha256()

    tables = admin.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = %s::regnamespace AND relkind = 'r' ORDER BY relname",
        (probe.SCHEMA,),
    ).fetchall()
    flags = {
        row["relname"]: (row["relrowsecurity"], row["relforcerowsecurity"])
        for row in tables
    }
    assert flags["phase0_schema_migrations"] == (False, False)
    for table in TENANT_TABLES:
        assert flags[table] == (True, True), f"{table} 必须 ENABLE+FORCE RLS"

    policies = admin.execute(
        "SELECT policyname, qual, with_check FROM pg_policies WHERE schemaname = %s",
        (probe.SCHEMA,),
    ).fetchall()
    assert len(policies) == 4
    for policy in policies:
        assert "phase0_current_workspace_id" in policy["qual"]
        if policy["policyname"] != "phase0_t25_principals_scope":
            assert "phase0_principal_active" in policy["with_check"]

    attrs = probe.role_security_attributes(admin, role=probe.APP_ROLE)
    assert attrs["rolsuper"] is False
    assert attrs["rolbypassrls"] is False
    assert attrs["rolcanlogin"] is True

    owned = admin.execute(
        "SELECT count(*) AS total FROM pg_class "
        "WHERE relnamespace = %s::regnamespace "
        "AND relowner = (SELECT oid FROM pg_roles WHERE rolname = %s)",
        (probe.SCHEMA, probe.APP_ROLE),
    ).fetchone()["total"]
    assert owned == 0, "应用角色不得是任何表 owner（否则 FORCE RLS 之外仍可绕过）"

    app.rollback()
    actor = app.execute("SELECT current_user AS who").fetchone()["who"]
    assert actor == probe.APP_ROLE

    # deny 默认：没有 SET LOCAL 作用域时应用角色看不到任何租户行。
    for table in ("phase0_chunks", "phase0_documents"):
        total = app.execute(f"SELECT count(*) AS total FROM {probe.SCHEMA}.{table}").fetchone()
        assert total["total"] == 0, f"{table} 在无 scope 时必须 0 行"
    app.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute(
            f"INSERT INTO {probe.SCHEMA}.phase0_principals "
            "(workspace_id, principal_id, role, status, membership_revision) "
            "VALUES (%s, %s, 'owner', 'active', 1)",
            (seed.workspaces["A"], seed.principals["A"]),
        )
    app.rollback()
    print(
        "[rls] actor=p0_t25_app rolsuper=false rolbypassrls=false "
        "policies=4 tables_with_force=4 unscoped_rows=0 write_denied=42501"
    )


# ---------------------------------------------------------------------------
# 2) 旧版本 / 未发布 chunk 不可见（仍在表里）
# ---------------------------------------------------------------------------
def test_superseded_and_unpublished_chunks_are_invisible(
    admin: psycopg.Connection[Any],
    app: psycopg.Connection[Any],
    seed: probe.SeedData,
) -> None:
    old_rows = admin.execute(
        f"SELECT count(*) AS total FROM {probe.SCHEMA}.phase0_chunks "
        "WHERE chunk_id = ANY(%s)",
        (list(seed.old_chunks),),
    ).fetchone()["total"]
    staged_rows = admin.execute(
        f"SELECT count(*) AS total FROM {probe.SCHEMA}.phase0_chunks "
        "WHERE chunk_id = ANY(%s)",
        (list(seed.staged_chunks),),
    ).fetchone()["total"]
    assert old_rows == len(seed.old_chunks) == 12
    assert staged_rows == len(seed.staged_chunks) == 3

    with probe.scoped_transaction(
        app,
        workspace_id=seed.workspaces["A"],
        principal_id=seed.principals["A"],
    ):
        assert probe.visible_chunk_count(app) == 30
        assert probe.search(app, "legacy", mode="bm25").hits == ()
        assert probe.search(app, "legacy", mode="fts").hits == ()
        # 同一文档 v2 是 active：唯一 anchor 只命中 v2 chunk。
        hits = probe.search(app, "aa00v2c1", mode="bm25").hits
        assert [hit.index_version for hit in hits] == [2]
        assert probe.read_active_version(app, seed.switch_document) == 1

    retired = admin.execute(
        f"SELECT status FROM {probe.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
        (seed.workspaces["A"], seed.switch_document),
    ).fetchone()
    assert retired["status"] == "active"  # switch 文档：v1 仍是 active（等快照用例再切）
    legacy_status = admin.execute(
        f"SELECT status FROM {probe.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
        (seed.workspaces["A"], seed.mixed_document),
    ).fetchone()
    # 混合 profile 的 chunk 就在 v1 active 版本里：靠 profile/dim 过滤，不靠版本隔离。
    assert legacy_status["status"] == "active"

    with probe.scoped_transaction(
        app,
        workspace_id=seed.workspaces["B"],
        principal_id=seed.principals["B"],
    ):
        assert probe.visible_chunk_count(app) == 30
        assert probe.search(app, "staged-b0", mode="bm25").hits == ()
        assert probe.search(app, "staged-b0", mode="fts").hits == ()
    print(
        "[visibility] old_chunks_in_table=12 hidden_hits=0 "
        "staged_chunks_in_table=3 hidden_hits=0 visible_A=30 visible_B=30"
    )


# ---------------------------------------------------------------------------
# 3) 跨空间探测（≥24 次）与自报作用域拒绝
# ---------------------------------------------------------------------------
def test_cross_workspace_probes_never_leak(permissions: dict[str, Any]) -> None:
    cross = permissions["cross_workspace"]
    assert cross["probes"] == 24
    assert cross["read_attempts"] == 24
    assert cross["rows_read"] == 0
    assert cross["search_hits"] == 0
    assert cross["scope_shortcut_attempts"] == 24
    assert cross["scope_shortcut_rejected"] == 24
    print(
        f"[cross-workspace] probes={cross['probes']} read_attempts={cross['read_attempts']} "
        f"rows_read={cross['rows_read']} search_hits={cross['search_hits']} "
        f"shortcut_rejected={cross['scope_shortcut_rejected']}/{cross['scope_shortcut_attempts']}"
    )


def test_unscoped_connection_sees_nothing(permissions: dict[str, Any]) -> None:
    unscoped = permissions["unscoped"]
    assert unscoped["rows_visible_without_scope"] == 0
    assert unscoped["workspace_guc"] is False


# ---------------------------------------------------------------------------
# 4) 撤销成员 / 切换 workspace：同一连接立即可见性变化
# ---------------------------------------------------------------------------
def test_membership_revocation_changes_visibility_immediately(
    permissions: dict[str, Any],
) -> None:
    revocation = permissions["membership_revocation"]
    assert revocation["before_visible"] == 6
    assert revocation["after_visible"] == 0
    assert revocation["after_search_hits"] == 0
    assert revocation["restored_visible"] == 6
    print(
        f"[revocation] before={revocation['before_visible']} "
        f"after={revocation['after_visible']} "
        f"after_search_hits={revocation['after_search_hits']} "
        f"restored={revocation['restored_visible']}"
    )


def test_workspace_switch_changes_visibility_immediately(
    permissions: dict[str, Any],
) -> None:
    switching = permissions["workspace_switch"]
    assert switching["a_visible"] == 30
    assert switching["b_visible"] == 30
    assert switching["a_again_visible"] == 30
    assert switching["a_rows_foreign"] == 0
    print(
        f"[workspace-switch] A={switching['a_visible']} B={switching['b_visible']} "
        f"A_again={switching['a_again_visible']} foreign_rows={switching['a_rows_foreign']}"
    )


# ---------------------------------------------------------------------------
# 5) 混合 embedding profile 拒绝
# ---------------------------------------------------------------------------
def test_mixed_embedding_profile_is_refused(permissions: dict[str, Any]) -> None:
    mixed = permissions["mixed_embedding"]
    assert mixed["chunks_present_in_table"] == 3
    assert mixed["leaked_mixed_chunks"] == 0
    assert mixed["vector_hits"] == 0
    assert mixed["profile_filtered"] >= 3
    assert mixed["foreign_query_rejected"] is True
    print(
        f"[mixed-embedding] chunks={mixed['chunks_present_in_table']} "
        f"vector_hits={mixed['vector_hits']} filtered={mixed['profile_filtered']} "
        f"foreign_query_rejected={mixed['foreign_query_rejected']}"
    )


# ---------------------------------------------------------------------------
# 6) 守卫：REPEATABLE READ / scope / mode / embedding 契约
# ---------------------------------------------------------------------------
def test_search_guards_fail_closed(
    app: psycopg.Connection[Any], seed: probe.SeedData
) -> None:
    app.rollback()
    previous_isolation = app.isolation_level
    try:
        app.isolation_level = previous_isolation
        with pytest.raises(probe.SnapshotIsolationRequired):
            probe.search(app, "topic00", mode="bm25")

        with app.transaction():
            with pytest.raises(probe.SnapshotIsolationRequired):
                probe.search(app, "topic00", mode="bm25")

        app.isolation_level = IsolationLevel.REPEATABLE_READ
        with app.transaction():
            with pytest.raises(probe.ScopeRequiredError):
                probe.search(app, "topic00", mode="bm25")
            scope.apply_scope(
                app,
                workspace_id=seed.workspaces["A"],
                principal_id=seed.principals["A"],
            )
            with pytest.raises(probe.ScopeMismatchError):
                probe.search(app, "topic00", mode="bm25", workspace_id=seed.workspaces["B"])
            with pytest.raises(probe.ModeInvalid):
                probe.search(app, "topic00", mode="hybrid")
            with pytest.raises(probe.EmbeddingProfileMismatch):
                probe.search(app, "topic00", mode="vector", embedding_profile="other.model")
    finally:
        if app.info.transaction_status is TransactionStatus.IDLE:
            app.isolation_level = previous_isolation
    print(
        "[guards] isolation_required=1 scope_required=1 scope_mismatch=1 "
        "mode_invalid=1 embedding_profile=1"
    )


# ---------------------------------------------------------------------------
# 7) 单快照：并发切换 active 指针
# ---------------------------------------------------------------------------
def test_single_snapshot_keeps_pointer_and_chunks_consistent(
    snapshot: dict[str, Any],
) -> None:
    assert snapshot["isolation"] == "repeatable read"
    assert snapshot["before_version"] == 1
    assert snapshot["during_version"] == 1, "旧事务内指针必须仍是 v1（快照隔离）"
    assert snapshot["after_version"] == 2, "新事务必须立即看到已提交的 v2"
    assert snapshot["during_switch_hits"] == 0, "v2 chunk 在旧快照内不可见，不得读到混版本"
    assert snapshot["after_switch_hits"] == 1
    assert snapshot["after_switch_versions"] == [2]
    assert snapshot["pointer_chunk_mismatch"] == 0
    assert snapshot["mixed_version_detected"] is False
    print(
        f"[single-snapshot] before={snapshot['before_version']} "
        f"during={snapshot['during_version']} after={snapshot['after_version']} "
        f"during_v2_hits={snapshot['during_switch_hits']} "
        f"after_v2_hits={snapshot['after_switch_hits']} mixed_versions=0"
    )


# ---------------------------------------------------------------------------
# 8) 100 QA 三路对照
# ---------------------------------------------------------------------------
def test_three_way_metrics_over_100_qa(metrics: dict[str, Any]) -> None:
    assert metrics["qa_total"] == 100
    assert metrics["qa_runs"] == 100
    assert metrics["answerable"] == 88
    assert metrics["no_answer"] == 8
    assert metrics["superseded"] == 4

    for mode in ("vector", "fts", "bm25", "rrf"):
        block = metrics["modes"][mode]
        assert 0.0 < block["recall_at_5"] <= 1.0, f"{mode} recall 必须落在 (0, 1]"
        assert 0.0 <= block["mrr_at_10"] <= 1.0
        assert 0.0 <= block["ndcg_at_5"] <= 1.0
        assert block["hit_rate_at_10"] >= block["recall_at_5"]
        assert block["no_answer_false_positives"] == 0
        assert block["superseded_hits"] == 0
        assert block["avg_latency_ms"] >= 0.0

    anchor = metrics["subsets"]["anchor"]
    assert anchor["count"] == 54
    for mode in ("vector", "fts", "bm25"):
        assert anchor["modes"][mode]["recall_at_5"] == 1.0, (
            f"{mode} 在唯一 anchor 子集必须 Recall@5=1.0"
        )

    spaced = metrics["subsets"]["chinese_spaced"]["modes"]
    unspaced = metrics["subsets"]["chinese_unspaced"]["modes"]
    assert metrics["subsets"]["chinese_spaced"]["count"] == 5
    assert metrics["subsets"]["chinese_unspaced"]["count"] == 5
    assert spaced["fts"]["recall_at_5"] == 1.0, "有空格中文：simple 分词可命中"
    assert unspaced["fts"]["recall_at_5"] == 0.0, "无空格中文：simple 整段成词，必须测出 0"
    assert unspaced["bm25"]["recall_at_5"] == 1.0, "bigram BM25 必须补上中文召回"

    diagnostics = metrics["diagnostics"]
    assert diagnostics["hits_with_wrong_source_or_version"] == 0
    assert diagnostics["hits_checked_for_source"] > 0
    assert diagnostics["vector_below_threshold"] > 0

    summary = {
        mode: {
            "recall_at_5": round(metrics["modes"][mode]["recall_at_5"], 4),
            "mrr_at_10": round(metrics["modes"][mode]["mrr_at_10"], 4),
            "ndcg_at_5": round(metrics["modes"][mode]["ndcg_at_5"], 4),
            "p50_ms": round(metrics["modes"][mode]["p50_latency_ms"], 2),
        }
        for mode in ("vector", "fts", "bm25", "rrf")
    }
    print(f"[three-way] qa=100 answerable=88 summary={json.dumps(summary)}")
    print(
        "[three-way-subsets] anchor_count=54 "
        f"chinese_spaced_fts={spaced['fts']['recall_at_5']} "
        f"chinese_unspaced_fts={unspaced['fts']['recall_at_5']} "
        f"chinese_unspaced_bm25={unspaced['bm25']['recall_at_5']}"
    )
    print(
        f"[three-way-diagnostics] vector_profile_filtered="
        f"{diagnostics['vector_profile_filtered']} "
        f"vector_below_threshold={diagnostics['vector_below_threshold']} "
        f"hits_checked={diagnostics['hits_checked_for_source']} "
        f"wrong_source={diagnostics['hits_with_wrong_source_or_version']}"
    )


# ---------------------------------------------------------------------------
# 9) 布尔组合 + 权限过滤组合
# ---------------------------------------------------------------------------
def test_boolean_combinations_do_not_escalate(
    boolean_combos: list[dict[str, Any]],
) -> None:
    assert len(boolean_combos) == 7
    names = {item["name"] for item in boolean_combos}
    assert names == {
        "local_anchor_AND_shared_topic",
        "foreign_anchor_AND_local_topic",
        "absent_token_AND_valid_topic",
        "vector_scope_filter",
    }
    for item in boolean_combos:
        assert item["all_local"] is True, f"{item['name']}/{item['mode']} 不得跨空间"
        if item["name"] == "local_anchor_AND_shared_topic":
            assert item["hits"] >= 1
            assert item["gold_first"] is True
        elif item["name"] == "vector_scope_filter":
            assert item["hits"] >= 1
        elif item["mode"] == "fts":
            # FTS 是 AND 语义：任一 token 缺失就不该命中。
            assert item["hits"] == 0
    print(f"[boolean-combos] {json.dumps(boolean_combos, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# 10) 证据 JSON（落盘 + 脱敏）
# ---------------------------------------------------------------------------
def test_evidence_json_is_written_and_redacted(
    admin: psycopg.Connection[Any],
    seed: probe.SeedData,
    schema: str,
    metrics: dict[str, Any],
    permissions: dict[str, Any],
    snapshot: dict[str, Any],
    boolean_combos: list[dict[str, Any]],
    admin_dsn: str,
) -> None:
    payload = probe.build_evidence(
        seed=seed,
        metrics=metrics,
        permissions=permissions,
        snapshot=snapshot,
        boolean_combos=boolean_combos,
        schema_digest=schema,
        dsn=admin_dsn,
        role_attributes=probe.role_security_attributes(admin, role=probe.APP_ROLE),
        server_version=str(
            admin.execute("SHOW server_version").fetchone()["server_version"]
        ),
    )
    path = probe.write_evidence(payload)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)

    assert data["task"] == "P0-T25"
    assert data["poc"] == "PoC-09"
    assert data["metrics"]["qa_total"] == 100
    assert data["corpus"]["chunks"] == seed.totals["chunks"]
    assert data["corpus"]["old_chunks"] == 12
    assert data["corpus"]["staged_chunks"] == 3
    assert data["corpus"]["qa_total"] == 100
    assert data["schema"]["ddl_sha256"] == schema
    assert data["single_snapshot"]["after_version"] == 2
    assert data["environment"]["role_attributes"]["rolbypassrls"] is False
    assert ":***@" in data["environment"]["dsn"]
    assert "phase0-local-only" not in text, "证据里不得出现真实口令"
    assert "sk-" not in text, "证据里不得出现密钥"
    assert data["not_run"], "必须显式列出 NOT_RUN 项"
    assert any("ANN" in item for item in data["not_run"])
    assert any("embedding" in item for item in data["not_run"])
    assert data["git"]["available"] is True, "只读 git 信息缺失时应显式失败而不是伪造"
    assert len(data["git"]["head"]) == 40

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(
        f"[evidence] path={path} bytes={path.stat().st_size} sha256={digest} "
        f"head={data['git']['head'][:12]} dirty={data['git']['dirty']}"
    )
