"""W14 / M3：判定口径 linter 的单元测试。

两类必须同时成立，否则这个 linter 要么形同虚设、要么变成噪音源：

* **合规放行**：正确写法（`**PASS（限域）**`）、被引用的字面量（行内代码/fenced 代码块）、
  否定语境（"不是 G-FULL 通过"）都不得报违规；
* **违规拦截**：同一行既写裸 `**PASS**` 又承认 `NOT_RUN`/`PARTIAL`/`BLOCKED`/`NOT_STARTED`、
  编号写错、波次记录缺小节，必须报出来。

另测基线机制：存量违规被豁免，但同类**新增**违规仍会失败（基线不等于永久豁免）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PHASE0_DIR) not in sys.path:  # pragma: no cover - conftest 之外的独立运行
    sys.path.insert(0, str(PHASE0_DIR))

import verdict_lint as vl  # noqa: E402


def _rules(rel: str, text: str) -> list[str]:
    """只取规则名，便于断言。"""
    return [item.rule for item in vl.lint_text(rel, text)]


# ---------------------------------------------------------------------------
# 合规放行（不能误报）
# ---------------------------------------------------------------------------


def test_qualified_pass_is_accepted() -> None:
    """`**PASS（限域）**` 是口径要求的写法，即使同行有 NOT_RUN 也不报。"""
    text = "| P0-T25 | **PASS（限域）** | ANN 已补跑，但质量阈值 NOT_RUN |"
    assert _rules("a.md", text) == []


def test_inline_code_literal_is_ignored() -> None:
    """正文解释规则时引用的 `` `**PASS**` `` 是字面量，不是判定语句。"""
    text = "规则：行内含 `NOT_RUN` 时不得标无保留 `**PASS**`。"
    assert _rules("a.md", text) == []


def test_fenced_block_is_ignored() -> None:
    """代码块里的示例行不进扫描（否则规则说明文档会自我违规）。"""
    text = "说明如下：\n\n```text\n| P0-T99 | **PASS** | 真实模型 NOT_RUN |\n```\n"
    assert _rules("a.md", text) == []


def test_negated_gate_claim_is_ignored() -> None:
    """「不是 G-FULL 通过」是澄清，不是声称；文档含 NOT_RUN 也不该报。"""
    text = "链路里存在 NOT_RUN；这是 `05:81-82` 允许的路径，但**不是** G-FULL 通过。"
    assert _rules("a.md", text) == []


# ---------------------------------------------------------------------------
# 违规拦截
# ---------------------------------------------------------------------------


def test_bare_pass_with_not_run_is_flagged() -> None:
    """D4 原始形态：裸 PASS 与 NOT_RUN 同行。"""
    text = "| T02 | **PASS** | 端口占用负向演练 NOT_RUN |"
    assert _rules("a.md", text) == ["BARE_PASS_WITH_CAVEAT"]


def test_bare_pass_with_partial_is_flagged() -> None:
    """承认子项 PARTIAL 却写裸 PASS，同样拦。"""
    text = "| 状态 | **PASS**（1 项子契约 PARTIAL：用替身验证） |"
    assert _rules("a.md", text) == ["BARE_PASS_WITH_CAVEAT"]


def test_not_run_document_claiming_gate_is_flagged() -> None:
    """文档含 NOT_RUN 而又声称 G-P0 通过 → 违规。"""
    text = "本波次仍有 NOT_RUN。\n结论：G-P0 通过。"
    assert _rules("a.md", text) == ["NOT_RUN_CLAIM"]


def test_task_id_format_is_flagged() -> None:
    """`P0-T5` 这类一位数字编号会让交叉检索失效。"""
    assert _rules("a.md", "见 P0-T5 与 P0-T025。") == ["TASK_ID_FORMAT", "TASK_ID_FORMAT"]
    assert _rules("a.md", "见 P0-T05。") == []


def test_wave_record_requires_four_sections() -> None:
    """波次记录缺 §2/§3/§4 时报一次结构性违规。"""
    rules = _rules("wave-w99-record.md", "## 1. 声明块\n")
    assert rules == ["WAVE_RECORD_SECTIONS"]
    assert _rules("wave-w99-record.md", "## 1.\n## 2.\n## 3.\n## 4.\n") == []


# ---------------------------------------------------------------------------
# 基线机制
# ---------------------------------------------------------------------------


def test_baseline_suppresses_existing_but_not_new(tmp_path: Path) -> None:
    """基线豁免已有条数；同类新增一条仍会被判为新增违规（基线不是永久豁免）。"""
    existing = [vl.Violation("a.md", 3, "BARE_PASS_WITH_CAVEAT", "x")]
    baseline = vl.build_baseline(existing)
    assert vl.new_violations(existing, baseline) == []

    grown = existing + [vl.Violation("a.md", 9, "BARE_PASS_WITH_CAVEAT", "y")]
    fresh = vl.new_violations(grown, baseline)
    assert [item.line for item in fresh] == [9]


def test_baseline_keeps_reason_for_known_entry() -> None:
    """已知存量条目的原因必须写进基线，避免「基线 = 无理由豁免」。"""
    violation = vl.Violation(
        "docs/poc/evidence/wave-w1-record.md", 88, "BARE_PASS_WITH_CAVEAT", "x"
    )
    payload = vl.build_baseline([violation])
    entry = payload["entries"][0]
    assert entry["count"] == 1
    assert "D4" in entry["reason"]


def test_baseline_write_and_check_roundtrip(tmp_path: Path, monkeypatch) -> None:
    """真实目录里的基线文件能被读回；没有基线时等价于零豁免。"""
    monkeypatch.setattr(vl, "BASELINE_PATH", Path("baseline.json"))
    assert vl.load_baseline(tmp_path) == {
        "schema": "pyknora.phase0.verdict-lint-baseline.v1",
        "entries": [],
    }

    violations = [vl.Violation("a.md", 1, "TASK_ID_FORMAT", "x")]
    path = vl.write_baseline(violations, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["rule"] == "TASK_ID_FORMAT"
    assert vl.new_violations(violations, vl.load_baseline(tmp_path)) == []


def test_repo_has_no_new_violations() -> None:
    """真实仓库回归：只允许基线内违规；新增违规说明本轮引入了口径问题。"""
    violations, scanned = vl.lint_all(vl.REPO_ROOT)
    fresh = vl.new_violations(violations, vl.load_baseline(vl.REPO_ROOT))
    assert scanned >= 40
    assert fresh == []
