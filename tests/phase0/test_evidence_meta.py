"""W14 / M2：证据元数据台账的单元测试。

覆盖的是**判定逻辑**（指纹稳定性、缺口识别、台账漂移检测），不覆盖「P0 证据是否通过」——
那是门评审的事。所有用例都在 `tmp_path` 里造微型仓库，不碰真实证据目录；只有
`test_repo_evidence_ledger_is_clean` 读真实仓库，用来防止台账被手改后无人发现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PHASE0_DIR) not in sys.path:  # pragma: no cover - conftest 之外单独跑时生效
    sys.path.insert(0, str(PHASE0_DIR))

import evidence_meta as em  # noqa: E402

EV = em.EVIDENCE_DIR_NAME


def _write_evidence(root: Path, name: str, payload: Any) -> Path:
    """在微型仓库里落一份证据 JSON。"""
    target = root / EV / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _write_doc(root: Path, rel: str, text: str) -> Path:
    """在微型仓库里落一份 derived_from 指向的文档/代码。"""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _spec(name: str = "e.json", **overrides: Any) -> em.EvidenceSpec:
    """构造一份只在测试里使用的台账条目。"""
    base: dict[str, Any] = {
        "name": name,
        "task": "P0-T99",
        "wave": "W99",
        "derived_from": ("docs/poc/evidence/wave-w99-record.md",),
        "timestamp_field": "generated_at",
    }
    base.update(overrides)
    return em.EvidenceSpec(**base)


# ---------------------------------------------------------------------------
# 指纹语义
# ---------------------------------------------------------------------------


def test_canonical_sha256_ignores_key_order_and_indent(tmp_path: Path) -> None:
    """同一载荷换键序/换缩进后指纹不变，改值则变——这是「语义等价」的判据。"""
    a = {"b": 1, "a": [1, 2, {"y": 2, "x": 1}]}
    b = {"a": [1, 2, {"x": 1, "y": 2}], "b": 1}
    assert em.canonical_sha256(a) == em.canonical_sha256(b)

    c = {"a": [1, 2, {"x": 1, "y": 3}], "b": 1}
    assert em.canonical_sha256(a) != em.canonical_sha256(c)


def test_canonical_sha256_keeps_non_ascii_readable() -> None:
    """非 ASCII 不转义：中文内容稳定可读，不因 ensure_ascii 差异产生假漂移。"""
    payload = {"结论": "PASS（限域）"}
    assert "结论".encode() in em.canonical_bytes(payload)


# ---------------------------------------------------------------------------
# 缺口识别
# ---------------------------------------------------------------------------


def test_collect_flags_task_mismatch(tmp_path: Path) -> None:
    """载荷 task 与台账不一致必须报 TASK_MISMATCH。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T01", "generated_at": "t"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    entry = em.collect(_spec(), tmp_path)
    assert "TASK_MISMATCH" in entry.problems
    assert entry.task_source == "payload"


def test_collect_flags_missing_derived_from(tmp_path: Path) -> None:
    """derived_from 指向的文件不存在时报 DERIVED_MISSING 且不中断其他检查。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t"})
    entry = em.collect(_spec(), tmp_path)
    assert "DERIVED_MISSING" in entry.problems
    assert entry.derived_from[0]["exists"] is False


def test_collect_flags_missing_citation(tmp_path: Path) -> None:
    """报告存在但没引用该任务编号 → DERIVED_NO_CITATION（防止挂错报告）。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "本波次没有提到编号")
    entry = em.collect(_spec(), tmp_path)
    assert "DERIVED_NO_CITATION" in entry.problems
    assert entry.derived_from[0]["cites_task"] is False


def test_collect_accepts_citation(tmp_path: Path) -> None:
    """报告里出现任务编号即视为已引用，不要求特定格式。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "| P0-T99 | PASS（限域） |")
    entry = em.collect(_spec(), tmp_path)
    assert entry.problems == []
    assert entry.derived_from[0]["cites_task"] is True


def test_collect_marks_manifest_as_task_source_when_payload_lacks_task(tmp_path: Path) -> None:
    """载荷缺 task/wave 时，来源标成 manifest（这是真实存在的 D 级缺口，不能被静默吞掉）。"""
    _write_evidence(tmp_path, "e.json", {"generated_at": "t"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    entry = em.collect(_spec(), tmp_path)
    assert entry.task_source == "manifest"
    assert entry.wave_source == "manifest"


def test_collect_treats_declared_absent_timestamp_as_expected(tmp_path: Path) -> None:
    """台账声明「本就没有时间戳」时不报 NO_TIMESTAMP；未声明而缺失才报。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")

    expected_absent = em.collect(_spec(timestamp_field=None), tmp_path)
    assert "NO_TIMESTAMP" not in expected_absent.problems
    assert expected_absent.timestamp_source == "absent"

    unexpected_absent = em.collect(_spec(), tmp_path)
    assert "NO_TIMESTAMP" in unexpected_absent.problems


def test_collect_falls_back_to_collected_at(tmp_path: Path) -> None:
    """时间字段兼容 collected_at（T17 实跑记录用这个字段名）。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "collected_at": "2026-10-28T11:44:41"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    entry = em.collect(_spec(), tmp_path)
    assert entry.timestamp == "2026-10-28T11:44:41"
    assert entry.timestamp_source == "payload:collected_at"


# ---------------------------------------------------------------------------
# 台账（SHA256SUMS）漂移检测
# ---------------------------------------------------------------------------


def test_ledger_roundtrip_then_detects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入台账后 check 干净；手改一次值后必须报 LEDGER_MISMATCH（这就是防手改的意义）。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t", "v": 1})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    spec = _spec()
    monkeypatch.setattr(em, "SPECS", (spec,))

    em.write_artifacts([em.collect(spec, tmp_path)], tmp_path)
    entries, global_problems = em.check(tmp_path)
    assert [e.problems for e in entries] == [[]]
    assert global_problems == []

    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t", "v": 2})
    entries, _ = em.check(tmp_path)
    assert "LEDGER_MISMATCH" in entries[0].problems


def test_check_reports_unregistered_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """证据目录里多出未登记 JSON 时必须报 UNREGISTERED（否则台账会悄悄失覆盖）。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t"})
    _write_evidence(tmp_path, "extra.json", {"anything": True})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    spec = _spec()
    monkeypatch.setattr(em, "SPECS", (spec,))
    em.write_artifacts([em.collect(spec, tmp_path)], tmp_path)

    _, global_problems = em.check(tmp_path)
    assert global_problems == ["UNREGISTERED:extra.json"]


def test_manifest_is_not_reported_as_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """台账 JSON 自身不能算未登记证据，否则每次 --write 后 check 都失败。"""
    _write_evidence(tmp_path, "e.json", {"task": "P0-T99", "generated_at": "t"})
    _write_doc(tmp_path, "docs/poc/evidence/wave-w99-record.md", "P0-T99")
    spec = _spec()
    monkeypatch.setattr(em, "SPECS", (spec,))
    em.write_artifacts([em.collect(spec, tmp_path)], tmp_path)

    _, global_problems = em.check(tmp_path)
    assert (tmp_path / EV / em.MANIFEST_NAME).is_file()
    assert em.MANIFEST_NAME not in " ".join(global_problems)


# ---------------------------------------------------------------------------
# 真实仓库回归：防止有人手改证据后台账无人更新
# ---------------------------------------------------------------------------


def test_repo_evidence_ledger_is_clean() -> None:
    """真实仓库的 8 份证据必须与 SHA256SUMS 一致；失败说明证据被手改或台账过期。"""
    entries, global_problems = em.check(em.REPO_ROOT)
    assert global_problems == []
    problems = {entry.name: entry.problems for entry in entries if entry.problems}
    assert problems == {}

    # 两份载荷缺 task 的证据必须仍被识别出来（缺口已知且被记录，不是被掩盖）
    from_manifest = {entry.name for entry in entries if entry.task_source == "manifest"}
    assert from_manifest == {"rag-citations.json", "research-batch.json"}
