"""极简幂等迁移 runner。

约定：
- 迁移文件放在 ``db/migrations``，文件名形如 ``0001_identity.sql``，按字典序执行。
- 每个文件一个事务；成功后在 ``app.schema_migrations`` 记录版本与校验和。
- 已执行文件的校验和变化视为「迁移被篡改」，直接失败而不是静默跳过。

用法::

    uv run python -m pyknora.migrate            # 应用所有未执行迁移
    uv run python -m pyknora.migrate --status   # 只看状态
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg

from pyknora.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.schema_migrations (
    version    text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    """迁移失败（校验和不一致或 SQL 报错）。"""


def discover(migrations_dir: Path) -> list[Path]:
    """按文件名排序返回 ``*.sql``。"""

    if not migrations_dir.is_dir():
        raise MigrationError(f"迁移目录不存在：{migrations_dir}")
    return sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM app.schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def run(database_url: str | None = None, migrations_dir: Path | None = None) -> list[str]:
    """应用迁移，返回本次新执行的版本列表。"""

    settings = get_settings()
    dsn = database_url or settings.sync_database_url
    directory = migrations_dir or MIGRATIONS_DIR
    files = discover(directory)

    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(BOOTSTRAP_SQL)
        applied = _applied(conn)
        for path in files:
            version = path.name
            digest = _checksum(path)
            if version in applied:
                if applied[version] != digest:
                    raise MigrationError(
                        f"迁移 {version} 的校验和已变化（记录 {applied[version][:12]}…，"
                        f"当前 {digest[:12]}…）；请新增迁移文件而不是修改已执行文件"
                    )
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO app.schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, digest),
                )
            applied_now.append(version)
    return applied_now


def status(
    database_url: str | None = None, migrations_dir: Path | None = None
) -> tuple[list[str], list[str]]:
    """返回 (已执行, 待执行) 版本列表。"""

    settings = get_settings()
    dsn = database_url or settings.sync_database_url
    directory = migrations_dir or MIGRATIONS_DIR
    files = [p.name for p in discover(directory)]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(BOOTSTRAP_SQL)
        applied = list(_applied(conn))
    return applied, [f for f in files if f not in applied]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyknora.migrate", description="PyKnora 数据库迁移")
    parser.add_argument("--status", action="store_true", help="只打印状态，不执行")
    parser.add_argument("--database-url", default=None, help="覆盖配置中的连接串")
    args = parser.parse_args(argv)

    if args.status:
        applied, pending = status(args.database_url)
        print(f"applied={len(applied)} pending={len(pending)}")
        for version in applied:
            print(f"  [x] {version}")
        for version in pending:
            print(f"  [ ] {version}")
        return 0

    applied_now = run(args.database_url)
    if applied_now:
        print(f"applied {len(applied_now)} migration(s): {', '.join(applied_now)}")
    else:
        print("database is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
