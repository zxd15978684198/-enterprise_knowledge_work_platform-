"""P0-T20 上下文预算：区段切分、确定性裁剪、工具对原子性、摘要/丢弃留痕。

契约来源
--------
* ``docs/tdd/02_Agent_Harness.md`` §6：ContextManager 按 provider 窗口保守估算，
  **预留工具定义和最大输出**；压缩时把 assistant tool calls 与对应 tool result 作为
  **不可拆单元**，不能留下孤立 tool result；摘要保留来源 ref、摘要失败降级窗口裁剪并
  标记 degraded，不把摘要当执行恢复依据。
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T20：上下文裁剪不破坏工具对；量化验收
  「孤立 tool 消息 0」。
* ``docs/tdd/07_Data_API_Security.md`` §3：内容表默认私有；本模块只处理**已授权**的
  区段文本，不做跨会话记忆，也不把原文写日志。

实现要点
--------
1. **区段固定四段**：``system`` / ``tools`` / ``history`` / ``retrieval``，各自给出
   占用（tokens + 条数）。``system`` 是硬地板，永不裁剪；裁剪顺序由
   :data:`TRUNCATION_ORDER` 固定为 retrieval → history → tools。
2. **确定性**：所有排序都带稳定 tie-break（score/rank/source_ref、turn_index、name），
   不使用随机、时钟或哈希表迭代序；``PreparedContext.fingerprint`` 对同一输入恒定。
3. **工具对原子性**：history 按 ``turn_index`` 分组，assistant 的 tool call 与 tool
   result 必须同组（否则 ``CONTEXT_INTEGRITY``）；裁剪以「整轮」为单位，绝不拆对。
4. **先摘要后丢弃**：超过 ``summary_trigger_tokens`` 的 retrieval 片段先用确定性抽取式
   摘要压缩；仍然超预算才整段丢弃，并同时记录 ``SummaryRecord`` 与 ``DropRecord``
   （reason=``SUMMARIZED_THEN_DROPPED``，含 tokens 计数）。
5. **预留输出**：有效输入预算 = ``max_context_tokens - reserved_output_tokens``；
   工具 schema 的 token 占用计入 ``tools`` 段。

边界（本任务不做）
------------------
* 不使用 provider 的真实 tokenizer（无外网、无 SDK）：估算函数是**保守启发式**，测试
  与 T21 必须把 ``estimate_tokens`` 当契约，而不是与真实计费对齐。
* 摘要不是模型摘要：默认实现是确定性抽取式（首尾片段），失败降级为直接丢弃并标记
  ``degraded``；真实摘要器由 T21 注入（构造参数 ``summarizer``）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_SUMMARY_MAX_TOKENS",
    "DEFAULT_SUMMARY_TRIGGER_TOKENS",
    "MESSAGE_OVERHEAD_TOKENS",
    "SECTION_OVERHEAD_TOKENS",
    "TOOL_OVERHEAD_TOKENS",
    "TRUNCATION_ORDER",
    "ContextBudgetError",
    "ContextBudgetExceededError",
    "ContextBuilder",
    "ContextInput",
    "ContextIntegrityError",
    "ContextLimits",
    "DropReason",
    "DropRecord",
    "HistoryMessage",
    "PreparedContext",
    "RenderedItem",
    "RetrievalChunk",
    "Section",
    "SectionUsage",
    "SummaryRecord",
    "SystemBlock",
    "ToolSchema",
    "TruncationPolicy",
    "estimate_context_tokens",
    "estimate_tokens",
    "summarize_for_test",
]

Section = Literal["system", "tools", "history", "retrieval"]
DropReason = Literal[
    "SUMMARIZED_THEN_DROPPED",
    "LOW_SCORE",
    "OLDEST_TURN",
    "OPTIONAL_TOOL",
    "SUMMARY_FAILED",
]

SECTIONS: Final[tuple[Section, ...]] = ("system", "tools", "history", "retrieval")
#: 既定裁剪顺序：先砍检索（最可替代），再砍旧历史，最后才动可选工具；system 永不裁剪。
TRUNCATION_ORDER: Final[tuple[Section, ...]] = ("retrieval", "history", "tools")
SECTION_OVERHEAD_TOKENS: Final = 2
MESSAGE_OVERHEAD_TOKENS: Final = 4
TOOL_OVERHEAD_TOKENS: Final = 8
DEFAULT_SUMMARY_TRIGGER_TOKENS: Final = 160
DEFAULT_SUMMARY_MAX_TOKENS: Final = 96

_CJK_RE: Final = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ROLES: Final = ("system", "user", "assistant", "tool")


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
class ContextBudgetError(RuntimeError):
    code: str = "CONTEXT_BUDGET_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details

    def __str__(self) -> str:
        if self.details:
            return f"{self.code}: {super().__str__()} {self.details}"
        return f"{self.code}: {super().__str__()}"


class ContextBudgetExceededError(ContextBudgetError):
    code = "CONTEXT_BUDGET_EXCEEDED"


class ContextIntegrityError(ContextBudgetError):
    code = "CONTEXT_INTEGRITY"


# ---------------------------------------------------------------------------
# 估算（确定性启发式；不是真实 tokenizer）
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """保守估算：CJK 每字符 1 token，其它字符每 4 字符 1 token（向上取整）。

    这是**契约内**的确定性估算：同一文本在任何进程都得到同一数值。真实 provider
    tokenizer 的差异必须由 T21 在装配层处理，不能在本模块静默换算法。
    """

    if not isinstance(text, str):
        raise ContextBudgetError("estimate_tokens 只接受 str")
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 输入区段
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SystemBlock:
    """系统区段的一块文本（指令/约束）。``key`` 用于稳定排序与留痕。"""

    key: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not 1 <= len(self.key) <= 64:
            raise ContextBudgetError("SystemBlock.key 长度必须在 [1, 64]")
        if not isinstance(self.text, str):
            raise ContextBudgetError("SystemBlock.text 必须是 str")


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """工具声明：schema 占用计入 tools 段；``required=False`` 才允许被裁剪。"""

    name: str
    version: str
    parameters: Mapping[str, Any]
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 64:
            raise ContextBudgetError("ToolSchema.name 长度必须在 [1, 64]")
        if not isinstance(self.version, str) or not 1 <= len(self.version) <= 32:
            raise ContextBudgetError("ToolSchema.version 长度必须在 [1, 32]")
        if not isinstance(self.parameters, Mapping):
            raise ContextBudgetError("ToolSchema.parameters 必须是映射")

    def canonical(self) -> str:
        return _canonical_json(
            {"name": self.name, "version": self.version, "parameters": self.parameters}
        )


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """历史消息；``tool_call_id`` 把 assistant call 与 tool result 绑成不可拆单元。"""

    role: Literal["system", "user", "assistant", "tool"]
    text: str
    turn_index: int
    tool_call_id: str | None = None
    tool_name: str | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ContextBudgetError(f"HistoryMessage.role 非法: {self.role!r}")
        if not isinstance(self.text, str):
            raise ContextBudgetError("HistoryMessage.text 必须是 str")
        if isinstance(self.turn_index, bool) or not isinstance(self.turn_index, int):
            raise ContextBudgetError("HistoryMessage.turn_index 必须是 int")
        if self.turn_index < 0:
            raise ContextBudgetError("HistoryMessage.turn_index 不能为负")
        if self.tool_call_id is not None and (
            not isinstance(self.tool_call_id, str) or not 1 <= len(self.tool_call_id) <= 128
        ):
            raise ContextBudgetError("tool_call_id 长度必须在 [1, 128]")

    def item_id(self) -> str:
        return f"history/{self.turn_index}/{self.role}/{self.tool_call_id or '-'}"


@dataclass(frozen=True, slots=True)
class RetrievalChunk:
    """检索片段：``score`` 越高越重要；``rank`` 是检索器原始顺序（稳定 tie-break）。"""

    source_ref: str
    text: str
    score: int = 0
    rank: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not 1 <= len(self.source_ref) <= 200:
            raise ContextBudgetError("RetrievalChunk.source_ref 长度必须在 [1, 200]")
        if not isinstance(self.text, str):
            raise ContextBudgetError("RetrievalChunk.text 必须是 str")
        for name in ("score", "rank"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContextBudgetError(f"RetrievalChunk.{name} 必须是 int")


@dataclass(frozen=True, slots=True)
class ContextInput:
    system: tuple[SystemBlock, ...] = ()
    tools: tuple[ToolSchema, ...] = ()
    history: tuple[HistoryMessage, ...] = ()
    retrieval: tuple[RetrievalChunk, ...] = ()

    def __post_init__(self) -> None:
        for name in ("system", "tools", "history", "retrieval"):
            if not isinstance(getattr(self, name), tuple):
                raise ContextBudgetError(f"ContextInput.{name} 必须是 tuple")


# ---------------------------------------------------------------------------
# 限制 / 策略
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ContextLimits:
    max_context_tokens: int
    reserved_output_tokens: int = 0
    keep_last_turns: int = 1
    summary_trigger_tokens: int = DEFAULT_SUMMARY_TRIGGER_TOKENS
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS

    def __post_init__(self) -> None:
        for name in (
            "max_context_tokens",
            "reserved_output_tokens",
            "keep_last_turns",
            "summary_trigger_tokens",
            "summary_max_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContextBudgetError(f"ContextLimits.{name} 必须是非负 int")
        if self.max_context_tokens <= 0:
            raise ContextBudgetError("max_context_tokens 必须 > 0")
        if self.reserved_output_tokens >= self.max_context_tokens:
            raise ContextBudgetError("reserved_output_tokens 必须小于 max_context_tokens")
        if self.summary_max_tokens >= self.summary_trigger_tokens:
            raise ContextBudgetError("summary_max_tokens 必须小于 summary_trigger_tokens")

    @property
    def effective_input_tokens(self) -> int:
        return self.max_context_tokens - self.reserved_output_tokens


@dataclass(frozen=True, slots=True)
class TruncationPolicy:
    """裁剪顺序（system 不允许出现在 order 里）；默认 retrieval → history → tools。"""

    order: tuple[Section, ...] = TRUNCATION_ORDER

    def __post_init__(self) -> None:
        if not self.order:
            raise ContextBudgetError("裁剪顺序不能为空")
        if "system" in self.order:
            raise ContextBudgetError("system 是硬地板，不能出现在裁剪顺序里")
        if len(set(self.order)) != len(self.order):
            raise ContextBudgetError("裁剪顺序不能重复")
        unknown = [item for item in self.order if item not in TRUNCATION_ORDER]
        if unknown:
            raise ContextBudgetError(f"裁剪顺序含未知区段: {unknown}")


# ---------------------------------------------------------------------------
# 输出事实
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SectionUsage:
    section: Section
    tokens: int
    items: int
    dropped_items: int
    summary_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "tokens": self.tokens,
            "items": self.items,
            "dropped_items": self.dropped_items,
            "summary_tokens": self.summary_tokens,
        }


@dataclass(frozen=True, slots=True)
class DropRecord:
    section: Section
    item_id: str
    reason: DropReason
    tokens: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item_id": self.item_id,
            "reason": self.reason,
            "tokens": self.tokens,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    section: Section
    item_id: str
    source_ref: str
    original_tokens: int
    summary_tokens: int
    summary_ref: str
    method: str
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item_id": self.item_id,
            "source_ref": self.source_ref,
            "original_tokens": self.original_tokens,
            "summary_tokens": self.summary_tokens,
            "summary_ref": self.summary_ref,
            "method": self.method,
            "degraded": self.degraded,
        }


@dataclass(frozen=True, slots=True)
class RenderedItem:
    section: Section
    item_id: str
    text: str
    tokens: int
    summary_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item_id": self.item_id,
            "tokens": self.tokens,
            "summary_ref": self.summary_ref,
            "text_excerpt": self.text[:80],
        }


@dataclass(frozen=True, slots=True)
class PreparedContext:
    items: tuple[RenderedItem, ...]
    sections: tuple[SectionUsage, ...]
    total_tokens: int
    limit_tokens: int
    reserved_output_tokens: int
    dropped: tuple[DropRecord, ...]
    summaries: tuple[SummaryRecord, ...]
    degraded: bool
    fingerprint: str

    def section_usage(self, section: Section) -> SectionUsage:
        for usage in self.sections:
            if usage.section == section:
                return usage
        raise ContextBudgetError(f"未知区段: {section}")

    def item_ids(self, section: Section | None = None) -> tuple[str, ...]:
        return tuple(
            item.item_id for item in self.items if section is None or item.section == section
        )

    def dropped_count(self, section: Section | None = None) -> int:
        return sum(
            1 for record in self.dropped if section is None or record.section == section
        )

    def dropped_tokens(self) -> int:
        return sum(record.tokens for record in self.dropped)

    def orphan_tool_messages(self) -> int:
        """保留的 tool 消息里缺少同轮 assistant tool_call 的数量（必须为 0）。"""

        calls: set[str] = set()
        results: list[str] = []
        for item in self.items:
            if item.section != "history":
                continue
            parts = item.item_id.split("/")
            if len(parts) < 4:
                continue
            role, call_id = parts[2], parts[3]
            if role == "assistant" and call_id != "-":
                calls.add(call_id)
            elif role == "tool" and call_id != "-":
                results.append(call_id)
        return sum(1 for call_id in results if call_id not in calls)

    def as_prompt_parts(self) -> tuple[dict[str, str], ...]:
        """按 system → tools → history → retrieval 的顺序给出可拼装的文本块。"""

        order = {name: index for index, name in enumerate(SECTIONS)}
        ordered = sorted(self.items, key=lambda item: order[item.section])
        return tuple(
            {"section": item.section, "item_id": item.item_id, "text": item.text}
            for item in ordered
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "limit_tokens": self.limit_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "fingerprint": self.fingerprint,
            "degraded": self.degraded,
            "sections": [usage.as_dict() for usage in self.sections],
            "items": [item.as_dict() for item in self.items],
            "dropped": [record.as_dict() for record in self.dropped],
            "summaries": [record.as_dict() for record in self.summaries],
        }


def estimate_context_tokens(context_input: ContextInput) -> int:
    """整段估算（含区段/消息/工具开销），便于调用方在 prepare 前做粗判。"""

    if not isinstance(context_input, ContextInput):
        raise ContextBudgetError("estimate_context_tokens 需要 ContextInput")
    total = 0
    if context_input.system:
        total += SECTION_OVERHEAD_TOKENS
        total += sum(estimate_tokens(block.text) for block in context_input.system)
    if context_input.tools:
        total += SECTION_OVERHEAD_TOKENS
        total += sum(
            estimate_tokens(tool.canonical()) + TOOL_OVERHEAD_TOKENS
            for tool in context_input.tools
        )
    if context_input.history:
        total += SECTION_OVERHEAD_TOKENS
        total += sum(
            estimate_tokens(message.text) + MESSAGE_OVERHEAD_TOKENS
            for message in context_input.history
        )
    if context_input.retrieval:
        total += SECTION_OVERHEAD_TOKENS
        total += sum(estimate_tokens(chunk.text) for chunk in context_input.retrieval)
    return total


# ---------------------------------------------------------------------------
# 确定性抽取式摘要（默认实现；失败降级由 builder 处理）
# ---------------------------------------------------------------------------
def _extractive_summary(text: str, max_tokens: int) -> str:
    """取首尾片段并标注省略，保证同一输入同一输出；不调用模型。"""

    if max_tokens <= 0:
        return ""
    # 估算换算成字符预算：CJK 1 字符≈1 token，其它 4 字符≈1 token，取保守下界。
    budget_chars = max(16, max_tokens * 2)
    if len(text) <= budget_chars:
        return text
    head = text[: max(1, budget_chars // 2)]
    tail = text[-max(1, budget_chars // 2) :]
    return f"{head}…[summary omitted {len(text) - len(head) - len(tail)} chars]…{tail}"


def summarize_for_test(text: str, max_tokens: int) -> str:
    """把默认摘要器暴露给测试（不引入私有名字耦合）。"""

    return _extractive_summary(text, max_tokens)


# ---------------------------------------------------------------------------
# 构建器
# ---------------------------------------------------------------------------
class ContextBuilder:
    """把 :class:`ContextInput` 压成不超预算的 :class:`PreparedContext`。

    ``summarizer`` 可注入（T21 用真实摘要器）；抛异常时降级为直接丢弃并把
    ``degraded`` 置 True（TDD02 §6：摘要失败降级窗口裁剪并标记 degraded）。
    """

    __slots__ = ("_limits", "_policy", "_summarizer", "_summary_counter")

    def __init__(
        self,
        limits: ContextLimits,
        *,
        policy: TruncationPolicy | None = None,
        summarizer: Callable[[str, int], str] | None = None,
    ) -> None:
        if not isinstance(limits, ContextLimits):
            raise ContextBudgetError("ContextBuilder 需要 ContextLimits")
        self._limits = limits
        self._policy = policy or TruncationPolicy()
        self._summarizer = summarizer or _extractive_summary
        self._summary_counter = 0

    def prepare(self, context: ContextInput) -> PreparedContext:
        if not isinstance(context, ContextInput):
            raise ContextBudgetError("prepare 需要 ContextInput")
        self._summary_counter = 0
        self._validate_integrity(context)

        system_items = list(context.system)
        tool_items = list(context.tools)
        history_units = self._history_units(context.history)
        retrieval_items = sorted(
            context.retrieval, key=lambda chunk: (chunk.rank, chunk.source_ref, chunk.score)
        )
        summaries: list[SummaryRecord] = []
        dropped: list[DropRecord] = []
        degraded = False
        limit = self._limits.effective_input_tokens

        def current_total() -> int:
            return self._total_tokens(system_items, tool_items, history_units, retrieval_items)

        if current_total() > limit:
            for section in self._policy.order:
                if current_total() <= limit:
                    break
                if section == "retrieval":
                    degraded = self._trim_retrieval(
                        retrieval_items, current_total, limit, summaries, dropped
                    )
                elif section == "history":
                    self._trim_history(history_units, current_total, limit, dropped)
                elif section == "tools":
                    self._trim_tools(tool_items, current_total, limit, dropped)
                else:  # pragma: no cover - TruncationPolicy 已拒绝 system
                    raise ContextBudgetError(f"未知裁剪区段: {section}")

        total = current_total()
        if total > limit:
            raise ContextBudgetExceededError(
                "system 区段 + 预留输出已超过上下文窗口，无法在不破坏硬地板的前提下裁剪",
                section="system",
                over_by=total - limit,
                limit=limit,
                total=total,
            )

        items = self._render(system_items, tool_items, history_units, retrieval_items, summaries)
        sections = self._section_usage(items, dropped, summaries)
        fingerprint = self._fingerprint(items, dropped, summaries, degraded)
        return PreparedContext(
            items=tuple(items),
            sections=tuple(sections),
            total_tokens=total,
            limit_tokens=limit,
            reserved_output_tokens=self._limits.reserved_output_tokens,
            dropped=tuple(dropped),
            summaries=tuple(summaries),
            degraded=degraded,
            fingerprint=fingerprint,
        )

    # -- 校验 ---------------------------------------------------------
    def _validate_integrity(self, context: ContextInput) -> None:
        keys = [block.key for block in context.system]
        if len(set(keys)) != len(keys):
            raise ContextIntegrityError("SystemBlock.key 重复", keys=keys)
        names = [tool.name for tool in context.tools]
        if len(set(names)) != len(names):
            raise ContextIntegrityError("ToolSchema.name 重复", names=names)
        refs = [chunk.source_ref for chunk in context.retrieval]
        if len(set(refs)) != len(refs):
            raise ContextIntegrityError("RetrievalChunk.source_ref 重复", refs=refs)

        last_turn = -1
        for message in context.history:
            if message.turn_index < last_turn:
                raise ContextIntegrityError(
                    "history 必须按 turn_index 非降序给出", turn_index=message.turn_index
                )
            last_turn = message.turn_index

        # 工具对原子性：每个 tool result 必须有同轮 assistant tool_call；反之不必
        # （assistant 可以只提议而未执行，但那种情况下结果缺失不影响提示的安全）。
        call_ids: dict[int, set[str]] = {}
        for message in context.history:
            if message.role == "assistant" and message.tool_call_id:
                call_ids.setdefault(message.turn_index, set()).add(message.tool_call_id)
        result_ids: list[str] = []
        for message in context.history:
            if message.role == "tool":
                if not message.tool_call_id:
                    raise ContextIntegrityError("tool 消息必须带 tool_call_id")
                if message.tool_call_id not in call_ids.get(message.turn_index, set()):
                    raise ContextIntegrityError(
                        "tool result 找不到同轮 assistant tool call（孤立 tool 消息）",
                        tool_call_id=message.tool_call_id,
                        turn_index=message.turn_index,
                    )
                result_ids.append(message.tool_call_id)
        if len(set(result_ids)) != len(result_ids):
            raise ContextIntegrityError("同一 tool_call_id 出现多条 tool result", ids=result_ids)

    @staticmethod
    def _history_units(
        history: Sequence[HistoryMessage],
    ) -> list[list[HistoryMessage]]:
        """按 turn_index 分组（保持出现顺序）；裁剪以整轮为单位。"""

        units: list[list[HistoryMessage]] = []
        for message in history:
            if units and units[-1][0].turn_index == message.turn_index:
                units[-1].append(message)
            else:
                units.append([message])
        return units

    # -- 计数 ---------------------------------------------------------
    def _total_tokens(
        self,
        system_items: Sequence[SystemBlock],
        tool_items: Sequence[ToolSchema],
        history_units: Sequence[Sequence[HistoryMessage]],
        retrieval_items: Sequence[RetrievalChunk],
    ) -> int:
        total = 0
        if system_items:
            total += SECTION_OVERHEAD_TOKENS
            total += sum(estimate_tokens(block.text) for block in system_items)
        if tool_items:
            total += SECTION_OVERHEAD_TOKENS
            total += sum(
                estimate_tokens(tool.canonical()) + TOOL_OVERHEAD_TOKENS for tool in tool_items
            )
        if history_units:
            total += SECTION_OVERHEAD_TOKENS
            total += sum(
                estimate_tokens(message.text) + MESSAGE_OVERHEAD_TOKENS
                for unit in history_units
                for message in unit
            )
        if retrieval_items:
            total += SECTION_OVERHEAD_TOKENS
            total += sum(estimate_tokens(chunk.text) for chunk in retrieval_items)
        return total

    # -- 裁剪 ---------------------------------------------------------
    def _trim_retrieval(
        self,
        retrieval_items: list[RetrievalChunk],
        current_total: Callable[[], int],
        limit: int,
        summaries: list[SummaryRecord],
        dropped: list[DropRecord],
    ) -> bool:
        """低分先处理；大片段先摘要（可能就此够用），仍然超预算才整段丢弃。"""

        degraded = False
        ordered = sorted(
            list(retrieval_items), key=lambda item: (item.score, item.rank, item.source_ref)
        )
        for original in ordered:
            if current_total() <= limit:
                break
            if original not in retrieval_items:  # pragma: no cover - 防御
                continue
            index = retrieval_items.index(original)
            current = retrieval_items[index]
            original_text = current.text
            tokens = estimate_tokens(original_text)
            summary_applied = False
            summary_failed = False
            if tokens > self._limits.summary_trigger_tokens and not any(
                record.source_ref == original.source_ref for record in summaries
            ):
                self._summary_counter += 1
                summary_ref = _sha256(
                    _canonical_json(
                        {
                            "source_ref": original.source_ref,
                            "text": original_text,
                            "n": self._summary_counter,
                        }
                    )
                )[:16]
                try:
                    summary_text = self._summarizer(original_text, self._limits.summary_max_tokens)
                except Exception:
                    summary_failed = True
                    degraded = True
                else:
                    if not isinstance(summary_text, str) or not summary_text:
                        summary_failed = True
                        degraded = True
                    elif estimate_tokens(summary_text) < tokens:
                        summaries.append(
                            SummaryRecord(
                                section="retrieval",
                                item_id=original.source_ref,
                                source_ref=original.source_ref,
                                original_tokens=tokens,
                                summary_tokens=estimate_tokens(summary_text),
                                summary_ref=summary_ref,
                                method=getattr(self._summarizer, "__name__", "custom"),
                            )
                        )
                        retrieval_items[index] = RetrievalChunk(
                            source_ref=original.source_ref,
                            text=summary_text,
                            score=original.score,
                            rank=original.rank,
                        )
                        current = retrieval_items[index]
                        tokens = estimate_tokens(summary_text)
                        summary_applied = True
                        if current_total() <= limit:
                            break
            retrieval_items.pop(index)
            if summary_applied:
                reason: DropReason = "SUMMARIZED_THEN_DROPPED"
            elif summary_failed:
                reason = "SUMMARY_FAILED"
            else:
                reason = "LOW_SCORE"
            dropped.append(
                DropRecord(
                    section="retrieval",
                    item_id=original.source_ref,
                    reason=reason,
                    tokens=tokens,
                    detail=f"score={original.score} rank={original.rank}",
                )
            )
        return degraded

    def _trim_history(
        self,
        history_units: list[list[HistoryMessage]],
        current_total: Callable[[], int],
        limit: int,
        dropped: list[DropRecord],
    ) -> None:
        """从最旧的可裁剪轮开始整轮丢弃；最近 ``keep_last_turns`` 轮与 pinned 轮保留。"""

        keep = max(0, len(history_units) - self._limits.keep_last_turns)
        for index in range(keep):
            if current_total() <= limit:
                break
            unit = history_units[index]
            if not unit or any(message.pinned for message in unit):
                continue
            unit_tokens = sum(
                estimate_tokens(message.text) + MESSAGE_OVERHEAD_TOKENS for message in unit
            )
            history_units[index] = []
            dropped.append(
                DropRecord(
                    section="history",
                    item_id=f"turn/{unit[0].turn_index}",
                    reason="OLDEST_TURN",
                    tokens=unit_tokens,
                    detail=f"messages={len(unit)}",
                )
            )
        history_units[:] = [unit for unit in history_units if unit]

    def _trim_tools(
        self,
        tool_items: list[ToolSchema],
        current_total: Callable[[], int],
        limit: int,
        dropped: list[DropRecord],
    ) -> None:
        """只裁 optional 工具，按 name 逆序（稳定）；required 工具永不裁剪。"""

        optional = sorted(
            [item for item in tool_items if not item.required],
            key=lambda item: item.name,
            reverse=True,
        )
        for tool in optional:
            if current_total() <= limit:
                break
            if tool not in tool_items:  # pragma: no cover - 防御
                continue
            tokens = estimate_tokens(tool.canonical()) + TOOL_OVERHEAD_TOKENS
            tool_items.remove(tool)
            dropped.append(
                DropRecord(
                    section="tools",
                    item_id=tool.name,
                    reason="OPTIONAL_TOOL",
                    tokens=tokens,
                    detail=tool.version,
                )
            )

    # -- 渲染 ---------------------------------------------------------
    def _render(
        self,
        system_items: Sequence[SystemBlock],
        tool_items: Sequence[ToolSchema],
        history_units: Sequence[Sequence[HistoryMessage]],
        retrieval_items: Sequence[RetrievalChunk],
        summaries: Sequence[SummaryRecord],
    ) -> list[RenderedItem]:
        summary_refs = {record.source_ref: record.summary_ref for record in summaries}
        items: list[RenderedItem] = []
        for block in system_items:
            items.append(
                RenderedItem(
                    section="system",
                    item_id=f"system/{block.key}",
                    text=block.text,
                    tokens=estimate_tokens(block.text),
                )
            )
        for tool in tool_items:
            items.append(
                RenderedItem(
                    section="tools",
                    item_id=tool.name,
                    text=tool.canonical(),
                    tokens=estimate_tokens(tool.canonical()) + TOOL_OVERHEAD_TOKENS,
                )
            )
        for unit in history_units:
            for message in unit:
                items.append(
                    RenderedItem(
                        section="history",
                        item_id=message.item_id(),
                        text=message.text,
                        tokens=estimate_tokens(message.text) + MESSAGE_OVERHEAD_TOKENS,
                    )
                )
        for chunk in retrieval_items:
            items.append(
                RenderedItem(
                    section="retrieval",
                    item_id=f"retrieval/{chunk.source_ref}",
                    text=chunk.text,
                    tokens=estimate_tokens(chunk.text),
                    summary_ref=summary_refs.get(chunk.source_ref),
                )
            )
        return items

    def _section_usage(
        self,
        items: Sequence[RenderedItem],
        dropped: Sequence[DropRecord],
        summaries: Sequence[SummaryRecord],
    ) -> list[SectionUsage]:
        usages: list[SectionUsage] = []
        for section in SECTIONS:
            section_items = [item for item in items if item.section == section]
            section_dropped = [record for record in dropped if record.section == section]
            summary_tokens = sum(
                record.summary_tokens for record in summaries if record.section == section
            )
            tokens = sum(item.tokens for item in section_items)
            if section_items:
                tokens += SECTION_OVERHEAD_TOKENS
            usages.append(
                SectionUsage(
                    section=section,
                    tokens=tokens,
                    items=len(section_items),
                    dropped_items=len(section_dropped),
                    summary_tokens=summary_tokens,
                )
            )
        return usages

    def _fingerprint(
        self,
        items: Sequence[RenderedItem],
        dropped: Sequence[DropRecord],
        summaries: Sequence[SummaryRecord],
        degraded: bool,
    ) -> str:
        payload = {
            "limit": self._limits.effective_input_tokens,
            "reserved_output": self._limits.reserved_output_tokens,
            "items": [
                {
                    "section": item.section,
                    "item_id": item.item_id,
                    "tokens": item.tokens,
                    "summary_ref": item.summary_ref,
                    "text_sha256": _sha256(item.text),
                }
                for item in items
            ],
            "dropped": [
                {
                    "section": record.section,
                    "item_id": record.item_id,
                    "reason": record.reason,
                    "tokens": record.tokens,
                }
                for record in dropped
            ],
            "summaries": [
                {
                    "source_ref": record.source_ref,
                    "summary_ref": record.summary_ref,
                    "summary_tokens": record.summary_tokens,
                }
                for record in summaries
            ],
            "degraded": degraded,
        }
        return _sha256(_canonical_json(payload))
