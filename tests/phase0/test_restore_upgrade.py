"""P0-T30：备份、还原、迁移可重复执行与版本回滚的回归测试。

整套演练只跑一次（session 级 fixture），每条用例只对报告的一个面下断言：这样
「一次真实演练 ⇒ 多条可独立失败的验收断言」，失败时能直接指出是哪一项没站住。

边界（与证据文件一致）：T05 为 PARTIAL（原生 ``PostgresSaver`` 无法 fence 独立连接，
结论是单活 runner），所以本文件只在**单实例**前提下验证备份/还原/迁移/回滚；
多实例切换、leader election、RPO/RTO 校准一律不在本文件断言。
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
import pytest

PROJECT = Path(__file__).resolve().parents[2]
PHASE0_DIR = PROJECT / "experiments" / "phase0"
if str(PHASE0_DIR) not in sys.path:
    sys.path.insert(0, str(PHASE0_DIR))

import checkpoint_probe  # noqa: E402
from restore_probe import drill, migrations, pgctl  # noqa: E402

EXPECTED_SLOTS = ("001", "002", "003", "004", "005", "006", "007", "008")
SCHEMA_BY_SLOT = {
    "001": "p0_t03",
    "002": "p0_t06",
    "003": "p0_t07",
    "004": "p0_t12",
    "005": "p0_t20",
    "006": "p0_t23",
    "007": "p0_t27",
    "008": "p0_t13",
}


def _reset_scratch(conn: psycopg.Connection, name: str) -> None:
    """Drop one scratch database; ``ensure_absent`` only accepts drill databases."""

    if pgctl.database_exists(conn, name):
        pgctl.drop_database(conn, name)


def _base_dsn() -> str:
    from scope import read_dsn

    return read_dsn()


@pytest.fixture(scope="session")
def report() -> drill.DrillReport:
    """真实跑一次完整演练；结束时无论成败都清掉临时库。"""

    report = drill.run_full_drill()
    try:
        yield report
    finally:
        with pgctl.admin_connection() as conn:
            pgctl.ensure_absent(conn, pgctl.DRILL_DATABASES)


@pytest.fixture()
def scratch_database() -> str:
    """一次性演练库，测试结束即删除。"""

    name = "p0_t30_scratch"
    with pgctl.admin_connection() as conn:
        _reset_scratch(conn, name)
        pgctl.create_database(conn, name)
    try:
        yield name
    finally:
        with pgctl.admin_connection() as conn:
            _reset_scratch(conn, name)


# --------------------------------------------------------------------------- #
# 迁移：可重复执行时必须是「显式失败」，不能静默跳过
# --------------------------------------------------------------------------- #


def test_every_migration_is_discovered_with_its_checksum() -> None:
    files = migrations.discover_migrations()

    assert tuple(item.slot for item in files) == EXPECTED_SLOTS
    assert migrations.missing_slots(files) == ()
    for item in files:
        assert item.schema == SCHEMA_BY_SLOT[item.slot]
        assert len(item.sha256) == 64
        assert item.size_bytes > 0


def test_migrations_are_hashed_over_the_real_file_bytes() -> None:
    for item in migrations.discover_migrations():
        raw = item.path.read_bytes()

        assert len(raw) == item.size_bytes
        assert pgctl.sha256_file(item.path) == item.sha256


def test_apply_records_the_ledger_and_reapply_fails_explicitly(
    report: drill.DrillReport,
) -> None:
    assert {outcome.status for outcome in report.apply_outcomes} == {"applied"}
    assert len(report.apply_outcomes) == len(EXPECTED_SLOTS)

    # 第二次必须显式失败：迁移脚本里没有 IF NOT EXISTS，重复执行只能以 42P06 结束。
    assert {outcome.status for outcome in report.reapply_outcomes} == {"failed"}
    assert {outcome.sqlstate for outcome in report.reapply_outcomes} == {"42P06"}
    assert all(
        "already exists" in outcome.message for outcome in report.reapply_outcomes
    )
    assert not any(outcome.ledger_row for outcome in report.reapply_outcomes)

    assert len(report.ledger_rows) == len(EXPECTED_SLOTS)
    assert report.ledger_problems == ()
    ledgers = {row.schema: row for row in report.ledger_rows}
    assert set(ledgers) == set(SCHEMA_BY_SLOT.values())
    for item in report.migrations:
        assert ledgers[item.schema].sha256 == item.sha256


def test_ledger_rows_are_the_same_bytes_that_were_applied() -> None:
    files = migrations.discover_migrations()
    with pgctl.admin_connection() as conn:
        _reset_scratch(conn, "p0_t30_scratch")
        pgctl.create_database(conn, "p0_t30_scratch")
    try:
        dsn = pgctl.database_dsn(_base_dsn(), "p0_t30_scratch")
        with psycopg.connect(dsn, autocommit=True) as conn:
            migrations.apply_all(conn, files)
            problems = migrations.assert_ledger_complete(conn, files)
            assert list(problems) == []
            assert sum(
                migrations.ledger_count(conn, schema)
                for schema in set(SCHEMA_BY_SLOT.values())
            ) == len(files)
            reapply = migrations.reapply_all(conn, files)
            assert all(outcome.status == "failed" for outcome in reapply)
    finally:
        with pgctl.admin_connection() as conn:
            _reset_scratch(conn, "p0_t30_scratch")


# --------------------------------------------------------------------------- #
# 备份 → 还原：必须回到一致状态
# --------------------------------------------------------------------------- #


def test_backup_artifact_is_readable_and_complete(report: drill.DrillReport) -> None:
    backup = report.backup
    assert backup is not None
    assert backup.size_bytes > 0
    assert backup.entries > 0
    assert backup.sha256 and len(backup.sha256) == 64
    assert backup.stderr_tail == ""
    assert report.restore is not None
    assert report.restore.entries == backup.entries


def test_restore_reproduces_every_table_and_row(report: drill.DrillReport) -> None:
    source = report.source_snapshot
    restored = report.restore_snapshot
    assert source is not None and restored is not None
    assert restored.table_count == source.table_count
    assert restored.total_rows == source.total_rows
    assert report.restore_diff is not None
    assert report.restore_diff.equal, report.restore_diff.describe()
    assert report.restore_diff.missing == ()
    assert report.restore_diff.extra == ()
    assert report.restore_diff.row_mismatches == ()
    assert report.restore_diff.digest_mismatches == ()


def test_restore_keeps_every_business_pointer(report: drill.DrillReport) -> None:
    # 还原库的指针校验在演练里就地完成（restore_pointer_problems 为空即一致）；
    # 之后回滚库是从同一份备份重放出来的，可以再逐项对比一次。
    assert report.restore_pointer_problems == ()
    before = {fact.name: fact.value for fact in report.source_pointers}
    after = {fact.name: fact.value for fact in report.rollback_pointers}
    assert set(after) == set(before)
    assert after == before
    assert "documents.active_version" in before


def test_restore_reads_the_latest_native_checkpoint(report: drill.DrillReport) -> None:
    # 快照层面一致还不够：还要能用 T05 的只读 API 读出「最新且一致」的 checkpoint。
    assert checkpoint_probe.SCHEMA == "p0_t05"  # 只读复用 T05 的替身存储
    assert report.restore_read_consistent is True
    assert report.restore_checkpoints == report.source_checkpoints
    assert report.source_checkpoints
    for _thread_id, seq, _checkpoint_id in report.source_checkpoints:
        assert seq >= 1


def test_restore_snapshot_matches_source_byte_for_byte(
    report: drill.DrillReport,
) -> None:
    source = report.source_snapshot
    restored = report.restore_snapshot
    assert source is not None and restored is not None
    left = {table.name: table.digest for table in source.tables}
    right = {table.name: table.digest for table in restored.tables}
    assert left == right


# --------------------------------------------------------------------------- #
# 升级 → 回滚
# --------------------------------------------------------------------------- #


def test_upgrade_actually_changes_state(report: drill.DrillReport) -> None:
    assert report.upgrade is not None
    assert report.upgrade.documents_promoted >= 1
    assert report.upgrade.publications_added >= 1
    # 升级必须真的改动数据，否则后面的「回滚」什么也没证明。
    assert report.upgrade_differs_from_backup is True


def test_rollback_returns_to_the_pre_upgrade_state(report: drill.DrillReport) -> None:
    assert report.rollback_diff is not None
    assert report.rollback_diff.equal, report.rollback_diff.describe()
    before = {fact.name: fact.value for fact in report.source_pointers}
    after = {fact.name: fact.value for fact in report.rollback_pointers}
    assert after == before
    assert report.backup is not None
    assert report.rollback_entries == report.backup.entries


def test_rollback_database_is_still_usable(report: drill.DrillReport) -> None:
    labels = {label for label, _value in report.rollback_availability}
    assert labels == {
        "active documents",
        "committed publications",
        "succeeded jobs",
        "document->max published version",
    }
    values = dict(report.rollback_availability)
    assert int(values["active documents"]) >= 1
    assert int(values["committed publications"]) >= 1
    assert values["document->max published version"]


# --------------------------------------------------------------------------- #
# 故障注入：三类都必须明确失败
# --------------------------------------------------------------------------- #


def test_every_injected_fault_fails_explicitly(report: drill.DrillReport) -> None:
    by_name = {fault.name: fault for fault in report.faults}
    assert set(by_name) == {
        "backup_interrupted",
        "restore_interrupted",
        "restore_into_occupied_target",
    }
    assert all(fault.explicit_failure for fault in report.faults)
    assert by_name["backup_interrupted"].raised_code == "BACKUP_INTERRUPTED"
    assert by_name["restore_interrupted"].raised_code == "RESTORE_INTERRUPTED"
    assert (
        by_name["restore_into_occupied_target"].raised_code == "RESTORE_TARGET_OCCUPIED"
    )
    # 证据必须把产物状态记全：是否落盘、多大、能否列出目录、能否真的还原。
    # 实测教训：被打断的 dump 常常带着完整的 TOC（pg_restore -l 能列 512 项），
    # 但数据是截断的、还原会失败——所以「能列出目录」绝不能当成「产物可用」。
    detail = by_name["backup_interrupted"].detail
    assert "partial_exists=" in detail
    assert "partial_size=" in detail
    assert "partial_entries=" in detail
    assert "partial_restorable=" in detail
    assert "bulk_rows=300000" in detail
    assert "partial_restorable=True" not in detail


def test_faults_are_cancelled_server_side_not_by_killing_backends(
    report: drill.DrillReport,
) -> None:
    # 实例级教训：terminate 正在执行 COPY ... TO PROGRAM 的后端会让 postmaster 重启
    # 整个集群。因此故障注入只允许服务器侧取消（statement_timeout），不发信号。
    interrupted = [fault for fault in report.faults if fault.killed_by]
    assert interrupted
    assert all("statement_timeout" in (fault.killed_by or "") for fault in interrupted)
    assert all(fault.backend_pid is None for fault in report.faults)


# --------------------------------------------------------------------------- #
# 清理与「不夸大结论」
# --------------------------------------------------------------------------- #


def test_cleanup_leaves_no_drill_artifacts(report: drill.DrillReport) -> None:
    cleanup = report.cleanup
    assert cleanup is not None
    assert set(cleanup.dropped_databases) == set(pgctl.DRILL_DATABASES)
    assert not [
        name for name in cleanup.remaining_databases if name in pgctl.DRILL_DATABASES
    ]
    assert cleanup.workdir_state == "removed"
    assert cleanup.workdir_leftovers == ()
    assert cleanup.server_artifacts_purged is True


def test_verdict_does_not_claim_what_was_not_run(report: drill.DrillReport) -> None:
    verdict = report.to_json()["verdict"]
    assert verdict["consistency"] == "PASS"
    assert verdict["rollback"] == "PASS"
    assert verdict["faults"] == "PASS"
    # T05 是 PARTIAL（单活），因此这三项在任何情况下都不许写成 PASS。
    assert verdict["multi_instance"] == "NOT_RUN"
    assert verdict["rpo_rto"] == "NOT_RUN"
    assert verdict["object_storage"] == "NOT_RUN"


def test_drill_is_measured_and_timed(report: drill.DrillReport) -> None:
    names = [step.name for step in report.timings]
    assert names == [
        "migrate+seed",
        "backup",
        "restore+verify",
        "upgrade+rollback",
        "faults",
        "cleanup",
    ]
    assert all(step.seconds >= 0 for step in report.timings)
    assert report.started_at
    assert "pg_dump" in report.toolchain


# --------------------------------------------------------------------------- #
# 生命周期辅助：不需要整套演练的低成本断言
# --------------------------------------------------------------------------- #


def test_create_database_refuses_to_overwrite() -> None:
    name = "p0_t30_scratch"
    with pgctl.admin_connection() as conn:
        _reset_scratch(conn, name)
        pgctl.create_database(conn, name)
        try:
            with pytest.raises(pgctl.DatabaseStateError) as caught:
                pgctl.create_database(conn, name)
            assert caught.value.code == pgctl.CODE_ALREADY_EXISTS
            assert pgctl.database_exists(conn, name) is True
        finally:
            _reset_scratch(conn, name)
    with pgctl.admin_connection() as conn:
        assert pgctl.database_exists(conn, name) is False


def test_ensure_absent_is_idempotent() -> None:
    with pgctl.admin_connection() as conn:
        assert list(pgctl.ensure_absent(conn, pgctl.DRILL_DATABASES)) == []
        assert list(pgctl.ensure_absent(conn, pgctl.DRILL_DATABASES)) == []
        with pytest.raises(pgctl.DatabaseStateError) as caught:
            pgctl.ensure_absent(conn, ("pyknora",))
        assert caught.value.detail == pgctl.CODE_NOT_DRILL


def test_redact_dsn_keeps_shape_but_drops_the_password() -> None:
    dsn = "postgresql://someone:s3cret@127.0.0.1:5433/pyknora"
    redacted = pgctl.redact_dsn(dsn)
    assert "s3cret" not in redacted
    assert redacted.startswith("postgresql://someone:***@")
    assert redacted.endswith("/pyknora")


def test_sql_literal_quotes_embedded_quotes() -> None:
    assert pgctl.sql_literal("O'Brien") == "'O''Brien'"
    assert pgctl.sql_literal("/tmp/plain.dump") == "'/tmp/plain.dump'"
