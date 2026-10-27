"""P0-T22 Planner 探针：真实样例、跨文档对账与「不走 Planner」边界。

这不是业务 Planner 实现，而是 **契约探针**：

* 从只读文档 ``docs/poc/04_Phase0_Dependency_DAG.md`` 解析 Phase 0 规划图
  （32 节点 / 72 条直接边），从 ``docs/poc/03_Phase0_Task_Backlog.md`` 解析每个
  任务的「依赖」字段做第二来源交叉校验；
* 与文档里的 Mermaid 边集对比：**发现差异只报告，不删依赖**；
* 用 TDD 03 §4 的九节点研究 DAG 作为「合法计划」样例；
* 演示「普通 RAG 路径不调用 Planner」：``plan_if_needed`` 对 RAG 意图直接返回
  ``None`` 且不增加调用计数（Backlog 量化验收「普通 RAG 路径 Planner 调用 0」）；
* 不做调度/执行/重规划（T23/T24）。

CLI：``python experiments/phase0/planner_probe.py`` 输出 JSON 报告，全部不变量成立
时退出码 0，否则 1（便于波次门禁直接引用）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any, Final

from dag_contracts import (
    DEFAULT_LIMITS,
    PHASE0_SAMPLE_LIMITS,
    ContractViolation,
    GraphLimits,
    TaskGraph,
    TaskNode,
    chain_weight,
    parse_plan,
    path_edges,
    validate_graph,
)

__all__ = [
    "BACKLOG_DOC",
    "DAG_DOC",
    "NINE_NODE_KINDS",
    "NINE_NODE_PLAN",
    "NON_PLANNING_INTENTS",
    "ProbePlanner",
    "REPO_ROOT",
    "build_report",
    "doc_fingerprints",
    "load_backlog_dependencies",
    "load_mermaid_edges",
    "load_phase0_graph",
    "main",
    "nine_node_graph",
    "parse_dependency_table",
    "planning_required",
]

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DAG_DOC: Final = REPO_ROOT / "docs" / "poc" / "04_Phase0_Dependency_DAG.md"
BACKLOG_DOC: Final = REPO_ROOT / "docs" / "poc" / "03_Phase0_Task_Backlog.md"

#: 普通 RAG 问答/检索不应经过 Planner（TDD 03 §1：简单 RAG 不强制 Planner）。
NON_PLANNING_INTENTS: Final[frozenset[str]] = frozenset(
    {"rag", "rag_qa", "simple_retrieval", "direct_answer"}
)

#: TDD 03 §4 九节点研究 DAG 的 kind 登记表。
NINE_NODE_KINDS: Final = frozenset(
    {"discover", "collect", "extract", "evaluate", "review", "ingest", "report"}
)

#: TDD 03 §4 的九节点示例（标签仅作实验样例；真实持久化时替换为服务端 UUID）。
#: ``maximum_cost_microunits`` 属于预算契约（T20），本任务忽略未知字段、不校验成本。
NINE_NODE_PLAN: Final[tuple[dict[str, Any], ...]] = (
    {
        "task_id": "D",
        "kind": "discover",
        "depends_on": (),
        "title": "发现来源候选",
        "maximum_cost_microunits": 10,
    },
    {
        "task_id": "C1",
        "kind": "collect",
        "depends_on": ("D",),
        "title": "标准机构采集",
        "maximum_cost_microunits": 20,
    },
    {
        "task_id": "C2",
        "kind": "collect",
        "depends_on": ("D",),
        "title": "监管机构采集",
        "maximum_cost_microunits": 20,
    },
    {
        "task_id": "X1",
        "kind": "extract",
        "depends_on": ("C1",),
        "title": "标准快照抽取",
        "maximum_cost_microunits": 10,
    },
    {
        "task_id": "X2",
        "kind": "extract",
        "depends_on": ("C2",),
        "title": "监管快照抽取",
        "maximum_cost_microunits": 10,
    },
    {
        "task_id": "E",
        "kind": "evaluate",
        "depends_on": ("X1", "X2"),
        "title": "覆盖/冲突评价",
        "maximum_cost_microunits": 10,
    },
    {
        "task_id": "H",
        "kind": "review",
        "depends_on": ("E",),
        "title": "人工审阅",
        "maximum_cost_microunits": 0,
    },
    {
        "task_id": "I",
        "kind": "ingest",
        "depends_on": ("H",),
        "title": "统一入库",
        "maximum_cost_microunits": 15,
    },
    {
        "task_id": "R",
        "kind": "report",
        "depends_on": ("I",),
        "title": "最终报告",
        "maximum_cost_microunits": 5,
    },
)

_TABLE_ROW = re.compile(r"^\|\s*(P0-T\d+)\s*\|\s*([^|]*?)\s*\|\s*(\d+)\s*\|")
_BACKLOG_HEADING = re.compile(r"^##\s+(P0-T\d+)：")
_BACKLOG_DEPS = re.compile(r"^-\s*依赖：(.+?)。?\s*$")
_MERMAID_EDGE = re.compile(
    r"^\s*([A-Za-z]\w*)(?:\[[^\]]*\])?\s*-->\s*([A-Za-z]\w*)(?:\[[^\]]*\])?\s*$"
)
_SHORT_ID = re.compile(r"^T\d+$", re.IGNORECASE)


def _read(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"缺少只读文档：{path}")
    return path.read_text(encoding="utf-8")


@cache
def parse_dependency_table(text: str) -> tuple[tuple[str, tuple[str, ...], int], ...]:
    """解析 ``docs/poc/04`` §1 权威依赖表为 ``(task_id, 前置, 复杂度点)``。"""
    rows: list[tuple[str, tuple[str, ...], int]] = []
    for line in text.splitlines():
        match = _TABLE_ROW.match(line)
        if match is None:
            continue
        task_id, raw_dependencies, points = match.groups()
        rows.append((task_id, _split_dependencies(raw_dependencies), int(points)))
    return tuple(rows)


@cache
def parse_mermaid_edges(text: str) -> frozenset[tuple[str, str]]:
    """解析文档中第一个 Mermaid 块的边，短标签 ``Txx`` 归一为 ``P0-Txx``。"""
    edges: set[tuple[str, str]] = set()
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_block:
                break
            in_block = stripped == "```mermaid"
            continue
        if not in_block:
            continue
        match = _MERMAID_EDGE.match(line)
        if match is None:
            continue
        source, target = match.groups()
        edges.add((_normalize_task_id(source), _normalize_task_id(target)))
    return frozenset(edges)


@cache
def parse_backlog_dependencies(text: str) -> dict[str, tuple[str, ...]]:
    """解析 Backlog 每张任务卡的「依赖：」字段（第二来源）。"""
    dependencies: dict[str, tuple[str, ...]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = _BACKLOG_HEADING.match(line)
        if heading is not None:
            current = heading.group(1)
            continue
        if current is None:
            continue
        match = _BACKLOG_DEPS.match(line)
        if match is None:
            continue
        dependencies[current] = _split_dependencies(match.group(1))
        current = None
    return dependencies


@cache
def load_phase0_graph() -> TaskGraph:
    """由只读依赖表构造 Phase 0 规划图（权重 = 复杂度点）。"""
    rows = parse_dependency_table(_read(DAG_DOC))
    nodes = tuple(
        TaskNode(node_id=task_id, depends_on=dependencies, weight=points, title=task_id)
        for task_id, dependencies, points in rows
    )
    return TaskGraph(nodes=nodes, task_graph_revision=1, run_id=None)


@cache
def load_mermaid_edges() -> frozenset[tuple[str, str]]:
    return parse_mermaid_edges(_read(DAG_DOC))


@cache
def load_backlog_dependencies() -> dict[str, tuple[str, ...]]:
    return parse_backlog_dependencies(_read(BACKLOG_DOC))


def nine_node_graph(allowed_kinds: frozenset[str] | None = NINE_NODE_KINDS) -> TaskGraph:
    """TDD 九节点研究 DAG 的合法样例；``allowed_kinds`` 默认为登记表。"""
    return parse_plan(
        {"run_id": "p0-t22-nine-node", "task_graph_revision": 1, "nodes": list(NINE_NODE_PLAN)},
        allowed_kinds=allowed_kinds,
    )


def planning_required(intent_kind: str) -> bool:
    """RAG/直答不需要 Planner；其余（研究、多步任务）才需要。"""
    return intent_kind.strip().lower() not in NON_PLANNING_INTENTS


class ProbePlanner:
    """最小 Planner 替身：只负责产出/拒绝草案与计数，不做任何调度。

    ``allowed_kinds=None`` 时不限制 kind（只要求非空字符串）；实验用
    ``NINE_NODE_KINDS`` 做 kind 登记表校验，未知 kind 直接拒绝。
    """

    def __init__(
        self,
        *,
        limits: GraphLimits = DEFAULT_LIMITS,
        allowed_kinds: frozenset[str] | None = None,
    ) -> None:
        self.limits = limits
        self.allowed_kinds = allowed_kinds
        self.calls = 0
        self.rejected = 0

    def plan(self, payload: Mapping[str, Any]) -> TaskGraph:
        """解析 + 校验；不合法抛 ``ContractViolation``（含第一个错误）。"""
        self.calls += 1
        try:
            graph = parse_plan(payload, allowed_kinds=self.allowed_kinds)
            validate_graph(graph, self.limits).raise_if_invalid()
        except ContractViolation:
            self.rejected += 1
            raise
        return graph

    def plan_if_needed(self, intent_kind: str, payload: Mapping[str, Any]) -> TaskGraph | None:
        """RAG 路径直接返回 ``None`` 且**不**增加调用计数。"""
        if not planning_required(intent_kind):
            return None
        return self.plan(payload)


def edge_diff(
    left: Sequence[tuple[str, str]], right: Sequence[tuple[str, str]]
) -> dict[str, list[list[str]]]:
    """两个边集的差集；只报告，绝不据此删改依赖。"""
    left_set = set(left)
    right_set = set(right)
    return {
        "only_left": sorted([list(edge) for edge in left_set - right_set]),
        "only_right": sorted([list(edge) for edge in right_set - left_set]),
    }


def doc_fingerprints() -> dict[str, str]:
    """两份只读输入的 sha256，供证据引用。"""
    return {
        DAG_DOC.name: hashlib.sha256(DAG_DOC.read_bytes()).hexdigest(),
        BACKLOG_DOC.name: hashlib.sha256(BACKLOG_DOC.read_bytes()).hexdigest(),
    }


def build_report() -> dict[str, Any]:
    """跑一遍全部不变量，返回可打印/可断言的结构化报告。"""
    graph = load_phase0_graph()
    table_edges = graph.edges
    mermaid_edges = load_mermaid_edges()
    backlog_dependencies = load_backlog_dependencies()
    backlog_edges = {
        (dependency, task_id)
        for task_id, dependencies in backlog_dependencies.items()
        for dependency in dependencies
    }

    sample_validation = validate_graph(graph, PHASE0_SAMPLE_LIMITS)
    default_validation = validate_graph(graph, DEFAULT_LIMITS)
    stats = sample_validation.stats

    parse_line = (
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
    job_line = (
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
    full_line = (*parse_line, "P0-T32")
    present_edges = set(graph.edges)

    def chain_present(chain: Sequence[str]) -> bool:
        return all(edge in present_edges for edge in path_edges(chain))

    nine_node = nine_node_graph()
    nine_validation = validate_graph(nine_node, DEFAULT_LIMITS)
    planner = ProbePlanner()
    planner.plan_if_needed("rag_qa", {"nodes": list(NINE_NODE_PLAN)})

    return {
        "task": "P0-T22",
        "doc_fingerprints": doc_fingerprints(),
        "phase0_graph": {
            "nodes": len(graph.nodes),
            "edges": len(table_edges),
            "total_weight": stats.total_weight if stats else None,
            "roots": list(stats.roots) if stats else [],
            "leaves": list(stats.leaves) if stats else [],
        },
        "default_limits": {
            "max_nodes": DEFAULT_LIMITS.max_nodes,
            "max_edges": DEFAULT_LIMITS.max_edges,
            "max_depth": DEFAULT_LIMITS.max_depth,
            "ok": default_validation.ok,
            "codes": [code.value for code in default_validation.codes],
        },
        "sample_limits": {
            "max_nodes": PHASE0_SAMPLE_LIMITS.max_nodes,
            "max_edges": PHASE0_SAMPLE_LIMITS.max_edges,
            "max_depth": PHASE0_SAMPLE_LIMITS.max_depth,
            "ok": sample_validation.ok,
            "depth_nodes": stats.depth if stats else None,
            "depth_edges": stats.depth_edges if stats else None,
            "depth_weight": stats.depth_weight if stats else None,
            "longest_path": list(stats.longest_path) if stats else [],
        },
        "critical_paths": {
            "parse_line": {
                "present": chain_present(parse_line),
                "weight": chain_weight(graph, parse_line),
            },
            "job_line": {
                "present": chain_present(job_line),
                "weight": chain_weight(graph, job_line),
            },
            "full_line_with_T32": {
                "present": chain_present(full_line),
                "weight": chain_weight(graph, full_line),
            },
        },
        "mermaid_diff": {
            "table_edges": len(table_edges),
            "mermaid_edges": len(mermaid_edges),
            **edge_diff(table_edges, mermaid_edges),
        },
        "backlog_diff": {
            "table_edges": len(table_edges),
            "backlog_edges": len(backlog_edges),
            **edge_diff(table_edges, backlog_edges),
        },
        "nine_node": {
            "nodes": len(nine_node.nodes),
            "edges": len(nine_node.edges),
            "ok": nine_validation.ok,
            "depth_nodes": nine_validation.stats.depth if nine_validation.stats else None,
            "topological_order": list(nine_validation.stats.topological_order)
            if nine_validation.stats
            else [],
        },
        "rag_path": {
            "intent_kind": "rag_qa",
            "planner_calls": planner.calls,
            "graph": None,
        },
    }


def _invariants_hold(report: Mapping[str, Any]) -> bool:
    graph_facts = report["phase0_graph"]
    return bool(
        graph_facts["nodes"] == 32
        and graph_facts["edges"] == 72
        and report["sample_limits"]["ok"]
        and report["mermaid_diff"]["only_left"] == []
        and report["mermaid_diff"]["only_right"] == []
        and report["backlog_diff"]["only_left"] == []
        and report["backlog_diff"]["only_right"] == []
        and report["nine_node"]["ok"]
        and report["rag_path"]["planner_calls"] == 0
    )


def main(argv: Sequence[str] | None = None) -> int:
    """打印报告；本任务不接参数（``argv`` 仅保留 CLI 兼容位）。"""
    del argv
    report = build_report()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write(payload + "\n")
    return 0 if _invariants_hold(report) else 1


def _split_dependencies(cell: str) -> tuple[str, ...]:
    cleaned = cell.strip()
    if not cleaned or cleaned == "无":
        return ()
    parts = re.split(r"[,、，]", cleaned)
    return tuple(part.strip() for part in parts if part.strip())


def _normalize_task_id(label: str) -> str:
    return f"P0-{label.upper()}" if _SHORT_ID.match(label) else label


if __name__ == "__main__":  # pragma: no cover - 手工探针入口
    raise SystemExit(main())
