"""P0-T11 五格式窄适配解析器（txt / md / html / docx / pdf）。

范围与边界（对应 Backlog P0-T11 与 Lead 收口口径）：

* 只做**窄适配**：够用、可复验、可定位，不追求生产级解析。PDF 只认本目录
  ``parsing_assets`` 生成的最小子集（ASCII 字面量 + UTF-16BE 十六进制串）；
  DOCX 只读 ``word/document.xml``；HTML 只取正文/标题/表格/代码块；MD 只认
  ATX 标题、围栏代码、管道表格、列表与段落。真实格式的深度解析属于替代引擎
  评估（T11 风险项），不在本文件里假装完成。
* **失败一律通过 ``ParseResult.error`` 返回，绝不抛异常**。这与 P0-T10 的实测
  语义一致：``docreader.proto`` 的 ``ReadResponse.error`` 是字段 5，解析失败时
  gRPC status 仍为 OK（``docreader_probe/wire_evidence.json`` 的
  ``parse_error_field`` 证据）。调用方必须先看 ``error``，再看内容，绝不能把
  空结果当成功（T09 D-07）。
* 空文档、无可见文本、损坏容器都必须给出**明确错误**，不得静默返回空文本。
* C-10：本文件不产出 ``asset_bindings``/版本/workspace 归属，只产 ``Block`` +
  ``locator``；版本发布与资产唯一归属由 P0-T12/T13 负责。

定位符与片段映射见 ``locator_map``；合成素材与 golden 期望片段见
``parsing_assets``。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from xml.etree import ElementTree

from locator_map import (
    KIND_CODE_BLOCK,
    KIND_HEADING,
    KIND_PAGE,
    KIND_PARAGRAPH,
    KIND_TABLE_CELL,
    Locator,
)

#: ``docreader.proto:88`` 的 ``ReadResponse.error = 5``；P0-T10 已实测「失败只落
#: 该字段而 gRPC status 仍 OK」。本模块的错误通道必须与它同名同语义。
READ_RESPONSE_ERROR_FIELD = "error"
READ_RESPONSE_ERROR_FIELD_NUMBER = 5

SUPPORTED_FORMATS = ("pdf", "docx", "md", "html", "txt")

_FORMAT_BY_EXTENSION = {
    "pdf": "pdf",
    "docx": "docx",
    "md": "md",
    "markdown": "md",
    "html": "html",
    "htm": "html",
    "txt": "txt",
    "text": "txt",
}

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class _ParseFailure(Exception):
    """内部异常：消息会写进 ``ParseResult.error``（不向上抛给调用方）。"""


@dataclass(frozen=True, slots=True)
class Block:
    """解析出的一个文本块；``locator`` 是结构位置，``aliases`` 是等价定位。"""

    kind: str
    text: str
    locator: Locator
    aliases: tuple[Locator, ...] = ()


@dataclass(frozen=True, slots=True)
class ParseResult:
    """解析结果；``error`` 非空即失败（等同 ``ReadResponse.error`` 语义）。"""

    format: str
    text: str
    blocks: tuple[Block, ...]
    error: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.error

    def as_read_response_fields(self) -> dict[str, str]:
        """映射成 docreader ``ReadResponse`` 的两个字段（便于跨引用断言）。"""
        return {
            "markdown_content": self.text if self.ok else "",
            READ_RESPONSE_ERROR_FIELD: self.error,
        }


_Outcome = tuple[list[Block], tuple[str, ...]]


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------
def detect_format(file_name: str, file_type: str = "") -> str:
    """按显式 ``file_type`` 优先、扩展名兜底识别格式；未知返回空串。"""
    declared = file_type.strip().lower().lstrip(".")
    if declared in _FORMAT_BY_EXTENSION:
        return _FORMAT_BY_EXTENSION[declared]
    extension = os.path.splitext(file_name)[1].lstrip(".").lower()
    return _FORMAT_BY_EXTENSION.get(extension, "")


def _decode_utf8(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ParseFailure(f"invalid UTF-8 at byte {exc.start}: {exc.reason}") from exc


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_text(text: str) -> str:
    """折叠空白（含全角空格之外的 unicode 空白），不做其他变形。"""
    return " ".join(text.split())


def _paragraph_locators(
    fmt: str,
    heading_path: tuple[str, ...],
    section_paragraph: int,
    document_paragraph: int,
) -> tuple[Locator, tuple[Locator, ...]]:
    """正文段的主定位符与别名（标题路径 + 节内序号优先，文档级序号兜底）。"""
    if heading_path:
        primary = Locator(
            kind=KIND_HEADING,
            format=fmt,
            heading_path=heading_path,
            paragraph=section_paragraph,
        )
        alias = Locator(kind=KIND_PARAGRAPH, format=fmt, paragraph=document_paragraph)
        return primary, (alias,)
    return Locator(kind=KIND_PARAGRAPH, format=fmt, paragraph=document_paragraph), ()


def _assemble(fmt: str, blocks: list[Block], warnings: tuple[str, ...]) -> ParseResult:
    if not any(block.text.strip() for block in blocks):
        raise _ParseFailure("no visible text")
    return ParseResult(
        format=fmt,
        text="\n\n".join(block.text for block in blocks),
        blocks=tuple(blocks),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def _parse_txt(content: bytes) -> _Outcome:
    text = _normalize_newlines(_decode_utf8(content))
    raw_paragraphs = re.split(r"\n[ \t]*\n", text)
    paragraphs = [paragraph.strip() for paragraph in raw_paragraphs if paragraph.strip()]
    blocks = [
        Block(
            "paragraph",
            paragraph,
            Locator(kind=KIND_PARAGRAPH, format="txt", paragraph=index),
        )
        for index, paragraph in enumerate(paragraphs)
    ]
    return blocks, ()


# ---------------------------------------------------------------------------
# Markdown（窄适配）
# ---------------------------------------------------------------------------
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_MD_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_MD_LIST_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(.*)$")
_MD_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(.*)$")


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells)


def _parse_md(content: bytes) -> _Outcome:
    lines = _normalize_newlines(_decode_utf8(content)).split("\n")
    blocks: list[Block] = []
    warnings: list[str] = []
    heading_path: tuple[str, ...] = ()
    section_paragraph = 0
    section_code = 0
    section_table = 0
    document_paragraph = 0
    pending: list[str] = []

    def emit_paragraph(text: str) -> None:
        nonlocal section_paragraph, document_paragraph
        normalized = _normalize_text(text)
        if not normalized:
            return
        primary, aliases = _paragraph_locators(
            "md", heading_path, section_paragraph, document_paragraph
        )
        blocks.append(Block("paragraph", normalized, primary, aliases))
        section_paragraph += 1
        document_paragraph += 1

    def flush_pending() -> None:
        if not pending:
            return
        text = " ".join(pending)
        pending.clear()
        emit_paragraph(text)

    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _MD_HEADING_RE.match(line)
        fence = _MD_FENCE_RE.match(line)
        if not line.strip():
            flush_pending()
            index += 1
            continue
        if heading:
            flush_pending()
            level = len(heading.group(1))
            title = _normalize_text(heading.group(2))
            index += 1
            if not title:
                warnings.append(f"md: empty heading at line {index}")
                continue
            heading_path = heading_path[: level - 1] + (title,)
            section_paragraph = 0
            section_code = 0
            section_table = 0
            blocks.append(
                Block(
                    "heading",
                    title,
                    Locator(kind=KIND_HEADING, format="md", heading_path=heading_path),
                )
            )
            continue
        if fence:
            flush_pending()
            marker = fence.group(1)
            fence_char = marker[0]
            closing = index + 1
            while closing < len(lines) and not lines[closing].strip().startswith(
                fence_char * 3
            ):
                closing += 1
            inner = lines[index + 1 : closing]
            if closing >= len(lines):
                warnings.append(f"md: unterminated code fence at line {index + 1}")
            code = "\n".join(inner).strip("\n")
            if code.strip():
                blocks.append(
                    Block(
                        "code_block",
                        code,
                        Locator(
                            kind=KIND_CODE_BLOCK,
                            format="md",
                            heading_path=heading_path,
                            code_block=section_code,
                        ),
                    )
                )
                section_code += 1
            index = closing + 1
            continue
        if (
            "|" in line
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1])
        ):
            flush_pending()
            rows = [_split_table_row(line)]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                rows.append(_split_table_row(lines[index]))
                index += 1
            width = max(len(row) for row in rows)
            for row in rows:
                if len(row) != width:
                    warnings.append("md: table row width mismatch; missing cells are empty")
            for row_index, row in enumerate(rows):
                for col_index in range(width):
                    cell = row[col_index] if col_index < len(row) else ""
                    blocks.append(
                        Block(
                            "table_cell",
                            cell,
                            Locator(
                                kind=KIND_TABLE_CELL,
                                format="md",
                                heading_path=heading_path,
                                table=section_table,
                                row=row_index,
                                col=col_index,
                            ),
                        )
                    )
            section_table += 1
            continue
        list_item = _MD_LIST_RE.match(line)
        quote = _MD_QUOTE_RE.match(line)
        if list_item or quote:
            flush_pending()
            emit_paragraph((list_item or quote).group(1))
            index += 1
            continue
        pending.append(line.strip())
        index += 1
    flush_pending()
    return blocks, tuple(warnings)


# ---------------------------------------------------------------------------
# HTML（stdlib HTMLParser，只取正文结构）
# ---------------------------------------------------------------------------
class _HtmlBlockParser(HTMLParser):
    """收集标题/段落/代码/表格单元格；跳过 script/style。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.warnings: list[str] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._heading_buffer: list[str] = []
        self._paragraph_buffer: list[str] | None = None
        self._code_buffer: list[str] = []
        self._in_code = False
        self._heading_path: tuple[str, ...] = ()
        self._section_paragraph = 0
        self._section_code = 0
        self._section_table = 0
        self._document_paragraph = 0
        self._table_depth = 0
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_buffer: list[str] | None = None
        self._table_open = False

    # -- 文本缓冲 ----------------------------------------------------------
    def _append(self, text: str) -> None:
        if self._cell_buffer is not None:
            self._cell_buffer.append(text)
        elif self._heading_level:
            self._heading_buffer.append(text)
        elif self._in_code:
            self._code_buffer.append(text)
        elif self._paragraph_buffer is not None:
            self._paragraph_buffer.append(text)

    def _emit_paragraph(self) -> None:
        if self._paragraph_buffer is None:
            return
        text = _normalize_text("".join(self._paragraph_buffer))
        self._paragraph_buffer = None
        if not text:
            return
        primary, aliases = _paragraph_locators(
            "html", self._heading_path, self._section_paragraph, self._document_paragraph
        )
        self.blocks.append(Block("paragraph", text, primary, aliases))
        self._section_paragraph += 1
        self._document_paragraph += 1

    def _reset_section(self) -> None:
        self._section_paragraph = 0
        self._section_code = 0
        self._section_table = 0

    # -- HTMLParser 回调 ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._emit_paragraph()
            self._heading_level = int(tag[1])
            self._heading_buffer = []
        elif tag in {"p", "li"}:
            self._emit_paragraph()
            self._paragraph_buffer = []
        elif tag == "pre":
            self._emit_paragraph()
            self._in_code = True
            self._code_buffer = []
        elif tag == "br":
            self._append("\n")
        elif tag == "table":
            self._emit_paragraph()
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
                self._table_open = True
            else:
                self.warnings.append("html: nested table flattened (depth>1 unsupported)")
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in {"td", "th"}:
            self._cell_buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS and self._heading_level:
            title = _normalize_text("".join(self._heading_buffer))
            level = self._heading_level
            self._heading_level = 0
            self._heading_buffer = []
            if title:
                self._heading_path = self._heading_path[: level - 1] + (title,)
                self._reset_section()
                self.blocks.append(
                    Block(
                        "heading",
                        title,
                        Locator(kind=KIND_HEADING, format="html", heading_path=self._heading_path),
                    )
                )
        elif tag in {"p", "li"}:
            self._emit_paragraph()
        elif tag == "pre" and self._in_code:
            code = "".join(self._code_buffer).strip("\n")
            self._in_code = False
            if code.strip():
                self.blocks.append(
                    Block(
                        "code_block",
                        code,
                        Locator(
                            kind=KIND_CODE_BLOCK,
                            format="html",
                            heading_path=self._heading_path,
                            code_block=self._section_code,
                        ),
                    )
                )
                self._section_code += 1
        elif self._table_depth == 1 and tag in {"td", "th"}:
            cell = _normalize_text("".join(self._cell_buffer or []))
            if self._row is None:
                self._row = []
            self._row.append(cell)
            self._cell_buffer = None
        elif self._table_depth == 1 and tag == "tr":
            if self._row is not None:
                self._rows.append(self._row)
                self._row = None
        elif tag == "table":
            if self._table_depth:
                self._table_depth -= 1
            if self._table_depth == 0 and self._table_open:
                self._emit_table()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._append(data)

    def _emit_table(self) -> None:
        self._table_open = False
        rows = self._rows
        self._rows = []
        if not rows:
            self.warnings.append("html: empty table skipped")
            return
        width = max(len(row) for row in rows)
        for row in rows:
            if len(row) != width:
                self.warnings.append("html: table row width mismatch; missing cells are empty")
                row.extend([""] * (width - len(row)))
        for row_index, row in enumerate(rows):
            for col_index, cell in enumerate(row):
                self.blocks.append(
                    Block(
                        "table_cell",
                        cell,
                        Locator(
                            kind=KIND_TABLE_CELL,
                            format="html",
                            heading_path=self._heading_path,
                            table=self._section_table,
                            row=row_index,
                            col=col_index,
                        ),
                    )
                )
        self._section_table += 1

    def close(self) -> None:
        super().close()
        self._emit_paragraph()
        if self._in_code:
            self.warnings.append("html: unclosed pre auto-closed")
            self._in_code = False
        if self._table_open:
            self.warnings.append("html: unclosed table auto-closed")
            self._emit_table()


def _parse_html(content: bytes) -> _Outcome:
    # HTML 规范要求输入流先把 CRLF/CR 归一为 LF；否则 <pre> 内的 CRLF 会漏进块文本。
    parser = _HtmlBlockParser()
    parser.feed(_normalize_newlines(_decode_utf8(content)))
    parser.close()
    return parser.blocks, tuple(parser.warnings)


# ---------------------------------------------------------------------------
# DOCX（zipfile + word/document.xml）
# ---------------------------------------------------------------------------
def _w(tag: str) -> str:
    return f"{_W_NS}{tag}"


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _w("t") and node.text:
            parts.append(node.text)
        elif node.tag == _w("tab"):
            parts.append("\t")
        elif node.tag in {_w("br"), _w("cr")}:
            parts.append("\n")
    return _normalize_text("".join(parts))


def _docx_heading_level(paragraph: ElementTree.Element) -> int:
    properties = paragraph.find(_w("pPr"))
    if properties is None:
        return 0
    style = properties.find(_w("pStyle"))
    if style is None:
        return 0
    name = (style.get(_w("val")) or "").strip()
    if name.lower().startswith("heading"):
        digits = re.search(r"(\d+)", name)
        return int(digits.group(1)) if digits else 1
    if name in {"Title", "标题"}:
        return 1
    return 0


def _parse_docx(content: bytes) -> _Outcome:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise _ParseFailure(f"invalid zip: {exc}") from exc
    with archive:
        try:
            document_xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise _ParseFailure("missing word/document.xml") from exc
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise _ParseFailure(f"malformed word/document.xml: {exc}") from exc
    body = root.find(_w("body"))
    if body is None:
        raise _ParseFailure("missing w:body")

    blocks: list[Block] = []
    heading_path: tuple[str, ...] = ()
    section_paragraph = 0
    section_table = 0
    document_paragraph = 0
    for child in body:
        if child.tag == _w("p"):
            text = _docx_paragraph_text(child)
            if not text:
                continue
            level = _docx_heading_level(child)
            if level:
                heading_path = heading_path[: level - 1] + (text,)
                section_paragraph = 0
                section_table = 0
                blocks.append(
                    Block(
                        "heading",
                        text,
                        Locator(kind=KIND_HEADING, format="docx", heading_path=heading_path),
                    )
                )
                continue
            primary = Locator(
                kind=KIND_PARAGRAPH, format="docx", paragraph=document_paragraph
            )
            aliases = (
                (
                    Locator(
                        kind=KIND_HEADING,
                        format="docx",
                        heading_path=heading_path,
                        paragraph=section_paragraph,
                    ),
                )
                if heading_path
                else ()
            )
            blocks.append(Block("paragraph", text, primary, aliases))
            document_paragraph += 1
            section_paragraph += 1
        elif child.tag == _w("tbl"):
            rows: list[list[str]] = []
            for row in child.findall(_w("tr")):
                cells: list[str] = []
                for cell in row.findall(_w("tc")):
                    cell_text = " ".join(
                        _docx_paragraph_text(paragraph)
                        for paragraph in cell.findall(_w("p"))
                    )
                    cells.append(_normalize_text(cell_text))
                rows.append(cells)
            for row_index, row in enumerate(rows):
                for col_index, cell_text in enumerate(row):
                    blocks.append(
                        Block(
                            "table_cell",
                            cell_text,
                            Locator(
                                kind=KIND_TABLE_CELL,
                                format="docx",
                                heading_path=heading_path,
                                table=section_table,
                                row=row_index,
                                col=col_index,
                            ),
                        )
                    )
            if rows:
                section_table += 1
    return blocks, ()


# ---------------------------------------------------------------------------
# PDF（最小子集：ASCII 字面量 + UTF-16BE 十六进制串）
# ---------------------------------------------------------------------------
_PDF_OBJECT_RE = re.compile(rb"(\d+)\s+\d+\s+obj(.*?)endobj", re.S)
_PDF_KIDS_RE = re.compile(rb"/Kids\s*\[(.*?)\]", re.S)
_PDF_REF_RE = re.compile(rb"(\d+)\s+\d+\s+R")
_PDF_CONTENTS_RE = re.compile(rb"/Contents\s+(\d+)\s+\d+\s+R")
_PDF_LENGTH_RE = re.compile(rb"/Length\s+(\d+)")
_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_PDF_LITERAL_TJ_RE = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj")
_PDF_HEX_TJ_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>\s*Tj")
_PDF_TJ_ARRAY_RE = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_PDF_ARRAY_ITEM_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<([0-9A-Fa-f\s]+)>")


def _pdf_unescape(raw: bytes) -> str:
    output = bytearray()
    index = 0
    escapes = {
        ord("n"): b"\n",
        ord("r"): b"\r",
        ord("t"): b"\t",
        ord("("): b"(",
        ord(")"): b")",
        ord("\\"): b"\\",
    }
    while index < len(raw):
        byte = raw[index]
        if byte == 0x5C and index + 1 < len(raw):
            following = raw[index + 1]
            if following in escapes:
                output += escapes[following]
                index += 2
                continue
            if 0x30 <= following <= 0x37:
                digits = bytes(
                    byte for byte in raw[index + 1 : index + 4] if 0x30 <= byte <= 0x37
                )
                output.append(int(digits, 8) & 0xFF)
                index += 1 + len(digits)
                continue
            output.append(following)
            index += 2
            continue
        output.append(byte)
        index += 1
    return output.decode("latin-1")


def _pdf_decode_hex(payload: bytes) -> str:
    if payload.startswith(b"\xfe\xff"):
        return payload[2:].decode("utf-16-be", errors="replace")
    return payload.decode("latin-1")


def _pdf_texts(stream: bytes) -> tuple[list[str], bool]:
    found: list[tuple[int, str]] = []
    saw_hex = False
    for match in _PDF_LITERAL_TJ_RE.finditer(stream):
        raw = match.group(0)
        literal = raw[raw.index(b"(") : raw.rindex(b")") + 1][1:-1]
        found.append((match.start(), _pdf_unescape(literal)))
    for match in _PDF_HEX_TJ_RE.finditer(stream):
        payload = bytes.fromhex(re.sub(rb"\s+", b"", match.group(1)).decode("ascii"))
        found.append((match.start(), _pdf_decode_hex(payload)))
        saw_hex = True
    for match in _PDF_TJ_ARRAY_RE.finditer(stream):
        pieces: list[str] = []
        for item in _PDF_ARRAY_ITEM_RE.finditer(match.group(1)):
            if item.group(1) is not None:
                payload = bytes.fromhex(re.sub(rb"\s+", b"", item.group(1)).decode("ascii"))
                pieces.append(_pdf_decode_hex(payload))
                saw_hex = True
            else:
                literal = item.group(0)[1:-1]
                pieces.append(_pdf_unescape(literal))
        found.append((match.start(), "".join(pieces)))
    found.sort(key=lambda item: item[0])
    return [text.strip() for _, text in found if text.strip()], saw_hex


def _parse_pdf(content: bytes) -> _Outcome:
    if not content.startswith(b"%PDF-1."):
        raise _ParseFailure("missing %PDF header")
    if b"%%EOF" not in content[-1024:]:
        raise _ParseFailure("truncated: missing %%EOF trailer")
    objects: dict[int, bytes] = {}
    for match in _PDF_OBJECT_RE.finditer(content):
        objects.setdefault(int(match.group(1)), match.group(2))
    if not objects:
        raise _ParseFailure("no indirect objects")
    pages_object = next(
        (body for body in objects.values() if re.search(rb"/Type\s*/Pages\b", body)), None
    )
    page_numbers: list[int] = []
    if pages_object is not None:
        kids = _PDF_KIDS_RE.search(pages_object)
        if kids:
            page_numbers = [int(ref.group(1)) for ref in _PDF_REF_RE.finditer(kids.group(1))]
    if not page_numbers:
        page_numbers = [
            number
            for number, body in sorted(objects.items())
            if re.search(rb"/Type\s*/Page\b", body)
        ]
    if not page_numbers:
        raise _ParseFailure("no page objects")

    blocks: list[Block] = []
    saw_hex = False
    for page_number, object_number in enumerate(page_numbers, start=1):
        page_body = objects.get(object_number)
        if page_body is None:
            raise _ParseFailure(f"page object {object_number} not found")
        contents = _PDF_CONTENTS_RE.search(page_body)
        if contents is None:
            raise _ParseFailure(f"page object {object_number} has no /Contents")
        content_number = int(contents.group(1))
        stream_body = objects.get(content_number)
        if stream_body is None:
            raise _ParseFailure(f"content object {content_number} not found")
        stream_match = _PDF_STREAM_RE.search(stream_body)
        if stream_match is None:
            raise _ParseFailure(f"content object {content_number} has no stream")
        stream = stream_match.group(1)
        declared = _PDF_LENGTH_RE.search(stream_body)
        if declared is not None and int(declared.group(1)) != len(stream):
            raise _ParseFailure(
                f"stream length mismatch in object {content_number}: "
                f"declared {int(declared.group(1))}, actual {len(stream)}"
            )
        texts, page_saw_hex = _pdf_texts(stream)
        saw_hex = saw_hex or page_saw_hex
        for paragraph_index, text in enumerate(texts):
            blocks.append(
                Block(
                    "paragraph",
                    text,
                    Locator(
                        kind=KIND_PAGE,
                        format="pdf",
                        page=page_number,
                        paragraph=paragraph_index,
                    ),
                )
            )
    warnings: tuple[str, ...] = ()
    if saw_hex:
        warnings = (
            "pdf: hex text decoded as UTF-16BE (probe assumption; "
            "real PDF needs font CMap/ToUnicode)",
        )
    return blocks, warnings


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
_PARSERS = {
    "txt": _parse_txt,
    "md": _parse_md,
    "html": _parse_html,
    "docx": _parse_docx,
    "pdf": _parse_pdf,
}


def read_document(content: bytes, *, file_name: str, file_type: str = "") -> ParseResult:
    """解析五格式之一；任何内容问题都返回 ``error``，不向调用方抛异常。

    返回值的错误通道与 docreader ``ReadResponse.error`` 同义：调用方必须先看
    ``error``；为空才可把 ``text`` 当作解析结果。空文档/损坏容器/无可见文本都
    会得到非空 ``error``，不允许静默返回空文本。
    """
    fmt = detect_format(file_name, file_type)
    if not fmt:
        declared = (file_type or os.path.splitext(file_name)[1].lstrip(".")).lower()
        return ParseResult(
            format="",
            text="",
            blocks=(),
            error=f"Unsupported file type: {declared or '<unknown>'}",
        )
    if not content:
        return ParseResult(format=fmt, text="", blocks=(), error=f"{fmt}: empty document (0 bytes)")
    try:
        blocks, warnings = _PARSERS[fmt](content)
        return _assemble(fmt, blocks, warnings)
    except _ParseFailure as exc:
        return ParseResult(format=fmt, text="", blocks=(), error=f"{fmt}: {exc}")
    except Exception as exc:
        return ParseResult(
            format=fmt,
            text="",
            blocks=(),
            error=f"{fmt}: parse failed: {type(exc).__name__}: {exc}",
        )
