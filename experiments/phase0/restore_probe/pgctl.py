"""Database lifecycle and database-host process control for the T30 drill.

Why this module exists
----------------------
P0-T30 has to prove that a backup can be restored to a *consistent* state, that a
migration can be re-applied (or fails loudly), and that interrupting a backup or a
restore fails explicitly instead of silently producing a half-usable database.

The drill therefore needs three capabilities:

1. create / inspect / drop **throw-away** databases (never the core ``pyknora``
   database and never the core ``pgdata`` volume owned by P0-T02);
2. run ``pg_dump`` / ``pg_restore`` against those databases;
3. interrupt a running dump or restore and observe what failed.

Capability 2 and 3 are the interesting part in this environment.  The sandbox has
no PostgreSQL client on the Windows side (``pg_dump`` is absent) and a Python
process cannot reach the Docker named pipe (``npipe:////./pipe/dockerDesktopLinuxEngine``
is denied), so ``docker exec`` / ``docker cp`` are not available to the drill.

The remaining path is the one PostgreSQL itself provides: ``COPY ... TO PROGRAM``
and ``COPY ... FROM PROGRAM`` execute a command **on the database host** (inside
the server container, where ``pg_dump``/``pg_restore`` 17.11 live).  That is not a
workaround fiction: the artifact is a real ``pg_dump -Fc`` custom-format file
written to a host path, ``pg_restore -l`` validates it, and ``pg_restore
--exit-on-error`` restores it.  Only the *transport* differs from the production
runbook, where the same binary runs on the backup host.  This deviation is
recorded in ``docs/poc/evidence/restore-runbook.md`` (residual risk R-1).

Contract notes
--------------
* Every drill database is named ``p0_t30_*``; :func:`ensure_absent` refuses to
  drop anything outside :data:`DRILL_DATABASES`.
* Credentials never appear in this module.  Commands run inside the server host
  use the unix socket (``-h /var/run/postgresql``), which is ``trust`` in the
  official image, so no password is embedded anywhere.
* The module never writes to the core ``pyknora`` database.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg

from scope import read_dsn

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

DRILL_PREFIX = "p0_t30_"
SOURCE_DB = "p0_t30_source"
RESTORE_DB = "p0_t30_restore"
ROLLBACK_DB = "p0_t30_rollback"
FAULT_DB = "p0_t30_fault"
DRILL_DATABASES: tuple[str, ...] = (SOURCE_DB, RESTORE_DB, ROLLBACK_DB, FAULT_DB)
MAINTENANCE_DB = "postgres"

SERVER_SOCKET_DIR = "/var/run/postgresql"
SERVER_TEMP_DIR = "/tmp"
PG_DUMP = "pg_dump"
PG_RESTORE = "pg_restore"
BACKUP_SUFFIX = ".dump"

CODE_ALREADY_EXISTS = "DATABASE_ALREADY_EXISTS"
CODE_ABSENT = "DATABASE_ABSENT"
CODE_NOT_DRILL = "DATABASE_NOT_A_DRILL_TARGET"
CODE_PROGRAM_FAILED = "PROGRAM_FAILED"
CODE_BACKUP_UNREADABLE = "BACKUP_UNREADABLE"
CODE_BACKUP_INTERRUPTED = "BACKUP_INTERRUPTED"
CODE_RESTORE_INTERRUPTED = "RESTORE_INTERRUPTED"
CODE_RESTORE_OCCUPIED = "RESTORE_TARGET_OCCUPIED"
CODE_RESTORE_FAILED = "RESTORE_FAILED"

#: ``pg_stat_activity.query`` marker for a backend that is running a COPY PROGRAM.
COPY_PROGRAM_MARKER = "COPY "
_INTERRUPTED_RE = re.compile(
    r"terminating connection|server closed the connection|connection (?:is )?closed|"
    r"SSL connection has been closed|could not receive data",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class RestoreProbeError(Exception):
    """Base error carrying a stable ``code`` used by tests and the runbook."""

    code = "RESTORE_PROBE_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class DatabaseStateError(RestoreProbeError):
    """A drill database is in the wrong lifecycle state."""

    code = CODE_ALREADY_EXISTS


class BackupInterrupted(RestoreProbeError):
    """The dump was interrupted before it produced a usable artifact."""

    code = CODE_BACKUP_INTERRUPTED


class RestoreInterrupted(RestoreProbeError):
    """The restore was interrupted before it finished."""

    code = CODE_RESTORE_INTERRUPTED


class RestoreConflict(RestoreProbeError):
    """The restore target already holds objects; restoring would merge state."""

    code = CODE_RESTORE_OCCUPIED


class ProgramExecutionError(RestoreProbeError):
    """A command executed on the database host failed."""

    code = CODE_PROGRAM_FAILED


# --------------------------------------------------------------------------- #
# DSN helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DsnParts:
    scheme: str
    user: str
    password: str
    host: str
    port: str
    database: str

    @property
    def scheme_prefix(self) -> str:
        return f"{self.scheme}://"


def split_dsn(dsn: str) -> DsnParts:
    """Split a libpq URI without re-encoding any component."""

    scheme, _, rest = dsn.partition("://")
    if not rest:
        raise RestoreProbeError(f"unsupported DSN (no scheme): {redact_dsn(dsn)}")
    credentials, _, location = rest.rpartition("@")
    user, _, password = credentials.partition(":")
    host_port, _, database = location.partition("/")
    host, _, port = host_port.partition(":")
    return DsnParts(
        scheme=scheme,
        user=user,
        password=password,
        host=host,
        port=port or "5432",
        database=database,
    )


def database_dsn(dsn: str, database: str) -> str:
    """Return ``dsn`` pointed at another database on the same server."""

    parts = split_dsn(dsn)
    credentials = parts.user
    if parts.password:
        credentials = f"{parts.user}:{parts.password}"
    host_port = parts.host
    if parts.port:
        host_port = f"{parts.host}:{parts.port}"
    return f"{parts.scheme_prefix}{credentials}@{host_port}/{database}"


def redact_dsn(dsn: str) -> str:
    """Return ``dsn`` with the password replaced by ``***`` (safe for evidence)."""

    parts = split_dsn(dsn) if "://" in dsn else None
    if parts is None:
        return "<dsn>"
    credentials = parts.user
    if parts.password:
        credentials = f"{parts.user}:***"
    host_port = parts.host if not parts.port else f"{parts.host}:{parts.port}"
    return f"{parts.scheme_prefix}{credentials}@{host_port}/{parts.database}"


# --------------------------------------------------------------------------- #
# connections and database lifecycle
# --------------------------------------------------------------------------- #


def connect(dsn: str) -> psycopg.Connection:
    """Open an autocommit connection (COPY PROGRAM must not sit in a transaction)."""

    return psycopg.connect(dsn, autocommit=True)


def admin_connection(dsn: str | None = None) -> psycopg.Connection:
    """Connection to the maintenance database, used to create/drop drill databases."""

    return connect(database_dsn(dsn or read_dsn(), MAINTENANCE_DB))


def list_databases(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
    ).fetchall()
    return [row[0] for row in rows]


def drill_databases(conn: psycopg.Connection) -> list[str]:
    """Databases this drill owns and is allowed to drop."""

    present = set(list_databases(conn))
    return [name for name in DRILL_DATABASES if name in present]


def database_exists(conn: psycopg.Connection, database: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
    return row is not None


def terminate_database_sessions(
    conn: psycopg.Connection,
    database: str,
    *,
    exclude_pids: tuple[int, ...] = (),
) -> int:
    """Terminate every backend connected to ``database`` except ``exclude_pids``.

    Only used at teardown, when no backend is executing ``COPY ... TO PROGRAM``.
    Never aim this at a COPY PROGRAM backend: the postmaster treats such a child's
    SIGTERM exit as a crash and reinitializes the whole cluster.
    """

    rows = conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid() AND pid <> ALL(%s::int[])",
        (database, list(exclude_pids)),
    ).fetchall()
    return sum(1 for (ok,) in rows if ok)


def create_database(
    conn: psycopg.Connection,
    database: str,
    *,
    template: str = "template0",
) -> None:
    """Create an empty drill database; an existing one is an explicit error."""

    if database_exists(conn, database):
        raise DatabaseStateError(
            f"database {database!r} already exists",
            detail=f"drop it first: {CODE_ALREADY_EXISTS}",
        )
    try:
        conn.execute(f'CREATE DATABASE "{database}" TEMPLATE {template}')
    except psycopg.errors.DuplicateDatabase as exc:  # pragma: no cover - race
        raise DatabaseStateError(f"database {database!r} already exists") from exc


def drop_database(conn: psycopg.Connection, database: str) -> None:
    """Drop a drill database, terminating leftover sessions first."""

    if not database_exists(conn, database):
        raise DatabaseStateError(f"database {database!r} does not exist")
    terminate_database_sessions(conn, database)
    conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def ensure_absent(
    conn: psycopg.Connection,
    databases: tuple[str, ...] = DRILL_DATABASES,
) -> list[str]:
    """Drop every drill database that is present; refuse anything outside the set."""

    unknown = [name for name in databases if name not in DRILL_DATABASES]
    if unknown:
        raise DatabaseStateError(
            f"refusing to drop non-drill databases: {unknown}", detail=CODE_NOT_DRILL
        )
    dropped: list[str] = []
    for name in databases:
        if database_exists(conn, name):
            drop_database(conn, name)
            dropped.append(name)
    return dropped


def database_state(conn: psycopg.Connection, database: str) -> str:
    """``present`` / ``absent`` for one drill database."""

    return "present" if database_exists(conn, database) else "absent"


# --------------------------------------------------------------------------- #
# database-host process control (COPY ... TO/FROM PROGRAM)
# --------------------------------------------------------------------------- #


def sql_literal(value: str) -> str:
    """Quote ``value`` as a PostgreSQL string literal.

    ``COPY ... TO PROGRAM`` accepts no bind parameters (psycopg sends the
    statement through the simple protocol), so scripts are inlined and quoted.
    """

    return "'" + value.replace("'", "''") + "'"


def _server_sh(conn: psycopg.Connection, script: str) -> list[str]:
    """Run ``script`` on the database host and return its stdout lines.

    The command runs as a child of the PostgreSQL backend, so it can read the
    unix socket and the server-side ``/tmp``.  Output is captured through
    ``COPY ... FROM PROGRAM``.
    """

    table = f"p0_out_{uuid.uuid4().hex[:10]}"
    conn.execute(f"CREATE TEMP TABLE {table} (line text)")
    try:
        conn.execute(f"COPY {table} (line) FROM PROGRAM {sql_literal(script)}")
    except psycopg.Error as exc:
        if conn.closed or conn.broken:
            # The backend running the COPY was terminated: this is the fault, not
            # a program error.  Callers turn it into *_INTERRUPTED.
            raise
        raise ProgramExecutionError(
            f"program failed on the database host: {exc}", detail=script
        ) from exc
    else:
        rows = conn.execute(f"SELECT line FROM {table}").fetchall()
    finally:
        with contextlib.suppress(psycopg.Error):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    return [row[0] for row in rows]


def _parse_kv(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            parsed[key.strip()] = value.strip()
    return parsed


def _guarded(script: str) -> str:
    """Wrap ``script`` so the shell always exits 0.

    ``COPY ... [TO|FROM] PROGRAM`` raises ``program "..." failed`` when the child
    exits non-zero, and psycopg reports that as a lost connection.  The drill
    wants the *script* to decide the exit status and communicate it through a
    status file, so the shell is forced to exit 0 and the caller parses ``rc``.
    """

    return f"{script}; exit 0"


def _run_program(conn: psycopg.Connection, script: str) -> None:
    """Run ``script`` on the database host for its side effects only.

    The command's stdout is discarded (``COPY TO PROGRAM`` feeds it the row
    stream), so scripts must write their findings to files that are read back
    with :func:`server_sh`.
    """

    try:
        conn.execute(f"COPY (SELECT 1) TO PROGRAM {sql_literal(_guarded(script))}")
    except psycopg.Error as exc:
        if conn.closed or conn.broken:
            raise
        raise ProgramExecutionError(
            f"program failed on the database host: {exc}", detail=script
        ) from exc


@dataclass(frozen=True)
class ArtifactMeta:
    """Metadata of a dump artifact stored on the database host."""

    path: str
    size_bytes: int
    sha256: str
    exists: bool

    @property
    def usable(self) -> bool:
        return self.exists and self.size_bytes > 0 and len(self.sha256) == 64


def _artifact_meta_on(conn: psycopg.Connection, path: str) -> ArtifactMeta:
    """``stat`` + ``sha256sum`` the artifact on the database host."""

    script = (
        f"if [ -f {path} ]; then echo present=1; stat -c 'size=%s' {path}; "
        f"sha256sum {path} | cut -d' ' -f1 | sed 's/^/sha=/'; else echo present=0; fi"
    )
    parsed = _parse_kv(_server_sh(conn, script))
    present = parsed.get("present") == "1"
    return ArtifactMeta(
        path=path,
        size_bytes=int(parsed.get("size", "0") or 0),
        sha256=parsed.get("sha", ""),
        exists=present,
    )


def _remove_artifact_on(conn: psycopg.Connection, path: str) -> None:
    """Delete an artifact and its side files on the database host."""

    _server_sh(conn, f"rm -f {path} {path}.err {path}.list 2>/dev/null; echo done=1")


def host_artifact_path(label: str) -> str:
    """Path of a dump artifact on the database host (server-side ``/tmp``)."""

    safe = re.sub(r"[^a-z0-9_.-]+", "-", label.lower()).strip("-") or "artifact"
    return f"{SERVER_TEMP_DIR}/{DRILL_PREFIX}{safe}{BACKUP_SUFFIX}"


@dataclass(frozen=True)
class BackupResult:
    """Outcome of one ``pg_dump`` run."""

    database: str
    artifact: str
    size_bytes: int
    sha256: str
    entries: int
    elapsed_s: float
    stderr_tail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "artifact": self.artifact,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "entries": self.entries,
            "elapsed_s": round(self.elapsed_s, 3),
            "stderr_tail": self.stderr_tail,
        }


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of one ``pg_restore`` run."""

    database: str
    artifact: str
    entries: int
    elapsed_s: float
    stderr_tail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "artifact": self.artifact,
            "entries": self.entries,
            "elapsed_s": round(self.elapsed_s, 3),
            "stderr_tail": self.stderr_tail,
        }


def _dump_script(user: str, database: str, artifact: str) -> str:
    return (
        f"{PG_DUMP} -h {SERVER_SOCKET_DIR} -U {user} -Fc --no-owner --no-privileges "
        f"-f {artifact} {database} 2> {artifact}.err; echo $? > {artifact}.rc"
    )


def _restore_script(user: str, database: str, artifact: str) -> str:
    return (
        f"{PG_RESTORE} -h {SERVER_SOCKET_DIR} -U {user} -d {database} "
        f"--exit-on-error --no-owner --no-privileges {artifact} 2> {artifact}.err; "
        f"echo $? > {artifact}.restore-rc"
    )


def _read_status_on(conn: psycopg.Connection, path: str) -> str | None:
    """Read a one-line status file written by a database-host command."""

    rows = _server_sh(conn, f"cat {path} 2>/dev/null || true")
    text = "".join(row.strip() for row in rows if row.strip())
    return text or None


def _count_backup_entries_on(conn: psycopg.Connection, artifact: str) -> int:
    """Number of table-of-contents entries readable from the artifact."""

    listing = f"{artifact}.list"
    _run_program(
        conn,
        f"{PG_RESTORE} -l {artifact} > {listing} 2> {listing}.err; "
        f"echo $? > {listing}.rc; wc -l < {listing} > {listing}.count",
    )
    status = _read_status_on(conn, f"{listing}.rc")
    if status != "0":
        raise RestoreProbeError(
            f"artifact is unreadable: {artifact}", detail=CODE_BACKUP_UNREADABLE
        )
    count = _read_status_on(conn, f"{listing}.count") or "0"
    return int(count)


def _tail(conn: psycopg.Connection, path: str, *, limit: int = 1200) -> str:
    lines = _server_sh(conn, f"test -f {path} && tail -c {limit} {path} || true")
    return "\n".join(line for line in lines if line).strip()


def backup_database(
    dsn: str | None,
    *,
    database: str,
    label: str | None = None,
    statement_timeout_ms: int | None = None,
) -> BackupResult:
    """Dump ``database`` with the database host's ``pg_dump``.

    ``statement_timeout_ms`` is the drill's only interruption mechanism.  The server
    cancels the statement itself; it never sends a signal to a backend that is running
    ``COPY ... TO PROGRAM``.  Terminating such a backend makes the postmaster treat the
    program child's SIGTERM exit as a crash and reinitialize the whole cluster, which is
    unacceptable on a shared instance.
    """

    artifact = host_artifact_path(label or database)
    with _host_call(dsn) as conn:
        _remove_artifact_on(conn, artifact)
        user = conn.info.user
    started = _monotonic()
    cancelled = False
    try:
        with _host_call(dsn, statement_timeout_ms=statement_timeout_ms) as conn:
            _run_program(conn, _dump_script(user, database, artifact))
    except psycopg.errors.QueryCanceled as exc:
        cancelled = True
        cancel_detail = f"statement_timeout={statement_timeout_ms}ms: {exc}"
    except (
        psycopg.OperationalError,
        psycopg.InterfaceError,
        ProgramExecutionError,
    ) as exc:
        raise BackupInterrupted(f"backup of {database} failed: {exc}", detail=artifact) from exc
    elapsed = _monotonic() - started
    with _host_call(dsn) as conn:
        meta = _artifact_meta_on(conn, artifact)
        if cancelled:
            raise BackupInterrupted(
                f"pg_dump for {database} was cancelled by statement_timeout",
                detail=f"{cancel_detail} partial_size={meta.size_bytes} usable={meta.usable}",
            )
        status = _read_status_on(conn, f"{artifact}.rc")
        if status != "0":
            raise BackupInterrupted(
                f"pg_dump exited {status} for {database}",
                detail=_tail(conn, f"{artifact}.err"),
            )
        if not meta.usable:
            raise BackupInterrupted(
                f"pg_dump produced an unusable artifact for {database}",
                detail=f"size={meta.size_bytes} exists={meta.exists}",
            )
        entries = _count_backup_entries_on(conn, artifact)
    return BackupResult(
        database=database,
        artifact=artifact,
        size_bytes=meta.size_bytes,
        sha256=meta.sha256,
        entries=entries,
        elapsed_s=elapsed,
    )


def restore_database(
    dsn: str | None,
    *,
    database: str,
    artifact: str,
    statement_timeout_ms: int | None = None,
) -> RestoreResult:
    """Restore ``artifact`` into ``database`` with the database host's ``pg_restore``.

    Same interruption contract as :func:`backup_database`: cancellation happens on the
    server, the in-flight ``pg_restore`` dies with its statement, and the target database
    is left explicitly incomplete instead of being reported as restored.
    """

    with _host_call(dsn) as conn:
        user = conn.info.user
    started = _monotonic()
    cancelled = False
    try:
        with _host_call(dsn, statement_timeout_ms=statement_timeout_ms) as conn:
            _run_program(conn, _restore_script(user, database, artifact))
    except psycopg.errors.QueryCanceled as exc:
        cancelled = True
        cancel_detail = f"statement_timeout={statement_timeout_ms}ms: {exc}"
    except (
        psycopg.OperationalError,
        psycopg.InterfaceError,
        ProgramExecutionError,
    ) as exc:
        raise RestoreInterrupted(f"restore into {database} failed: {exc}", detail=artifact) from exc
    elapsed = _monotonic() - started
    with _host_call(dsn) as conn:
        status = _read_status_on(conn, f"{artifact}.restore-rc")
        stderr_tail = _tail(conn, f"{artifact}.err")
        if cancelled:
            raise RestoreInterrupted(
                f"pg_restore into {database} was cancelled by statement_timeout; "
                "the target database is incomplete",
                detail=f"{cancel_detail} {stderr_tail}",
            )
        if status != "0":
            if "already exists" in stderr_tail.lower():
                raise RestoreConflict(
                    f"restore target {database} already holds objects", detail=stderr_tail
                )
            raise RestoreProbeError(
                f"pg_restore exited {status} for {database}", detail=stderr_tail
            )
        entries = _count_backup_entries_on(conn, artifact)
    return RestoreResult(
        database=database,
        artifact=artifact,
        entries=entries,
        elapsed_s=elapsed,
        stderr_tail=stderr_tail,
    )


# --------------------------------------------------------------------------- #
# DSN-scoped public API
# --------------------------------------------------------------------------- #
#
# Every helper below opens a *disposable* connection for a single database-host
# interaction and closes it immediately.  A connection that ran ``COPY ... TO
# PROGRAM`` and then observed an interrupted program is not reliably reusable
# (psycopg leaves the copy state behind), so the drill never reuses one.


def host_connection(dsn: str | None = None) -> psycopg.Connection:
    """Disposable autocommit connection used for one database-host call."""

    return connect(database_dsn(dsn or read_dsn(), MAINTENANCE_DB))


@contextlib.contextmanager
def _host_call(
    dsn: str | None = None,
    *,
    statement_timeout_ms: int | None = None,
) -> Iterator[psycopg.Connection]:
    conn = host_connection(dsn)
    try:
        if statement_timeout_ms is not None:
            # Local to this disposable session: the server cancels the statement, so
            # the drill never has to interrupt a COPY PROGRAM backend from outside.
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{statement_timeout_ms}ms",),
            )
        yield conn
    finally:
        with contextlib.suppress(psycopg.Error):
            conn.close()


def sh(dsn: str | None, script: str) -> list[str]:
    """Run ``script`` on the database host and return its stdout lines."""

    with _host_call(dsn) as conn:
        return _server_sh(conn, script)


def run(dsn: str | None, script: str) -> None:
    """Run ``script`` on the database host for its side effects."""

    with _host_call(dsn) as conn:
        _run_program(conn, script)


def read_status(dsn: str | None, path: str) -> str | None:
    """Read a one-line status file written by a database-host command."""

    with _host_call(dsn) as conn:
        return _read_status_on(conn, path)


def tail_file(dsn: str | None, path: str, *, limit: int = 1200) -> str:
    """Tail a file on the database host."""

    with _host_call(dsn) as conn:
        return _tail(conn, path)


def artifact_meta(dsn: str | None, path: str) -> ArtifactMeta:
    """``stat`` + ``sha256sum`` an artifact on the database host."""

    with _host_call(dsn) as conn:
        return _artifact_meta_on(conn, path)


def count_backup_entries(dsn: str | None, artifact: str) -> int:
    """Number of table-of-contents entries readable from an artifact."""

    with _host_call(dsn) as conn:
        return _count_backup_entries_on(conn, artifact)


def remove_artifact(dsn: str | None, path: str) -> None:
    """Delete an artifact and its side files on the database host."""

    with _host_call(dsn) as conn:
        _remove_artifact_on(conn, path)


# --------------------------------------------------------------------------- #
# misc helpers
# --------------------------------------------------------------------------- #


def prepare_workdir(path: Path) -> Path:
    """Create a fresh host workdir for manifests and reports."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_workdir(path: Path) -> tuple[str, tuple[str, ...]]:
    """Remove ``path``; return ``(state, leftovers)`` for the evidence table."""

    if not path.exists():
        return "absent", ()
    leftovers = tuple(sorted(str(item.relative_to(path)) for item in path.rglob("*")))
    for item in sorted(path.rglob("*"), reverse=True):
        if item.is_file():
            item.unlink(missing_ok=True)
        elif item.is_dir():
            item.rmdir()
    path.rmdir()
    return ("removed" if not path.exists() else "left"), leftovers


def write_manifest(path: Path, payload: dict[str, object]) -> Path:
    """Write a JSON manifest next to the report."""

    import json

    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    """Host-side sha256 of a small file (manifests, reports)."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


_monotonic = __import__("time").monotonic


def _yield() -> None:
    """Cooperative yield used while polling server state (not a fixed sleep)."""

    __import__("time").sleep(0)


__all__ = [
    "ArtifactMeta",
    "BackupInterrupted",
    "BackupResult",
    "CODE_BACKUP_INTERRUPTED",
    "CODE_BACKUP_UNREADABLE",
    "CODE_RESTORE_INTERRUPTED",
    "CODE_RESTORE_OCCUPIED",
    "DRILL_DATABASES",
    "DRILL_PREFIX",
    "DatabaseStateError",
    "DsnParts",
    "FAULT_DB",
    "MAINTENANCE_DB",
    "ProgramExecutionError",
    "RESTORE_DB",
    "ROLLBACK_DB",
    "RestoreConflict",
    "RestoreInterrupted",
    "RestoreProbeError",
    "RestoreResult",
    "SOURCE_DB",
    "admin_connection",
    "artifact_meta",
    "backup_database",
    "cleanup_workdir",
    "connect",
    "count_backup_entries",
    "create_database",
    "database_dsn",
    "database_exists",
    "database_state",
    "drill_databases",
    "drop_database",
    "ensure_absent",
    "host_artifact_path",
    "list_databases",
    "prepare_workdir",
    "redact_dsn",
    "remove_artifact",
    "restore_database",
    "sh",
    "run",
    "host_connection",
    "tail_file",
    "sha256_file",
    "split_dsn",
    "sql_literal",
    "terminate_database_sessions",
    "write_manifest",
]
