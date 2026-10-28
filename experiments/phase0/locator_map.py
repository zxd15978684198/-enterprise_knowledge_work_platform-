"""P0-T11 locator 映射：结构定位符 → 原文片段（offset-free，不做资产绑定）。

C-10 边界（写权与范围）：本模块**只**定义 locator schema 与「locator → 原文片段」
解析；``asset_bindings``、版本号、workspace 归属、发布状态属于 P0-T12/T13，
这里不提供、也不允许提前出现。

locator schema v1（JSON 可序列化，字段随 ``kind`` 变化）：

* ``page``        pdf：``{kind, format, page, paragraph}``；
                  ``page`` 从 1 开始（对齐 docreader ``SourceBlock.locator_json`` 的
                  ``{"type":"pdf","page":3}`` 约定），``paragraph`` 是在该页内第几段（0 基）。
* ``paragraph``   所有格式：``{kind, format, paragraph}``，文档级正文段序号（0 基）。
* ``heading``     md/html/docx：``{kind, format, heading_path, paragraph?}``；
                  ``paragraph`` 缺省表示标题本身，出现表示该节内第几段（0 基）。
* ``code_block``  md/html：``{kind, format, heading_path, code_block}``。
* ``table_cell``  md/html/docx：``{kind, format, heading_path, table, row, col}``，
                  ``table`` 是在该节内第几张表（0 基），``row``/``col`` 从 0 开始。

两条硬约束：

1. **不存绝对字符偏移**。解析时按 locator 结构重新解析原文，片段在文档文本中的
   ``ResolvedSpan.start/end`` 是**派生值**，不写回 locator。插入/删除一个字符不会
   让 locator 失效（由 ``tests/phase0/test_parser_golden.py`` 验证）。
2. **命中不了返回 ``None``**。不猜、不伪造片段；调用方必须显式处理降级
   （``LocatorMap.degraded`` / ``require`` 会抛异常）。

模块内不 import ``simple_reader``（避免循环依赖）：``resolve_in_source`` /
``LocatorMap.from_result`` 只做鸭子类型访问，必要时才局部 import。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Protocol

SCHEMA_VERSION = 1

KIND_PAGE = "page"
KIND_PARAGRAPH = "paragraph"
KIND_HEADING = "heading"
KIND_CODE_BLOCK = "code_block"
KIND_TABLE_CELL = "table_cell"
KINDS = frozenset({KIND_PAGE, KIND_PARAGRAPH, KIND_HEADING, KIND_CODE_BLOCK, KIND_TABLE_CELL})

#: 被明确禁止的字段名（locator 一旦带这些名字即视为「偏移型 locator」）。
_FORBIDDEN_KEY_PARTS = ("offset",)
_FORBIDDEN_EXACT_KEYS = frozenset({"start", "end", "span", "span_start", "span_end"})

#: 允许出现在序列化 locator 里的键（严格校验，防止悄悄加进偏移字段）。
_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "format",
        "kind",
        "page",
        "paragraph",
        "heading_path",
        "table",
        "row",
        "col",
        "code_block",
    }
)


class LocatorError(ValueError):
    """locator schema 非法（未知 kind、缺字段、带偏移字段等）。"""


class LocatorResolutionError(LookupError):
    """locator 在文档中定位不到；调用方必须显式降级，不能拿到伪造片段。"""


class BlockLike(Protocol):
    """``LocatorMap`` 需要的最小块协议（与 ``simple_reader.Block`` 同形）。"""

    kind: str
    text: str
    locator: Locator
    aliases: tuple[Locator, ...]


@dataclass(frozen=True, slots=True)
class Locator:
    """结构定位符；``None``/空 tuple 表示该 kind 不使用该字段。"""

    kind: str
    format: str
    page: int | None = None
    paragraph: int | None = None
    heading_path: tuple[str, ...] = ()
    table: int | None = None
    row: int | None = None
    col: int | None = None
    code_block: int | None = None
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_VERSION:
            raise LocatorError(f"unsupported locator schema: {self.schema}")
        if self.kind not in KINDS:
            raise LocatorError(f"unknown locator kind: {self.kind!r}")
        if not self.format:
            raise LocatorError("locator.format must be non-empty")
        for name in ("page", "paragraph", "table", "row", "col", "code_block"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LocatorError(f"locator.{name} must be a non-negative int, got {value!r}")
        if not isinstance(self.heading_path, tuple) or any(
            not isinstance(item, str) for item in self.heading_path
        ):
            raise LocatorError("locator.heading_path must be a tuple[str, ...]")
        self._validate_kind_shape()

    def _validate_kind_shape(self) -> None:
        if self.kind == KIND_PAGE:
            if self.page is None or self.paragraph is None:
                raise LocatorError("page locator requires page and paragraph")
            self._reject(self.table, self.row, self.col, self.code_block)
        elif self.kind == KIND_PARAGRAPH:
            if self.paragraph is None:
                raise LocatorError("paragraph locator requires paragraph")
            if self.heading_path:
                raise LocatorError("paragraph locator must not carry heading_path")
            self._reject(self.page, self.table, self.row, self.col, self.code_block)
        elif self.kind == KIND_HEADING:
            if not self.heading_path:
                raise LocatorError("heading locator requires non-empty heading_path")
            self._reject(self.page, self.table, self.row, self.col, self.code_block)
        elif self.kind == KIND_CODE_BLOCK:
            if self.code_block is None:
                raise LocatorError("code_block locator requires code_block")
            self._reject(self.page, self.paragraph, self.table, self.row, self.col)
        else:  # KIND_TABLE_CELL
            if self.table is None or self.row is None or self.col is None:
                raise LocatorError("table_cell locator requires table, row and col")
            self._reject(self.page, self.paragraph, self.code_block)

    @staticmethod
    def _reject(*values: int | None) -> None:
        present = [value for value in values if value is not None]
        if present:
            raise LocatorError(f"locator kind does not accept fields: {present}")

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容字典；只输出该 kind 实际使用的字段。"""
        data: dict[str, Any] = {
            "schema": self.schema,
            "format": self.format,
            "kind": self.kind,
        }
        for name in ("page", "paragraph", "table", "row", "col", "code_block"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.heading_path:
            data["heading_path"] = list(self.heading_path)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Locator:
        """严格反序列化：未知键、偏移键、未知 kind 一律拒绝。"""
        if not isinstance(data, Mapping):
            raise LocatorError(f"locator must be a mapping, got {type(data).__name__}")
        unknown = set(data) - _ALLOWED_KEYS
        if unknown:
            raise LocatorError(f"unknown locator keys: {sorted(unknown)}")
        for key in data:
            lowered = key.lower()
            if lowered in _FORBIDDEN_EXACT_KEYS or any(
                part in lowered for part in _FORBIDDEN_KEY_PARTS
            ):
                raise LocatorError(
                    f"locator schema v1 is offset-free; refusing key {key!r}"
                )
        heading_path = data.get("heading_path", ())
        if isinstance(heading_path, list):
            heading_path = tuple(heading_path)
        if not isinstance(heading_path, tuple):
            raise LocatorError("heading_path must be a list of strings")
        return cls(
            schema=int(data.get("schema", SCHEMA_VERSION)),
            format=str(data.get("format", "")),
            kind=str(data.get("kind", "")),
            page=data.get("page"),
            paragraph=data.get("paragraph"),
            heading_path=heading_path,
            table=data.get("table"),
            row=data.get("row"),
            col=data.get("col"),
            code_block=data.get("code_block"),
        )

    def key(self) -> tuple[Any, ...]:
        """索引键（与序列化字段一一对应，可哈希）。"""
        return (
            self.kind,
            self.format,
            self.page,
            self.paragraph,
            self.heading_path,
            self.table,
            self.row,
            self.col,
            self.code_block,
        )


@dataclass(frozen=True, slots=True)
class ResolvedSpan:
    """一次命中的结果；``start``/``end`` 是**派生**位置，不写回 locator。"""

    text: str
    start: int
    end: int
    locator: Locator
    block_kind: str


class LocatorMap:
    """按 locator 索引块文本；由解析结果的块顺序派生 span 偏移。"""

    SEPARATOR = "\n\n"

    def __init__(self, text: str, blocks: Iterable[BlockLike]) -> None:
        self.text = text
        self.blocks = tuple(blocks)
        self.degraded = not self.blocks
        self._index: dict[tuple[Any, ...], ResolvedSpan] = {}
        self._build_index()

    def _build_index(self) -> None:
        rendered = self.SEPARATOR.join(block.text for block in self.blocks)
        consistent = rendered == self.text
        cursor = 0
        for position, block in enumerate(self.blocks):
            if consistent:
                start, end = cursor, cursor + len(block.text)
            else:  # 文本与块顺序不一致时退回顺序查找；找不到就不索引
                start = self.text.find(block.text, cursor) if block.text else cursor
                if start < 0:
                    continue
                end = start + len(block.text)
            span = ResolvedSpan(
                text=block.text,
                start=start,
                end=end,
                locator=block.locator,
                block_kind=block.kind,
            )
            self._index.setdefault(block.locator.key(), span)
            for alias in block.aliases:
                self._index.setdefault(alias.key(), span)
            cursor = end + (len(self.SEPARATOR) if position + 1 < len(self.blocks) else 0)

    @classmethod
    def from_result(cls, result: Any) -> LocatorMap:
        """从任何带 ``text``/``blocks`` 属性的解析结果构建（鸭子类型）。"""
        return cls(result.text, result.blocks)

    def resolve(self, locator: Locator) -> ResolvedSpan | None:
        """命中返回片段；定位不到返回 ``None``（不伪造）。"""
        return self._index.get(locator.key())

    def resolve_text(self, locator: Locator) -> str | None:
        span = self.resolve(locator)
        return None if span is None else span.text

    def require(self, locator: Locator) -> ResolvedSpan:
        span = self.resolve(locator)
        if span is None:
            raise LocatorResolutionError(f"locator not found in document: {locator.to_dict()}")
        return span

    def covers(self, locator: Locator) -> bool:
        return locator.key() in self._index

    def kinds(self) -> frozenset[str]:
        return frozenset(span.locator.kind for span in self._index.values())

    def __len__(self) -> int:
        return len(self._index)


def locator_fields() -> frozenset[str]:
    """暴露 Locator 的字段名，供测试断言「没有偏移字段」。"""
    return frozenset(field.name for field in fields(Locator))


def resolve_in_source(
    content: bytes,
    *,
    file_name: str,
    file_type: str = "",
    locator: Locator,
    parser: Any = None,
) -> ResolvedSpan | None:
    """source-anchored 解析：重新读原始 bytes，再按 locator 取片段。

    这是**非循环**的验证入口：golden 的期望片段在 ``parsing_assets`` 里独立书写，
    不来自解析结果；locator 只描述结构位置。
    """
    if parser is None:  # 局部 import 避免 locator_map ↔ simple_reader 循环依赖
        from simple_reader import read_document as parser
    result = parser(content, file_name=file_name, file_type=file_type)
    if not result.ok:
        return None
    return LocatorMap.from_result(result).resolve(locator)
