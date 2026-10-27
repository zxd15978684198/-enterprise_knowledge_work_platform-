"""P0-T22 Task DAG 契约：把结构化计划变成「可验证 DAG」。

范围边界（Backlog P0-T22）：

* 只做 **schema 解析 + 图契约校验**：节点/边完整性、重复 id、自依赖、悬空依赖、
  无环、节点/边/深度上限、入度/出度统计、最长链（关键路径）识别。
* **不做**依赖调度、ready/claim/lease、执行与结果对账（T23），也**不做**评价与
  重规划 revision（T24）。本模块不写任何 Task/Run 状态，不访问 DB/网络/模型。

不可信输入：

* 模型产出的计划**只是草案**。``parse_plan`` 先做结构与权限字段检查，
  ``validate_graph`` 再做图契约检查；任一步失败都在「调度前」拒绝，
  返回第一个错误并定位到具体节点/边。
* 模型不能授予权限或指定任意资源：节点/顶层出现 ``workspace_id``、
  ``permissions``、``grants``、``token`` 等字段直接判 ``FORBIDDEN_FIELD``。
  真实授权只来自服务端已登记的 Principal/Scope（T03）与 policy refs。

错误模型：

* ``GraphError.code`` 是稳定错误码，供 T23/T24 做 API/状态映射；
* ``message`` 必须可读且指出节点或边；
* ``ContractViolation.errors`` 保留全部结构错误，``first_error`` 按确定顺序给出
  「第一个错误」，便于上层只落一条拒绝原因。

深度口径：``depth`` = 最长路径上的**节点数**（单节点图 depth=1，边数 = depth-1）。
Backlog 给 Planner 产出的规模上限是「32 节点 / 64 边 / 深度 8」，而 Phase 0 自己的
规划图是 32 节点 / 72 边 / 11 节点链（点数值不同，见 docs/poc/04 §4），因此上限
必须可配置：默认值 ``GraphLimits()`` 用于 Planner 产出，``PHASE0_SAMPLE_LIMITS``
只用于校验 Phase 0 文档图本身。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

__all__ = [
    "ContractViolation",
    "DEFAULT_LIMITS",
    "ErrorCode",
    "GraphError",
    "GraphLimits",
    "GraphStats",
    "PHASE0_SAMPLE_LIMITS",
    "TaskGraph",
    "TaskNode",
    "ValidationResult",
    "chain_weight",
    "deepest_chain",
    "dumps",
    "find_cycle",
    "in_degree",
    "longest_path",
    "out_degree",
    "parse_plan",
    "path_edges",
    "payload_from_graph",
    "topological_order",
    "validate_graph",
]

#: 节点 id 允许的字符集：不允许空白/箭头，避免把图结构编码进 id。
NODE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

#: 模型不得写入的授权字段（顶层与节点级都检查）。
FORBIDDEN_AUTHORITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "workspace_id",
        "principal_id",
        "permissions",
        "grants",
        "grant",
        "authorization",
        "auth_token",
        "api_key",
        "token",
        "secret",
    }
)

#: 计划节点允许的 id 键（按优先级取第一个存在的）。
_NODE_ID_KEYS: Final[tuple[str, ...]] = ("task_id", "node_id", "id")


class ErrorCode(str, Enum):
    """稳定错误码；值即 API 可映射的字符串。"""

    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_NODE_ID = "INVALID_NODE_ID"
    DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
    INVALID_WEIGHT = "INVALID_WEIGHT"
    INVALID_KIND = "INVALID_KIND"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    UNKNOWN_NODE_REF = "UNKNOWN_NODE_REF"
    SELF_DEPENDENCY = "SELF_DEPENDENCY"
    DUPLICATE_EDGE = "DUPLICATE_EDGE"
    EMPTY_GRAPH = "EMPTY_GRAPH"
    CYCLE = "CYCLE"
    NODE_LIMIT_EXCEEDED = "NODE_LIMIT_EXCEEDED"
    EDGE_LIMIT_EXCEEDED = "EDGE_LIMIT_EXCEEDED"
    DEPTH_EXCEEDED = "DEPTH_EXCEEDED"


@dataclass(frozen=True)
class GraphError:
    """一条可定位的契约错误。"""

    code: ErrorCode
    message: str
    node_id: str | None = None
    edge: tuple[str, str] | None = None

    def __str__(self) -> str:
        where = []
        if self.node_id is not None:
            where.append(f"node={self.node_id}")
        if self.edge is not None:
            where.append(f"edge={self.edge[0]}->{self.edge[1]}")
        suffix = f" [{', '.join(where)}]" if where else ""
        return f"{self.code.value}: {self.message}{suffix}"


class ContractViolation(Exception):
    """计划不合法：携带确定顺序的错误列表，``first`` 即「第一个错误」。"""

    def __init__(self, errors: Iterable[GraphError]) -> None:
        self.errors: tuple[GraphError, ...] = tuple(errors)
        if not self.errors:
            raise ValueError("ContractViolation 至少需要一条错误")
        super().__init__(str(self.errors[0]))

    @property
    def first_error(self) -> GraphError:
        return self.errors[0]

    @property
    def codes(self) -> tuple[ErrorCode, ...]:
        return tuple(error.code for error in self.errors)

    def __str__(self) -> str:
        return str(self.first_error)


@dataclass(frozen=True)
class GraphLimits:
    """规模上限；默认值来自 Backlog P0-T22「32 节点 / 64 边 / 深度 8」。"""

    max_nodes: int = 32
    max_edges: int = 64
    max_depth: int = 8

    def __post_init__(self) -> None:
        for name in ("max_nodes", "max_edges", "max_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数，实际 {value!r}")


DEFAULT_LIMITS: Final = GraphLimits()

#: Phase 0 文档图本身（docs/poc/04 §4）：32 节点、72 条直接边、最长链 11 节点。
PHASE0_SAMPLE_LIMITS: Final = GraphLimits(max_nodes=32, max_edges=72, max_depth=16)


@dataclass(frozen=True)
class TaskNode:
    """计划中的一个任务节点；``depends_on`` 是本节点依赖的前置节点 id。"""

    node_id: str
    depends_on: tuple[str, ...] = ()
    kind: str = "task"
    weight: int = 1
    required: bool = True
    title: str = ""


@dataclass(frozen=True)
class TaskGraph:
    """任务图：节点按声明顺序保存，边由 ``depends_on`` 导出。"""

    nodes: tuple[TaskNode, ...] = ()
    task_graph_revision: int = 1
    run_id: str | None = None

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes)

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        """按声明顺序展开的边；方向为 ``前置 -> 后继``。"""
        return tuple(
            (dependency, node.node_id)
            for node in self.nodes
            for dependency in node.depends_on
        )

    def node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def dependency_map(self) -> dict[str, tuple[str, ...]]:
        return {node.node_id: tuple(node.depends_on) for node in self.nodes}

    def successors(self) -> dict[str, tuple[str, ...]]:
        """邻接表：前置 -> 直接后继（按节点声明顺序）。"""
        table: dict[str, list[str]] = {node.node_id: [] for node in self.nodes}
        for source, target in self.edges:
            table[source].append(target)
        return {node_id: tuple(targets) for node_id, targets in table.items()}

    def weight_map(self) -> dict[str, int]:
        return {node.node_id: node.weight for node in self.nodes}


@dataclass(frozen=True)
class GraphStats:
    """校验通过后的事实快照；也用于文档对账（节点/边/深度/关键路径）。"""

    node_count: int
    edge_count: int
    total_weight: int
    roots: tuple[str, ...]
    leaves: tuple[str, ...]
    in_degree: dict[str, int]
    out_degree: dict[str, int]
    topological_order: tuple[str, ...]
    longest_path: tuple[str, ...]
    depth: int
    depth_weight: int

    @property
    def depth_edges(self) -> int:
        return max(self.depth - 1, 0)


@dataclass(frozen=True)
class ValidationResult:
    """校验结果：``ok`` 为真时 ``stats`` 必定存在。"""

    ok: bool
    errors: tuple[GraphError, ...] = ()
    stats: GraphStats | None = None

    @property
    def first_error(self) -> GraphError | None:
        return self.errors[0] if self.errors else None

    @property
    def codes(self) -> tuple[ErrorCode, ...]:
        return tuple(error.code for error in self.errors)

    def raise_if_invalid(self) -> GraphStats:
        if not self.ok or self.stats is None:
            fallback = GraphError(ErrorCode.INVALID_PAYLOAD, "计划无效")
            raise ContractViolation(self.errors or (fallback,))
        return self.stats


def parse_plan(
    payload: Mapping[str, Any],
    *,
    allowed_kinds: frozenset[str] | None = None,
    forbidden_keys: frozenset[str] = FORBIDDEN_AUTHORITY_KEYS,
) -> TaskGraph:
    """把 Planner 的 JSON 草案解析成 :class:`TaskGraph`。

    结构错误立即抛 :class:`ContractViolation`（含第一个错误的位置）；语义/拓扑错误
    由 :func:`validate_graph` 判定。``allowed_kinds`` 为 ``None`` 时不限制 kind，
    但 kind 必须是非空字符串；给了集合则未登记的 kind 直接 ``INVALID_KIND``。
    """
    errors: list[GraphError] = []
    if not isinstance(payload, Mapping):
        actual = type(payload).__name__
        raise ContractViolation(
            (GraphError(ErrorCode.INVALID_PAYLOAD, f"计划必须是对象，实际 {actual}"),)
        )

    for key in sorted(forbidden_keys & set(payload)):
        errors.append(
            GraphError(
                ErrorCode.FORBIDDEN_FIELD,
                f"顶层不得包含授权字段 {key!r}：权限只能来自服务端登记的 Principal/Scope",
            )
        )

    raw_nodes = payload.get("nodes")
    if raw_nodes is None:
        errors.append(GraphError(ErrorCode.MISSING_FIELD, "计划缺少 nodes 字段"))
        raise ContractViolation(errors)
    if isinstance(raw_nodes, str | bytes) or not isinstance(raw_nodes, Sequence):
        errors.append(
            GraphError(
                ErrorCode.INVALID_TYPE, f"nodes 必须是数组，实际 {type(raw_nodes).__name__}"
            )
        )
        raise ContractViolation(errors)
    if not raw_nodes:
        raise ContractViolation((GraphError(ErrorCode.EMPTY_GRAPH, "计划 nodes 为空数组"),))

    nodes: list[TaskNode] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_TYPE,
                    f"nodes[{index}] 必须是对象，实际 {type(raw).__name__}",
                )
            )
            continue
        for key in sorted(forbidden_keys & set(raw)):
            errors.append(
                GraphError(
                    ErrorCode.FORBIDDEN_FIELD,
                    f"节点不得包含授权字段 {key!r}（模型不能授予权限）",
                    edge=None,
                )
            )
        node_id = _first_present(raw, _NODE_ID_KEYS)
        if node_id is None:
            errors.append(
                GraphError(
                    ErrorCode.MISSING_FIELD,
                    f"nodes[{index}] 缺少 id 字段（{'/'.join(_NODE_ID_KEYS)}）",
                )
            )
            continue
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(
                GraphError(
                    ErrorCode.INVALID_NODE_ID,
                    f"nodes[{index}] 的 id 必须是非空字符串，实际 {node_id!r}",
                )
            )
            continue
        if not NODE_ID_PATTERN.fullmatch(node_id):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_NODE_ID,
                    f"节点 id {node_id!r} 含非法字符（允许字母开头，后接字母/数字/_.:-）",
                    node_id=str(node_id),
                )
            )
            continue

        raw_dependencies = raw.get("depends_on", [])
        if raw_dependencies is None:
            raw_dependencies = []
        if isinstance(raw_dependencies, str | bytes) or not isinstance(
            raw_dependencies, Sequence
        ):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_TYPE,
                    f"节点 {node_id} 的 depends_on 必须是数组",
                    node_id=node_id,
                )
            )
            continue
        dependencies: list[str] = []
        bad_dependency = False
        for dependency in raw_dependencies:
            if not isinstance(dependency, str) or not dependency.strip():
                errors.append(
                    GraphError(
                        ErrorCode.INVALID_TYPE,
                        f"节点 {node_id} 的 depends_on 含非字符串/空值 {dependency!r}",
                        node_id=node_id,
                    )
                )
                bad_dependency = True
                break
            dependencies.append(dependency)

        kind = raw.get("kind", "task")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(
                GraphError(
                    ErrorCode.INVALID_KIND,
                    f"节点 {node_id} 的 kind 必须是非空字符串，实际 {kind!r}",
                    node_id=node_id,
                )
            )
            continue
        if allowed_kinds is not None and kind not in allowed_kinds:
            errors.append(
                GraphError(
                    ErrorCode.INVALID_KIND,
                    f"节点 {node_id} 的 kind {kind!r} 未登记；允许：{sorted(allowed_kinds)}",
                    node_id=node_id,
                )
            )
            continue

        weight = raw.get("weight", raw.get("complexity", 1))
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            errors.append(
                GraphError(
                    ErrorCode.INVALID_WEIGHT,
                    f"节点 {node_id} 的 weight 必须是正整数，实际 {weight!r}",
                    node_id=node_id,
                )
            )
            continue

        required = raw.get("required", True)
        if not isinstance(required, bool):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_TYPE,
                    f"节点 {node_id} 的 required 必须是布尔值，实际 {required!r}",
                    node_id=node_id,
                )
            )
            continue

        title = raw.get("title", "")
        if not isinstance(title, str):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_TYPE,
                    f"节点 {node_id} 的 title 必须是字符串",
                    node_id=node_id,
                )
            )
            continue

        if not bad_dependency:
            nodes.append(
                TaskNode(
                    node_id=node_id,
                    depends_on=tuple(dependencies),
                    kind=kind,
                    weight=weight,
                    required=required,
                    title=title,
                )
            )

    if errors:
        raise ContractViolation(errors)

    revision = payload.get("task_graph_revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ContractViolation(
            (
                GraphError(
                    ErrorCode.INVALID_TYPE,
                    f"task_graph_revision 必须是正整数，实际 {revision!r}",
                ),
            )
        )
    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ContractViolation(
            (GraphError(ErrorCode.INVALID_TYPE, "run_id 必须是字符串或省略"),)
        )
    return TaskGraph(nodes=tuple(nodes), task_graph_revision=revision, run_id=run_id)


def payload_from_graph(graph: TaskGraph) -> dict[str, Any]:
    """把图序列化回 Planner 草案格式（供 round-trip 与 T23 登记使用）。"""
    payload: dict[str, Any] = {
        "task_graph_revision": graph.task_graph_revision,
        "nodes": [
            {
                "task_id": node.node_id,
                "kind": node.kind,
                "depends_on": list(node.depends_on),
                "weight": node.weight,
                "required": node.required,
                "title": node.title,
            }
            for node in graph.nodes
        ],
    }
    if graph.run_id is not None:
        payload["run_id"] = graph.run_id
    return payload


def validate_graph(graph: TaskGraph, limits: GraphLimits | None = None) -> ValidationResult:
    """按固定顺序校验图契约，返回全部错误与（无结构错误时的）统计。"""
    active_limits = limits or DEFAULT_LIMITS
    errors: list[GraphError] = []

    if not graph.nodes:
        return ValidationResult(
            False, (GraphError(ErrorCode.EMPTY_GRAPH, "图为空：至少需要一个节点"),), None
        )

    first_index: dict[str, int] = {}
    for index, node in enumerate(graph.nodes):
        node_id = node.node_id
        if not node_id or node_id != node_id.strip():
            errors.append(
                GraphError(
                    ErrorCode.INVALID_NODE_ID,
                    f"nodes[{index}] 的 id 为空或首尾含空白：{node_id!r}",
                )
            )
            continue
        if not NODE_ID_PATTERN.fullmatch(node_id):
            errors.append(
                GraphError(
                    ErrorCode.INVALID_NODE_ID,
                    f"节点 id {node_id!r} 含非法字符",
                    node_id=node_id,
                )
            )
            continue
        if node_id in first_index:
            errors.append(
                GraphError(
                    ErrorCode.DUPLICATE_NODE_ID,
                    f"节点 id {node_id!r} 重复（首次出现在 nodes[{first_index[node_id]}]）",
                    node_id=node_id,
                )
            )
            continue
        first_index[node_id] = index
        if not node.kind or not node.kind.strip():
            errors.append(
                GraphError(
                    ErrorCode.INVALID_KIND, f"节点 {node_id} 的 kind 为空", node_id=node_id
                )
            )
        if isinstance(node.weight, bool) or not isinstance(node.weight, int) or node.weight <= 0:
            errors.append(
                GraphError(
                    ErrorCode.INVALID_WEIGHT,
                    f"节点 {node_id} 的 weight 必须是正整数，实际 {node.weight!r}",
                    node_id=node_id,
                )
            )

    known = set(first_index)
    seen_edges: set[tuple[str, str]] = set()
    for node in graph.nodes:
        if node.node_id not in known:
            continue
        for dependency in node.depends_on:
            if dependency == node.node_id:
                errors.append(
                    GraphError(
                        ErrorCode.SELF_DEPENDENCY,
                        f"节点 {node.node_id} 依赖自身（边 {dependency} -> {node.node_id}）",
                        node_id=node.node_id,
                        edge=(dependency, node.node_id),
                    )
                )
                continue
            if dependency not in known:
                errors.append(
                    GraphError(
                        ErrorCode.UNKNOWN_NODE_REF,
                        f"节点 {node.node_id} 依赖不存在的节点 {dependency}",
                        node_id=node.node_id,
                        edge=(dependency, node.node_id),
                    )
                )
                continue
            edge = (dependency, node.node_id)
            if edge in seen_edges:
                errors.append(
                    GraphError(
                        ErrorCode.DUPLICATE_EDGE,
                        f"重复依赖边 {dependency} -> {node.node_id}",
                        node_id=node.node_id,
                        edge=edge,
                    )
                )
                continue
            seen_edges.add(edge)

    if errors:
        return ValidationResult(False, tuple(errors), None)

    # 规模上限不阻止统计：需要把「边超限」与「深度超限」一次性报全。
    limit_errors: list[GraphError] = []
    if len(known) > active_limits.max_nodes:
        limit_errors.append(
            GraphError(
                ErrorCode.NODE_LIMIT_EXCEEDED,
                f"节点数 {len(known)} 超过上限 {active_limits.max_nodes}",
            )
        )
    if len(seen_edges) > active_limits.max_edges:
        limit_errors.append(
            GraphError(
                ErrorCode.EDGE_LIMIT_EXCEEDED,
                f"边数 {len(seen_edges)} 超过上限 {active_limits.max_edges}",
            )
        )

    cycle = find_cycle(graph)
    if cycle is not None:
        path = " -> ".join(cycle)
        cycle_error = GraphError(
            ErrorCode.CYCLE,
            f"检测到环：{path}",
            node_id=cycle[0],
            edge=(cycle[-2], cycle[-1]),
        )
        return ValidationResult(False, (*limit_errors, cycle_error), None)

    order = topological_order(graph)
    weight_chain = longest_path(graph)
    depth_chain = deepest_chain(graph)
    weight_map = graph.weight_map()
    source_ids = {source for source, _ in seen_edges}
    stats = GraphStats(
        node_count=len(known),
        edge_count=len(seen_edges),
        total_weight=sum(weight_map.values()),
        roots=tuple(node.node_id for node in graph.nodes if not node.depends_on),
        leaves=tuple(node.node_id for node in graph.nodes if node.node_id not in source_ids),
        in_degree=in_degree(graph),
        out_degree=out_degree(graph),
        topological_order=order,
        longest_path=weight_chain,
        depth=len(depth_chain),
        depth_weight=chain_weight(graph, weight_chain),
    )

    if stats.depth > active_limits.max_depth:
        path = " -> ".join(depth_chain)
        limit_errors.append(
            GraphError(
                ErrorCode.DEPTH_EXCEEDED,
                f"最长路径深度 {stats.depth} 超过上限 {active_limits.max_depth}：{path}",
                node_id=depth_chain[-1],
            )
        )
    return ValidationResult(not limit_errors, tuple(limit_errors), stats)


def in_degree(graph: TaskGraph) -> dict[str, int]:
    """每个节点的直接前置数（按节点声明顺序）。"""
    table = {node.node_id: 0 for node in graph.nodes}
    for node in graph.nodes:
        for dependency in node.depends_on:
            if dependency in table:
                table[node.node_id] += 1
    return table


def out_degree(graph: TaskGraph) -> dict[str, int]:
    """每个节点的直接后继数（按节点声明顺序）。"""
    table = {node.node_id: 0 for node in graph.nodes}
    for node in graph.nodes:
        for dependency in node.depends_on:
            if dependency in table:
                table[dependency] += 1
    return table


def path_edges(chain: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (source, target) for source, target in zip(chain, chain[1:], strict=False)
    )


def chain_weight(graph: TaskGraph, chain: Sequence[str]) -> int:
    """链上节点的 weight 之和；节点不存在直接 KeyError（调用方先校验）。"""
    weight_map = graph.weight_map()
    return sum(weight_map[node_id] for node_id in chain)


def topological_order(graph: TaskGraph) -> tuple[str, ...]:
    """确定性拓扑序（就绪集按节点 id 排序），解析同一计划必须得到同一顺序。"""
    dependency_map = graph.dependency_map()
    remaining = {node_id: len(deps) for node_id, deps in dependency_map.items()}
    ready = sorted(node_id for node_id, count in remaining.items() if count == 0)
    successors = graph.successors()
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in successors[current]:
            remaining[target] -= 1
            if remaining[target] == 0:
                ready.append(target)
        ready.sort()
    if len(order) != len(dependency_map):
        raise ContractViolation(
            (GraphError(ErrorCode.CYCLE, "拓扑排序未覆盖全部节点：图存在环"),)
        )
    return tuple(order)


def find_cycle(graph: TaskGraph) -> tuple[str, ...] | None:
    """DFS 找第一个环，返回形如 ``[A, B, C, A]`` 的闭合路径；无环返回 ``None``。"""
    successors = graph.successors()
    state: dict[str, int] = {}  # 0=未访问 1=在栈上 2=已完成
    for start in graph.node_ids:
        if state.get(start, 0) != 0:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = [start]
        state[start] = 1
        while stack:
            node_id, index = stack[-1]
            targets = successors[node_id]
            if index >= len(targets):
                stack.pop()
                path.pop()
                state[node_id] = 2
                continue
            target = targets[index]
            stack[-1] = (node_id, index + 1)
            if state.get(target, 0) == 1:
                closed = path[path.index(target) :]
                return (*closed, target)
            if state.get(target, 0) == 0:
                state[target] = 1
                stack.append((target, 0))
                path.append(target)
    return None


def longest_path(graph: TaskGraph) -> tuple[str, ...]:
    """按节点权重求最长链。

    同权重时取节点更多的链，再按链的字典序取最前者 —— 同一份计划在任何进程
    都必须得到同一条关键路径，否则表/图对账不可复现。
    """
    weight_map = graph.weight_map()
    dependency_map = graph.dependency_map()
    order = topological_order(graph)
    best: dict[str, tuple[int, tuple[str, ...]]] = {}
    for node_id in order:
        candidates = [best[dependency] for dependency in dependency_map[node_id]]
        if candidates:
            best_prefix = min(candidates, key=lambda item: (-item[0], -len(item[1]), item[1]))
        else:
            best_prefix = (0, ())
        best[node_id] = (best_prefix[0] + weight_map[node_id], (*best_prefix[1], node_id))
    return min(best.values(), key=lambda item: (-item[0], -len(item[1]), item[1]))[1]


def deepest_chain(graph: TaskGraph) -> tuple[str, ...]:
    """按节点数求最长链（深度口径）；与权重无关，用于 ``max_depth`` 上限。"""
    dependency_map = graph.dependency_map()
    order = topological_order(graph)
    best: dict[str, tuple[str, ...]] = {}
    for node_id in order:
        candidates = [best[dependency] for dependency in dependency_map[node_id]]
        prefix = min(candidates, key=lambda chain: (-len(chain), chain)) if candidates else ()
        best[node_id] = (*prefix, node_id)
    return min(best.values(), key=lambda chain: (-len(chain), chain))


def dumps(result: ValidationResult) -> str:
    """把校验结果序列化为 JSON（T23 落拒绝原因时可复用）。"""
    return json.dumps(
        {
            "ok": result.ok,
            "errors": [
                {
                    "code": error.code.value,
                    "message": error.message,
                    "node_id": error.node_id,
                    "edge": list(error.edge) if error.edge else None,
                }
                for error in result.errors
            ],
            "stats": None
            if result.stats is None
            else {
                "node_count": result.stats.node_count,
                "edge_count": result.stats.edge_count,
                "total_weight": result.stats.total_weight,
                "roots": list(result.stats.roots),
                "leaves": list(result.stats.leaves),
                "depth": result.stats.depth,
                "depth_weight": result.stats.depth_weight,
                "longest_path": list(result.stats.longest_path),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None
