"""P0-T25 补跑：pgvector ANN（HNSW / IVFFlat）基准 — W13 / ADR-0010 步骤 3。

背景
----
`retrieval_probe.py` 的向量检索是 **Python 精确全扫描**（`double precision[]` + 逐行余弦），
`poc09-report.md` 把 ANN 标为 `NOT_RUN`，理由是 W1 的 `postgres:17-alpine` 镜像不含 `vector` 扩展。
`fix/w1-t02-pgvector` 把镜像换成 `pgvector/pgvector:pg17`（`vector` 0.8.7 可用），ADR-0010 因此要求
**补跑 ANN**，且结论必须建立在环境证据已重采（`environment.md` §15）之上。

本模块回答的是**「ANN 机制是否可用、代价多少」**，不是「语义质量是否达标」：

* 向量是**确定性合成向量**（正态随机、L2 归一化），因为本机没有真实 embedding 模型；
* 因此 recall 衡量的是**索引近似误差**（对同一批向量，ANN 结果 vs 精确扫描结果），
  与「真实模型下召回率高不高」是两件事，后者仍是 `NOT_RUN`（DG-4 不解除）；
* 语料**不复用** `p0_t25` 的业务表：只借它的资源槽命名法，建独立 schema `p0_t25_ann`，跑完即删。

为什么基准用 128 维而不是 T25 的 4096 维
----------------------------------------
pgvector 的 `vector` 类型**索引上限是 2000 维**（HNSW/IVFFlat 都一样；`halfvec` 可到 4000）。
`retrieval_probe.EMBEDDING_DIM = 4096`（假词袋哈希，靠维度压噪声），**这个维度建不了 ANN 索引**。
这是一条必须写进报告的发现：真实落地要么换 1024/1536/3072 维的真实模型，要么用 `halfvec`，
要么截断——三者都需要 ADR，不在本任务自行决定。本条限制已写进 `ann-bench.json` 的 `limitations`。

用法
----
    python experiments/phase0/ann_bench.py            # 默认 20000×128，100 查询
    python experiments/phase0/ann_bench.py --rows 4000 --queries 40
    python experiments/phase0/ann_bench.py --keep     # 跑完保留 schema（调试用）

副作用：建/删 schema `p0_t25_ann`；写 `docs/poc/evidence/ann-bench.json`。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

import scope
from retrieval_probe import redact_dsn

# ---------------------------------------------------------------------------
# 常量（规格参数全部进证据，便于复现）
# ---------------------------------------------------------------------------
SCHEMA: str = "p0_t25_ann"
TABLE: str = "phase0_ann_chunks"

DEFAULT_DIM: int = 128
DEFAULT_ROWS: int = 20_000
DEFAULT_QUERIES: int = 100
DEFAULT_K: int = 10

HNSW_M: int = 16
HNSW_EF_CONSTRUCTION: int = 64
EF_SEARCH_SWEEP: tuple[int, ...] = (40, 64, 128)
IVFFLAT_LISTS: int = 100
IVFFLAT_PROBES_SWEEP: tuple[int, ...] = (1, 8, 32)

WORKSPACES: tuple[str, ...] = ("ws_a", "ws_b")
SEED: int = 20261029

#: 合成语料用「聚类 + 抖动」而不是纯均匀随机：高维均匀随机向量两两近乎正交，
#: 真实最近邻与随机点距离差极小（top-10 与随机 10 个几乎不可区分），
#: 那样算出来的 recall 只反映噪声。聚簇结构才让「近邻」有可测量的语义。
CLUSTERS: int = 100
JITTER: float = 0.35

#: pgvector `vector` 类型的索引维度上限（HNSW/IVFFlat 相同）。T25 的假向量是 4096 维。
PGVECTOR_INDEX_MAX_DIM: int = 2000

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
EVIDENCE_PATH: Path = REPO_ROOT / "docs" / "poc" / "evidence" / "ann-bench.json"


# ---------------------------------------------------------------------------
# 连接与 schema
# ---------------------------------------------------------------------------
def connect(dsn: str | None = None) -> psycopg.Connection[Any]:
    """连实验库（DSN 复用 `scope.read_dsn()`，与其它探针同一口径）。"""

    if dsn is None:
        dsn = scope.read_dsn()
    return psycopg.connect(dsn, autocommit=False, row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection[Any], *, dim: int) -> None:
    """建本槽 schema + `vector` 扩展 + 基准表（调用方 commit）。"""

    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.{} ("
            "  id integer PRIMARY KEY,"
            "  workspace_id text NOT NULL,"
            "  emb vector({dim}) NOT NULL"
            ")"
        ).format(
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
            dim=sql.Literal(dim),
        )
    )


def drop_schema(conn: psycopg.Connection[Any]) -> bool:
    """删本槽 schema（只碰 `p0_t25_ann`），返回是否原本存在。"""

    exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (SCHEMA,)).fetchone()
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    return bool(exists)


def _analyze(conn: psycopg.Connection[Any]) -> None:
    """统计信息刷新（索引建好后必须做，否则规划器可能不走索引）。"""

    conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE)))


# ---------------------------------------------------------------------------
# 合成向量（确定性）
# ---------------------------------------------------------------------------
def make_centers(*, dim: int, clusters: int = CLUSTERS, seed: int = SEED) -> list[list[float]]:
    """随机单位中心（数据与查询必须共用同一组中心，否则查询落在数据簇之外，recall 无意义）。"""

    rng = random.Random(seed)
    centers: list[list[float]] = []
    for _ in range(clusters):
        center = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        scale = math.sqrt(sum(x * x for x in center)) or 1.0
        centers.append([x / scale for x in center])
    return centers


def synth_vectors(
    n: int,
    *,
    dim: int,
    seed: int = SEED,
    clusters: int = CLUSTERS,
    jitter: float = JITTER,
    centers: Sequence[Sequence[float]] | None = None,
) -> list[list[float]]:
    """确定性聚簇向量：`normalize(center[c] + jitter * noise)`，同 seed 逐位可复现。

    聚簇的必要性见 `CLUSTERS` 注释：均匀随机高维向量没有可测的近邻结构。
    传入 `centers` 时（查询侧）必须用**数据同一组中心**，否则查询会落在簇外。
    """

    rng = random.Random(seed)
    if centers is None:
        centers = make_centers(dim=dim, clusters=clusters, seed=seed)

    out: list[list[float]] = []
    # 抖动必须按维度归一：128 维 `gauss(0,1)` 的模长约 sqrt(128)≈11.3，
    # 若直接用 jitter=0.35 作标准差，噪声会盖过单位中心（模 1），簇结构被抹平，
    # 测出来就退化成「均匀随机向量」（详见 CLUSTERS 注释）。
    scale = jitter / math.sqrt(dim)
    for i in range(n):
        center = centers[i % len(centers)]
        vec = [c + scale * rng.gauss(0.0, 1.0) for c in center]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / norm for x in vec])
    return out


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector 文本字面量 `[a,b,c]`（不经 pgvector-python 适配器，降低依赖）。"""

    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def workspace_of(index: int, *, workspaces: int = len(WORKSPACES)) -> str:
    return WORKSPACES[index % workspaces]


# ---------------------------------------------------------------------------
# 装载与查询
# ---------------------------------------------------------------------------
def load_rows(conn: psycopg.Connection[Any], vectors: Sequence[Sequence[float]]) -> float:
    """逐行 INSERT（整数 id、workspace、向量），返回耗时秒数。"""

    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.executemany(
            sql.SQL("INSERT INTO {}.{} (id, workspace_id, emb) VALUES (%s, %s, %s::vector)").format(
                sql.Identifier(SCHEMA), sql.Identifier(TABLE)
            ),
            [(i, workspace_of(i), vector_literal(vec)) for i, vec in enumerate(vectors, start=1)],
        )
    return time.perf_counter() - started


def create_hnsw(conn: psycopg.Connection[Any], *, dim: int) -> float:
    started = time.perf_counter()
    # CREATE INDEX 的 WITH 选项不接受绑定参数，必须内联字面量
    conn.execute(
        sql.SQL(
            "CREATE INDEX {} ON {}.{} USING hnsw (emb vector_cosine_ops) "
            "WITH (m = {}, ef_construction = {})"
        ).format(
            sql.Identifier(f"{TABLE}_hnsw"),
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
            sql.Literal(HNSW_M),
            sql.Literal(HNSW_EF_CONSTRUCTION),
        )
    )
    return time.perf_counter() - started


def create_ivfflat(conn: psycopg.Connection[Any]) -> float:
    started = time.perf_counter()
    conn.execute(
        sql.SQL(
            "CREATE INDEX {} ON {}.{} USING ivfflat (emb vector_cosine_ops) WITH (lists = {})"
        ).format(
            sql.Identifier(f"{TABLE}_ivfflat"),
            sql.Identifier(SCHEMA),
            sql.Identifier(TABLE),
            sql.Literal(IVFFLAT_LISTS),
        )
    )
    return time.perf_counter() - started


def _table_ref() -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(TABLE))


def exact_ids(
    conn: psycopg.Connection[Any],
    query: Sequence[float],
    *,
    k: int,
    workspace_id: str | None = None,
) -> list[int]:
    """精确全扫描（数据库侧，`<=>` 算子 + 强制 seq scan），作为 ground truth。"""

    if workspace_id is None:
        stmt = sql.SQL("SELECT id FROM {} ORDER BY emb <=> %s::vector LIMIT %s").format(
            _table_ref()
        )
        params: list[Any] = [vector_literal(query), k]
    else:
        stmt = sql.SQL(
            "SELECT id FROM {} WHERE workspace_id = %s ORDER BY emb <=> %s::vector LIMIT %s"
        ).format(_table_ref())
        params = [workspace_id, vector_literal(query), k]
    conn.execute("SET LOCAL enable_indexscan = off")
    conn.execute("SET LOCAL enable_bitmapscan = off")
    rows = conn.execute(stmt, params).fetchall()
    return [int(row["id"]) for row in rows]


def ann_ids(
    conn: psycopg.Connection[Any],
    query: Sequence[float],
    *,
    k: int,
    ef_search: int,
    workspace_id: str | None = None,
) -> list[int]:
    """HNSW 近似查询（`hnsw.ef_search` 会话级设置，索引必须命中）。

    强制 `enable_seqscan = off`：小数据量下规划器会选「顺序扫描 + 排序」，
    那样测出来的 recall 恒等于 1.0，等于没测 ANN。强制后若索引不存在会直接报错，
    反而是「索引真的被用上了」的自证。
    """

    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(ef_search)))
    if workspace_id is None:
        stmt = sql.SQL("SELECT id FROM {} ORDER BY emb <=> %s::vector LIMIT %s").format(
            _table_ref()
        )
        rows = conn.execute(stmt, (vector_literal(query), k)).fetchall()
    else:
        stmt = sql.SQL(
            "SELECT id FROM {} WHERE workspace_id = %s ORDER BY emb <=> %s::vector LIMIT %s"
        ).format(_table_ref())
        rows = conn.execute(stmt, (workspace_id, vector_literal(query), k)).fetchall()
    return [int(row["id"]) for row in rows]


def ivfflat_ids(
    conn: psycopg.Connection[Any],
    query: Sequence[float],
    *,
    k: int,
    probes: int,
) -> list[int]:
    conn.execute("SET LOCAL enable_seqscan = off")
    conn.execute(sql.SQL("SET LOCAL ivfflat.probes = {}").format(sql.Literal(probes)))
    stmt = sql.SQL("SELECT id FROM {} ORDER BY emb <=> %s::vector LIMIT %s").format(_table_ref())
    rows = conn.execute(stmt, (vector_literal(query), k)).fetchall()
    return [int(row["id"]) for row in rows]


def query_plan(
    conn: psycopg.Connection[Any], query: Sequence[float], *, ef_search: int = 64
) -> list[str]:
    """确认 ANN 语句真的走索引（不靠「跑得快」猜）。用 `SET LOCAL` 仅影响当前事务。"""

    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(ef_search)))
        plan = conn.execute(
            sql.SQL("EXPLAIN SELECT id FROM {} ORDER BY emb <=> %s::vector LIMIT 10").format(
                _table_ref()
            ),
            (vector_literal(query),),
        ).fetchall()
    return [str(row["QUERY PLAN"]) for row in plan]


def query_plan_ivfflat(
    conn: psycopg.Connection[Any], query: Sequence[float], *, probes: int
) -> list[str]:
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ivfflat.probes = {}").format(sql.Literal(probes)))
        plan = conn.execute(
            sql.SQL("EXPLAIN SELECT id FROM {} ORDER BY emb <=> %s::vector LIMIT 10").format(
                _table_ref()
            ),
            (vector_literal(query),),
        ).fetchall()
    return [str(row["QUERY PLAN"]) for row in plan]


def drop_index(conn: psycopg.Connection[Any], name: str) -> None:
    """删单个索引（`IF EXISTS`），用于 HNSW / IVFFlat 分开测。"""

    conn.execute(
        sql.SQL("DROP INDEX IF EXISTS {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(name))
    )


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def recall_at_k(predicted: Sequence[int], truth: Sequence[int]) -> float:
    if not truth:
        return 0.0
    return len(set(predicted) & set(truth)) / len(truth)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def latency_block(seconds: Iterable[float]) -> dict[str, float]:
    values = [s * 1000.0 for s in seconds]
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 3) if values else 0.0,
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


@dataclass(slots=True)
class SweepResult:
    label: str
    ef_search: int | None
    probes: int | None
    k: int
    queries: int
    recall_mean: float
    recall_min: float
    latency: dict[str, float]
    index_used: str | None = None
    server_exec_ms: float | None = None
    returned_mean: float = 0.0
    returned_min: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ef_search": self.ef_search,
            "probes": self.probes,
            "k": self.k,
            "queries": self.queries,
            "recall_mean": round(self.recall_mean, 4),
            "recall_min": round(self.recall_min, 4),
            "latency": self.latency,
            "index_used": self.index_used,
            "server_exec_ms": self.server_exec_ms,
            "returned_mean": self.returned_mean,
            "returned_min": self.returned_min,
        }


def explain_probe(
    conn: psycopg.Connection[Any],
    query: Sequence[float],
    *,
    k: int,
    ef_search: int | None = None,
    probes: int | None = None,
    workspace_id: str | None = None,
) -> tuple[str | None, float | None]:
    """对一条查询跑 `EXPLAIN (ANALYZE, FORMAT JSON)`：取实际命中的索引名与服务端执行毫秒。

    这是「指标是否可信」的自证：若返回的索引名与预期不符，说明规划器走了顺序扫描，
    当组 recall 不能当作 ANN 指标使用。
    """

    with conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")
        if probes is not None:
            conn.execute(sql.SQL("SET LOCAL ivfflat.probes = {}").format(sql.Literal(probes)))
        elif ef_search is not None:
            conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(ef_search)))
        if workspace_id is None:
            stmt = sql.SQL(
                "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM {} "
                "ORDER BY emb <=> %s::vector LIMIT %s"
            ).format(_table_ref())
            params: list[Any] = [vector_literal(query), k]
        else:
            stmt = sql.SQL(
                "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM {} WHERE workspace_id = %s "
                "ORDER BY emb <=> %s::vector LIMIT %s"
            ).format(_table_ref())
            params = [workspace_id, vector_literal(query), k]
        row = conn.execute(stmt, params).fetchone()
    doc = row["QUERY PLAN"] if row else None
    if isinstance(doc, str):
        doc = json.loads(doc)
    if not doc:
        return None, None
    node = doc[0]
    exec_ms = float(node.get("Execution Time", 0.0))
    index_name: str | None = None

    def walk(plan: dict[str, Any]) -> None:
        nonlocal index_name
        if index_name is None and plan.get("Index Name"):
            index_name = str(plan["Index Name"])
        for child in plan.get("Plans", []) or []:
            walk(child)

    walk(node.get("Plan", {}))
    return index_name, round(exec_ms, 3)


def sweep(
    conn: psycopg.Connection[Any],
    queries: Sequence[Sequence[float]],
    *,
    k: int,
    label: str,
    ef_search: int | None = None,
    probes: int | None = None,
    workspace_id: str | None = None,
) -> SweepResult:
    """对一组查询测 recall@k 与延迟（每个查询单独事务，`SET LOCAL` 才生效）。"""

    recalls: list[float] = []
    latencies: list[float] = []
    returned: list[int] = []
    for vec in queries:
        with conn.transaction():
            truth = exact_ids(conn, vec, k=k, workspace_id=workspace_id)
        started = time.perf_counter()
        with conn.transaction():
            if probes is not None:
                predicted = ivfflat_ids(conn, vec, k=k, probes=probes)
            else:
                predicted = ann_ids(
                    conn, vec, k=k, ef_search=ef_search or 64, workspace_id=workspace_id
                )
        latencies.append(time.perf_counter() - started)
        returned.append(len(predicted))
        recalls.append(recall_at_k(predicted, truth))
    index_used: str | None = None
    server_exec_ms: float | None = None
    if queries:
        index_used, server_exec_ms = explain_probe(
            conn,
            queries[0],
            k=k,
            ef_search=ef_search,
            probes=probes,
            workspace_id=workspace_id,
        )
    return SweepResult(
        label=label,
        ef_search=ef_search,
        probes=probes,
        k=k,
        queries=len(queries),
        recall_mean=statistics.fmean(recalls) if recalls else 0.0,
        recall_min=min(recalls) if recalls else 0.0,
        latency=latency_block(latencies),
        index_used=index_used,
        server_exec_ms=server_exec_ms,
        returned_mean=round(statistics.fmean(returned), 2) if returned else 0.0,
        returned_min=min(returned) if returned else 0,
    )


def sweep_exact(
    conn: psycopg.Connection[Any],
    queries: Sequence[Sequence[float]],
    *,
    k: int,
    workspace_id: str | None = None,
) -> SweepResult:
    latencies: list[float] = []
    for vec in queries:
        started = time.perf_counter()
        with conn.transaction():
            exact_ids(conn, vec, k=k, workspace_id=workspace_id)
        latencies.append(time.perf_counter() - started)
    return SweepResult(
        label="exact_seqscan",
        ef_search=None,
        probes=None,
        k=k,
        queries=len(queries),
        recall_mean=1.0,
        recall_min=1.0,
        latency=latency_block(latencies),
    )


def index_sizes(conn: psycopg.Connection[Any], *, index_name: str | None = None) -> dict[str, Any]:
    """索引/表占用字节数；`index_name` 指定时只取该索引（HNSW、IVFFlat 分开测）。"""

    sql_text = (
        "SELECT indexname, "
        "pg_relation_size(quote_ident(schemaname)||'.'||quote_ident(indexname)) AS bytes "
        "FROM pg_indexes WHERE schemaname = %s AND tablename = %s"
    )
    params: list[Any] = [SCHEMA, TABLE]
    if index_name is not None:
        sql_text += " AND indexname = %s"
        params.append(index_name)
    sql_text += " ORDER BY indexname"
    rows = conn.execute(sql_text, params).fetchall()
    table_bytes = conn.execute(
        sql.SQL("SELECT pg_relation_size({}) AS bytes").format(sql.Literal(f"{SCHEMA}.{TABLE}"))
    ).fetchone()
    return {
        "indexes": [{"name": row["indexname"], "bytes": int(row["bytes"])} for row in rows],
        "table_bytes": int(table_bytes["bytes"]) if table_bytes else 0,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_evidence(
    *,
    rows: int,
    dim: int,
    queries: int,
    k: int,
    dsn: str,
    load_seconds: float,
    hnsw_build_seconds: float,
    ivfflat_build_seconds: float,
    plans: dict[str, list[str]],
    sweeps: Sequence[SweepResult],
    filtered: Sequence[SweepResult],
    exact_baseline: SweepResult,
    sizes: dict[str, Any],
    total_seconds: float,
) -> dict[str, Any]:
    return {
        "task": "P0-T25",
        "wave": "W13",
        "adr": "docs/adr/0010_Pgvector_Core_Profile.md",
        "purpose": "ADR-0010 步骤 3：ANN 机制补跑（索引近似误差与延迟），不代表语义质量",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dsn": redact_dsn(dsn),
        "method": {
            "dataset": (
                f"合成聚簇向量：{CLUSTERS} 个随机单位中心 + "
                f"噪声标准差 {JITTER}/sqrt(dim) 的高斯抖动"
                "（按维度归一，使噪声模长≈JITTER，簇结构得以保留），L2 归一化，确定性 seed"
                "（聚簇保证近邻结构可测；均匀随机向量在 128 维近乎两两正交，recall 无意义）"
            ),
            "seed": SEED,
            "rows": rows,
            "dim": dim,
            "queries": queries,
            "k": k,
            "distance": "cosine（pgvector `<=>`）",
            "ground_truth": "精确全扫描（SET LOCAL enable_indexscan=off, enable_bitmapscan=off）",
            "recall_definition": "|ANN@k ∩ exact@k| / k，对 queries 取均值与最小值",
            "hnsw": {
                "m": HNSW_M,
                "ef_construction": HNSW_EF_CONSTRUCTION,
                "ef_search_sweep": list(EF_SEARCH_SWEEP),
            },
            "ivfflat": {"lists": IVFFLAT_LISTS, "probes_sweep": list(IVFFLAT_PROBES_SWEEP)},
            "workspaces": list(WORKSPACES),
            "clusters": CLUSTERS,
            "jitter": JITTER,
            "filtered_variant": (
                "WHERE workspace_id = %s（先过滤后排序的 SQL 变体，检验过滤与 ANN 共存）"
            ),
            "index_isolation": (
                "HNSW 与 IVFFlat 分开建、分开测、测完即删，避免规划器选错索引污染指标"
            ),
            "measure_filters": (
                "ANN 测量时 `SET LOCAL enable_seqscan = off` 强制走索引；"
                "不强制时本机 2 千行规模规划器会选「顺序扫描+排序」，recall 恒为 1.0（等于没测）"
            ),
        },
        "timings": {
            "load_seconds": round(load_seconds, 3),
            "hnsw_build_seconds": round(hnsw_build_seconds, 3),
            "ivfflat_build_seconds": round(ivfflat_build_seconds, 3),
            "total_seconds": round(total_seconds, 3),
        },
        "sizes": sizes,
        "plans_uses_index": {name: list(lines) for name, lines in plans.items()},
        "results": [s.as_dict() for s in sweeps],
        "results_filtered": [s.as_dict() for s in filtered],
        "results_exact": exact_baseline.as_dict(),
        "limitations": [
            "向量是合成聚簇向量，不是真实 embedding："
            "recall 只反映索引近似误差，不反映语义召回质量（DG-4 不解除）",
            f"pgvector `vector` 索引维度上限 {PGVECTOR_INDEX_MAX_DIM}；"
            f"T25 的假向量是 4096 维，无法直接建索引，故基准用 {dim} 维",
            "单机单实例、无并发写入、无 WAL 归档影响：延迟仅代表本机容器，不得当 SLA",
            "数据集规模 2 万条量级，不代表生产规模下的 recall/latency 曲线",
            "过滤变体未启用 hnsw.iterative_scan："
            "过滤率高时 ANN 可能欠召回（实测返回 5/10），报告需写明",
        ],
        "conclusion": {
            "ann_available": any(s.index_used is not None for s in sweeps),
            "indexes_actually_used": sorted({s.index_used for s in sweeps if s.index_used}),
            "engine": "pgvector 0.8.7（HNSW + IVFFlat）",
            "semantic_quality": "NOT_RUN",
        },
    }


def write_evidence(payload: dict[str, Any], path: Path = EVIDENCE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run(
    *,
    rows: int = DEFAULT_ROWS,
    dim: int = DEFAULT_DIM,
    queries: int = DEFAULT_QUERIES,
    k: int = DEFAULT_K,
    keep: bool = False,
    write_json: bool = True,
) -> dict[str, Any]:
    """跑一次完整基准；返回证据 dict（测试直接调用，不依赖 CLI）。"""

    if dim > PGVECTOR_INDEX_MAX_DIM:
        raise ValueError(
            f"dim={dim} 超过 pgvector 索引上限 {PGVECTOR_INDEX_MAX_DIM}；"
            "真实高维方案（halfvec/降维）需 ADR，不在本探针内自行决定"
        )
    started = time.perf_counter()
    centers = make_centers(dim=dim)
    vectors = synth_vectors(rows, dim=dim, centers=centers)
    # 查询必须落在数据的同一批簇里，否则「最近邻」本身就是随机的（recall 无意义）
    query_vectors = synth_vectors(queries, dim=dim, seed=SEED + 1, centers=centers)

    with connect() as conn:
        conn.autocommit = True
        drop_schema(conn)
        ensure_schema(conn, dim=dim)
        load_seconds = load_rows(conn, vectors)
        _analyze(conn)

        # --- HNSW：单独建、单独测、测完即删（避免规划器误选 IVFFlat 索引污染指标）
        hnsw_seconds = create_hnsw(conn, dim=dim)
        _analyze(conn)
        hnsw_plan = query_plan(conn, query_vectors[0], ef_search=EF_SEARCH_SWEEP[1])
        hnsw_size = index_sizes(conn, index_name=f"{TABLE}_hnsw")

        sweeps: list[SweepResult] = []
        for ef in EF_SEARCH_SWEEP:
            sweeps.append(sweep(conn, query_vectors, k=k, label=f"hnsw_ef_{ef}", ef_search=ef))

        filtered: list[SweepResult] = []
        for ef in (EF_SEARCH_SWEEP[1], EF_SEARCH_SWEEP[-1]):
            filtered.append(
                sweep(
                    conn,
                    query_vectors,
                    k=k,
                    label=f"hnsw_ef_{ef}_filtered_ws_a",
                    ef_search=ef,
                    workspace_id=WORKSPACES[0],
                )
            )
        drop_index(conn, f"{TABLE}_hnsw")

        # --- IVFFlat：同样单独建/测/删
        ivfflat_seconds = create_ivfflat(conn)
        _analyze(conn)
        ivfflat_plan = query_plan_ivfflat(conn, query_vectors[0], probes=IVFFLAT_PROBES_SWEEP[-1])
        ivfflat_size = index_sizes(conn, index_name=f"{TABLE}_ivfflat")

        for probes in IVFFLAT_PROBES_SWEEP:
            sweeps.append(
                sweep(conn, query_vectors, k=k, label=f"ivfflat_probes_{probes}", probes=probes)
            )
        drop_index(conn, f"{TABLE}_ivfflat")

        # --- 无索引精确基线（顺序扫，作为 ground truth 与对照延迟）
        table_size = index_sizes(conn)
        exact_baseline = sweep_exact(conn, query_vectors, k=k)

        total_seconds = time.perf_counter() - started
        payload = build_evidence(
            rows=rows,
            dim=dim,
            queries=queries,
            k=k,
            dsn=scope.read_dsn(),
            load_seconds=load_seconds,
            hnsw_build_seconds=hnsw_seconds,
            ivfflat_build_seconds=ivfflat_seconds,
            plans={"hnsw": hnsw_plan, "ivfflat": ivfflat_plan},
            sweeps=sweeps,
            filtered=filtered,
            exact_baseline=exact_baseline,
            sizes={"hnsw": hnsw_size, "ivfflat": ivfflat_size, "table": table_size},
            total_seconds=total_seconds,
        )
        conn.autocommit = False
        if not keep:
            drop_schema(conn)
    if write_json:
        write_evidence(payload)
    return payload


def _sweep_line(item: dict[str, Any], width: int = 26) -> str:
    """把一次 sweep 压成单行（拆成多个短 f-string，避免 E501）。"""

    latency = item["latency"]
    head = f"{item['label']:{width}s}"
    recall = f"recall_mean={item['recall_mean']:.4f} recall_min={item['recall_min']:.4f}"
    tail = (
        f"p50={latency['p50_ms']}ms p95={latency['p95_ms']}ms "
        f"server={item['server_exec_ms']}ms index={item['index_used']} "
        f"returned={item['returned_mean']}/{item['k']}"
    )
    return f"{head} {recall} {tail}"


def format_summary(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    method = payload["method"]
    lines.append(
        f"dataset: rows={method['rows']} dim={method['dim']} "
        f"queries={method['queries']} k={method['k']}"
    )
    timings = payload["timings"]
    lines.append(
        f"build: load={timings['load_seconds']}s "
        f"hnsw={timings['hnsw_build_seconds']}s "
        f"ivfflat={timings['ivfflat_build_seconds']}s "
        f"total={timings['total_seconds']}s"
    )
    for item in payload["results"]:
        lines.append(_sweep_line(item))
    for item in payload["results_filtered"]:
        lines.append(_sweep_line(item))
    exact = payload["results_exact"]
    lines.append(
        f"{exact['label']:22s} "
        f"p50={exact['latency']['p50_ms']}ms p95={exact['latency']['p95_ms']}ms"
    )
    for name, plan_lines in payload["plans_uses_index"].items():
        lines.append(f"plan[{name}]: " + " | ".join(plan_lines[:2]))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-T25 ANN 基准（pgvector HNSW/IVFFlat）")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--queries", type=int, default=DEFAULT_QUERIES)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--keep", action="store_true", help="跑完保留 p0_t25_ann schema")
    parser.add_argument("--no-write", action="store_true", help="不写 ann-bench.json")
    args = parser.parse_args(argv)

    payload = run(
        rows=args.rows,
        dim=args.dim,
        queries=args.queries,
        k=args.k,
        keep=args.keep,
        write_json=not args.no_write,
    )
    print(format_summary(payload))
    print(f"evidence: {EVIDENCE_PATH if not args.no_write else '(not written)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
