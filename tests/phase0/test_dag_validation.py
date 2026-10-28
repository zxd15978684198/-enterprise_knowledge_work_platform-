"""P0-T22 Task DAG 契约校验的真实测试（禁 skip/xfail）。

覆盖 Backlog 量化验收：

* 合法/非法计划都在**调度前**判定：非法计划 100%（本文件 ≥20 个参数化非法样例）
  被拒绝，并给出可读的第一个错误与节点/边定位；
* 合法九节点研究 DAG（TDD 03 §4）拓扑可复现；
* Phase 0 真实规划样例（docs/poc/04：32 节点 / 72 边 / 66 点）通过放宽 profile，
  且关键链与文档声明一致（23 点，加 T32 为 24 点）；
* 与 Mermaid 边集、Backlog「依赖」字段逐项对账，差异只报告不删依赖；
* 普通 RAG 路径 Planner 调用为 0。

本测试只依赖标准库与被测模块，不需要 DB/网络/模型；docs/poc/03、04 只读解析。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dag_contracts import (
    DEFAULT_LIMITS,
    PHASE0_SAMPLE_LIMITS,
    ContractViolation,
    ErrorCode,
    GraphLimits,
    TaskGraph,
    chain_weight,
    dumps,
    longest_path,
    parse_plan,
    path_edges,
    payload_from_graph,
    topological_order,
    validate_graph,
)
from planner_probe import (
    BACKLOG_DOC,
    DAG_DOC,
    NINE_NODE_KINDS,
    NINE_NODE_PLAN,
    ProbePlanner,
    build_report,
    edge_diff,
    load_backlog_dependencies,
    load_mermaid_edges,
    load_phase0_graph,
    nine_node_graph,
    parse_dependency_table,
    planning_required,
)

#: Planner 替身的 kind 登记表：TDD 九节点 kind + 通用 task。
TEST_KINDS = NINE_NODE_KINDS | frozenset({"task"})

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE0_GRAPH = load_phase0_graph()
PHASE0_SAMPLE = validate_graph(PHASE0_GRAPH, PHASE0_SAMPLE_LIMITS)

#: docs/poc/04 §4 声明的两条并列关键链（23 点，11 节点）。
PARSE_LINE = (
    "P0-T01",
    "P0-T02",
    "P0-T04",
    "P0-T10",
    "P0-T11",
    "P0-T12",
    "P0-T13",
    "P0-T25",
    "P0-T26",
    "P0-T29",
    "P0-T31",
)
JOB_LINE = (
    "P0-T01",
    "P0-T02",
    "P0-T03",
    "P0-T07",
    "P0-T08",
    "P0-T12",
    "P0-T13",
    "P0-T25",
    "P0-T26",
    "P0-T29",
    "P0-T31",
)


def _node(task_id: str, depends_on: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_id": task_id, "depends_on": list(depends_on)}
    payload.update(extra)
    return payload


def _plan(nodes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_graph_revision": 1, "nodes": nodes}
    payload.update(extra)
    return payload


def _chain(length: int, prefix: str = "N") -> list[dict[str, Any]]:
    return [
        _node(f"{prefix}{index}", (() if index == 1 else (f"{prefix}{index - 1}",)))
        for index in range(1, length + 1)
    ]


def _invalid_cases() -> tuple[tuple[str, dict[str, Any], ErrorCode], ...]:
    """≥20 个确定性非法计划：环、自依赖、悬空、重复、越界、越权字段。"""
    cases: list[tuple[str, dict[str, Any], ErrorCode]] = []
    cases.append(("cycle-2", _plan([_node("A", ("B",)), _node("B", ("A",))]), ErrorCode.CYCLE))
    cases.append(
        (
            "cycle-3",
            _plan([_node("A", ("C",)), _node("B", ("A",)), _node("C", ("B",))]),
            ErrorCode.CYCLE,
        )
    )
    cases.append(
        (
            "cycle-4",
            _plan(
                [
                    _node("A", ("D",)),
                    _node("B", ("A",)),
                    _node("C", ("B",)),
                    _node("D", ("C",)),
                ]
            ),
            ErrorCode.CYCLE,
        )
    )
    cases.append(
        (
            "cycle-5",
            _plan(
                [
                    _node("A", ("E",)),
                    _node("B", ("A",)),
                    _node("C", ("B",)),
                    _node("D", ("C",)),
                    _node("E", ("D",)),
                ]
            ),
            ErrorCode.CYCLE,
        )
    )
    cases.append(("self-loop", _plan([_node("A", ("A",))]), ErrorCode.SELF_DEPENDENCY))
    cases.append(
        ("self-loop-with-parent", _plan([_node("A"), _node("B", ("A", "B"))]), ErrorCode.SELF_DEPENDENCY),
    )
    cases.append(("dangling", _plan([_node("A", ("GHOST",))]), ErrorCode.UNKNOWN_NODE_REF))
    cases.append(
        ("dangling-second", _plan([_node("A"), _node("B", ("A", "GHOST"))]), ErrorCode.UNKNOWN_NODE_REF)
    )
    cases.append(
        ("duplicate-id", _plan([_node("A"), _node("A")]), ErrorCode.DUPLICATE_NODE_ID),
    )
    cases.append(
        (
            "duplicate-id-with-deps",
            _plan([_node("A"), _node("B"), _node("A", ("B",))]),
            ErrorCode.DUPLICATE_NODE_ID,
        )
    )
    cases.append(
        ("duplicate-edge", _plan([_node("A"), _node("B", ("A", "A"))]), ErrorCode.DUPLICATE_EDGE)
    )
    cases.append(("id-with-space", _plan([_node("bad id")]), ErrorCode.INVALID_NODE_ID))
    cases.append(("id-with-arrow", _plan([_node("A->B")]), ErrorCode.INVALID_NODE_ID))
    cases.append(("id-empty", _plan([_node("")]), ErrorCode.INVALID_NODE_ID))
    cases.append(
        (
            "missing-task-id",
            _plan([{"kind": "task", "depends_on": []}]),
            ErrorCode.MISSING_FIELD,
        )
    )
    cases.append(
        (
            "depends-on-string",
            _plan([{"task_id": "A", "depends_on": "B"}]),
            ErrorCode.INVALID_TYPE,
        )
    )
    cases.append(
        (
            "depends-on-int",
            _plan([{"task_id": "A", "depends_on": [1]}]),
            ErrorCode.INVALID_TYPE,
        )
    )
    cases.append(("weight-zero", _plan([_node("A", weight=0)]), ErrorCode.INVALID_WEIGHT))
    cases.append(("weight-bool", _plan([_node("A", weight=True)]), ErrorCode.INVALID_WEIGHT))
    cases.append(
        ("unknown-kind", _plan([_node("A", kind="exploit")]), ErrorCode.INVALID_KIND)
    )
    cases.append(("empty-kind", _plan([_node("A", kind=" ")]), ErrorCode.INVALID_KIND))
    cases.append(
        (
            "forbidden-top-level-workspace",
            _plan([_node("A")], workspace_id="w-other"),
            ErrorCode.FORBIDDEN_FIELD,
        )
    )
    cases.append(
        (
            "forbidden-node-permissions",
            _plan([_node("A", permissions=["*"])]),
            ErrorCode.FORBIDDEN_FIELD,
        )
    )
    cases.append(("empty-graph", _plan([]), ErrorCode.EMPTY_GRAPH))
    cases.append(("nodes-missing", {"task_graph_revision": 1}, ErrorCode.MISSING_FIELD))
    cases.append(
        ("node-limit", _plan([_node(f"N{index}") for index in range(1, 34)]), ErrorCode.NODE_LIMIT_EXCEEDED)
    )
    cases.append(("depth-limit", _plan(_chain(9)), ErrorCode.DEPTH_EXCEEDED))
    complete = [_node(f"N{index}", tuple(f"N{other}" for other in range(1, index))) for index in range(1, 13)]
    cases.append(("edge-limit", _plan(complete), ErrorCode.EDGE_LIMIT_EXCEEDED))
    return tuple(cases)


INVALID_CASES = _invalid_cases()


# --------------------------------------------------------------------------- #
# 1. Phase 0 真实样例：32 节点 / 72 边 / 66 点
# --------------------------------------------------------------------------- #
def test_dependency_table_has_32_nodes_72_edges_66_points() -> None:
    rows = parse_dependency_table(DAG_DOC.read_text(encoding="utf-8"))
    assert len(rows) == 32, f"docs/poc/04 依赖表行数应为 32，实际 {len(rows)}"
    assert sum(len(dependencies) for _, dependencies, _ in rows) == 72
    assert sum(points for _, _, points in rows) == 66


def test_phase0_graph_passes_sample_limits() -> None:
    assert PHASE0_SAMPLE.ok, PHASE0_SAMPLE.errors
    stats = PHASE0_SAMPLE.stats
    assert stats is not None
    assert stats.node_count == 32
    assert stats.edge_count == 72
    assert stats.total_weight == 66
    assert PHASE0_GRAPH.node_ids == tuple(f"P0-T{index:02d}" for index in range(1, 33))
    assert len(stats.topological_order) == 32
    assert set(stats.topological_order) == set(PHASE0_GRAPH.node_ids)


def test_phase0_graph_degrees_roots_leaves() -> None:
    stats = PHASE0_SAMPLE.stats
    assert stats is not None
    assert stats.roots == ("P0-T01",), "唯一根节点应为 T01"
    assert stats.leaves == ("P0-T32",), "唯一叶节点应为 T32"
    assert stats.in_degree["P0-T05"] == 2, "T05 依赖 T03/T04"
    assert stats.out_degree["P0-T01"] == 3, "T01 直连 T02/T09/T14"
    assert stats.in_degree["P0-T31"] == 3, "T31 依赖 T26/T29/T30"
    assert stats.out_degree["P0-T32"] == 0


def test_phase0_depth_and_key_chain_match_doc_declaration() -> None:
    stats = PHASE0_SAMPLE.stats
    assert stats is not None
    # docs/poc/04 §4：P0 核心最长链 11 节点 / 23 点（到 T31）；全量链加 T32 = 12 节点 / 24 点。
    assert stats.depth == 12, "含 T32 的最长链为 12 个节点"
    assert stats.depth_edges == 11
    assert stats.depth_weight == 24, "全量最长链应为 24 点（23 + T32）"
    chain = longest_path(PHASE0_GRAPH)
    assert len(chain) == 12
    assert chain[-1] == "P0-T32"
    assert chain_weight(PHASE0_GRAPH, chain) == 24
    core = chain[:-1]
    assert len(core) == 11
    assert chain_weight(PHASE0_GRAPH, core) == 23, "去掉 T32 后应与文档声明的 23 点一致"
    assert core in (PARSE_LINE, JOB_LINE), f"最长链核心段应为文档声明的并列关键链：{core}"
    assert stats.longest_path == chain


def test_declared_critical_chains_exist_and_score_23() -> None:
    edges = set(PHASE0_GRAPH.edges)
    for name, chain in (("解析线", PARSE_LINE), ("可靠作业线", JOB_LINE)):
        assert all(edge in edges for edge in path_edges(chain)), f"{name} 不连通：{chain}"
        assert chain_weight(PHASE0_GRAPH, chain) == 23, f"{name} 应为 23 点"
        full = (*chain, "P0-T32")
        assert (chain[-1], "P0-T32") in edges, f"{name} 应能接上 T32"
        assert chain_weight(PHASE0_GRAPH, full) == 24, "全量最长链应为 24 点"


def test_default_limits_reject_phase0_sample_and_name_the_gap() -> None:
    result = validate_graph(PHASE0_GRAPH, DEFAULT_LIMITS)
    assert not result.ok
    assert ErrorCode.EDGE_LIMIT_EXCEEDED in result.codes
    assert ErrorCode.DEPTH_EXCEEDED in result.codes
    text = " | ".join(str(error) for error in result.errors)
    assert "72" in text and "64" in text, "边数超限须同时报出实际值与上限"
    assert "12" in text and "8" in text, "深度超限须同时报出实际值与上限"
    assert result.stats is not None, "规模超限也必须输出统计，便于定位"


# --------------------------------------------------------------------------- #
# 2. 跨文档对账：Mermaid 边集 / Backlog 依赖字段（差异只报告）
# --------------------------------------------------------------------------- #
def test_mermaid_edges_match_table_and_report_any_gap() -> None:
    mermaid = load_mermaid_edges()
    assert len(mermaid) == 72, f"Mermaid 边数应为 72，实际 {len(mermaid)}"
    assert ("P0-T01", "P0-T02") in mermaid, "历史缺失边 P0-T01 -> P0-T02 已闭合，需回归守护"
    diff = edge_diff(PHASE0_GRAPH.edges, mermaid)
    assert diff == {"only_left": [], "only_right": []}, (
        "docs/poc/04 的表与 Mermaid 不一致；按 Backlog 纪律只报告、不删依赖：" f"{diff}"
    )


def test_backlog_dependency_fields_match_authority_table() -> None:
    backlog = load_backlog_dependencies()
    assert len(backlog) == 32
    table = PHASE0_GRAPH.dependency_map()
    mismatch = {
        task_id: (sorted(table[task_id]), sorted(backlog.get(task_id, ())))
        for task_id in table
        if set(table[task_id]) != set(backlog.get(task_id, ()))
    }
    assert not mismatch, f"Backlog 依赖字段与 docs/poc/04 权威表不一致：{mismatch}"


def test_edge_diff_reports_both_directions() -> None:
    diff = edge_diff((("A", "B"), ("B", "C")), (("B", "C"), ("C", "D")))
    assert diff == {"only_left": [["A", "B"]], "only_right": [["C", "D"]]}


# --------------------------------------------------------------------------- #
# 3. 九节点合法计划与 Planner 契约
# --------------------------------------------------------------------------- #
def test_nine_node_plan_is_legal_and_depth_within_default_limit() -> None:
    graph = nine_node_graph()
    result = validate_graph(graph, DEFAULT_LIMITS)
    assert result.ok, result.errors
    stats = result.stats
    assert stats is not None
    assert stats.node_count == 9
    assert stats.edge_count == 9
    assert stats.depth == 7, "D→C1/C2→X1/X2→E→H→I→R 的节点深度为 7"
    assert stats.depth_edges == 6
    assert stats.depth <= DEFAULT_LIMITS.max_depth


def test_nine_node_topological_order_is_reproducible() -> None:
    expected = ("D", "C1", "C2", "X1", "X2", "E", "H", "I", "R")
    orders = {topological_order(nine_node_graph()) for _ in range(3)}
    assert orders == {expected}, f"同一计划的拓扑序必须可复现，实际 {orders}"


def test_planner_output_roundtrips_through_validator() -> None:
    planner = ProbePlanner()
    graph = planner.plan(_plan(list(NINE_NODE_PLAN)))
    assert planner.calls == 1 and planner.rejected == 0
    payload = payload_from_graph(graph)
    reparsed = parse_plan(payload, allowed_kinds=None)
    assert reparsed.node_ids == graph.node_ids
    assert set(reparsed.edges) == set(graph.edges)
    assert topological_order(reparsed) == topological_order(graph)
    assert validate_graph(reparsed, DEFAULT_LIMITS).ok


def test_planner_rejects_invalid_plan_and_reports_first_error_location() -> None:
    planner = ProbePlanner(allowed_kinds=TEST_KINDS)
    payload = _plan([_node("A", ("C",)), _node("B", ("A",)), _node("C", ("B",))])
    with pytest.raises(ContractViolation) as excinfo:
        planner.plan(payload)
    error = excinfo.value.first_error
    assert error.code is ErrorCode.CYCLE
    assert error.node_id == "A"
    assert error.edge == ("C", "A"), "环错误应定位到最后一条回边 C -> A"
    assert "检测到环" in str(error)
    assert planner.calls == 1 and planner.rejected == 1


def test_rag_path_never_invokes_planner() -> None:
    planner = ProbePlanner()
    for intent in ("rag", "rag_qa", "simple_retrieval", "direct_answer"):
        assert planning_required(intent) is False
        assert planner.plan_if_needed(intent, _plan(list(NINE_NODE_PLAN))) is None
    assert planner.calls == 0, "普通 RAG 路径 Planner 调用必须为 0"
    graph = planner.plan_if_needed("research", _plan(list(NINE_NODE_PLAN)))
    assert graph is not None
    assert planner.calls == 1


# --------------------------------------------------------------------------- #
# 4. 拒绝用例：≥20 个确定性非法计划
# --------------------------------------------------------------------------- #
def test_invalid_case_catalog_is_at_least_twenty() -> None:
    assert len(INVALID_CASES) >= 20, "Backlog 要求 ≥20 个非法环/自依赖/缺节点/越界输入"


@pytest.mark.parametrize(
    ("case_id", "payload", "expected"),
    INVALID_CASES,
    ids=[case[0] for case in INVALID_CASES],
)
def test_invalid_plan_rejected_before_scheduling(
    case_id: str, payload: dict[str, Any], expected: ErrorCode
) -> None:
    planner = ProbePlanner(allowed_kinds=TEST_KINDS)
    with pytest.raises(ContractViolation) as excinfo:
        planner.plan(payload)
    assert excinfo.value.first_error.code is expected, f"{case_id}: {excinfo.value.errors}"
    assert planner.calls == 1, "非法计划必须在调度前拒绝，不得进入执行"
    assert planner.rejected == 1
    message = str(excinfo.value)
    assert message, f"{case_id} 的错误消息不能为空"
    assert expected.value in message


def test_cycle_error_reports_closed_path() -> None:
    result = validate_graph(parse_plan(_plan([_node("A", ("B",)), _node("B", ("A",))])))
    assert not result.ok
    error = result.first_error
    assert error is not None and error.code is ErrorCode.CYCLE
    assert error.message.endswith("A -> B -> A") or "A -> B -> A" in error.message


def test_depth_error_names_offending_chain() -> None:
    result = validate_graph(parse_plan(_plan(_chain(9))))
    assert not result.ok
    error = result.first_error
    assert error is not None and error.code is ErrorCode.DEPTH_EXCEEDED
    assert "N1" in error.message and "N9" in error.message
    assert "9" in error.message and "8" in error.message
    assert result.stats is not None and result.stats.depth == 9


def test_node_and_edge_limits_report_counts() -> None:
    nodes = [_node(f"N{index}") for index in range(1, 34)]
    node_result = validate_graph(parse_plan(_plan(nodes)), GraphLimits(max_nodes=32, max_edges=64, max_depth=8))
    assert ErrorCode.NODE_LIMIT_EXCEEDED in node_result.codes
    assert "33" in str(node_result.first_error)

    complete = [
        _node(f"N{index}", tuple(f"N{other}" for other in range(1, index))) for index in range(1, 13)
    ]
    edge_result = validate_graph(parse_plan(_plan(complete)))
    assert ErrorCode.EDGE_LIMIT_EXCEEDED in edge_result.codes
    assert "66" in str(edge_result.first_error)


def test_dangling_and_self_dependency_errors_point_to_edges() -> None:
    dangling = validate_graph(parse_plan(_plan([_node("A"), _node("B", ("GHOST",))])))
    assert dangling.first_error is not None
    assert dangling.first_error.code is ErrorCode.UNKNOWN_NODE_REF
    assert dangling.first_error.edge == ("GHOST", "B")

    self_loop = validate_graph(parse_plan(_plan([_node("A", ("A",))])))
    assert self_loop.first_error is not None
    assert self_loop.first_error.code is ErrorCode.SELF_DEPENDENCY
    assert self_loop.first_error.edge == ("A", "A")


def test_empty_graph_is_rejected_for_both_entrypoints() -> None:
    assert validate_graph(TaskGraph()).first_error is not None
    assert validate_graph(TaskGraph()).first_error.code is ErrorCode.EMPTY_GRAPH
    with pytest.raises(ContractViolation) as excinfo:
        parse_plan(_plan([]))
    assert excinfo.value.first_error.code is ErrorCode.EMPTY_GRAPH


def test_error_code_serialization_is_stable() -> None:
    result = validate_graph(parse_plan(_plan([_node("A", ("B",)), _node("B", ("A",))])))
    payload = json.loads(dumps(result))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "CYCLE"
    assert payload["errors"][0]["node_id"] == "A"
    assert payload["errors"][0]["edge"] == ["B", "A"]


def test_phase0_report_holds_all_invariants() -> None:
    report = build_report()
    assert report["phase0_graph"]["nodes"] == 32
    assert report["phase0_graph"]["edges"] == 72
    assert report["sample_limits"]["ok"] is True
    assert report["sample_limits"]["depth_nodes"] == 12
    assert report["sample_limits"]["depth_weight"] == 24
    assert report["critical_paths"]["parse_line"]["weight"] == 23
    assert report["critical_paths"]["job_line"]["weight"] == 23
    assert report["critical_paths"]["full_line_with_T32"]["weight"] == 24
    assert report["mermaid_diff"]["only_left"] == []
    assert report["mermaid_diff"]["only_right"] == []
    assert report["backlog_diff"]["only_left"] == []
    assert report["backlog_diff"]["only_right"] == []
    assert report["nine_node"]["ok"] is True
    assert report["rag_path"]["planner_calls"] == 0
    assert report["default_limits"]["ok"] is False
    assert set(report["default_limits"]["codes"]) == {"EDGE_LIMIT_EXCEEDED", "DEPTH_EXCEEDED"}


def test_docs_are_only_read_not_modified() -> None:
    """契约测试只读文档：断言两份输入确实在仓库内且可解析（防路径漂移）。"""
    assert DAG_DOC.is_file() and BACKLOG_DOC.is_file()
    assert DAG_DOC.parent == REPO_ROOT / "docs" / "poc"
    assert NINE_NODE_PLAN  # 固定样例不能被清空
