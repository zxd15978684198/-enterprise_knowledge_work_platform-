"""备份 / 还原 / 迁移 / 版本回滚演练的编排与命令行入口。

``run_full_drill()`` 的固定顺序（每一步的结论都会进报告）：

1. 清理旧演练库与旧目录，确认起点干净；
2. 在 ``p0_t30_source`` 内 apply 001–008，记录每份 sha256 与台账行；
3. 对已应用迁移再次 apply，记录「显式失败 / 幂等」实测；
4. 种入业务事实与 T05 checkpoint，取源库快照与关键指针；
5. ``pg_dump`` 备份 → 还原到 ``p0_t30_restore`` → 比对逐表行数/内容指纹/指针；
6. 模拟新版本升级（加列 + 指针前移 + 追加发布），再用**升级前备份**回滚到
   ``p0_t30_rollback``，比对指针已回到升级前并验证可用性；
7. 三类故障注入：备份中途被打断（terminate 正在跑 ``COPY ... TO PROGRAM`` 的后端）、
   还原中途被打断、目标库已存在同名对象；
8. 删库、删目录、删除数据库主机上的临时产物，并输出清理后的 ``SELECT datname``。

备份/还原由**数据库主机上的** ``pg_dump``/``pg_restore`` 17.11 经
``COPY ... TO/FROM PROGRAM`` 执行（本沙箱宿主无客户端、Python 进程也到不了 Docker
命名管道），产物是真实的 ``pg_dump -Fc`` 自定义格式文件，偏差写进 runbook 的残余风险。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from . import inventory, migrations, pgctl, seed

DEFAULT_WORKDIR = Path(__file__).resolve().parent / "_drill_tmp"
DRILL_WORKDIR_NAME = "p0_t30_restore"


@dataclass(frozen=True, slots=True)
class StepTiming:
    name: str
    seconds: float


@dataclass(frozen=True, slots=True)
class FaultReport:
    """一次故障注入的实测结论；``raised`` 为空说明「静默继续」——必须判失败。"""

    name: str
    expected: str
    raised_code: str | None
    raised_message: str = ""
    returncode: int | None = None
    killed_by: str | None = None
    backend_pid: int | None = None
    detail: str = ""

    @property
    def explicit_failure(self) -> bool:
        return self.raised_code is not None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    dropped_databases: tuple[str, ...]
    remaining_databases: tuple[str, ...]
    workdir_state: str
    workdir_leftovers: tuple[str, ...]
    server_artifacts_purged: bool


@dataclass(frozen=True, slots=True)
class DrillReport:
    """一次完整演练的全部原始结论（证据文件从这里摘取）。"""

    dsn: str
    toolchain: str
    workdir: str
    started_at: str
    migrations: tuple[migrations.MigrationFile, ...] = field(default_factory=tuple)
    missing_slots: tuple[str, ...] = field(default_factory=tuple)
    apply_outcomes: tuple[migrations.ApplyOutcome, ...] = field(default_factory=tuple)
    reapply_outcomes: tuple[migrations.ApplyOutcome, ...] = field(default_factory=tuple)
    ledger_rows: tuple[migrations.LedgerRow, ...] = field(default_factory=tuple)
    ledger_problems: tuple[str, ...] = field(default_factory=tuple)
    seed_report: seed.SeedReport | None = None
    checkpoint_seed: seed.CheckpointSeed | None = None
    source_snapshot: inventory.Snapshot | None = None
    source_pointers: tuple[inventory.PointerFact, ...] = field(default_factory=tuple)
    source_checkpoints: tuple[tuple[str, int, str], ...] = field(default_factory=tuple)
    backup: pgctl.BackupResult | None = None
    restore: pgctl.RestoreResult | None = None
    restore_snapshot: inventory.Snapshot | None = None
    restore_diff: inventory.SnapshotDiff | None = None
    restore_pointer_problems: tuple[str, ...] = field(default_factory=tuple)
    restore_checkpoints: tuple[tuple[str, int, str], ...] = field(default_factory=tuple)
    restore_read_consistent: bool = False
    upgrade: seed.UpgradeReport | None = None
    rollback_entries: int | None = None
    rollback_snapshot: inventory.Snapshot | None = None
    rollback_diff: inventory.SnapshotDiff | None = None
    rollback_pointers: tuple[inventory.PointerFact, ...] = field(default_factory=tuple)
    rollback_availability: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    upgrade_differs_from_backup: bool = False
    faults: tuple[FaultReport, ...] = field(default_factory=tuple)
    cleanup: CleanupReport | None = None
    timings: tuple[StepTiming, ...] = field(default_factory=tuple)

    @property
    def consistency_proven(self) -> bool:
        return bool(self.restore_diff and self.restore_diff.equal)

    @property
    def rollback_proven(self) -> bool:
        return bool(self.rollback_diff and self.rollback_diff.equal)

    @property
    def faults_all_explicit(self) -> bool:
        return bool(self.faults) and all(fault.explicit_failure for fault in self.faults)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("migrations",):
            payload[key] = [asdict(item) | {"path": str(item.path)} for item in self.migrations]
        payload["migrations"] = [
            {
                "migration_id": item.migration_id,
                "filename": item.filename,
                "schema": item.schema,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "path": str(item.path),
            }
            for item in self.migrations
        ]
        payload["verdict"] = {
            "consistency": "PASS" if self.consistency_proven else "FAIL",
            "rollback": "PASS" if self.rollback_proven else "FAIL",
            "faults": "PASS" if self.faults_all_explicit else "FAIL",
            "multi_instance": "NOT_RUN",
            "rpo_rto": "NOT_RUN",
            "object_storage": "NOT_RUN",
        }
        return payload


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _snapshot_connection(dsn: str) -> inventory.Snapshot:
    """在 ``REPEATABLE READ`` 快照内取表级事实，避免读串。"""

    with psycopg.connect(dsn, autocommit=False) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return inventory.snapshot(conn)


def _pointer_view(dsn: str) -> tuple[inventory.PointerFact, ...]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return inventory.pointer_table(conn)


BULK_SCHEMA = "p0_t30_bulk"
BULK_TABLE = f"{BULK_SCHEMA}.phase0_bulk_rows"
BULK_ROWS = 300_000


# The drill interrupts a backup/restore by cancelling the statement on the *server*
# (``statement_timeout``).  It never signals a backend: terminating a backend that runs
# ``COPY ... TO PROGRAM`` makes the postmaster treat the program child's SIGTERM exit as
# a crash and reinitialize the whole cluster -- observed twice on the shared Phase 0
# instance on 2026-10-28.  A cancelled statement produces the same evidence (a truncated
# artifact / a half-restored database) without touching the rest of the cluster.
FAULT_TIMEOUT_MS = 250
FAULT_DEADLINE_S = 30.0


def _prepare_bulk_source(dsn: str) -> int:
    """在源库造一张足够大的表，让备份/还原有真实可打断的时间窗。"""

    with psycopg.connect(pgctl.database_dsn(dsn, pgctl.SOURCE_DB), autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {BULK_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {BULK_SCHEMA}")
        conn.execute(f"CREATE TABLE {BULK_TABLE} (id bigint PRIMARY KEY, payload text NOT NULL)")
        conn.execute(
            f"INSERT INTO {BULK_TABLE} (id, payload) "
            f"SELECT g, repeat('x', 200) FROM generate_series(1, {BULK_ROWS}) AS g"
        )
        return int(conn.execute(f"SELECT count(*) FROM {BULK_TABLE}").fetchone()[0])


def _raised(call: Any) -> tuple[str | None, str]:
    """Run ``call`` and return the error code/message it raised (or a failure note)."""

    try:
        call()
    except pgctl.RestoreProbeError as exc:
        return exc.code, exc.message
    except psycopg.Error as exc:  # 未归类的数据库错误也是显式失败
        return None, f"未预期的数据库错误 {type(exc).__name__}: {exc}"
    return None, "命令正常完成，没有制造出中断窗口"


def _run_faults(
    *,
    dsn: str,
    admin: psycopg.Connection[Any],
    workdir: Path,
) -> tuple[FaultReport, ...]:
    reports: list[FaultReport] = []
    bulk_rows = _prepare_bulk_source(dsn)
    bulk_backup = pgctl.backup_database(dsn, database=pgctl.SOURCE_DB, label="bulk")
    artifact = bulk_backup.artifact

    # 1) 备份中途被取消：statement_timeout 在服务器侧中止 COPY TO PROGRAM
    interrupted_path = pgctl.host_artifact_path("interrupted")
    code, message = _raised(
        lambda: pgctl.backup_database(
            dsn,
            database=pgctl.SOURCE_DB,
            label="interrupted",
            statement_timeout_ms=FAULT_TIMEOUT_MS,
        )
    )
    meta = pgctl.artifact_meta(dsn, interrupted_path)
    partial_entries: int | None = None
    partial_code: str | None = None
    partial_restorable: object = "not_attempted"
    if meta.exists:
        try:
            partial_entries = pgctl.count_backup_entries(dsn, interrupted_path)
        except pgctl.RestoreProbeError as exc:
            partial_code = exc.code
        # 「能列出目录」不等于「能还原」：被打断的 dump 常常带完整 TOC 却缺数据，
        # 所以这里真的往一次性库里放一次，把结论写进证据，而不是靠推断。
        try:
            pgctl.ensure_absent(admin, (pgctl.FAULT_DB,))
            pgctl.create_database(admin, pgctl.FAULT_DB)
            pgctl.restore_database(dsn, database=pgctl.FAULT_DB, artifact=interrupted_path)
            partial_restorable = True
        except pgctl.RestoreProbeError as exc:
            partial_restorable = exc.code
        except psycopg.Error as exc:  # pragma: no cover - 记录型分支
            partial_restorable = f"pg_error:{type(exc).__name__}"
        finally:
            pgctl.ensure_absent(admin, (pgctl.FAULT_DB,))
    reports.append(
        FaultReport(
            name="backup_interrupted",
            expected="BACKUP_INTERRUPTED（被打断的产物不可用）",
            raised_code=code,
            raised_message=message,
            returncode=None,
            killed_by=f"statement_timeout={FAULT_TIMEOUT_MS}ms",
            backend_pid=None,
            detail=(
                f"bulk_rows={bulk_rows} partial_exists={meta.exists} "
                f"partial_size={meta.size_bytes} partial_entries={partial_entries} "
                f"partial_code={partial_code} partial_restorable={partial_restorable}"
            ),
        )
    )
    pgctl.remove_artifact(dsn, interrupted_path)

    # 2) 还原中途被取消：先建空库，再让服务器取消 pg_restore
    pgctl.ensure_absent(admin, (pgctl.FAULT_DB,))
    pgctl.create_database(admin, pgctl.FAULT_DB)
    code, message = _raised(
        lambda: pgctl.restore_database(
            dsn,
            database=pgctl.FAULT_DB,
            artifact=artifact,
            statement_timeout_ms=FAULT_TIMEOUT_MS,
        )
    )
    incomplete_tables = _count_incomplete_restore(dsn)
    reports.append(
        FaultReport(
            name="restore_interrupted",
            expected="RESTORE_INTERRUPTED（中断的还原必须显式失败）",
            raised_code=code,
            raised_message=message,
            returncode=None,
            killed_by=f"statement_timeout={FAULT_TIMEOUT_MS}ms",
            backend_pid=None,
            detail=(f"目标库 {pgctl.FAULT_DB} 未被告知成功；剩余对象 {incomplete_tables}"),
        )
    )

    # 3) 目标库已存在同名对象：同一份备份再导一次，必须显式冲突
    code, message = _raised(
        lambda: pgctl.restore_database(dsn, database=pgctl.RESTORE_DB, artifact=artifact)
    )
    reports.append(
        FaultReport(
            name="restore_into_occupied_target",
            expected="RESTORE_TARGET_OCCUPIED",
            raised_code=code,
            raised_message=message,
            detail=f"目标库 {pgctl.RESTORE_DB} 已存在同名 schema",
        )
    )
    return tuple(reports)


def _count_incomplete_restore(dsn: str) -> int:
    """中断的还原里落下的 schema 数（0 与 1+ 都合法，只作为证据记录）。"""

    try:
        with psycopg.connect(pgctl.database_dsn(dsn, pgctl.FAULT_DB), autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_namespace WHERE nspname LIKE %s",
                (r"p0\_%",),
            ).fetchone()
    except psycopg.Error:
        return 0
    return int(row[0]) if row else 0


def run_full_drill(
    *,
    dsn: str | None = None,
    workdir: Path | None = None,
    keep: bool = False,
) -> DrillReport:
    """执行一次完整演练并返回报告；``keep=True`` 时保留临时库与目录以便排查。"""

    from scope import read_dsn

    base_dsn = dsn or read_dsn()
    toolchain = "database host: pg_dump/pg_restore 17.11 via COPY ... TO/FROM PROGRAM"
    drill_dir = pgctl.prepare_workdir(workdir or (Path(_default_workdir()) / DRILL_WORKDIR_NAME))
    timings: list[StepTiming] = []
    started_at = _now()

    def timed(name: str, started: float) -> None:
        timings.append(StepTiming(name=name, seconds=round(time.monotonic() - started, 3)))

    admin = pgctl.admin_connection(base_dsn)
    pgctl.ensure_absent(admin, pgctl.DRILL_DATABASES)
    try:
        # --- 1. 迁移 ---------------------------------------------------------
        mark = time.monotonic()
        pgctl.create_database(admin, pgctl.SOURCE_DB)
        source_dsn = pgctl.database_dsn(base_dsn, pgctl.SOURCE_DB)
        files = migrations.discover_migrations()
        missing = migrations.missing_slots(files)
        with psycopg.connect(source_dsn, autocommit=True) as conn:
            apply_outcomes = migrations.apply_all(conn, files)
            reapply_outcomes = migrations.reapply_all(conn, files)
            ledger = migrations.ledger_rows(conn)
            ledger_problems = migrations.assert_ledger_complete(conn, files)
            seed_report = seed.seed_business(conn)
        checkpoint_seed = seed.seed_checkpoints(source_dsn)
        source_checkpoints = seed.checkpoint_state(source_dsn)
        # 基线必须取在“备份时刻”：checkpoint 种入之后、备份之前，否则原生 checkpoint
        # schema 会被误判成“还原多出来的表”。
        source_snapshot = _snapshot_connection(source_dsn)
        source_pointers = _pointer_view(source_dsn)
        timed("migrate+seed", mark)

        # --- 2. 备份 ---------------------------------------------------------
        mark = time.monotonic()
        backup = pgctl.backup_database(base_dsn, database=pgctl.SOURCE_DB, label="source")
        timed("backup", mark)

        # --- 3. 还原并校验 ---------------------------------------------------
        mark = time.monotonic()
        pgctl.create_database(admin, pgctl.RESTORE_DB)
        restore = pgctl.restore_database(
            base_dsn, database=pgctl.RESTORE_DB, artifact=backup.artifact
        )
        restore_dsn = pgctl.database_dsn(base_dsn, pgctl.RESTORE_DB)
        restore_snapshot = _snapshot_connection(restore_dsn)
        restore_diff = inventory.diff_snapshots(source_snapshot, restore_snapshot)
        restore_pointers = _pointer_view(restore_dsn)
        restore_pointer_problems = inventory.diff_pointers(source_pointers, restore_pointers)
        restore_checkpoints = seed.checkpoint_state(restore_dsn)
        restore_read_consistent = _checkpoint_restore_read(restore_dsn, checkpoint_seed)
        timed("restore+verify", mark)

        # --- 4. 升级 → 失败 → 回滚 -------------------------------------------
        mark = time.monotonic()
        with psycopg.connect(source_dsn, autocommit=True) as conn:
            upgrade = seed.upgrade_to_v2(conn)
            upgraded_snapshot = inventory.snapshot(conn, database=pgctl.SOURCE_DB)
        upgrade_differs = not inventory.diff_snapshots(source_snapshot, upgraded_snapshot).equal
        pgctl.create_database(admin, pgctl.ROLLBACK_DB)
        rollback = pgctl.restore_database(
            base_dsn, database=pgctl.ROLLBACK_DB, artifact=backup.artifact
        )
        rollback_entries = rollback.entries
        rollback_dsn = pgctl.database_dsn(base_dsn, pgctl.ROLLBACK_DB)
        rollback_snapshot = _snapshot_connection(rollback_dsn)
        rollback_diff = inventory.diff_snapshots(source_snapshot, rollback_snapshot)
        rollback_pointers = _pointer_view(rollback_dsn)
        with psycopg.connect(rollback_dsn, autocommit=True) as conn:
            availability = seed.availability_probe(conn)
        timed("upgrade+rollback", mark)

        # --- 5. 故障注入 -----------------------------------------------------
        mark = time.monotonic()
        faults = _run_faults(dsn=base_dsn, admin=admin, workdir=drill_dir)
        timed("faults", mark)

        # --- 6. 清理 ---------------------------------------------------------
        mark = time.monotonic()
        if keep:
            remaining = pgctl.list_databases(admin)
            cleanup = CleanupReport(
                dropped_databases=(),
                remaining_databases=remaining,
                workdir_state="kept",
                workdir_leftovers=tuple(sorted(p.name for p in drill_dir.iterdir())),
                server_artifacts_purged=False,
            )
        else:
            cleared = tuple(pgctl.ensure_absent(admin, pgctl.DRILL_DATABASES))
            for path in (
                pgctl.host_artifact_path("source"),
                pgctl.host_artifact_path("bulk"),
                pgctl.host_artifact_path("interrupted"),
            ):
                pgctl.remove_artifact(base_dsn, path)
            server_purged = True
            state, leftovers = pgctl.cleanup_workdir(drill_dir)
            cleanup = CleanupReport(
                dropped_databases=cleared,
                remaining_databases=pgctl.list_databases(admin),
                workdir_state=state,
                workdir_leftovers=leftovers,
                server_artifacts_purged=server_purged,
            )
        timed("cleanup", mark)

        return DrillReport(
            dsn=pgctl.redact_dsn(base_dsn),
            toolchain=toolchain,
            workdir=str(drill_dir),
            started_at=started_at,
            migrations=files,
            missing_slots=missing,
            apply_outcomes=apply_outcomes,
            reapply_outcomes=reapply_outcomes,
            ledger_rows=ledger,
            ledger_problems=ledger_problems,
            seed_report=seed_report,
            checkpoint_seed=checkpoint_seed,
            source_snapshot=source_snapshot,
            source_pointers=source_pointers,
            source_checkpoints=source_checkpoints,
            backup=backup,
            restore=restore,
            restore_snapshot=restore_snapshot,
            restore_diff=restore_diff,
            restore_pointer_problems=restore_pointer_problems,
            restore_checkpoints=restore_checkpoints,
            restore_read_consistent=restore_read_consistent,
            upgrade=upgrade,
            rollback_entries=rollback_entries,
            rollback_snapshot=rollback_snapshot,
            rollback_diff=rollback_diff,
            rollback_pointers=rollback_pointers,
            rollback_availability=availability,
            upgrade_differs_from_backup=upgrade_differs,
            faults=faults,
            cleanup=cleanup,
            timings=tuple(timings),
        )
    finally:
        # Teardown is unconditional: the drill must never leave a p0_t30_* database
        # behind (objects owned by the p0_app role would block T03's DROP ROLE) and
        # must reclaim them even when a step raised.
        if not keep:
            # Best effort only: the explicit cleanup step already reported what it
            # removed, and a teardown error must never mask the real failure.
            with contextlib.suppress(Exception):
                pgctl.ensure_absent(admin, pgctl.DRILL_DATABASES)
        admin.close()


def _checkpoint_restore_read(dsn: str, seed_report: seed.CheckpointSeed | None) -> bool:
    """用 T05 的 ``restore_read`` 验证「恢复后读到最新一致 checkpoint」。"""

    if seed_report is None:
        return False
    harness = probe_module().Harness(dsn=dsn)
    try:
        ok = True
        for workspace_id, run_id, thread_id in seed_report.bindings:
            binding = probe_module().RunBinding(
                workspace_id=workspace_id,
                run_id=run_id,
                thread_id=thread_id,
                graph_version=seed.GRAPH_VERSION,
                state_schema_version=seed.STATE_SCHEMA_VERSION,
            )
            read = harness.restore_read(binding)
            ok = ok and bool(read) and read.consistent
        return ok
    finally:
        harness.close()


def probe_module() -> Any:
    """惰性拿到 T05 模块（只读复用，避免在 import 期拉起重依赖）。"""

    import checkpoint_probe

    return checkpoint_probe


def _default_workdir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-T30 备份/还原/迁移/回滚演练")
    parser.add_argument("--json", type=Path, default=None, help="把完整报告写到该文件")
    parser.add_argument("--keep", action="store_true", help="保留临时库与目录（默认全部清理）")
    parser.add_argument("--workdir", type=Path, default=None, help="演练工作目录")
    args = parser.parse_args(argv)
    report = run_full_drill(workdir=args.workdir, keep=args.keep)
    payload = report.to_json()
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return (
        0
        if (
            report.consistency_proven
            and report.rollback_proven
            and report.faults_all_explicit
            and not report.ledger_problems
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
