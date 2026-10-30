"""分块：把解析后的文本切成带重叠的检索单元。

设计取舍：

* 先按段落聚合到目标长度，段落本身超长才硬切 —— 段落边界比任意字符边界更语义化；
* 硬切时按句子（``。！？；.!?;`` 与换行）找落点，找不到再退化为定长；
* 块之间保留 ``overlap`` 字符重叠，避免跨块语义被截断；
* ``\\f`` 分页符换算成页码，PDF 引用可以指到页。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pyknora.ingest.parsers import PAGE_BREAK

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])\s*|\n+")


@dataclass(frozen=True, slots=True)
class TextChunk:
    ordinal: int
    content: str
    char_count: int
    page: int | None


def _split_paragraphs(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text)]
    return [block for block in blocks if block]


def _split_long_paragraph(paragraph: str, size: int) -> list[str]:
    """把超长段落按句子切成不超过 ``size`` 的片段。"""

    pieces: list[str] = []
    buffer = ""
    for part in _SENTENCE_END.split(paragraph):
        if not part:
            continue
        candidate = f"{buffer}{part}"
        if len(candidate) <= size:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        while len(part) > size:  # 单句仍超长：定长切
            pieces.append(part[:size])
            part = part[size:]
        buffer = part
    if buffer:
        pieces.append(buffer)
    return pieces


def _tail_overlap(text: str, overlap: int) -> str:
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    # 从词/句边界起步，避免把句子切一半带进下一块
    match = re.search(r"[。！？；!?;\n]", tail)
    if match and match.end() < len(tail):
        return tail[match.end() :].lstrip()
    return tail.lstrip()


def split_text(
    text: str,
    *,
    size: int = 800,
    overlap: int = 120,
    max_chunks: int = 2000,
) -> list[TextChunk]:
    """按页与段落切块；返回的 ``ordinal`` 从 0 连续递增。"""

    if size <= 0:
        raise ValueError("size 必须为正数")
    if overlap >= size:
        raise ValueError("overlap 必须小于 size")

    chunks: list[TextChunk] = []
    for page_index, page_text in enumerate(text.split(PAGE_BREAK), start=1):
        if not page_text.strip():
            continue
        carry = ""
        buffer = ""
        for paragraph in _split_paragraphs(page_text):
            segments = (
                [paragraph]
                if len(paragraph) <= size
                else _split_long_paragraph(paragraph, size=max(size - overlap, 1))
            )
            for segment in segments:
                candidate = f"{buffer}\n\n{segment}" if buffer else segment
                if len(candidate) <= size:
                    buffer = candidate
                    continue
                if buffer:
                    chunks.append(
                        TextChunk(
                            ordinal=len(chunks),
                            content=buffer.strip(),
                            char_count=len(buffer.strip()),
                            page=page_index,
                        )
                    )
                    carry = _tail_overlap(buffer.strip(), overlap)
                buffer = f"{carry}\n\n{segment}".strip() if carry else segment
        if buffer.strip():
            chunks.append(
                TextChunk(
                    ordinal=len(chunks),
                    content=buffer.strip(),
                    char_count=len(buffer.strip()),
                    page=page_index,
                )
            )
        if len(chunks) >= max_chunks:
            return chunks[:max_chunks]

    return chunks


def find_page_for_offset(text: str, offset: int) -> int:
    """工具函数：按字符偏移反查页码（引用定位用）。"""

    return text.count(PAGE_BREAK, 0, offset) + 1
