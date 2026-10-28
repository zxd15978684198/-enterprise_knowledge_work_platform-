"""迁移清单与重复 apply 实测。

演练只**读取** ``experiments/phase0/db/migrations/*.sql``，不新增、不修改任何编号
迁移。每份迁移自带 schema 与其 schema 内的 ``phase0_schema_migrations`` 台账表；
本模块负责：执行迁移文件、写入台账行、并把「再次 apply 会发生什么」如实记录。

台账列由运行期内省决定（各迁移的台账表可能只实现列子集），避免对 SQL 做假设。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / "experiments" / "phase0" / "db" / "migrations"
EXPECTED_SLOTS: tuple[str, ...] = tuple(f"{n:03d}" for n in range(1, 9))
LEDGER_TABLE = "phase0_schema_migrations"
_SCHEMA_RE = re.compile(
    r"^\s*CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)\s*;", re.MULTILINE
)
_LEDGER_COLUMNS: tuple[str, ...] = ("migration_id", "filename", "sha256")


class MigrationDrillError(RuntimeError):
    """迁移演练前置条件错误。"""


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """一份迁移文件的静态事实（内容哈希在读取时计算，供证据比对）。"""

    migration_id: str
    filename: str
    schema: str
    sha256: str
    size_bytes: int
    path: Path

    @property
    def slot(self) -> str:
        return self.migration_id.split("_", 1)[0]


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """一次 apply / re-apply 的实测结果。"""

    migration_id: str
    filename: str
    schema: str
    status: str  # applied | failed | idempotent
    sqlstate: str | None
    message: str
    ledger_row: bool

    @property
    def explicit_failure(self) -> bool:
        return self.status == "failed"


def _schema_of(sql: str, filename: str) -> str:
    match = _SCHEMA_RE.search(sql)
    if match is None:
        raise MigrationDrillError(f"{filename} 未找到 CREATE SCHEMA 语句，无法确定归属 schema")
    return match.group(1)


def discover_migrations(directory: Path | None = None) -> tuple[MigrationFile, ...]:
    """按文件名排序返回现存迁移；缺号（如 007）由 :func:`missing_slots` 报告。"""

    base = directory or MIGRATIONS_DIR
    files: list[MigrationFile] = []
    for path in sorted(base.glob("0*.sql")):
        raw = path.read_bytes()
        sql = raw.decode("utf-8")
        files.append(
            MigrationFile(
                migration_id=path.stem,
                filename=path.name,
                schema=_schema_of(sql, path.name),
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                path=path,
            )
        )
    if not files:
        raise MigrationDrillError(f"{base} 下没有迁移文件")
    return tuple(files)


def missing_slots(migrations: tuple[MigrationFile, ...]) -> tuple[str, ...]:
    present = {item.slot for item in migrations}
    return tuple(slot for slot in EXPECTED_SLOTS if slot not in present)


def _ledger_columns(conn: psycopg.Connection[Any], schema: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, LEDGER_TABLE),
    ).fetchall()
    existing = {str(row[0]) for row in rows}
    if not existing:
        raise MigrationDrillError(f"{schema}.{LEDGER_TABLE} 不存在，迁移未创建台账表？")
    return tuple(name for name in _LEDGER_COLUMNS if name in existing)


def record_ledger(conn: psycopg.Connection[Any], migration: MigrationFile) -> bool:
    """在迁移自己的 schema 内写台账行；返回是否写入成功。"""

    columns = _ledger_columns(conn, migration.schema)
    if len(columns) != len(_LEDGER_COLUMNS):
        raise MigrationDrillError(
            f"{migration.schema}.{LEDGER_TABLE} 列不全: {columns}，无法写台账"
        )
    conn.execute(
        f"INSERT INTO {migration.schema}.{LEDGER_TABLE} "
        "(migration_id, filename, sha256) VALUES (%s, %s, %s)",
        (migration.migration_id, migration.filename, migration.sha256),
    )
    return True


def apply_migration(conn: psycopg.Connection[Any], migration: MigrationFile) -> ApplyOutcome:
    """在单个事务内执行迁移并写台账；失败则整体回滚。"""

    sql = migration.path.read_text(encoding="utf-8")
    try:
        with conn.transaction():
            conn.execute(sql)
            record_ledger(conn, migration)
    except psycopg.Error as exc:
        return ApplyOutcome(
            migration_id=migration.migration_id,
            filename=migration.filename,
            schema=migration.schema,
            status="failed",
            sqlstate=exc.sqlstate,
            message=str(exc).strip().splitlines()[0][:300],
            ledger_row=False,
        )
    return ApplyOutcome(
        migration_id=migration.migration_id,
        filename=migration.filename,
        schema=migration.schema,
        status="applied",
        sqlstate=None,
        message="ok",
        ledger_row=True,
    )


def apply_all(
    conn: psycopg.Connection[Any], migrations: tuple[MigrationFile, ...]
) -> tuple[ApplyOutcome, ...]:
    outcomes = [apply_migration(conn, migration) for migration in migrations]
    failed = [item for item in outcomes if item.status != "applied"]
    if failed:
        raise MigrationDrillError(
            "首次 apply 未全部成功: "
            + ", ".join(f"{item.migration_id}({item.sqlstate})" for item in failed)
        )
    return tuple(outcomes)


def reapply_all(
    conn: psycopg.Connection[Any], migrations: tuple[MigrationFile, ...]
) -> tuple[ApplyOutcome, ...]:
    """对已应用的迁移再次 apply：记录是「显式失败」还是「幂等成功」。

    两种都算可接受，但**静默跳过**（既不报错也不写台账）不可接受，因此这里把
    status 与 sqlstate 一并返回，由调用方断言「不存在静默跳过」。
    """

    outcomes: list[ApplyOutcome] = []
    for migration in migrations:
        sql = migration.path.read_text(encoding="utf-8")
        before = ledger_count(conn, migration.schema)
        try:
            with conn.transaction():
                conn.execute(sql)
                after_same = True
        except psycopg.Error as exc:
            outcomes.append(
                ApplyOutcome(
                    migration_id=migration.migration_id,
                    filename=migration.filename,
                    schema=migration.schema,
                    status="failed",
                    sqlstate=exc.sqlstate,
                    message=str(exc).strip().splitlines()[0][:300],
                    ledger_row=False,
                )
            )
            continue
        after = ledger_count(conn, migration.schema)
        outcomes.append(
            ApplyOutcome(
                migration_id=migration.migration_id,
                filename=migration.filename,
                schema=migration.schema,
                status="idempotent" if after_same else "applied",
                sqlstate=None,
                message="ok",
                ledger_row=after >= before,
            )
        )
    return tuple(outcomes)


def ledger_count(conn: psycopg.Connection[Any], schema: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {schema}.{LEDGER_TABLE}").fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True, slots=True)
class LedgerRow:
    schema: str
    migration_id: str
    filename: str
    sha256: str

    def matches(self, migration: MigrationFile) -> bool:
        return (
            self.migration_id == migration.migration_id
            and self.filename == migration.filename
            and self.sha256 == migration.sha256
        )


def ledger_rows(conn: psycopg.Connection[Any]) -> tuple[LedgerRow, ...]:
    """汇总所有 ``p0_*`` schema 下迁移台账的行（演练范围仅这些 schema）。"""

    schemas = conn.execute(
        "SELECT table_schema FROM information_schema.tables "
        "WHERE table_name = %s AND table_schema LIKE 'p0\\_%%' ORDER BY table_schema",
        (LEDGER_TABLE,),
    ).fetchall()
    rows: list[LedgerRow] = []
    for (schema,) in schemas:
        found = conn.execute(
            f"SELECT migration_id, filename, sha256 FROM {schema}.{LEDGER_TABLE} "
            "ORDER BY migration_id"
        ).fetchall()
        rows.extend(
            LedgerRow(
                schema=str(schema),
                migration_id=str(mid),
                filename=str(name),
                sha256=str(sha),
            )
            for mid, name, sha in found
        )
    return tuple(rows)


def assert_ledger_complete(
    conn: psycopg.Connection[Any], migrations: tuple[MigrationFile, ...]
) -> tuple[str, ...]:
    """断言每份迁移都有且仅有一条匹配台账行；返回问题清单（空表示通过）。"""

    rows = ledger_rows(conn)
    problems: list[str] = []
    for migration in migrations:
        matching = [row for row in rows if row.matches(migration)]
        if len(matching) != 1:
            problems.append(f"{migration.migration_id}: 台账匹配 {len(matching)} 条（期望 1）")
    return tuple(problems)
