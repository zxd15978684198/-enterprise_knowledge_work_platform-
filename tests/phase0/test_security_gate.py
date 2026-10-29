"""P0-T29 安全门禁回归测试（真实 PG + 本地控制站；禁 skip/xfail）。

对应 Backlog P0-T29（docs/poc/03_Phase0_Task_Backlog.md L490-505）与
docs/poc/evidence/wave-w9-record.md §1.1 限域口径：

* 本文件只断言 `security_probe.run_security_probes()` 的真实运行结果，
  不 mock、不 skip/xfail、不吞失败；
* 探测矩阵覆盖跨空间读/写/检索/引用/对象/事件/Job、撤权窗口、载荷与审批篡改、
  SSRF 逐跳校验、canary 全 sink、业务事实状态后端授权；
* 真实 chat 模型 / 真实身份提供商 / 真实对象存储 / 真实 DNS 重绑定 / pgvector ANN
  属 NOT_RUN（见证据 JSON 的 not_run），因此最高判定为 ``PASS（限域）``。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
sys.path.insert(0, str(PROJECT))

import scope  # noqa: E402
import security_probe as probe  # noqa: E402


def _records(run: probe.SecurityRun) -> dict[str, probe.ProbeRecord]:
    return {str(record.probe_id): record for record in run.log.records()}


def _category(run: probe.SecurityRun, name: str) -> dict[str, int]:
    return run.evidence["summary"]["categories"][name]


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    return scope.read_dsn()


@pytest.fixture(scope="session")
def security_run(admin_dsn: str) -> Iterator[probe.SecurityRun]:
    """跑一次完整探针矩阵；会话结束时清理 schema、角色与 Redis 前缀。"""

    run = probe.run_security_probes(dsn=admin_dsn)
    yield run
    with probe.connect(admin_dsn, autocommit=True) as admin:
        probe.reset_schema(admin)
    with probe.redis_client() as client:
        probe.flush_prefix(client)


def test_probe_budget_meets_backlog_minimum(security_run: probe.SecurityRun) -> None:
    evidence = security_run.evidence
    total = evidence["summary"]["total"]
    records = security_run.log.records()

    assert total["probes"] == len(records) == len(evidence["probes"])
    assert total["probes"] >= probe.MIN_TOTAL
    assert total["refused"] + total["allowed"] == total["probes"]
    assert evidence["budget"]["min_total"] == probe.MIN_TOTAL
    assert evidence["budget"]["min_probes"] == dict(probe.MIN_PROBES)

    categories = {record.category for record in records}
    assert categories == set(probe.MIN_PROBES)
    for category, minimum in probe.MIN_PROBES.items():
        assert _category(security_run, category)["probes"] >= minimum, category

    for record in records:
        assert record.vector and record.evidence_ref
        assert record.expected in {"allow", "deny"}
        assert isinstance(record.refused, bool)


def test_zero_unauthorized_successes(security_run: probe.SecurityRun) -> None:
    total = security_run.evidence["summary"]["total"]

    assert total["breaches"] == 0
    assert security_run.log.breaches() == ()
    assert security_run.evidence["breaches"] == []
    assert total["refused"] > 0


def test_cross_workspace_matrix_has_zero_hits(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "cross_workspace")
    rounds = 10
    operations = ("select_one", "select_list", "update", "delete")
    expected = rounds * len(probe.CROSS_TARGETS) * len(operations) + 1

    assert block["probes"] == expected
    assert block["refused"] == expected - 1
    assert block["allowed"] == 1
    assert block["breaches"] == 0

    ids = set(_records(security_run))
    for target in probe.CROSS_TARGETS:
        for operation in operations:
            for offset in range(rounds):
                probe_id = f"xw-{target.table}-{operation}-{offset:02d}"
                assert probe_id in ids, probe_id

    control = _records(security_run)["xw-control-owner-visible"]
    assert control.expected == "allow"
    assert control.refused is False
    assert control.observed.startswith("rows=")


def test_revocation_window_blocks_then_restores(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "revocation")
    records = _records(security_run)
    denied = (
        "rev-disabled-scope",
        "rev-disabled-citation",
        "rev-disabled-job",
        "rev-stale-token",
        "rev-expired-token",
        "rev-tampered-token",
        "rev-stale-approval",
        "rev-old-cursor",
        "rev-kb-grant",
        "rev-forged-scope-header",
    )
    allowed = (
        "rev-baseline-live",
        "rev-restored-scope",
        "rev-restored-citation",
        "rev-restored-job",
    )

    assert block["probes"] >= probe.MIN_PROBES["revocation"]
    assert block["refused"] == len(denied)
    assert block["allowed"] == len(allowed)
    assert block["breaches"] == 0
    for probe_id in denied:
        assert records[probe_id].refused is True, probe_id
        assert records[probe_id].expected == "deny"
    for probe_id in allowed:
        assert records[probe_id].refused is False, probe_id
        assert records[probe_id].expected == "allow"


def test_tampered_payload_and_approval_are_rejected(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "payload")
    records = _records(security_run)
    denied = (
        "pl-approval-operation",
        "pl-approval-expired",
        "pl-approval-status",
        "pl-db-intent",
        "pl-db-operation",
        "pl-db-scope",
        "pl-db-idempotency",
        "pl-db-run",
        "pl-db-status",
        "pl-db-expired",
        "pl-db-missing",
        "pl-db-approver-disabled",
        "pl-citation-cross-space",
        "pl-citation-stale",
        "pl-citation-unknown",
        "pl-citation-handle",
        "pl-citation-duplicate",
    )
    controls = ("pl-approval-control", "pl-db-control", "pl-citation-control")

    assert block["probes"] == len(denied) + len(controls)
    assert block["refused"] == len(denied)
    assert block["allowed"] == len(controls)
    assert block["breaches"] == 0
    for probe_id in denied:
        assert records[probe_id].refused is True, probe_id
        assert records[probe_id].observed.startswith("denied:")
    for probe_id in controls:
        assert records[probe_id].refused is False, probe_id


def test_ssrf_matrix_blocks_forbidden_targets(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "ssrf")
    records = _records(security_run)
    allowed = ("ssrf-allow-literal", "ssrf-allow-localhost", "ssrf-allow-redirect")

    assert block["probes"] == probe.MIN_PROBES["ssrf"] == 24
    assert block["refused"] == 21
    assert block["allowed"] == len(allowed)
    assert block["breaches"] == 0
    for probe_id in allowed:
        assert records[probe_id].refused is False, probe_id
        assert records[probe_id].observed.startswith("reached:"), probe_id
    assert records["ssrf-allow-redirect"].observed.startswith("reached:200")

    blocked = [record for record in records.values() if record.category == "ssrf"]
    blocked = [record for record in blocked if record.expected == "deny"]
    assert len(blocked) == 21
    for record in blocked:
        assert record.refused is True, record.probe_id
        assert record.observed.startswith("blocked:"), record.probe_id
    for probe_id in (
        "ssrf-block-redirect-metadata",
        "ssrf-block-redirect-private",
        "ssrf-block-redirect-loop",
        "ssrf-block-metadata",
        "ssrf-block-private-10",
        "ssrf-block-private-192",
        "ssrf-block-decimal-ip",
    ):
        assert records[probe_id].refused is True, probe_id


def test_canary_scan_finds_no_secret_leak(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "canary")
    records = _records(security_run)

    assert block["probes"] >= probe.MIN_PROBES["canary"]
    assert block["breaches"] == 0
    canary_records = [record for record in records.values() if record.category == "canary"]
    assert len(canary_records) == 14
    for record in canary_records:
        if record.expected == "deny":
            assert record.refused is True, record.probe_id
        else:
            assert record.refused is False, record.probe_id

    assert records["canary-full-sink-scan"].refused is True
    assert "canary_hits=0" in records["canary-full-sink-scan"].observed

    evidence_text = probe.EVIDENCE_PATH.read_text(encoding="utf-8")
    matrix_text = probe.MATRIX_PATH.read_text(encoding="utf-8")
    for value in probe.CANARY_VALUES:
        assert value not in evidence_text, value
        assert value not in matrix_text, value
    assert "phase0-local-only" not in evidence_text
    assert "**@" in json.loads(evidence_text)["dsn"]


def test_business_state_requires_backend_authorization(security_run: probe.SecurityRun) -> None:
    block = _category(security_run, "business_state")
    records = _records(security_run)
    denied = (
        "bs-event-no-scope",
        "bs-event-cross-workspace",
        "bs-event-forged-principal",
        "bs-event-terminal",
        "bs-citation-revoked",
        "bs-citation-cross-insert",
        "bs-object-cross-workspace",
        "bs-object-expired",
        "bs-object-method",
        "bs-object-forged",
        "bs-object-forged-key",
        "bs-job-cross-workspace",
        "bs-job-payload-workspace",
        "bs-job-payload-principal",
        "bs-kb-revoked-retrieval",
    )

    assert block["probes"] == len(denied) == 15
    assert block["refused"] == len(denied)
    assert block["allowed"] == 0
    assert block["breaches"] == 0
    for probe_id in denied:
        assert records[probe_id].refused is True, probe_id


def test_fabricated_and_unauthorized_citations_are_rejected(
    security_run: probe.SecurityRun,
) -> None:
    block = _category(security_run, "citations")
    records = _records(security_run)
    kinds = (
        "unknown-handle",
        "unknown-chunk-ref",
        "cross-workspace-chunk",
        "retired-version",
        "revoked-kb-grant",
        "not-in-evidence",
        "duplicate-handle",
        "out-of-range",
        "mixed-valid-forged",
        "zero-handle",
    )

    assert block["probes"] == 1 + len(kinds) * 10
    assert block["refused"] == len(kinds) * 10
    assert block["allowed"] == 1
    assert block["breaches"] == 0
    assert records["ci-control"].refused is False
    for offset in range(10):
        for kind in kinds:
            probe_id = f"ci-{kind}-{offset:02d}"
            assert records[probe_id].refused is True, probe_id
            assert records[probe_id].observed.startswith("denied:")


def test_schema_uses_force_rls_and_non_superuser_role(security_run: probe.SecurityRun) -> None:
    snapshot = security_run.evidence["schema"]["snapshot"]
    tables = snapshot["tables"]
    roles = security_run.evidence["role"]

    tenant = set(probe.TENANT_TABLES)
    assert tenant <= set(tables)
    for table in tenant:
        assert tables[table] == [True, True], table
    assert tables["phase0_schema_migrations"] == [False, False]

    assert len(snapshot["policies"]) == 13
    for policy in snapshot["policies"]:
        assert "phase0_current_workspace_id" in policy["qual"], policy["name"]
        assert policy["cmd"] == "*"
    restricted = [p for p in snapshot["policies"] if "phase0_principal_active" in p["qual"]]
    assert len(restricted) == 11

    functions = {item["name"]: item for item in snapshot["functions"]}
    assert len(functions) == 5
    assert functions["phase0_principal_active"]["security_definer"] is True
    assert functions["phase0_current_workspace_id"]["security_definer"] is False

    assert roles["attributes"]["rolsuper"] is False
    assert roles["attributes"]["rolbypassrls"] is False
    assert roles["attributes"]["rolcanlogin"] is True
    assert roles["owns_tables"] == 0


def test_evidence_json_and_matrix_are_complete(security_run: probe.SecurityRun) -> None:
    payload = json.loads(probe.EVIDENCE_PATH.read_text(encoding="utf-8"))

    assert payload["task"] == "P0-T29"
    assert payload["poc"] == "PoC-01/02/03/04/06/09"
    assert payload["schema"]["name"] == probe.SCHEMA
    assert payload["schema"]["migration_id"] == probe.MIGRATION_ID
    assert payload["schema"]["ddl_sha256"] == probe.schema_sha256(probe.MIGRATION_SQL)
    assert payload["schema"]["snapshot"]["ledger"]["sha256"] == payload["schema"]["ddl_sha256"]
    assert payload["dsn"].startswith("postgresql://")
    assert payload["redis"]["db"] == probe.REDIS_DB
    assert payload["redis"]["evidence"]["atomic_ok"] is True
    assert payload["summary"] == security_run.evidence["summary"]
    assert len(payload["probes"]) == payload["summary"]["total"]["probes"]

    not_run = payload["not_run"]
    assert len(not_run) == 5
    assert all(item["item"] and item["reason"] for item in not_run)
    assert any("真实 chat 模型链路" in item["item"] for item in not_run)
    assert any("pgvector ANN" in item["item"] for item in not_run)

    assert payload["git"]["available"] is True
    assert len(payload["git"]["head"]) == 40
    assert payload["git"]["branch"]

    baseline = payload["rag_baseline"]
    assert baseline["available"] is True
    assert baseline["sample_count"] == 100
    assert "不能当作安全验收" in baseline["note"]

    matrix = probe.MATRIX_PATH.read_text(encoding="utf-8")
    for heading in (
        "## 1. 矩阵总表",
        "## 2. 探测向量与实测",
        "## 3. 拒绝证据",
        "## 4. 覆盖缺口与 NOT_RUN",
        "## 5. 对 T31 的输入",
    ):
        assert heading in matrix, heading
    assert "越权/漏放 **0**" in matrix
    assert len(matrix.encode("utf-8")) > 10000


def test_redis_evidence_uses_isolated_db(security_run: probe.SecurityRun) -> None:
    redis = security_run.evidence["redis"]
    redis_evidence = redis["evidence"]

    # W9 记录指定 DB16，但服务器 databases=16 只支持索引 0-15（实测 DB16 不可用），
    # 因此改用空闲 DB15，并在证据里显式记录偏差；断言 db 既非 0（共享库）也在合法区间。
    assert redis["db"] == probe.REDIS_DB == 15
    assert 1 <= redis["db"] <= 15
    assert redis["url"].endswith("/15")
    assert "DB16" in redis["resource_note"]
    assert "0-15" in redis["resource_note"]
    assert redis_evidence["rounds"] == 10
    assert redis_evidence["atomic_ok"] is True
    assert redis_evidence["cleaned"] >= 1
    record = _records(security_run)["redis-atomic-counter"]
    assert record.expected == "allow"
    assert record.refused is False


def test_rag_baseline_metadata_is_not_treated_as_security_proof(
    security_run: probe.SecurityRun,
) -> None:
    baseline = security_run.evidence["rag_baseline"]

    assert baseline["available"] is True
    assert baseline["cases_embedded"] == 20
    assert "结构测量" in baseline["note"]
    assert baseline["fabricated_citations"] == 0
    assert baseline["unauthorized_citations"] == 0


def test_cleanup_removes_schema_role_and_redis_keys(admin_dsn: str) -> None:
    with probe.connect(admin_dsn, autocommit=True) as admin:
        before = probe.verify_schema(admin)
        assert set(before["tables"]) == set(probe.TENANT_TABLES) | {
            "phase0_workspaces",
            "phase0_schema_migrations",
        }
        assert probe.reset_schema(admin) is True
        assert admin.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (probe.SCHEMA,)
        ).fetchone() is None
        assert admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (probe.APP_ROLE,)
        ).fetchone() is None
    with probe.redis_client() as client:
        assert probe.flush_prefix(client) >= 0
        assert list(client.scan_iter(match=f"{probe.REDIS_PREFIX}*")) == []
