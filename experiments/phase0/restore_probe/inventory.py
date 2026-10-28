"""逐表行数与内容指纹的快照，以及两份快照的差异比对。

证据里「还原前后逐表行数一致」不能只比行数：指针相同而行内容不同同样属于不一致。
因此每张表同时记录：

* ``rows``：``count(*)``；
* ``digest``：把整表 ``to_jsonb`` 行文本排序后取 sha256（行内容指纹）。

小表（行数不超过 :data:`FULL_ROW_LIMIT`）全量取指纹，其等价于「整表内容相等」；
大表只取行数与聚合指纹，避免把大字段读进内存。快照只覆盖 ``p0_*`` schema，
即 Phase 0 实验表，不触碰任何业务核心库之外的对象。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import psycopg

SCHEMA_PATTERN = "p0\\_%"
FULL_ROW_LIMIT = 500
EXCLUDED_PREFIXES: tuple[str, ...] = ("pg_", "sql_")


@dataclass(frozen=True, slots=True)
class TableFact:
    """单表事实：行数 + 内容指纹 + 是否全量取行。"""

    name: str
    rows: int
    digest: str
    full: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """某数据库在某一时刻的表级快照。"""

    database: str
    tables: tuple[TableFact, ...]

    def table_map(self) -> dict[str, TableFact]:
        return {fact.name: fact for fact in self.tables}

    @property
    def total_rows(self) -> int:
        return sum(fact.rows for fact in self.tables)

    @property
    def table_count(self) -> int:
        return len(self.tables)


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    """两份快照的差异；``equal`` 为真才允许宣称「还原到一致状态」。"""

    left: str
    right: str
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    row_mismatches: tuple[tuple[str, int, int], ...]
    digest_mismatches: tuple[tuple[str, str, str], ...]

    @property
    def equal(self) -> bool:
        return not (self.missing or self.extra or self.row_mismatches or self.digest_mismatches)

    def describe(self) -> tuple[str, ...]:
        lines: list[str] = []
        for name in self.missing:
            lines.append(f"缺失表: {name}")
        for name in self.extra:
            lines.append(f"多余表: {name}")
        for name, left, right in self.row_mismatches:
            lines.append(f"行数不一致: {name} {left} != {right}")
        for name, left, right in self.digest_mismatches:
            lines.append(f"内容指纹不一致: {name} {left[:12]} != {right[:12]}")
        return tuple(lines)


def _base_tables(conn: psycopg.Connection[Any]) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema LIKE %s "
        "ORDER BY table_schema, table_name",
        (SCHEMA_PATTERN,),
    ).fetchall()
    names = [str(row[0]) for row in rows]
    return tuple(name for name in names if not name.split(".", 1)[1].startswith(EXCLUDED_PREFIXES))


def _row_count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _table_digest(conn: psycopg.Connection[Any], table: str, *, full: bool) -> str:
    """整表内容哈希。

    ``full=True`` 时对每行 ``to_jsonb`` 文本排序后取哈希（行内容完全参与比对）；
    否则对行数 + 若干聚合列取哈希，仍能发现「行数相同但内容变化」。
    """

    hasher = hashlib.sha256()
    if full:
        rows = conn.execute(f"SELECT to_jsonb(t)::text FROM {table} t").fetchall()
        for text in sorted(str(row[0]) for row in rows):
            hasher.update(text.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()
    row = conn.execute(
        f"SELECT count(*), md5(coalesce(string_agg(t::text, '|'), '')) FROM {table} t"
    ).fetchone()
    hasher.update(f"rows={row[0]};md5={row[1]}".encode())
    return hasher.hexdigest()


def snapshot(conn: psycopg.Connection[Any], *, database: str | None = None) -> Snapshot:
    """取一次表级快照（同一事务内，调用方负责事务边界与隔离级别）。"""

    name = database or str(conn.execute("SELECT current_database()").fetchone()[0])
    facts: list[TableFact] = []
    for table in _base_tables(conn):
        rows = _row_count(conn, table)
        full = rows <= FULL_ROW_LIMIT
        facts.append(
            TableFact(
                name=table,
                rows=rows,
                digest=_table_digest(conn, table, full=full),
                full=full,
            )
        )
    return Snapshot(database=name, tables=tuple(facts))


def diff_snapshots(left: Snapshot, right: Snapshot) -> SnapshotDiff:
    left_map = left.table_map()
    right_map = right.table_map()
    missing = tuple(sorted(set(left_map) - set(right_map)))
    extra = tuple(sorted(set(right_map) - set(left_map)))
    row_mismatches: list[tuple[str, int, int]] = []
    digest_mismatches: list[tuple[str, str, str]] = []
    for name in sorted(set(left_map) & set(right_map)):
        left_fact = left_map[name]
        right_fact = right_map[name]
        if left_fact.rows != right_fact.rows:
            row_mismatches.append((name, left_fact.rows, right_fact.rows))
        elif left_fact.digest != right_fact.digest:
            digest_mismatches.append((name, left_fact.digest, right_fact.digest))
    return SnapshotDiff(
        left=left.database,
        right=right.database,
        missing=missing,
        extra=extra,
        row_mismatches=tuple(row_mismatches),
        digest_mismatches=tuple(digest_mismatches),
    )


@dataclass(frozen=True, slots=True)
class PointerFact:
    """关键指针的显式取值，用于证据里的「指针对比表」。"""

    name: str
    value: str


def pointer_table(conn: psycopg.Connection[Any]) -> tuple[PointerFact, ...]:
    """抓取任务卡点名的关键指针（缺失的表/列自动跳过，不伪造 0）。

    * ``documents.active_version`` 分布：可见版本指针是否整体回退；
    * 发布计数：每文档已发布版本数；
    * checkpoint 最新序列：业务事实与执行状态是否同点；
    * Job/Outbox 状态计数。
    """

    facts: list[PointerFact] = []
    if _has_table(conn, "p0_t12", "phase0_documents"):
        rows = conn.execute(
            "SELECT count(*)::text || ' docs / active[1]=' || "
            "count(*) FILTER (WHERE active_version = 1)::text || ' active[2]=' || "
            "count(*) FILTER (WHERE active_version = 2)::text || ' active[null]=' || "
            "count(*) FILTER (WHERE active_version IS NULL)::text || ' sum=' || "
            "coalesce(sum(active_version), 0)::text FROM p0_t12.phase0_documents"
        ).fetchone()
        facts.append(PointerFact("documents.active_version", str(rows[0])))
    if _has_table(conn, "p0_t13", "phase0_publications"):
        rows = conn.execute(
            "SELECT count(*)::text || ' pubs / committed=' || "
            "count(*) FILTER (WHERE committed_at IS NOT NULL)::text || ' chunk_sum=' || "
            "coalesce(sum(chunk_count), 0)::text || ' v2=' || "
            "count(*) FILTER (WHERE to_version = 2)::text FROM p0_t13.phase0_publications"
        ).fetchone()
        facts.append(PointerFact("publications", str(rows[0])))
    if _has_table(conn, "p0_t05", "phase0_runs"):
        rows = conn.execute(
            "SELECT count(*)::text || ' runs / seq_sum=' || "
            "coalesce(sum(last_checkpoint_seq), 0)::text || ' epochs=' || "
            "coalesce(sum(owner_epoch), 0)::text FROM p0_t05.phase0_runs"
        ).fetchone()
        facts.append(PointerFact("checkpoints.runs", str(rows[0])))
    if _has_table(conn, "p0_t05", "phase0_checkpoints"):
        rows = conn.execute(
            "SELECT count(*)::text || ' rows / max_seq=' || "
            "coalesce(max(seq), 0)::text FROM p0_t05.phase0_checkpoints"
        ).fetchone()
        facts.append(PointerFact("checkpoints.rows", str(rows[0])))
    if _has_table(conn, "p0_t07", "phase0_jobs"):
        rows = conn.execute(
            "SELECT count(*)::text || ' jobs / succeeded=' || "
            "count(*) FILTER (WHERE status = 'succeeded')::text FROM p0_t07.phase0_jobs"
        ).fetchone()
        facts.append(PointerFact("jobs", str(rows[0])))
    if _has_table(conn, "p0_t07", "phase0_outbox"):
        rows = conn.execute(
            "SELECT count(*)::text || ' outbox / dispatched=' || "
            "count(*) FILTER (WHERE status IN ('dispatched', 'succeeded'))::text "
            "FROM p0_t07.phase0_outbox"
        ).fetchone()
        facts.append(PointerFact("outbox", str(rows[0])))
    if _has_table(conn, "p0_t06", "phase0_tool_receipts"):
        rows = conn.execute(
            "SELECT count(*)::text || ' receipts / distinct_intents=' || "
            "count(DISTINCT intent_key)::text FROM p0_t06.phase0_tool_receipts"
        ).fetchone()
        facts.append(PointerFact("tool_receipts", str(rows[0])))
    return tuple(facts)


def diff_pointers(left: tuple[PointerFact, ...], right: tuple[PointerFact, ...]) -> tuple[str, ...]:
    right_map = {fact.name: fact.value for fact in right}
    problems: list[str] = []
    for fact in left:
        other = right_map.get(fact.name)
        if other is None:
            problems.append(f"指针缺失: {fact.name}")
        elif other != fact.value:
            problems.append(f"指针不一致: {fact.name} {fact.value!r} != {other!r}")
    return tuple(problems)


def _has_table(conn: psycopg.Connection[Any], schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchone()
    return row is not None
