"""P0-T11 合成素材工厂：五格式文档字节、golden 用例、边界/损坏样本。

素材来源声明（必须与证据一起看）：

* **全部为合成素材（synthetic）**，由本文件的确定性构造函数在本地生成，
  **不是真实世界样本**；``AssetSpec.is_real_sample`` 恒为 ``False``。
* 许可：``LicenseRef-PyKnora-Phase0-TestFixture``（与
  ``experiments/phase0/fixtures/manifest.json`` 的 ``content_license`` 一致：
  Phase 0 自产确定性载荷，非第三方素材）。
* 可复现：构造函数不读时间/随机数；DOCX 的 zip 条目使用固定 ``date_time``，
  PDF 不含 ``/Info``/``/ID``，因此同一份代码两次生成的字节与 sha256 完全一致。
* 覆盖：五格式各 ≥10 条 golden（共 50）；边界（中文/emoji、CRLF、超长段落、
  表格、代码块、空文档、损坏文件）另列在 ``boundary_assets()``。

五格式的窄适配限制（与 ``simple_reader`` 的 docstring 对齐，不是伪造能力）：

* PDF：只覆盖本工厂写出的最小 PDF 子集（ASCII ``Tj`` + UTF-16BE 十六进制串）；
  真实 CJK PDF 需要字体 CMap/ToUnicode，本探针不做，故中文 PDF 用例只证明
  locator/golden 管线，不证明生产 PDF 能力。
* DOCX：只读 ``word/document.xml``，不处理页眉页脚/脚注/嵌套表格。
* HTML：只用 stdlib ``HTMLParser`` 取正文结构，不执行脚本、不做正文算法（如
  trafilatura），``script``/``style`` 内容一律丢弃。
* MD/TXT：窄语法子集；不支持的构造只告警不猜。
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape as _escape_xml

LICENSE = "LicenseRef-PyKnora-Phase0-TestFixture"
ORIGIN = "synthetic (generated in-process; not a real-world sample)"
FORMAT_ORDER = ("pdf", "docx", "md", "html", "txt")

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AssetSpec:
    """一份合成素材 + 来源元数据（证据里必须能追溯到构造函数）。"""

    asset_id: str
    format: str
    file_name: str
    content: bytes
    generator: str
    notes: str
    sha256: str
    byte_length: int
    expected_error: str = ""
    origin: str = ORIGIN
    license: str = LICENSE
    is_real_sample: bool = False


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """一条 golden：locator → 独立书写的期望片段。"""

    case_id: str
    asset_id: str
    format: str
    locator: dict[str, Any]
    expected_snippet: str
    note: str = ""

    @property
    def locator_kind(self) -> str:
        return str(self.locator["kind"])


def _asset(
    asset_id: str,
    fmt: str,
    file_name: str,
    content: bytes,
    *,
    generator: str,
    notes: str,
    expected_error: str = "",
) -> AssetSpec:
    return AssetSpec(
        asset_id=asset_id,
        format=fmt,
        file_name=file_name,
        content=content,
        generator=generator,
        notes=notes,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        expected_error=expected_error,
    )


# ---------------------------------------------------------------------------
# 共享文本（golden 的期望片段直接引用这些常量，确保与素材同源且可独立核对）
# ---------------------------------------------------------------------------
MD_TITLE = "PyKnora 解析指南"
MD_INTRO = "本文档用于验证 Markdown 窄适配与定位符。"
MD_INTRO_2 = "第二段介绍标题路径与代码块定位。"
MD_INSTALL = "安装"
MD_INSTALL_PARA = "按照以下步骤安装依赖："
MD_CODE = "pip install pyknora\npyknora --version"
MD_NOTE = "注意事项"
MD_NOTE_PARA = "注意代码块后的段落属于 H3 节。"
MD_TABLE_HEADING = "表格"
MD_TABLE_PARA = "下表列出格式支持："
MD_TABLE_ROWS: tuple[tuple[str, str], ...] = (
    ("格式", "定位符"),
    ("txt", "paragraph"),
    ("pdf", "page"),
)
MD_END = "结尾"
MD_END_PARA = "结尾段落包含 `行内代码` 与 **加粗** 标记。"

HTML_TITLE = "PyKnora HTML 样本"
HTML_PARA_1 = "第一段：HTML 正文抽取与标题路径定位。"
HTML_PARA_2 = "第二段：包含 emoji 📄 与实体 & 字符。"
HTML_INSTALL = "安装"
HTML_INSTALL_PARA = "按照以下步骤安装："
HTML_CODE = "pip install pyknora\npyknora --version"
HTML_NOTE = "注意"
HTML_NOTE_PARA = "代码块后的段落属于 H3 节。"
HTML_TABLE_HEADING = "表格"
HTML_TABLE_ROWS: tuple[tuple[str, str], ...] = (
    ("格式", "定位符"),
    ("txt", "paragraph"),
    ("pdf", "page"),
)
HTML_END_PARA = "表格之后的结尾段。"

DOCX_TITLE = "PyKnora DOCX 样本"
DOCX_PARAGRAPHS = (
    "第一段：DOCX 段落定位。",
    "第二段：包含中文与 emoji 📄。",
    "下面是一个 2x2 表格。",
    "表格之后的段落。",
)
DOCX_HEADING = "数据表"
DOCX_TABLE_ROWS: tuple[tuple[str, str], ...] = (("格式", "支持"), ("docx", "是"))

PDF_PAGES: tuple[tuple[str, ...], ...] = (
    ("PDF 第一页第一段。", "PDF page 1 paragraph 2."),
    ("PDF page 2 paragraph 1.", "PDF page 2 paragraph 2."),
    ("中文第三页：页面定位与段落序号。", "PDF page 3 paragraph 2."),
    ("PDF page 4 paragraph 1.", "PDF page 4 paragraph 2."),
    ("PDF page 5 paragraph 1.", "PDF page 5 paragraph 2."),
)

TXT_PARAGRAPHS: tuple[str, ...] = (
    "第一章 引言",
    "PyKnora Phase 0 用合成素材验证五格式解析与定位符映射。",
    "第二段：包含中文标点，例如「书名号」与（括号）。",
    "第三段：emoji 😀 与制表符\t保留为内部空白。",
    "The quick brown fox jumps over the lazy dog.",
    "第五段：CRLF 版本应与 LF 版本产生相同块。",
    "第六段：短段落。",
    '第七段：包含 "引号" 与反斜杠 \\ 字符。',
    "第八段：包含数字 1234567890 与符号 @#%&*。",
    "第九段：结尾段落。",
)

INSERTION_FIRST = "Alpha 段一。"
INSERTION_SECOND = "Beta 段二。"


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def build_txt_document(paragraphs: Sequence[str]) -> bytes:
    return ("\n\n".join(paragraphs) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def build_md_simple(title: str, paragraphs: Sequence[str]) -> bytes:
    lines = [f"# {title}", ""]
    for paragraph in paragraphs:
        lines.extend([paragraph, ""])
    return "\n".join(lines).encode("utf-8")


def build_md_rich(crlf: bool = False) -> bytes:
    rows = [
        "| " + " | ".join(MD_TABLE_ROWS[0]) + " |",
        "| " + " | ".join("---" for _ in MD_TABLE_ROWS[0]) + " |",
    ]
    rows.extend("| " + " | ".join(row) + " |" for row in MD_TABLE_ROWS[1:])
    lines = [
        f"# {MD_TITLE}",
        "",
        MD_INTRO,
        "",
        MD_INTRO_2,
        "",
        f"## {MD_INSTALL}",
        "",
        MD_INSTALL_PARA,
        "",
        "```python",
        *MD_CODE.split("\n"),
        "```",
        "",
        f"### {MD_NOTE}",
        "",
        MD_NOTE_PARA,
        "",
        f"## {MD_TABLE_HEADING}",
        "",
        MD_TABLE_PARA,
        "",
        *rows,
        "",
        f"## {MD_END}",
        "",
        MD_END_PARA,
        "",
    ]
    text = "\n".join(lines).rstrip("\n") + "\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def build_html_simple(title: str, paragraphs: Sequence[str]) -> bytes:
    body = "".join(f"<p>{_escape_xml(paragraph)}</p>" for paragraph in paragraphs)
    document = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
        f"<h1>{_escape_xml(title)}</h1>{body}</body></html>"
    )
    return document.encode("utf-8")


def build_html_rich(crlf: bool = False) -> bytes:
    header = "".join(f"<th>{_escape_xml(cell)}</th>" for cell in HTML_TABLE_ROWS[0])
    rows = "".join(
        "<tr>" + "".join(f"<td>{_escape_xml(cell)}</td>" for cell in row) + "</tr>"
        for row in HTML_TABLE_ROWS[1:]
    )
    document = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<title>页面标题（不应成为块）</title>\n"
        "<script>var secret = \"ignore-me\";</script>\n"
        "<style>.a{color:red}</style>\n</head>\n<body>\n"
        f"<h1>{HTML_TITLE}</h1>\n"
        f"<p>{HTML_PARA_1}</p>\n"
        f"<p>{HTML_PARA_2}</p>\n"
        f"<h2>{HTML_INSTALL}</h2>\n"
        f"<p>{HTML_INSTALL_PARA}</p>\n"
        f"<pre><code>{_escape_xml(HTML_CODE)}</code></pre>\n"
        f"<h3>{HTML_NOTE}</h3>\n"
        f"<p>{HTML_NOTE_PARA}</p>\n"
        f"<h2>{HTML_TABLE_HEADING}</h2>\n"
        f"<table>\n<thead><tr>{header}</tr></thead>\n<tbody>{rows}</tbody>\n</table>\n"
        f"<p>{HTML_END_PARA}</p>\n"
        "</body>\n</html>\n"
    )
    if crlf:
        document = document.replace("\n", "\r\n")
    return document.encode("utf-8")


# ---------------------------------------------------------------------------
# DOCX（确定性 zip 包）
# ---------------------------------------------------------------------------
_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" '
    b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" '
    b'ContentType="application/vnd.openxmlformats-officedocument.'
    b'wordprocessingml.document.main+xml"/>'
    b"</Types>"
)
_RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    b'<Relationship Id="rId1" '
    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    b'relationships/officeDocument" '
    b'Target="word/document.xml"/></Relationships>'
)


def _zip_bytes(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def _docx_paragraph_xml(text: str, style: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{_escape_xml(text)}</w:t></w:r></w:p>'


def _docx_table_xml(rows: Sequence[Sequence[str]]) -> str:
    body = ""
    for row in rows:
        cells = "".join(f"<w:tc>{_docx_paragraph_xml(cell)}</w:tc>" for cell in row)
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl>{body}</w:tbl>"


def _build_docx(body_xml: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body_xml}</w:body></w:document>'
    )
    return _zip_bytes(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _RELS,
            "word/document.xml": document.encode("utf-8"),
        }
    )


def build_docx_paragraphs(paragraphs: Sequence[str]) -> bytes:
    body = "".join(_docx_paragraph_xml(paragraph) for paragraph in paragraphs)
    return _build_docx(body)


def build_docx_rich() -> bytes:
    body = _docx_paragraph_xml(DOCX_TITLE, "Heading1")
    body += _docx_paragraph_xml(DOCX_PARAGRAPHS[0])
    body += _docx_paragraph_xml(DOCX_PARAGRAPHS[1])
    body += _docx_paragraph_xml(DOCX_HEADING, "Heading2")
    body += _docx_paragraph_xml(DOCX_PARAGRAPHS[2])
    body += _docx_table_xml(DOCX_TABLE_ROWS)
    body += _docx_paragraph_xml(DOCX_PARAGRAPHS[3])
    return _build_docx(body)


# ---------------------------------------------------------------------------
# PDF（最小、确定性的单节对象 + xref）
# ---------------------------------------------------------------------------
def _pdf_escape_literal(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def _pdf_page_stream(paragraphs: Sequence[str]) -> bytes:
    commands: list[str] = []
    y_position = 720
    for paragraph in paragraphs:
        if _pdf_is_ascii(paragraph):
            literal = _pdf_escape_literal(paragraph)
            commands.append(f"BT /F1 12 Tf 72 {y_position} Td ({literal}) Tj ET")
        else:
            payload = b"\xfe\xff" + paragraph.encode("utf-16-be")
            commands.append(f"BT /F2 12 Tf 72 {y_position} Td <{payload.hex().upper()}> Tj ET")
        y_position -= 24
    return ("\n".join(commands) + "\n").encode("ascii")


def _pdf_serialize(objects: dict[int, bytes]) -> bytes:
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    max_number = max(objects)
    xref_offset = len(output)
    output += f"xref\n0 {max_number + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for number in range(1, max_number + 1):
        if number in offsets:
            output += f"{offsets[number]:010d} 00000 n \n".encode()
        else:
            output += b"0000000000 65535 f \n"
    output += (
        f"trailer\n<< /Size {max_number + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(output)


def build_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    page_count = len(pages)
    font_helvetica = 3 + 2 * page_count
    font_type0 = font_helvetica + 1
    cid_font = font_helvetica + 2
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(page_count))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
    }
    for index, paragraphs in enumerate(pages):
        page_object = 3 + 2 * index
        content_object = page_object + 1
        stream = _pdf_page_stream(paragraphs)
        objects[page_object] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_helvetica} 0 R /F2 {font_type0} 0 R >> >> "
            f"/Contents {content_object} 0 R >>"
        ).encode()
        objects[content_object] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    objects[font_helvetica] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    objects[font_type0] = (
        f"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /Identity-H "
        f"/DescendantFonts [{cid_font} 0 R] >>"
    ).encode()
    objects[cid_font] = (
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/DW 1000 >>"
    )
    return _pdf_serialize(objects)


# ---------------------------------------------------------------------------
# 五格式 golden 素材
# ---------------------------------------------------------------------------
def build_golden_assets() -> tuple[AssetSpec, ...]:
    return (
        _asset(
            "pdf-golden",
            "pdf",
            "pdf-golden.pdf",
            build_pdf(PDF_PAGES),
            generator="build_pdf(PDF_PAGES)",
            notes="5 页 × 2 段；第 1/3 页含中文（UTF-16BE 十六进制串，探针假设）",
        ),
        _asset(
            "docx-golden",
            "docx",
            "docx-golden.docx",
            build_docx_rich(),
            generator="build_docx_rich()",
            notes="Heading1/Heading2 + 4 正文段 + 2x2 表格",
        ),
        _asset(
            "md-golden",
            "md",
            "md-golden.md",
            build_md_rich(),
            generator="build_md_rich()",
            notes="H1/H2/H3 + 围栏代码 + 3x2 管道表格",
        ),
        _asset(
            "html-golden",
            "html",
            "html-golden.html",
            build_html_rich(),
            generator="build_html_rich()",
            notes="h1/h2/h3 + pre/code + 3x2 表格 + script/style 干扰",
        ),
        _asset(
            "txt-golden",
            "txt",
            "txt-golden.txt",
            build_txt_document(TXT_PARAGRAPHS),
            generator="build_txt_document(TXT_PARAGRAPHS)",
            notes="10 段（含中文/emoji/引号/反斜杠/制表符）",
        ),
    )


# ---------------------------------------------------------------------------
# 50 条 golden（每格式 10 条；期望片段独立于解析器书写）
# ---------------------------------------------------------------------------
def _page(page: int, paragraph: int) -> dict[str, Any]:
    return {"schema": 1, "format": "pdf", "kind": "page", "page": page, "paragraph": paragraph}


def _paragraph(fmt: str, index: int) -> dict[str, Any]:
    return {"schema": 1, "format": fmt, "kind": "paragraph", "paragraph": index}


def _heading(fmt: str, path: Sequence[str], paragraph: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": 1,
        "format": fmt,
        "kind": "heading",
        "heading_path": list(path),
    }
    if paragraph is not None:
        data["paragraph"] = paragraph
    return data


def _code(fmt: str, path: Sequence[str], index: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "format": fmt,
        "kind": "code_block",
        "heading_path": list(path),
        "code_block": index,
    }


def _cell(
    fmt: str, path: Sequence[str], table: int, row: int, col: int
) -> dict[str, Any]:
    return {
        "schema": 1,
        "format": fmt,
        "kind": "table_cell",
        "heading_path": list(path),
        "table": table,
        "row": row,
        "col": col,
    }


def build_golden_cases() -> tuple[GoldenCase, ...]:
    cases: list[GoldenCase] = []

    def add(
        asset_id: str,
        fmt: str,
        locator: dict[str, Any],
        expected: str,
        note: str = "",
    ) -> None:
        index = sum(1 for case in cases if case.asset_id == asset_id) + 1
        cases.append(
            GoldenCase(
                case_id=f"{asset_id}-{index:02d}",
                asset_id=asset_id,
                format=fmt,
                locator=locator,
                expected_snippet=expected,
                note=note,
            )
        )

    # PDF：页内段落（page 从 1 开始）
    for page_index, paragraphs in enumerate(PDF_PAGES, start=1):
        for paragraph_index, text in enumerate(paragraphs):
            add("pdf-golden", "pdf", _page(page_index, paragraph_index), text)

    # DOCX：文档级段落 + 标题路径 + 表格单元格
    add("docx-golden", "docx", _paragraph("docx", 0), DOCX_PARAGRAPHS[0])
    add("docx-golden", "docx", _paragraph("docx", 1), DOCX_PARAGRAPHS[1])
    add("docx-golden", "docx", _paragraph("docx", 2), DOCX_PARAGRAPHS[2])
    add("docx-golden", "docx", _paragraph("docx", 3), DOCX_PARAGRAPHS[3])
    add("docx-golden", "docx", _heading("docx", [DOCX_TITLE]), DOCX_TITLE)
    add("docx-golden", "docx", _heading("docx", [DOCX_TITLE, DOCX_HEADING]), DOCX_HEADING)
    add("docx-golden", "docx", _heading("docx", [DOCX_TITLE], 0), DOCX_PARAGRAPHS[0])
    add(
        "docx-golden",
        "docx",
        _heading("docx", [DOCX_TITLE, DOCX_HEADING], 0),
        DOCX_PARAGRAPHS[2],
    )
    add("docx-golden", "docx", _cell("docx", [DOCX_TITLE, DOCX_HEADING], 0, 0, 0), "格式")
    add("docx-golden", "docx", _cell("docx", [DOCX_TITLE, DOCX_HEADING], 0, 0, 1), "支持")

    # MD：标题路径 + 节内段落 + 代码块 + 表格
    add("md-golden", "md", _heading("md", [MD_TITLE]), MD_TITLE)
    add("md-golden", "md", _heading("md", [MD_TITLE], 0), MD_INTRO)
    add("md-golden", "md", _heading("md", [MD_TITLE], 1), MD_INTRO_2)
    add("md-golden", "md", _heading("md", [MD_TITLE, MD_INSTALL]), MD_INSTALL)
    add("md-golden", "md", _heading("md", [MD_TITLE, MD_INSTALL], 0), MD_INSTALL_PARA)
    add("md-golden", "md", _code("md", [MD_TITLE, MD_INSTALL], 0), MD_CODE)
    add(
        "md-golden",
        "md",
        _heading("md", [MD_TITLE, MD_INSTALL, MD_NOTE], 0),
        MD_NOTE_PARA,
    )
    add("md-golden", "md", _heading("md", [MD_TITLE, MD_TABLE_HEADING], 0), MD_TABLE_PARA)
    add(
        "md-golden",
        "md",
        _cell("md", [MD_TITLE, MD_TABLE_HEADING], 0, 0, 1),
        MD_TABLE_ROWS[0][1],
    )
    add("md-golden", "md", _heading("md", [MD_TITLE, MD_END], 0), MD_END_PARA)

    # HTML：标题路径 + 正文段 + 代码块 + 表格单元格
    add("html-golden", "html", _heading("html", [HTML_TITLE]), HTML_TITLE)
    add("html-golden", "html", _heading("html", [HTML_TITLE], 0), HTML_PARA_1)
    add("html-golden", "html", _heading("html", [HTML_TITLE], 1), HTML_PARA_2)
    add("html-golden", "html", _heading("html", [HTML_TITLE, HTML_INSTALL]), HTML_INSTALL)
    add(
        "html-golden",
        "html",
        _heading("html", [HTML_TITLE, HTML_INSTALL], 0),
        HTML_INSTALL_PARA,
    )
    add("html-golden", "html", _code("html", [HTML_TITLE, HTML_INSTALL], 0), HTML_CODE)
    add(
        "html-golden",
        "html",
        _heading("html", [HTML_TITLE, HTML_INSTALL, HTML_NOTE], 0),
        HTML_NOTE_PARA,
    )
    add(
        "html-golden",
        "html",
        _heading("html", [HTML_TITLE, HTML_TABLE_HEADING], 0),
        HTML_END_PARA,
    )
    add(
        "html-golden",
        "html",
        _cell("html", [HTML_TITLE, HTML_TABLE_HEADING], 0, 0, 1),
        HTML_TABLE_ROWS[0][1],
    )
    add(
        "html-golden",
        "html",
        _cell("html", [HTML_TITLE, HTML_TABLE_HEADING], 0, 2, 0),
        HTML_TABLE_ROWS[2][0],
    )

    # TXT：文档级段落
    for index, text in enumerate(TXT_PARAGRAPHS):
        add("txt-golden", "txt", _paragraph("txt", index), text)

    return tuple(cases)


# ---------------------------------------------------------------------------
# 边界素材（空 / 无可见文本 / 损坏）
# ---------------------------------------------------------------------------
def empty_assets() -> tuple[AssetSpec, ...]:
    return tuple(
        _asset(
            f"{fmt}-empty",
            fmt,
            f"empty.{fmt}",
            b"",
            generator="b\"\"",
            notes="零字节输入",
            expected_error="empty document",
        )
        for fmt in FORMAT_ORDER
    )


def no_visible_text_assets() -> tuple[AssetSpec, ...]:
    return (
        _asset(
            "txt-whitespace",
            "txt",
            "whitespace.txt",
            b" \r\n\t\r\n ",
            generator="b\" \\r\\n\\t\\r\\n \"",
            notes="只有空白字符",
            expected_error="no visible text",
        ),
        _asset(
            "md-whitespace",
            "md",
            "whitespace.md",
            b"\n\n   \n\t\n",
            generator="b\"\\n\\n   \\n\\t\\n\"",
            notes="只有空白行",
            expected_error="no visible text",
        ),
        _asset(
            "html-no-text",
            "html",
            "no-text.html",
            b"<html><body><script>x</script><style>y</style></body></html>",
            generator="script/style-only HTML",
            notes="脚本/样式被丢弃后没有可见文本",
            expected_error="no visible text",
        ),
        _asset(
            "docx-empty-body",
            "docx",
            "empty-body.docx",
            build_docx_paragraphs([""]),
            generator="build_docx_paragraphs([\"\"])",
            notes="只有一个空段落",
            expected_error="no visible text",
        ),
        _asset(
            "pdf-no-text",
            "pdf",
            "no-text.pdf",
            build_pdf([[]]),
            generator="build_pdf([[]])",
            notes="页面内容流没有文本操作",
            expected_error="no visible text",
        ),
    )


def corrupt_assets() -> tuple[AssetSpec, ...]:
    pdf = build_pdf(PDF_PAGES)
    docx = build_docx_rich()
    pdf_missing_eof = pdf[: pdf.rindex(b"%%EOF")]
    pdf_bad_length = re.sub(
        rb"/Length (\d+)",
        lambda match: f"/Length {int(match.group(1)) + 7}".encode(),
        pdf,
        count=1,
    )
    truncated_docx = docx[: len(docx) // 2]
    malformed_docx = _zip_bytes(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _RELS,
            "word/document.xml": b"<w:document><w:body>",
        }
    )
    missing_document_docx = _zip_bytes(
        {"[Content_Types].xml": _CONTENT_TYPES, "_rels/.rels": _RELS}
    )
    return (
        _asset(
            "pdf-bad-header",
            "pdf",
            "bad-header.pdf",
            b"NOTPDF\n" + pdf[8:],
            generator="valid PDF with corrupted header",
            notes="魔数被替换",
            expected_error="missing %PDF header",
        ),
        _asset(
            "pdf-missing-eof",
            "pdf",
            "missing-eof.pdf",
            pdf_missing_eof,
            generator="valid PDF cut before %%EOF",
            notes="尾部被截断",
            expected_error="missing %%EOF",
        ),
        _asset(
            "pdf-bad-length",
            "pdf",
            "bad-length.pdf",
            pdf_bad_length,
            generator="valid PDF with /Length + 7",
            notes="流长度与声明不一致",
            expected_error="stream length mismatch",
        ),
        _asset(
            "docx-truncated",
            "docx",
            "truncated.docx",
            truncated_docx,
            generator="valid DOCX cut in half",
            notes="zip 尾部缺失",
            expected_error="invalid zip",
        ),
        _asset(
            "docx-missing-document",
            "docx",
            "missing-document.docx",
            missing_document_docx,
            generator="zip without word/document.xml",
            notes="缺主文档部件",
            expected_error="missing word/document.xml",
        ),
        _asset(
            "docx-malformed-xml",
            "docx",
            "malformed.docx",
            malformed_docx,
            generator="zip with unclosed document.xml",
            notes="XML 不合法",
            expected_error="malformed word/document.xml",
        ),
        _asset(
            "txt-invalid-utf8",
            "txt",
            "invalid.txt",
            b"hello \xff\xfe world",
            generator="b\"hello \\xff\\xfe world\"",
            notes="非法 UTF-8 字节",
            expected_error="invalid UTF-8",
        ),
        _asset(
            "md-invalid-utf8",
            "md",
            "invalid.md",
            b"# title\n\n\xff\xfe\n",
            generator="b\"# title\\n\\n\\xff\\xfe\\n\"",
            notes="非法 UTF-8 字节",
            expected_error="invalid UTF-8",
        ),
        _asset(
            "html-invalid-utf8",
            "html",
            "invalid.html",
            b"<html><body><p>\xff\xfe</p></body></html>",
            generator="invalid UTF-8 bytes inside HTML",
            notes="非法 UTF-8 字节",
            expected_error="invalid UTF-8",
        ),
    )


# ---------------------------------------------------------------------------
# 边界正例：CRLF / 超长段落 / 代码块 / 锯齿表格
# ---------------------------------------------------------------------------
LONG_PARAGRAPH = "长段落" + "0123456789" * 500 + "尾巴"


def edge_assets() -> tuple[AssetSpec, ...]:
    return (
        _asset(
            "txt-crlf",
            "txt",
            "crlf.txt",
            build_txt_document(TXT_PARAGRAPHS).replace(b"\n", b"\r\n"),
            generator="TXT with CRLF",
            notes="与 txt-golden 相同内容，仅换行符不同",
        ),
        _asset(
            "md-crlf",
            "md",
            "crlf.md",
            build_md_rich(crlf=True),
            generator="build_md_rich(crlf=True)",
            notes="与 md-golden 相同结构，仅换行符不同",
        ),
        _asset(
            "html-crlf",
            "html",
            "crlf.html",
            build_html_rich(crlf=True),
            generator="build_html_rich(crlf=True)",
            notes="与 html-golden 相同结构，仅换行符不同",
        ),
        _asset(
            "txt-long",
            "txt",
            "long.txt",
            build_txt_document([LONG_PARAGRAPH, "短段落。"]),
            generator="build_txt_document([LONG_PARAGRAPH, ...])",
            notes=f"单段 {len(LONG_PARAGRAPH)} 字符，用于验证不截断",
        ),
        _asset(
            "md-code-backticks",
            "md",
            "code-backticks.md",
            "# 代码\n\n```\nprint(`backtick`)\n```\n".encode(),
            generator="fenced code containing backticks",
            notes="代码块内的反引号不能被当作围栏",
        ),
        _asset(
            "md-table-jagged",
            "md",
            "jagged.md",
            "# 表格\n\n| a | b |\n| --- | --- |\n| 1 |\n| 2 | 3 | 4 |\n".encode(),
            generator="markdown with jagged table rows",
            notes="行宽不一致：应告警并补空单元格，而不是崩溃",
        ),
    )


# ---------------------------------------------------------------------------
# 单字符插入对（证明 locator 不依赖绝对偏移）
# ---------------------------------------------------------------------------
def insertion_pairs() -> dict[str, tuple[AssetSpec, AssetSpec]]:
    """每种格式返回 (原始, 在首段开头插入 ``Z`` 的变体)。"""
    original_paragraphs = (INSERTION_FIRST, INSERTION_SECOND)
    inserted_paragraphs = ("Z" + INSERTION_FIRST, INSERTION_SECOND)
    pairs: dict[str, tuple[AssetSpec, AssetSpec]] = {}
    for fmt in FORMAT_ORDER:
        if fmt == "txt":
            original = build_txt_document(original_paragraphs)
            inserted = build_txt_document(inserted_paragraphs)
        elif fmt == "md":
            original = build_md_simple("插入标记", original_paragraphs)
            inserted = build_md_simple("插入标记", inserted_paragraphs)
        elif fmt == "html":
            original = build_html_simple("插入标记", original_paragraphs)
            inserted = build_html_simple("插入标记", inserted_paragraphs)
        elif fmt == "docx":
            original = build_docx_paragraphs(original_paragraphs)
            inserted = build_docx_paragraphs(inserted_paragraphs)
        else:
            original = build_pdf([original_paragraphs])
            inserted = build_pdf([inserted_paragraphs])
        pairs[fmt] = (
            _asset(
                f"{fmt}-insertion-base",
                fmt,
                f"insertion.{fmt}",
                original,
                generator=f"insertion_pairs()[{fmt}][0]",
                notes="插入测试基线",
            ),
            _asset(
                f"{fmt}-insertion-variant",
                fmt,
                f"insertion.{fmt}",
                inserted,
                generator=f"insertion_pairs()[{fmt}][1]",
                notes="首段开头插入 Z",
            ),
        )
    return pairs


def corpus_digest() -> str:
    """五份 golden 素材整体 sha256（证据里记录，便于复现核对）。"""
    digest = hashlib.sha256()
    for asset in build_golden_assets():
        digest.update(asset.sha256.encode())
    return digest.hexdigest()
