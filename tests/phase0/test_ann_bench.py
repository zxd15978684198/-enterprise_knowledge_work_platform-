"""P0-T25 ANN 基准（`ann_bench.py`）的回归测试。

只测**契约与自证能力**，不在 CI 里跑 2 万行基准（那要 30+ 秒）。具体断言：

1. schema 用完即删（不残留 `p0_t25_ann`）；
2. 指标可信的前置条件：`index_used` 必须真的是 hnsw/ivfflat 索引名，
   否则 recall 只是顺序扫描的 1.0（假通过）；
3. HNSW 在小规模上也应给出接近精确的召回；
4. post-filter 变体必须暴露「返回行数不足 k」这一现象（pgvector 已知语义）；
5. 维度超过 pgvector 索引上限时**快速失败**而不是建出坏索引。
"""

from __future__ import annotations

import pytest

import ann_bench


def test_ann_bench_small_run_is_self_evidencing() -> None:
    payload = ann_bench.run(
        rows=600, dim=32, queries=8, k=5, keep=False, write_json=False
    )

    assert payload["task"] == "P0-T25"
    assert payload["method"]["rows"] == 600
    assert payload["method"]["dim"] == 32
    assert payload["conclusion"]["semantic_quality"] == "NOT_RUN"

    by_label = {item["label"]: item for item in payload["results"]}
    hnsw = by_label["hnsw_ef_128"]
    # 指标必须自证：索引真的被用上，否则 recall 恒为 1 是假象
    assert hnsw["index_used"] == "phase0_ann_chunks_hnsw"
    assert hnsw["returned_mean"] == 5.0
    assert hnsw["recall_mean"] >= 0.9

    ivfflat = by_label["ivfflat_probes_32"]
    assert ivfflat["index_used"] == "phase0_ann_chunks_ivfflat"

    # post-filter：过滤后返回行数会少于 k（pgvector 0.8 未开 iterative_scan 的已知语义）
    filtered = payload["results_filtered"]
    assert filtered, "过滤变体必须被测量，否则无法暴露欠召回"
    assert all(item["returned_mean"] <= 5.0 for item in filtered)

    assert payload["results_exact"]["recall_mean"] == 1.0
    assert payload["conclusion"]["ann_available"] is True


def test_ann_bench_schema_is_dropped_after_run() -> None:
    ann_bench.run(rows=120, dim=16, queries=3, k=3, keep=False, write_json=False)

    with ann_bench.connect() as conn:
        conn.autocommit = True
        row = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (ann_bench.SCHEMA,)
        ).fetchone()
    assert row is None, "跑完必须删掉 p0_t25_ann schema，避免残留污染后续波次"


def test_ann_bench_rejects_dimension_above_index_limit() -> None:
    with pytest.raises(ValueError, match="超过 pgvector 索引上限"):
        ann_bench.run(rows=10, dim=ann_bench.PGVECTOR_INDEX_MAX_DIM + 1, queries=1)


def test_synth_vectors_are_deterministic_and_unit_norm() -> None:
    left = ann_bench.synth_vectors(8, dim=16)
    right = ann_bench.synth_vectors(8, dim=16)
    assert left == right

    norm = sum(x * x for x in left[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_recall_at_k_and_latency_block() -> None:
    assert ann_bench.recall_at_k([1, 2, 3], [3, 2, 1]) == 1.0
    assert ann_bench.recall_at_k([1, 2], [3, 4]) == 0.0
    assert ann_bench.recall_at_k([], []) == 0.0

    block = ann_bench.latency_block([0.010, 0.020, 0.030])
    assert block["n"] == 3
    assert block["p50_ms"] >= 20.0
    assert block["max_ms"] == 30.0
