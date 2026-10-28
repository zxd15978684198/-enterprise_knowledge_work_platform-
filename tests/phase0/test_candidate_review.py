"""P0-T27 来源质量 / 去重 / 候选 / Review 契约的验收测试。

验收依据（Backlog P0-T27 + TDD06 §4–5 + TDD07 §5）：
* 来源质量分是**确定性**整数评分，分项写进入库行，可逐项复算；
* 去重双闸：精确重复（url+content）幂等并簇；近似重复**只提示不合并**；
* 候选准入：硬拒绝不留行（避免同内容被永久锁死），软拒绝落 ``needs_review``；
* ReviewDecision 只能由 ``kind=human`` 主体产出，身份/时间/理由三要素必填，
  同一候选至多一条 approved；
* 发布唯一入口必须携带 approved 决定，且决定的目标 KB 与发布目标一致；
* 所有事实表只追加：UPDATE/DELETE 一律被 DB 触发器拒绝。

证据边界：``007_candidates.sql`` 未建 RLS/角色（Phase 0 隔离，见迁移注释），
因此这里只验证**契约与 DB 约束**，不能替代生产身份域（T03 C-07 BLOCKED）。
真浏览器 / 真网络路径不在本任务范围（T16 / T17）。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

import candidate_review as cr

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "experiments" / "phase0" / "db" / "migrations" / "007_candidates.sql"

#: 固定"当前时刻"：评分/证据有效期全部相对它复算，避免 wall clock 抖动。
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

LOCATOR: dict[str, Any] = {
    "schema": 1,
    "format": "markdown",
    "kind": "paragraph",
    "paragraph": 2,
}

HUMAN = uuid.UUID("a0000000-0000-4000-8000-000000000001")
SERVICE = uuid.UUID("a0000000-0000-4000-8000-000000000002")
DISABLED = uuid.UUID("a0000000-0000-4000-8000-000000000003")

#: 近重复用例的公共底稿：12 个词 × 8 次重复，追加一个词后 simhash 距离稳定 ≤ 3。
NEAR_DUP_BASE = " ".join(
    [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
        "lambda",
        "mu",
    ]
    * 8
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@dataclass
class Env:
    """一次测试用的隔离环境：干净 schema + 两个 workspace + 合成主体。"""

    conn: psycopg.Connection[Any]
    store: cr.CandidateReviewStore
    workspace_id: uuid.UUID
    other_workspace: uuid.UUID
    kb_id: uuid.UUID

    def count(self, table: str, where: str = "true", params: tuple[Any, ...] = ()) -> int:
        row = self.conn.execute(
            f"select count(*) as n from {cr.SCHEMA}.{table} where {where}", params
        ).fetchone()
        assert row is not None
        return int(row["n"])


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection[Any]]:
    connection = cr.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def env(conn: psycopg.Connection[Any]) -> Iterator[Env]:
    cr.reset_schema(conn)
    cr.ensure_schema(conn)
    workspace_id = uuid.uuid4()
    other_workspace = uuid.uuid4()
    with conn.transaction():
        conn.execute(
            "insert into p0_t27.phase0_workspaces (workspace_id, name) values (%s, %s), (%s, %s)",
            (workspace_id, "t27-main", other_workspace, "t27-other"),
        )
        conn.execute(
            "insert into p0_t27.phase0_principals "
            "(workspace_id, principal_id, kind, status) values "
            "(%s, %s, 'human', 'active'), (%s, %s, 'service', 'active'), "
            "(%s, %s, 'human', 'disabled')",
            (
                workspace_id,
                HUMAN,
                workspace_id,
                SERVICE,
                workspace_id,
                DISABLED,
            ),
        )
    yield Env(
        conn=conn,
        store=cr.CandidateReviewStore(conn),
        workspace_id=workspace_id,
        other_workspace=other_workspace,
        kb_id=cr.kb_id_for_workspace(workspace_id),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def register_source(
    env: Env,
    *,
    url: str = "https://nist.gov/report",
    content: str = "正式的报告正文",
    license: str = "allowed",
    fetch_policy: str = "allow",
    published_at: datetime | None = NOW - timedelta(days=1),
    workspace_id: uuid.UUID | None = None,
    is_primary: bool = True,
    is_reprint: bool = False,
) -> cr.SourceRecord:
    spec = cr.SourceSpec.create(
        workspace_id or env.workspace_id,
        url,
        content=content,
        license=license,
        fetch_policy=fetch_policy,
        published_at=published_at,
        is_primary=is_primary,
        is_reprint=is_reprint,
        first_seen_at=NOW - timedelta(days=2),
    )
    return env.store.register_source(spec)


def approve_source(env: Env, record: cr.SourceRecord) -> None:
    env.store.append_source_event(
        record.workspace_id,
        record.source_id,
        "approved",
        actor_kind="human",
        actor_ref="t27-reviewer",
        reason_code="SOURCE_APPROVED",
    )


def make_draft(
    env: Env,
    record: cr.SourceRecord,
    *,
    content: str = "正式的报告正文，含一条关键事实。",
    facts: tuple[cr.FactSpec, ...] | None = None,
    evidence: tuple[cr.EvidenceSpec, ...] | None = None,
    origin: str = "ingest",
) -> cr.CandidateDraft:
    if facts is None:
        facts = (cr.FactSpec(fact_id="f1", text="关键事实：延迟低于 5ms。"),)
    if evidence is None:
        evidence = (
            cr.EvidenceSpec(
                fact_id="f1",
                locator=LOCATOR,
                quote="延迟低于 5ms",
                observed_at=NOW - timedelta(hours=1),
                verified=True,
            ),
        )
    return cr.CandidateDraft(
        workspace_id=record.workspace_id,
        source_id=record.source_id,
        source_version_id=uuid.uuid4(),
        content=content,
        facts=facts,
        evidence=evidence,
        origin=origin,
    )


def review(
    env: Env,
    candidate_id: uuid.UUID,
    *,
    decision: str = "approved",
    reviewer: uuid.UUID = HUMAN,
    kb_id: uuid.UUID | None = None,
    reason_code: str = "POLICY_OK",
    reason_text: str = "人工核对来源与证据后入库",
    decided_at: datetime | None = None,
) -> cr.ReviewOutcome:
    return env.store.submit_review(
        cr.ReviewRequest(
            workspace_id=env.workspace_id,
            candidate_id=candidate_id,
            reviewer_principal_id=reviewer,
            kb_id=kb_id or env.kb_id,
            decision=decision,
            reason_code=reason_code,
            reason_text=reason_text,
            decided_at=decided_at or NOW,
        )
    )


def submit(env: Env, draft: cr.CandidateDraft) -> cr.SubmissionOutcome:
    return env.store.submit_candidate(draft, at=NOW)


def approved_candidate(env: Env) -> tuple[cr.SourceRecord, cr.SubmissionOutcome]:
    record = register_source(env)
    approve_source(env, record)
    outcome = submit(env, make_draft(env, record))
    assert outcome.reason_code == cr.R_AWAITING_REVIEW
    return record, outcome


# ---------------------------------------------------------------------------
# 1. 常量与评分：确定性、可逐项复算
# ---------------------------------------------------------------------------
def test_quality_weights_sum_to_one_hundred() -> None:
    assert sum(cr.QUALITY_WEIGHTS.values()) == 100
    assert cr.MIN_AUTO_ADMIT_SCORE == 60
    assert cr.NEAR_DUPLICATE_MAX_DISTANCE == 3
    assert cr.QUALITY_ALGORITHM == "t27-quality-v1"


def test_reason_codes_are_unique_ascii_and_documented() -> None:
    assert len(cr.REASON_CODES) == len(set(cr.REASON_CODES))
    for code in cr.REASON_CODES:
        assert code == code.upper()
        assert code.replace("_", "").isalnum()


def test_authority_exact_beats_suffix_and_unknown_is_eight() -> None:
    assert cr._authority_value("nist.gov") == 35
    assert cr._authority_value("data.gov") == 34
    assert cr._authority_value("example.com") == 12
    assert cr._authority_value("unknown-host") == 8


def test_freshness_buckets_are_integer_days() -> None:
    assert cr._freshness_value(NOW, None) == 4
    assert cr._freshness_value(NOW, NOW + timedelta(days=1)) == 0
    assert cr._freshness_value(NOW, NOW - timedelta(days=7)) == 20
    assert cr._freshness_value(NOW, NOW - timedelta(days=30)) == 14
    assert cr._freshness_value(NOW, NOW - timedelta(days=180)) == 8
    assert cr._freshness_value(NOW, NOW - timedelta(days=365)) == 4
    assert cr._freshness_value(NOW, NOW - timedelta(days=366)) == 0


def test_primacy_zeroed_for_reprint_site() -> None:
    primary = cr.score_quality(
        cr.QualityInput(
            domain="nist.gov",
            is_primary=True,
            is_reprint=False,
            license="allowed",
            observed_at=NOW,
            published_at=NOW - timedelta(days=1),
            evidenced_critical=1,
            critical_total=1,
        )
    )
    reprint = cr.score_quality(
        cr.QualityInput(
            domain="nist.gov",
            is_primary=True,
            is_reprint=True,
            license="allowed",
            observed_at=NOW,
            published_at=NOW - timedelta(days=1),
            evidenced_critical=1,
            critical_total=1,
        )
    )
    assert primary.total == 100
    assert reprint.total == 80
    assert reprint.component("primacy").contribution == 0


def test_score_components_recompute_to_total() -> None:
    item = cr.QualityInput(
        domain="example.com",
        is_primary=True,
        is_reprint=False,
        license="unknown",
        observed_at=NOW,
        published_at=None,
        evidenced_critical=1,
        critical_total=1,
    )
    score = cr.score_quality(item)
    assert score.total == 55
    assert score.total == sum(component.contribution for component in score.components)
    assert score.as_dict()["algorithm"] == cr.QUALITY_ALGORITHM


def test_score_is_deterministic_under_repeated_calls() -> None:
    item = cr.QualityInput(
        domain="arxiv.org",
        is_primary=True,
        is_reprint=False,
        license="allowed",
        observed_at=NOW,
        published_at=NOW - timedelta(days=30),
        evidenced_critical=2,
        critical_total=4,
    )
    first = cr.score_quality(item)
    second = cr.score_quality(item)
    assert first == second
    assert first.component("evidence").contribution == 7


def test_quality_input_rejects_impossible_evidence_counts() -> None:
    with pytest.raises(cr.CandidateReviewError) as info:
        cr.QualityInput(
            domain="example.com",
            is_primary=True,
            is_reprint=False,
            license="allowed",
            observed_at=NOW,
            published_at=None,
            evidenced_critical=3,
            critical_total=1,
        )
    assert info.value.code == cr.R_LOCATOR_INVALID


# ---------------------------------------------------------------------------
# 2. 规范化与指纹
# ---------------------------------------------------------------------------
def test_canonical_url_lowercases_host_and_drops_default_port() -> None:
    assert cr.canonical_url("HTTPS://Example.COM:443/a/b#frag") == "https://example.com/a/b"
    assert cr.canonical_url("http://example.com") == "http://example.com/"
    assert cr.canonical_url("https://example.com:8443/x") == "https://example.com:8443/x"
    assert cr.canonical_url("https://example.com/a#fragment") == "https://example.com/a"


def test_canonical_url_keeps_path_case_and_semantic_query() -> None:
    assert cr.canonical_url("https://Example.COM/A/B?Q=1") == "https://example.com/A/B?Q=1"


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/a", "https://user:pw@example.com/a", "https:///no-host", ""],
)
def test_canonical_url_rejects_unsafe_inputs(url: str) -> None:
    with pytest.raises(cr.CandidateReviewError) as info:
        cr.canonical_url(url)
    assert info.value.code == cr.R_LOCATOR_INVALID


def test_hashes_are_hex64_and_dedup_key_is_stable() -> None:
    digest_url = cr.url_hash("https://example.com/a?x=1")
    digest_content = cr.content_hash("payload")
    assert len(digest_url) == 64
    assert len(digest_content) == 64
    assert cr.dedup_key(digest_url, digest_content) == cr.dedup_key(digest_url, digest_content)


def test_content_hash_is_byte_exact_not_whitespace_normalised() -> None:
    assert cr.content_hash("a b") != cr.content_hash("a  b")


def test_quote_hash_collapses_whitespace_only() -> None:
    assert cr.quote_hash("  延迟低于\n 5ms ") == cr.quote_hash("延迟低于 5ms")
    with pytest.raises(cr.CandidateReviewError):
        cr.quote_hash("   ")


def test_simhash_is_deterministic_and_detects_near_duplicates() -> None:
    base = NEAR_DUP_BASE
    digest = cr.simhash64(base)
    assert len(digest) == 16
    assert digest == cr.simhash64(base)
    assert cr.hamming64(digest, cr.simhash64(base + " omega")) <= cr.NEAR_DUPLICATE_MAX_DISTANCE
    assert cr.hamming64(digest, cr.simhash64("intro " + base)) > cr.NEAR_DUPLICATE_MAX_DISTANCE


def test_detect_pii_finds_email_and_cn_mobile() -> None:
    found = cr.detect_pii("联系 a.b@example.com 或 13812345678")
    assert set(found) == {"email", "cn_mobile"}
    assert cr.detect_pii("纯说明文字，没有个人信息") == ()


def test_normalise_locator_reuses_t11_schema_and_rejects_offsets() -> None:
    normalised = cr.normalise_locator(LOCATOR)
    assert normalised["kind"] == "paragraph"
    assert normalised["paragraph"] == 2
    with pytest.raises(cr.CandidateReviewError):
        cr.normalise_locator({**LOCATOR, "offset": 12})


def test_evidence_spec_rejects_inverted_validity_window() -> None:
    with pytest.raises(cr.CandidateReviewError):
        cr.EvidenceSpec(
            fact_id="f1",
            locator=LOCATOR,
            quote="q",
            observed_at=NOW,
            expires_at=NOW,
        )


def test_evidence_spec_rejects_unknown_fact_reference() -> None:
    with pytest.raises(cr.CandidateReviewError):
        cr.CandidateDraft(
            workspace_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            source_version_id=uuid.uuid4(),
            content="x",
            facts=(cr.FactSpec(fact_id="f1", text="a"),),
            evidence=(
                cr.EvidenceSpec(
                    fact_id="f2", locator=LOCATOR, quote="q", observed_at=NOW - timedelta(days=1)
                ),
            ),
        )


def test_evidence_validity_uses_observed_and_expires() -> None:
    valid = cr.EvidenceSpec(
        fact_id="f1",
        locator=LOCATOR,
        quote="q",
        observed_at=NOW - timedelta(hours=1),
        verified=True,
    )
    assert cr.evidence_is_valid(valid, NOW) is True
    # deny-by-default：没显式声明核验的证据一律不算有效。
    assert cr.evidence_is_valid(
        cr.EvidenceSpec(fact_id="f1", locator=LOCATOR, quote="q", observed_at=NOW), NOW
    ) is False
    unverified = cr.EvidenceSpec(
        fact_id="f1",
        locator=LOCATOR,
        quote="q",
        observed_at=NOW - timedelta(hours=1),
        verified=False,
    )
    assert cr.evidence_is_valid(unverified, NOW) is False
    expired = cr.EvidenceSpec(
        fact_id="f1",
        locator=LOCATOR,
        quote="q",
        observed_at=NOW - timedelta(days=10),
        expires_at=NOW - timedelta(days=1),
    )
    assert cr.evidence_is_valid(expired, NOW) is False


def test_review_request_rejects_unknown_decision() -> None:
    with pytest.raises(cr.CandidateReviewError) as info:
        cr.ReviewRequest(
            workspace_id=uuid.uuid4(),
            candidate_id=uuid.uuid4(),
            reviewer_principal_id=uuid.uuid4(),
            kb_id=uuid.uuid4(),
            decision="maybe",
            reason_code="X",
            reason_text="y",
            decided_at=NOW,
        )
    assert info.value.code == cr.R_LOCATOR_INVALID


def test_kb_id_for_workspace_is_stable_uuid5() -> None:
    workspace_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    first = cr.kb_id_for_workspace(workspace_id)
    assert first == cr.kb_id_for_workspace(workspace_id)
    assert first.version == 5


# ---------------------------------------------------------------------------
# 3. 迁移与 DB 层约束
# ---------------------------------------------------------------------------
def test_migration_defines_append_only_trigger_for_every_fact_table() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for table in cr.APPEND_ONLY_TABLES:
        assert f"CREATE TRIGGER {table}_append_only" in sql
    assert len(cr.APPEND_ONLY_TABLES) == 9


def test_migration_enforces_human_only_and_single_approval() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "reviewer_kind       text NOT NULL CHECK (reviewer_kind = 'human')" in sql
    assert "CREATE UNIQUE INDEX phase0_review_decisions_one_approval" in sql
    assert "evidence_count     smallint NOT NULL CHECK (evidence_count >= 1)" in sql


def test_ensure_schema_is_idempotent_and_ledger_matches_file(env: Env) -> None:
    first = cr.ensure_schema(env.conn)
    second = cr.ensure_schema(env.conn)
    on_disk = hashlib.sha256(MIGRATION_PATH.read_bytes()).hexdigest()
    assert first == second == on_disk
    row = env.conn.execute(
        "select sha256, filename from p0_t27.phase0_schema_migrations where migration_id = %s",
        (cr.MIGRATION_ID,),
    ).fetchone()
    assert row is not None
    assert row["sha256"] == on_disk
    assert row["filename"] == MIGRATION_PATH.name


def test_ensure_schema_refuses_silent_drift(env: Env) -> None:
    cr.ensure_schema(env.conn)
    with env.conn.transaction():
        env.conn.execute(
            "update p0_t27.phase0_schema_migrations set sha256 = %s where migration_id = %s",
            ("0" * 64, cr.MIGRATION_ID),
        )
    with pytest.raises(cr.CandidateReviewError) as info:
        cr.ensure_schema(env.conn)
    assert info.value.code == cr.R_MIGRATION_DIGEST_MISMATCH


def test_append_only_trigger_is_bound_to_every_table(env: Env) -> None:
    """目录级证据：9 张追加史表都绑定了 BEFORE UPDATE/DELETE 的**行级**触发器。

    行级触发器在零行表上不会触发，所以"删空表不报错"是 Postgres 语义而不是实现缺陷；
    逐表的功能验证放在 `populate_every_append_only_table` 之后。
    """

    rows = env.conn.execute(
        """
        select c.relname as table_name, t.tgtype
        from pg_trigger as t
        join pg_class as c on c.oid = t.tgrelid
        join pg_namespace as n on n.oid = c.relnamespace
        where n.nspname = %s
          and not t.tgisinternal
          and t.tgname = c.relname || '_append_only'
        """,
        (cr.SCHEMA,),
    ).fetchall()
    covered = {str(row["table_name"]): int(row["tgtype"]) for row in rows}
    assert set(cr.APPEND_ONLY_TABLES) <= set(covered), covered
    for table, tgtype in covered.items():
        assert tgtype & 1, f"{table} 的触发器不是行级"
        assert tgtype & 8, f"{table} 未拦截 delete"
        assert tgtype & 16, f"{table} 未拦截 update"


def populate_every_append_only_table(env: Env) -> set[str]:
    """跑通一条 happy path，让 9 张追加史表都有行。"""

    record = register_source(env)
    approve_source(env, record)
    submit(env, make_draft(env, record, content=NEAR_DUP_BASE))
    second = submit(env, make_draft(env, record, content=NEAR_DUP_BASE + " omega"))
    assert second.reason_code == cr.R_NEAR_DUPLICATE
    decision = review(env, second.candidate_id)
    env.store.promote(
        env.workspace_id,
        second.candidate_id,
        review_decision_id=decision.review_decision_id,
        at=NOW,
    )
    return {table for table in cr.APPEND_ONLY_TABLES if env.count(table) > 0}


def test_append_only_rejects_delete_on_every_populated_table(env: Env) -> None:
    populated = populate_every_append_only_table(env)
    assert populated == set(cr.APPEND_ONLY_TABLES), populated
    for table in sorted(populated):
        with pytest.raises(psycopg.errors.CheckViolation):
            with env.conn.transaction():
                env.conn.execute(f"delete from {cr.SCHEMA}.{table}")


def test_append_only_rejects_update_on_sources_and_candidates(env: Env) -> None:
    populate_every_append_only_table(env)
    with pytest.raises(psycopg.errors.CheckViolation):
        with env.conn.transaction():
            env.conn.execute(
                f"update {cr.SCHEMA}.phase0_sources set domain = 'evil.example'"
            )
    with pytest.raises(psycopg.errors.CheckViolation):
        with env.conn.transaction():
            env.conn.execute(
                f"update {cr.SCHEMA}.phase0_candidates set quality_score = 0"
            )


# ---------------------------------------------------------------------------
# 4. 来源注册与状态
# ---------------------------------------------------------------------------
def test_register_source_is_idempotent_and_emits_one_event(env: Env) -> None:
    first = register_source(env)
    second = register_source(env)
    assert first.source_id == second.source_id
    assert first.canonical_url == second.canonical_url
    assert env.count("phase0_sources") == 1
    assert env.count("phase0_source_events", "event_type = 'captured'") == 1


def test_register_source_isolated_across_workspaces(env: Env) -> None:
    first = register_source(env)
    second = register_source(env, workspace_id=env.other_workspace)
    assert first.source_id != second.source_id
    assert first.url_hash == second.url_hash
    assert env.count("phase0_sources") == 2


def test_source_state_follows_appended_events(env: Env) -> None:
    record = register_source(env)
    assert env.store.source_state(record.workspace_id, record.source_id) == "captured"
    approve_source(env, record)
    assert env.store.source_state(record.workspace_id, record.source_id) == "approved"
    env.store.append_source_event(
        record.workspace_id,
        record.source_id,
        "revoked",
        actor_kind="human",
        actor_ref="t27-reviewer",
        reason_code="SOURCE_REVOKED",
    )
    assert env.store.source_state(record.workspace_id, record.source_id) == "revoked"


def test_unknown_source_event_is_rejected(env: Env) -> None:
    record = register_source(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.append_source_event(
            record.workspace_id,
            record.source_id,
            "teleported",
            actor_kind="human",
            actor_ref="x",
            reason_code="X",
        )
    assert info.value.code == cr.R_LOCATOR_INVALID


# ---------------------------------------------------------------------------
# 5. 候选准入：硬拒绝不留行
# ---------------------------------------------------------------------------
def test_submit_requires_registered_source(env: Env) -> None:
    draft = cr.CandidateDraft(
        workspace_id=env.workspace_id,
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        content="x",
        facts=(cr.FactSpec(fact_id="f1", text="a"),),
        evidence=(
            cr.EvidenceSpec(
                fact_id="f1", locator=LOCATOR, quote="q", observed_at=NOW - timedelta(hours=1)
            ),
        ),
    )
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, draft)
    assert info.value.code == cr.R_SOURCE_MISSING
    assert env.count("phase0_candidates") == 0


@pytest.mark.parametrize(
    ("url", "kwargs", "code"),
    [
        ("https://nist.gov/a", {"license": "forbidden"}, cr.R_LICENSE_FORBIDDEN),
        ("https://nist.gov/b", {"fetch_policy": "deny"}, cr.R_FETCH_DENIED),
        ("http://127.0.0.1/c", {}, cr.R_BLOCKED_DOMAIN),
    ],
)
def test_hard_gate_rejects_without_leaving_rows(
    env: Env, url: str, kwargs: dict[str, Any], code: str
) -> None:
    record = register_source(env, url=url, **kwargs)
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, make_draft(env, record))
    assert info.value.code == code
    assert env.count("phase0_candidates") == 0


def test_hard_gate_rejects_pii_in_content(env: Env) -> None:
    record = register_source(env)
    draft = make_draft(env, record, content="正文里带了 13812345678 这个手机号")
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, draft)
    assert info.value.code == cr.R_PII_DETECTED
    assert env.count("phase0_candidates") == 0


def test_hard_gate_rejects_missing_evidence(env: Env) -> None:
    record = register_source(env)
    draft = make_draft(env, record, evidence=())
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, draft)
    assert info.value.code == cr.R_EVIDENCE_MISSING


def test_hard_gate_rejects_unverified_evidence(env: Env) -> None:
    record = register_source(env)
    evidence = (
        cr.EvidenceSpec(
            fact_id="f1",
            locator=LOCATOR,
            quote="延迟低于 5ms",
            observed_at=NOW - timedelta(hours=1),
            verified=False,
        ),
    )
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, make_draft(env, record, evidence=evidence))
    assert info.value.code == cr.R_EVIDENCE_UNVERIFIABLE


def test_hard_gate_rejects_expired_evidence(env: Env) -> None:
    record = register_source(env)
    evidence = (
        cr.EvidenceSpec(
            fact_id="f1",
            locator=LOCATOR,
            quote="延迟低于 5ms",
            observed_at=NOW - timedelta(days=10),
            expires_at=NOW - timedelta(days=1),
            verified=True,
        ),
    )
    with pytest.raises(cr.CandidateReviewError) as info:
        submit(env, make_draft(env, record, evidence=evidence))
    assert info.value.code == cr.R_EVIDENCE_EXPIRED


# ---------------------------------------------------------------------------
# 6. 候选准入：软拒绝落 needs_review
# ---------------------------------------------------------------------------
def test_low_quality_candidate_goes_needs_review(env: Env) -> None:
    record = register_source(
        env,
        url="https://example.com/low",
        license="unknown",
        published_at=None,
        is_primary=False,
        content="低质来源",
    )
    outcome = submit(env, make_draft(env, record, content="低质来源的内容"))
    assert outcome.status == "needs_review"
    assert outcome.reason_code == cr.R_SCORE_BELOW_THRESHOLD
    assert outcome.quality.total < cr.MIN_AUTO_ADMIT_SCORE


def test_unapproved_source_goes_review_requested(env: Env) -> None:
    record = register_source(env)
    outcome = submit(env, make_draft(env, record))
    assert outcome.reason_code == cr.R_SOURCE_NOT_APPROVED
    assert outcome.status == "review_requested"
    assert outcome.quality.total == 100


def test_clean_candidate_awaits_human_review(env: Env) -> None:
    record = register_source(env)
    approve_source(env, record)
    outcome = submit(env, make_draft(env, record))
    assert outcome.created is True
    assert outcome.reason_code == cr.R_AWAITING_REVIEW
    assert outcome.status == "review_requested"
    assert outcome.quality.total == 100
    assert env.count("phase0_candidate_facts") == 1
    assert env.count("phase0_candidate_evidence") == 1


def test_replan_origin_always_needs_review(env: Env) -> None:
    record = register_source(env)
    approve_source(env, record)
    outcome = submit(env, make_draft(env, record, origin="replan_needs_review"))
    assert outcome.reason_code == cr.R_REPLAN_NEEDS_REVIEW
    assert outcome.needs_review is True


# ---------------------------------------------------------------------------
# 7. 去重：精确幂等、近似只提示
# ---------------------------------------------------------------------------
def test_duplicate_content_returns_existing_candidate(env: Env) -> None:
    record = register_source(env)
    approve_source(env, record)
    draft = make_draft(env, record)
    first = submit(env, draft)
    second = submit(env, draft)
    assert second.created is False
    assert second.candidate_id == first.candidate_id
    assert second.reason_code == cr.R_DUPLICATE_CONTENT
    assert env.count("phase0_candidates") == 1
    assert env.count("phase0_duplicate_clusters") == 1


def test_near_duplicate_is_hint_only_and_creates_new_candidate(env: Env) -> None:
    record = register_source(env)
    approve_source(env, record)
    base = NEAR_DUP_BASE
    first = submit(env, make_draft(env, record, content=base))
    second = submit(env, make_draft(env, record, content=base + " omega"))
    assert second.created is True
    assert second.candidate_id != first.candidate_id
    assert second.near_duplicate_of == first.candidate_id
    assert second.reason_code == cr.R_NEAR_DUPLICATE
    assert second.status == "needs_review"
    assert env.count("phase0_candidates") == 2


# ---------------------------------------------------------------------------
# 8. Review：三要素 + 人工主体 + 幂等/冲突
# ---------------------------------------------------------------------------
def test_review_requires_human_principal(env: Env) -> None:
    _, outcome = approved_candidate(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        review(env, outcome.candidate_id, reviewer=SERVICE)
    assert info.value.code == cr.R_REVIEWER_NOT_HUMAN


def test_review_requires_active_principal(env: Env) -> None:
    _, outcome = approved_candidate(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        review(env, outcome.candidate_id, reviewer=DISABLED)
    assert info.value.code == cr.R_REVIEWER_DISABLED


def test_review_requires_reason_text_and_code(env: Env) -> None:
    _, outcome = approved_candidate(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        review(env, outcome.candidate_id, reason_text="   ")
    assert info.value.code == cr.R_REASON_REQUIRED


def test_review_rejects_future_decided_at(env: Env) -> None:
    _, outcome = approved_candidate(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        review(env, outcome.candidate_id, decided_at=datetime.now(UTC) + timedelta(days=1))
    assert info.value.code == cr.R_REVIEW_TIME_IN_FUTURE


def test_review_persists_three_elements(env: Env) -> None:
    _, outcome = approved_candidate(env)
    result = review(env, outcome.candidate_id)
    assert result.created is True
    row = env.conn.execute(
        "select reviewer_kind, decision, reason_code, reason_text, decided_at, kb_id "
        "from p0_t27.phase0_review_decisions where review_decision_id = %s",
        (result.review_decision_id,),
    ).fetchone()
    assert row is not None
    assert row["reviewer_kind"] == "human"
    assert row["decision"] == "approved"
    assert row["reason_code"] == "POLICY_OK"
    assert row["reason_text"].startswith("人工核对")
    assert row["decided_at"] == NOW
    assert row["kb_id"] == env.kb_id


def test_review_is_idempotent_for_same_decision(env: Env) -> None:
    _, outcome = approved_candidate(env)
    first = review(env, outcome.candidate_id)
    second = review(env, outcome.candidate_id)
    assert second.created is False
    assert second.review_decision_id == first.review_decision_id
    assert env.count("phase0_review_decisions") == 1


def test_approved_cannot_be_overridden_by_rejected(env: Env) -> None:
    _, outcome = approved_candidate(env)
    review(env, outcome.candidate_id)
    with pytest.raises(cr.CandidateReviewError) as info:
        review(env, outcome.candidate_id, decision="rejected", reason_code="POLICY_BAD")
    assert info.value.code == cr.R_DECISION_CONFLICT


def test_db_forbids_second_approval_for_same_candidate(env: Env) -> None:
    _, outcome = approved_candidate(env)
    review(env, outcome.candidate_id, reviewer=HUMAN, reason_code="POLICY_OK")
    other_human = uuid.uuid4()
    with env.conn.transaction():
        env.conn.execute(
            "insert into p0_t27.phase0_principals (workspace_id, principal_id, kind, status) "
            "values (%s, %s, 'human', 'active')",
            (env.workspace_id, other_human),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with env.conn.transaction():
            env.conn.execute(
                "insert into p0_t27.phase0_review_decisions "
                "(workspace_id, review_decision_id, candidate_id, source_version_id, kb_id, "
                " reviewer_principal_id, reviewer_kind, decision, reason_code, reason_text, "
                " decided_at) values (%s, %s, %s, %s, %s, %s, 'human', 'approved', %s, %s, %s)",
                (
                    env.workspace_id,
                    uuid.uuid4(),
                    outcome.candidate_id,
                    uuid.uuid4(),
                    env.kb_id,
                    other_human,
                    "SECOND_APPROVAL",
                    "第二个批准",
                    NOW,
                ),
            )


# ---------------------------------------------------------------------------
# 9. 发布：唯一入口 + KB 绑定 + 幂等
# ---------------------------------------------------------------------------
def _approved_decision(env: Env) -> tuple[cr.SubmissionOutcome, cr.ReviewOutcome]:
    _, outcome = approved_candidate(env)
    decision = review(env, outcome.candidate_id)
    return outcome, decision


def test_promote_requires_decision_id(env: Env) -> None:
    _, outcome = approved_candidate(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.promote(env.workspace_id, outcome.candidate_id, review_decision_id=uuid.uuid4())
    assert info.value.code == cr.R_DECISION_REQUIRED


def test_promote_requires_approved_decision(env: Env) -> None:
    _, outcome = approved_candidate(env)
    rejected = review(env, outcome.candidate_id, decision="rejected", reason_code="POLICY_BAD")
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.promote(
            env.workspace_id,
            outcome.candidate_id,
            review_decision_id=rejected.review_decision_id,
        )
    assert info.value.code == cr.R_DECISION_NOT_APPROVED


def test_promote_requires_source_approved(env: Env) -> None:
    record = register_source(env)
    submitted = submit(env, make_draft(env, record))
    decision = review(env, submitted.candidate_id)
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.promote(
            env.workspace_id,
            submitted.candidate_id,
            review_decision_id=decision.review_decision_id,
        )
    assert info.value.code in {cr.R_SOURCE_NOT_APPROVED, cr.R_SOURCE_REVOKED}
    assert env.count("phase0_promotions") == 0


def test_promote_rejects_kb_mismatch(env: Env) -> None:
    outcome, decision = _approved_decision(env)
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.promote(
            env.workspace_id,
            outcome.candidate_id,
            review_decision_id=decision.review_decision_id,
            kb_id=uuid.uuid4(),
        )
    assert info.value.code == cr.R_KB_MISMATCH


def test_promote_rejects_decision_of_another_candidate(env: Env) -> None:
    first, decision = _approved_decision(env)
    record = register_source(env, url="https://nist.gov/other", content="另一份正文")
    second = submit(env, make_draft(env, record, content="另一份正文"))
    with pytest.raises(cr.CandidateReviewError) as info:
        env.store.promote(
            env.workspace_id,
            second.candidate_id,
            review_decision_id=decision.review_decision_id,
        )
    assert info.value.code == cr.R_DECISION_MISMATCH
    assert first.candidate_id != second.candidate_id


def test_promote_records_evidence_count_and_is_idempotent(env: Env) -> None:
    outcome, decision = _approved_decision(env)
    first = env.store.promote(
        env.workspace_id, outcome.candidate_id, review_decision_id=decision.review_decision_id
    )
    assert first.created is True
    assert first.evidence_count == 1
    assert first.kb_id == env.kb_id
    second = env.store.promote(
        env.workspace_id, outcome.candidate_id, review_decision_id=decision.review_decision_id
    )
    assert second.created is False
    assert second.promotion_id == first.promotion_id
    assert env.count("phase0_promotions") == 1
    assert env.store.candidate_state(env.workspace_id, outcome.candidate_id) == "promoted"


def test_db_guard_rejects_promotion_without_valid_approval(env: Env) -> None:
    _, outcome = approved_candidate(env)
    rejected = review(env, outcome.candidate_id, decision="rejected", reason_code="POLICY_BAD")
    candidate_row = env.conn.execute(
        "select source_version_id from p0_t27.phase0_candidates where candidate_id = %s",
        (outcome.candidate_id,),
    ).fetchone()
    assert candidate_row is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        with env.conn.transaction():
            env.conn.execute(
                "insert into p0_t27.phase0_promotions "
                "(workspace_id, promotion_id, candidate_id, review_decision_id, kb_id, "
                " evidence_count, promoted_at) values (%s, %s, %s, %s, %s, 1, %s)",
                (
                    env.workspace_id,
                    uuid.uuid4(),
                    outcome.candidate_id,
                    rejected.review_decision_id,
                    env.kb_id,
                    NOW,
                ),
            )


def test_candidate_state_reflects_promotion_and_is_queryable(env: Env) -> None:
    outcome, decision = _approved_decision(env)
    env.store.promote(
        env.workspace_id, outcome.candidate_id, review_decision_id=decision.review_decision_id
    )
    assert env.store.count_candidates(env.workspace_id) == 1
    assert env.count("phase0_candidate_events", "event_type = 'promoted'") == 1
    assert env.count("phase0_review_decisions", "decision = 'approved'") == 1
