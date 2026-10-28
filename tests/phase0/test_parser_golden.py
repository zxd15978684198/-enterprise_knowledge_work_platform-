"""P0-T11 五格式 golden、定位符映射与边界证据（真实测试，无 skip/xfail）。

运行（必须带 ``-c``，且不需要 ``--with``：依赖已进实验 venv）：

    uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
        tests/phase0/test_parser_golden.py -q

验收映射（Lead 收口口径）：

1. 五格式（pdf/docx/md/html/txt）各 ≥10 条 golden → ``test_00``、``test_02``；
   定位符→原文片段命中率 ≥49/50 → ``test_03``（本语料实测 50/50）。
2. 定位符不依赖绝对字符偏移；覆盖页/段/标题路径/表格单元格/代码块 →
   ``test_04``（schema 拒绝偏移字段）、``test_05``（插入一个字符后仍命中）、
   ``test_06``（反证：绝对偏移方案在插入后必然失效）、``test_07``。
3. 边界：UTF-8 中文/emoji、CRLF、超长段落、表格、代码块、空文档、损坏文件
   （必须明确失败而非静默返回空）→ ``test_08``–``test_16``。
4. 与 P0-T10 的 Read/ReadStream 错误语义对齐：失败只落 ``error`` 字段、不抛异常；
   ``test_17`` 直接跨引用 T10 的 proto 字段号与 ``wire_evidence.json`` 实测。
5. C-10：只做 locator 结构，不实现 ``asset_bindings``/版本发布 →
   ``test_18`` 用字段白名单守住边界。

素材来源（证据必须带此声明）：**全部为合成素材**，由
``experiments/phase0/parsing_assets.py`` 在本地确定性生成，``is_real_sample=False``；
不是真实世界样本，不能外推为「真实 PDF/DOCX 解析质量」。素材许可
``LicenseRef-PyKnora-Phase0-TestFixture``（Phase 0 自产载荷）。

不声称：真实 DocReader 镜像（T09 digest/许可 UNKNOWN）未参与；PDF 只覆盖探针
最小子集（ASCII Tj + UTF-16BE 十六进制），真实 CJK PDF 需要字体 CMap/ToUnicode。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import fields
from pathlib import Path

import locator_map
import parsing_assets
import pytest
import simple_reader

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
T10_EVIDENCE = PHASE0_DIR / "docreader_probe" / "wire_evidence.json"

GOLDEN_ASSETS: dict[str, parsing_assets.AssetSpec] = {
    asset.asset_id: asset for asset in parsing_assets.build_golden_assets()
}
GOLDEN_CASES: tuple[parsing_assets.GoldenCase, ...] = parsing_assets.build_golden_cases()
GOLDEN_IDS = [case.case_id for case in GOLDEN_CASES]

_OFFSET_KEYS = {
    "offset",
    "offsets",
    "char_offset",
    "start",
    "end",
    "span",
    "span_start",
    "span_end",
    "byte_offset",
}


def _parse(asset: parsing_assets.AssetSpec) -> simple_reader.ParseResult:
    return simple_reader.read_document(
        asset.content, file_name=asset.file_name, file_type=asset.format
    )


def _resolve(asset: parsing_assets.AssetSpec, locator: locator_map.Locator):
    return locator_map.resolve_in_source(
        asset.content,
        file_name=asset.file_name,
        file_type=asset.format,
        locator=locator,
    )


# ---------------------------------------------------------------------------
# 语料与 golden 覆盖
# ---------------------------------------------------------------------------
def test_00_corpus_covers_five_formats_with_ten_golden_each() -> None:
    counts = {fmt: 0 for fmt in parsing_assets.FORMAT_ORDER}
    for case in GOLDEN_CASES:
        counts[case.format] += 1
    assert counts == dict.fromkeys(parsing_assets.FORMAT_ORDER, 10), counts
    assert len(GOLDEN_CASES) == 50
    assert len({case.case_id for case in GOLDEN_CASES}) == 50
    assert set(GOLDEN_ASSETS) == {case.asset_id for case in GOLDEN_CASES}

    for asset in GOLDEN_ASSETS.values():
        assert asset.format in parsing_assets.FORMAT_ORDER
        assert asset.is_real_sample is False, "合成素材不得标记成真实样本"
        assert asset.origin.startswith("synthetic"), asset.origin
        assert asset.license == "LicenseRef-PyKnora-Phase0-TestFixture"
        assert asset.generator and asset.notes
        assert len(asset.sha256) == 64
        assert asset.byte_length == len(asset.content) > 0
        assert hashlib.sha256(asset.content).hexdigest() == asset.sha256


def test_01_golden_assets_are_byte_deterministic() -> None:
    first = {asset.asset_id: asset.sha256 for asset in parsing_assets.build_golden_assets()}
    second = {asset.asset_id: asset.sha256 for asset in parsing_assets.build_golden_assets()}
    assert first == second
    assert parsing_assets.corpus_digest() == parsing_assets.corpus_digest()
    # 单独复核 docx/pdf：它们含 zip/对象偏移，最容易因时间戳或顺序漂移。
    docx = parsing_assets.build_docx_rich()
    assert docx == parsing_assets.build_docx_rich()
    pdf = parsing_assets.build_pdf(parsing_assets.PDF_PAGES)
    assert pdf == parsing_assets.build_pdf(parsing_assets.PDF_PAGES)
    assert b"/CreationDate" not in pdf and b"/ID" not in docx


# ---------------------------------------------------------------------------
# golden：locator → 原文片段（两条独立路径）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", GOLDEN_CASES, ids=GOLDEN_IDS)
def test_02_golden_locator_hits_expected_snippet(case: parsing_assets.GoldenCase) -> None:
    asset = GOLDEN_ASSETS[case.asset_id]
    locator = locator_map.Locator.from_dict(case.locator)
    span = _resolve(asset, locator)
    assert span is not None, f"{case.case_id} 未命中：{case.locator}"
    assert span.text == case.expected_snippet, (
        f"{case.case_id} 片段不符\n期望: {case.expected_snippet!r}\n实际: {span.text!r}"
    )
    result = _parse(asset)
    assert result.ok, result.error
    assert result.text[span.start : span.end] == case.expected_snippet


def test_03_golden_hit_rate_meets_threshold() -> None:
    hits_by_format = {fmt: 0 for fmt in parsing_assets.FORMAT_ORDER}
    totals_by_format = {fmt: 0 for fmt in parsing_assets.FORMAT_ORDER}
    misses: list[str] = []
    parsed = {asset_id: _parse(asset) for asset_id, asset in GOLDEN_ASSETS.items()}
    for case in GOLDEN_CASES:
        totals_by_format[case.format] += 1
        result = parsed[case.asset_id]
        assert result.ok, result.error
        locator = locator_map.Locator.from_dict(case.locator)
        mapped = locator_map.LocatorMap.from_result(result).resolve_text(locator)
        if mapped == case.expected_snippet:
            hits_by_format[case.format] += 1
        else:
            misses.append(f"{case.case_id}: expected {case.expected_snippet!r} got {mapped!r}")

    hits = sum(hits_by_format.values())
    total = sum(totals_by_format.values())
    assert total == 50
    assert not misses, "\n".join(misses)
    assert hits >= 49, f"命中率不足：{hits}/{total}（阈值 ≥49/50）"
    for fmt, count in totals_by_format.items():
        assert hits_by_format[fmt] >= 9, f"{fmt} 仅命中 {hits_by_format[fmt]}/{count}（阈值 ≥9/10）"


# ---------------------------------------------------------------------------
# 定位符结构：offset-free、稳定、可降级
# ---------------------------------------------------------------------------
def test_04_locators_are_offset_free_and_schema_rejects_offsets() -> None:
    for case in GOLDEN_CASES:
        keys = {key.lower() for key in case.locator}
        poisoned_keys = keys & _OFFSET_KEYS
        assert not poisoned_keys, f"{case.case_id} 的 locator 含偏移字段：{poisoned_keys}"
        locator = locator_map.Locator.from_dict(case.locator)
        assert locator.to_dict() == case.locator

    locator_fields = locator_map.locator_fields()
    assert not locator_fields & _OFFSET_KEYS, locator_fields

    valid = {"schema": 1, "format": "txt", "kind": "paragraph", "paragraph": 0}
    for poisoned in (
        {**valid, "offset": 3},
        {**valid, "start": 0},
        {**valid, "char_offset": 7},
        {**valid, "span": [0, 1]},
    ):
        with pytest.raises(locator_map.LocatorError):
            locator_map.Locator.from_dict(poisoned)

    with pytest.raises(locator_map.LocatorError):
        locator_map.Locator.from_dict({**valid, "kind": "char_range"})
    with pytest.raises(locator_map.LocatorError):
        locator_map.Locator(kind="page", format="pdf")  # 缺 page/paragraph


def test_05_inserting_one_character_preserves_structural_locators() -> None:
    for fmt, (base, variant) in parsing_assets.insertion_pairs().items():
        base_result = _parse(base)
        variant_result = _parse(variant)
        assert base_result.ok, (fmt, base_result.error)
        assert variant_result.ok, (fmt, variant_result.error)
        base_map = locator_map.LocatorMap.from_result(base_result)
        variant_map = locator_map.LocatorMap.from_result(variant_result)

        changed_blocks = 0
        for block in base_result.blocks:
            span = base_map.resolve(block.locator)
            if span is None:
                continue
            variant_text = variant_map.resolve_text(block.locator)
            if span.text == parsing_assets.INSERTION_FIRST:
                assert variant_text == "Z" + parsing_assets.INSERTION_FIRST, (
                    f"{fmt}: 插入字符后 locator 应跟着内容走，而不是跳位"
                )
                changed_blocks += 1
            else:
                assert variant_text == span.text, (fmt, block.locator.to_dict(), variant_text)
        assert changed_blocks == 1, fmt


def test_06_absolute_offset_would_break_after_insertion() -> None:
    pairs = parsing_assets.insertion_pairs()
    checks = 0
    for fmt in ("txt", "md", "html", "docx", "pdf"):
        base, variant = pairs[fmt]
        base_text = _parse(base).text
        variant_text = _parse(variant).text
        old_offset = base_text.index(parsing_assets.INSERTION_FIRST)
        length = len(parsing_assets.INSERTION_FIRST)
        assert base_text[old_offset : old_offset + length] == parsing_assets.INSERTION_FIRST
        shifted = variant_text[old_offset : old_offset + length]
        assert shifted != parsing_assets.INSERTION_FIRST, (
            f"{fmt}: 绝对偏移在插入一个字符后居然还命中，说明反证前提不成立"
        )
        checks += 1
    assert checks == 5


def test_07_golden_covers_page_paragraph_heading_table_and_code_locators() -> None:
    kinds = {case.locator_kind for case in GOLDEN_CASES}
    assert kinds == {"page", "paragraph", "heading", "table_cell", "code_block"}, kinds
    stable_structural = {"page", "paragraph", "heading", "table_cell"}
    assert len(kinds & stable_structural) >= 2
    per_kind = {
        kind: sum(1 for case in GOLDEN_CASES if case.locator_kind == kind) for kind in kinds
    }
    assert per_kind["page"] >= 10
    assert per_kind["paragraph"] >= 10
    assert per_kind["heading"] >= 10
    assert per_kind["table_cell"] >= 4
    assert per_kind["code_block"] >= 2


# ---------------------------------------------------------------------------
# 边界：中文/emoji、CRLF、超长、表格、代码块、空、损坏
# ---------------------------------------------------------------------------
def test_08_utf8_chinese_and_emoji_roundtrip() -> None:
    emoji = "😀"
    assert len(emoji) == 1, "Python str 按 code point 计数，emoji 应为 1"
    emoji_case = next(
        case
        for case in GOLDEN_CASES
        if case.format == "txt" and emoji in case.expected_snippet
    )
    emoji_locator = locator_map.Locator.from_dict(emoji_case.locator)
    span = _resolve(GOLDEN_ASSETS[emoji_case.asset_id], emoji_locator)
    assert span is not None
    assert span.text == emoji_case.expected_snippet
    assert span.end - span.start == len(emoji_case.expected_snippet)
    result = _parse(GOLDEN_ASSETS[emoji_case.asset_id])
    assert result.text[span.start : span.end] == emoji_case.expected_snippet

    cjk_text = "中文第三页：页面定位与段落序号。"
    pdf_case = next(
        case for case in GOLDEN_CASES if case.expected_snippet == cjk_text
    )
    pdf_span = _resolve(
        GOLDEN_ASSETS[pdf_case.asset_id], locator_map.Locator.from_dict(pdf_case.locator)
    )
    assert pdf_span is not None and pdf_span.text == cjk_text

    for fmt in parsing_assets.FORMAT_ORDER:
        assert any(
            not case.expected_snippet.isascii() for case in GOLDEN_CASES if case.format == fmt
        ), f"{fmt} 缺少中文 golden"


def test_09_crlf_and_lf_produce_identical_blocks() -> None:
    edge = {asset.asset_id: asset for asset in parsing_assets.edge_assets()}
    pairs = (("txt-golden", "txt-crlf"), ("md-golden", "md-crlf"), ("html-golden", "html-crlf"))
    for lf_id, crlf_id in pairs:
        lf_asset, crlf_asset = GOLDEN_ASSETS[lf_id], edge[crlf_id]
        lf_result, crlf_result = _parse(lf_asset), _parse(crlf_asset)
        assert lf_result.ok and crlf_result.ok, (lf_result.error, crlf_result.error)
        lf_blocks = [
            (block.kind, block.text, block.locator.to_dict()) for block in lf_result.blocks
        ]
        crlf_blocks = [
            (block.kind, block.text, block.locator.to_dict()) for block in crlf_result.blocks
        ]
        assert lf_blocks == crlf_blocks, f"{crlf_id} 与 LF 版本块不一致"
        assert all("\r" not in block.text for block in crlf_result.blocks)
        assert "\r" not in crlf_result.text


def test_10_long_paragraph_is_not_truncated() -> None:
    edge = {asset.asset_id: asset for asset in parsing_assets.edge_assets()}
    asset = edge["txt-long"]
    result = _parse(asset)
    assert result.ok, result.error
    assert result.blocks[0].text == parsing_assets.LONG_PARAGRAPH
    assert len(result.blocks[0].text) == len(parsing_assets.LONG_PARAGRAPH) > 5000
    locator = result.blocks[0].locator
    assert locator_map.LocatorMap.from_result(result).resolve_text(locator) == result.blocks[0].text
    # 超长段落不得把 block 数量或后续段落吞掉
    assert result.blocks[1].text == "短段落。"


def test_11_table_cell_locators_resolve_exact_cells() -> None:
    table_cases = [case for case in GOLDEN_CASES if case.locator_kind == "table_cell"]
    assert len(table_cases) >= 4
    for case in table_cases:
        span = _resolve(GOLDEN_ASSETS[case.asset_id], locator_map.Locator.from_dict(case.locator))
        assert span is not None, case.case_id
        assert span.text == case.expected_snippet, (case.case_id, span.text)

    md_map = locator_map.LocatorMap.from_result(_parse(GOLDEN_ASSETS["md-golden"]))
    absent = locator_map.Locator.from_dict(
        {
            "schema": 1,
            "format": "md",
            "kind": "table_cell",
            "heading_path": [parsing_assets.MD_TITLE, parsing_assets.MD_TABLE_HEADING],
            "table": 0,
            "row": 99,
            "col": 0,
        }
    )
    assert md_map.resolve(absent) is None, "越界的表格坐标必须显式降级，不能伪造单元格"

    edge = {asset.asset_id: asset for asset in parsing_assets.edge_assets()}
    jagged = locator_map.LocatorMap.from_result(_parse(edge["md-table-jagged"]))
    padded = locator_map.Locator.from_dict(
        {
            "schema": 1,
            "format": "md",
            "kind": "table_cell",
            "heading_path": ["表格"],
            "table": 0,
            "row": 1,
            "col": 1,
        }
    )
    assert jagged.resolve_text(padded) == ""
    wide = locator_map.Locator.from_dict(
        {
            "schema": 1,
            "format": "md",
            "kind": "table_cell",
            "heading_path": ["表格"],
            "table": 0,
            "row": 2,
            "col": 2,
        }
    )
    assert jagged.resolve_text(wide) == "4"


def test_12_code_block_locators_preserve_verbatim_text() -> None:
    code_cases = [case for case in GOLDEN_CASES if case.locator_kind == "code_block"]
    assert len(code_cases) >= 2
    for case in code_cases:
        span = _resolve(GOLDEN_ASSETS[case.asset_id], locator_map.Locator.from_dict(case.locator))
        assert span is not None, case.case_id
        assert span.text == case.expected_snippet
        assert "\n" in span.text

    edge = {asset.asset_id: asset for asset in parsing_assets.edge_assets()}
    result = _parse(edge["md-code-backticks"])
    assert result.ok, result.error
    locator = locator_map.Locator.from_dict(
        {
            "schema": 1,
            "format": "md",
            "kind": "code_block",
            "heading_path": ["代码"],
            "code_block": 0,
        }
    )
    assert locator_map.LocatorMap.from_result(result).resolve_text(locator) == "print(`backtick`)"


def test_13_empty_documents_fail_explicitly_without_raising() -> None:
    for asset in parsing_assets.empty_assets():
        result = simple_reader.read_document(
            asset.content, file_name=asset.file_name, file_type=asset.format
        )
        assert result.ok is False
        assert "empty document" in result.error, result.error
        assert result.error.startswith(f"{asset.format}: ")
        assert result.text == "" and result.blocks == ()


def test_14_container_without_visible_text_fails_explicitly() -> None:
    for asset in parsing_assets.no_visible_text_assets():
        result = simple_reader.read_document(
            asset.content, file_name=asset.file_name, file_type=asset.format
        )
        assert result.ok is False, f"{asset.asset_id} 不得静默返回空文本"
        assert asset.expected_error in result.error, (asset.asset_id, result.error)
        assert result.text == "" and result.blocks == ()


def test_15_corrupted_documents_fail_with_format_specific_error() -> None:
    for asset in parsing_assets.corrupt_assets():
        result = simple_reader.read_document(
            asset.content, file_name=asset.file_name, file_type=asset.format
        )
        assert result.ok is False, asset.asset_id
        assert result.error.startswith(f"{asset.format}: "), result.error
        assert asset.expected_error in result.error, (asset.asset_id, result.error)
        assert result.text == "" and result.blocks == ()


def test_16_reader_never_raises_on_truncated_or_mutated_input() -> None:
    checked = 0
    for asset in (*parsing_assets.build_golden_assets(), *parsing_assets.edge_assets()):
        for fraction in (0.25, 0.5, 0.75):
            truncated = asset.content[: int(len(asset.content) * fraction)]
            truncated_result = simple_reader.read_document(
                truncated, file_name=asset.file_name, file_type=asset.format
            )
            _assert_result_invariants(truncated_result)
            if asset.format in {"pdf", "docx"}:
                assert not truncated_result.ok, (
                    f"{asset.asset_id} 截断到 {fraction:.0%} 仍被当成成功；"
                    "容器格式必须明确失败，不能静默返回半份内容"
                )
            checked += 1
        if asset.format == "docx":
            # zip 以中央目录为准，改首字节不一定坏；破坏中央目录签名才是确定性损坏。
            marker = asset.content.rfind(b"PK\x01\x02")
            assert marker > 0, f"{asset.asset_id} 缺少 zip 中央目录签名"
            mutated_bytes = bytearray(asset.content)
            mutated_bytes[marker] ^= 0xFF
            mutated = bytes(mutated_bytes)
        else:
            mutated = bytes([asset.content[0] ^ 0xFF]) + asset.content[1:]
        mutated_result = simple_reader.read_document(
            mutated, file_name=asset.file_name, file_type=asset.format
        )
        _assert_result_invariants(mutated_result)
        if asset.format in {"pdf", "docx"}:
            assert not mutated_result.ok, f"{asset.asset_id} 关键结构被破坏后仍被当成成功"
        checked += 1
    expected_checks = 4 * (
        len(parsing_assets.build_golden_assets()) + len(parsing_assets.edge_assets())
    )
    assert checked == expected_checks


def _assert_result_invariants(result: simple_reader.ParseResult) -> None:
    assert result.ok or result.error
    if not result.ok:
        assert result.text == "" and result.blocks == ()
    else:
        assert result.text == "\n\n".join(block.text for block in result.blocks)


# ---------------------------------------------------------------------------
# 与 P0-T10 DocReader 错误语义对齐（跨引用实测证据）
# ---------------------------------------------------------------------------
def test_17_parse_failure_matches_docreader_error_field_contract() -> None:
    from docreader_probe.generated import docreader_pb2 as pb

    field = pb.ReadResponse.DESCRIPTOR.fields_by_name["error"]
    assert field.number == simple_reader.READ_RESPONSE_ERROR_FIELD_NUMBER == 5
    assert simple_reader.READ_RESPONSE_ERROR_FIELD == "error"

    assert T10_EVIDENCE.is_file(), f"缺少 P0-T10 证据：{T10_EVIDENCE}"
    evidence = json.loads(T10_EVIDENCE.read_text(encoding="utf-8"))
    parse_error = evidence["parse_error_field"]
    assert parse_error["unary_status"] == "OK", "T10 实测：解析失败的 gRPC status 必须仍是 OK"
    assert parse_error["unary_error"], "T10 实测：错误只落在 ReadResponse.error 字段"
    assert parse_error["publishable"] is False

    failed = simple_reader.read_document(
        b"\xff\xfe", file_name="broken.txt", file_type="txt"
    )
    assert failed.ok is False and failed.error
    wire_fields = failed.as_read_response_fields()
    assert set(wire_fields) == {"markdown_content", "error"}
    assert wire_fields["markdown_content"] == ""
    response = pb.ReadResponse(**wire_fields)
    assert response.error == failed.error
    assert response.markdown_content == ""

    ok_result = _parse(GOLDEN_ASSETS["txt-golden"])
    ok_fields = ok_result.as_read_response_fields()
    assert ok_fields["error"] == "" and ok_fields["markdown_content"]
    assert pb.ReadResponse(**ok_fields).error == ""


# ---------------------------------------------------------------------------
# C-10：只做 locator 结构，不越界到资产绑定/版本发布
# ---------------------------------------------------------------------------
def test_18_scope_guard_no_asset_binding_or_version_publishing() -> None:
    forbidden = {
        "asset_binding",
        "asset_bindings",
        "publish",
        "publication",
        "workspace_id",
        "version_id",
        "active_version",
    }
    for module in (locator_map, simple_reader, parsing_assets):
        names = {name.lower() for name in dir(module) if not name.startswith("__")}
        leaked = {name for name in names if any(token in name for token in forbidden)}
        assert not leaked, f"{module.__name__} 越界提供了 T12/T13 的符号：{leaked}"

    result_fields = {field.name for field in fields(simple_reader.ParseResult)}
    assert not result_fields & forbidden, result_fields
    block_fields = {field.name for field in fields(simple_reader.Block)}
    assert block_fields == {"kind", "text", "locator", "aliases"}, block_fields
    locator_fields = locator_map.locator_fields()
    assert not locator_fields & {"asset_binding", "asset_id", "workspace_id", "version_id"}

    for case in GOLDEN_CASES:
        assert not {key.lower() for key in case.locator} & {
            "asset_id",
            "workspace_id",
            "version_id",
            "binding",
        }


# ---------------------------------------------------------------------------
# 性能与降级
# ---------------------------------------------------------------------------
def test_19_parse_timing_is_recorded_within_budget() -> None:
    assets = [*parsing_assets.build_golden_assets(), *parsing_assets.edge_assets()]
    timings: dict[str, float] = {}
    started = time.perf_counter()
    for asset in assets:
        asset_started = time.perf_counter()
        _parse(asset)
        timings[asset.asset_id] = time.perf_counter() - asset_started
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"解析 {len(assets)} 份素材耗时 {elapsed:.3f}s，超出 5s 预算"
    per_format = {
        fmt: round(sum(value for key, value in timings.items() if key.startswith(fmt)), 4)
        for fmt in parsing_assets.FORMAT_ORDER
    }
    assert set(per_format) == set(parsing_assets.FORMAT_ORDER)
    assert all(value >= 0.0 for value in per_format.values())


def test_20_degraded_resolution_is_explicit_not_fabricated() -> None:
    result = _parse(GOLDEN_ASSETS["txt-golden"])
    mapped = locator_map.LocatorMap.from_result(result)
    missing = locator_map.Locator.from_dict(
        {"schema": 1, "format": "txt", "kind": "paragraph", "paragraph": 999}
    )
    assert mapped.resolve(missing) is None
    with pytest.raises(locator_map.LocatorResolutionError):
        mapped.require(missing)

    wrong_format = locator_map.Locator.from_dict(
        {"schema": 1, "format": "pdf", "kind": "page", "page": 1, "paragraph": 0}
    )
    assert mapped.resolve(wrong_format) is None, "跨格式 locator 不得误命中"

    empty = locator_map.LocatorMap("", ())
    assert empty.degraded is True
    assert len(empty) == 0
    assert empty.resolve(missing) is None
