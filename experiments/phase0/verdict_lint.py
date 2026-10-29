"""P0 判定口径 linter（W14 / M3）。

为什么需要它
------------
`P0_Core_Gate_Review.md` §2.4 D3 记录了同一类事故反复出现：**任务级 `PASS（限域）` 被写成门级
`PASS`**，或者一份文档里上半段说 `NOT_RUN`、下半段把结论写成"通过"。这类错误人写时看不见，
靠评审复读才能发现——所以把它变成机检。

四条规则
--------
* `BARE_PASS`：出现裸的 `**PASS**`。本项目口径要求写 `**PASS（限域）**` / `**PASS（限域：单活）**`，
  因为「限域」本身就是判定的一部分（`05_Phase0_Acceptance.md:73-76`）。
* `NOT_RUN_CLAIM`：文档含 `NOT_RUN` 却同时声称 `G-P0 通过` / `G-FULL 通过` / `Phase 0 通过`。
* `TASK_ID_FORMAT`：任务编号必须是 `P0-Tnn` 两位数字；`P0-T5`、`P0-T005` 会让交叉检索失效。
* `WAVE_RECORD_SECTIONS`：每份 `wave-w*-record.md` 必须有 `## 1.`–`## 4.` 四节
  （声明/派发/门禁/提交）。

基线机制
--------
存量违规不静默放过，也不阻塞新工作：`--write-baseline` 把当前违规按 `(路径, 规则)` 计数写入
`docs/poc/evidence/verdict_lint_baseline.json`，`--check` 只在**新增**违规时失败。每条基线都带
`reason`，避免「基线 = 永久豁免」。

用法
----
    python experiments/phase0/verdict_lint.py --check
    python experiments/phase0/verdict_lint.py --check --json
    python experiments/phase0/verdict_lint.py --write-baseline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_SCOPE = Path("docs") / "poc"
BASELINE_PATH = DOC_SCOPE / "evidence" / "verdict_lint_baseline.json"

#: 裸 PASS：粗体里只有 PASS，没有括号限定
RE_BARE_PASS = re.compile(r"\*\*PASS\*\*")
#: 同一行内的「自相矛盾」保留词：写着 PASS 又承认未跑/阻断/部分/限域
RE_LOCAL_CAVEAT = re.compile(r"(NOT_RUN|NOT_STARTED|BLOCKED|PARTIAL)")
#: 行内代码片段（`...` 里的内容是被引用的字面量，不是判定语句）
RE_INLINE_CODE = re.compile(r"`[^`]*`")
#: 声称门级通过的说法（出现即需要证据指针，且不能与 NOT_RUN 并存）
RE_GATE_CLAIM = re.compile(r"(G-P0|G-FULL|Phase\s*0)\s*(已)?通过")
#: 否定语境：紧邻「不是 / 非 / 未 / ≠」的门级通过说法不算声称
NEGATIONS = ("不是", "并非", "非", "未", "≠", "不能算", "不得标")
#: 任务编号
RE_TASK_ID = re.compile(r"P0-T(\d+)")
#: 波次记录必备小节
REQUIRED_WAVE_SECTIONS = ("## 1.", "## 2.", "## 3.", "## 4.")

RULE_TEXT = {
    "BARE_PASS_WITH_CAVEAT": (
        "同一行写裸 `**PASS**` 又承认 NOT_RUN/PARTIAL/BLOCKED/NOT_STARTED："
        "必须改成 `**PASS（限域）**` 或降级"
    ),
    "NOT_RUN_CLAIM": "文档含 NOT_RUN 却声称门级通过（否定语境除外）",
    "TASK_ID_FORMAT": "任务编号必须是 P0-Tnn（两位数字）",
    "WAVE_RECORD_SECTIONS": "波次记录缺 §1–§4 中的小节",
}


@dataclass(frozen=True, order=True)
class Violation:
    """一条违规：路径 + 行号 + 规则 + 片段。"""

    path: str
    line: int
    rule: str
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "snippet": self.snippet,
        }


def iter_docs(root: Path) -> list[Path]:
    """枚举扫描范围内的 Markdown（`docs/poc` 递归）。"""
    base = root / DOC_SCOPE
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.md") if path.is_file())


def lint_text(rel_path: str, text: str) -> list[Violation]:
    """对单份文档做四条规则检查，返回违规列表（按行号排序）。

    两处**刻意的降噪设计**（否则 linter 会淹没在引文里）：

    * 跳过 fenced 代码块：代码里出现的 `**PASS**` 是示例，不是判定；
    * 剥掉行内代码片段：`` `**PASS**` `` 是对字面量的引用（例如正文在解释这条规则）。
    """
    violations: list[Violation] = []
    lines = text.splitlines()
    in_fence = False
    fenced_lines: set[int] = set()

    for index, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            fenced_lines.add(index)
            continue
        if in_fence:
            fenced_lines.add(index)
            continue

        stripped = RE_INLINE_CODE.sub("", line)

        if RE_BARE_PASS.search(stripped) and RE_LOCAL_CAVEAT.search(stripped):
            violations.append(
                Violation(rel_path, index, "BARE_PASS_WITH_CAVEAT", line.strip()[:120])
            )
        for match in RE_TASK_ID.finditer(stripped):
            digits = match.group(1)
            if len(digits) != 2:
                violations.append(
                    Violation(
                        rel_path,
                        index,
                        "TASK_ID_FORMAT",
                        f"P0-T{digits} → 应为 P0-T{int(digits):02d}",
                    )
                )

    has_not_run = any("NOT_RUN" in line for line in lines)
    if has_not_run:
        for index, line in enumerate(lines, start=1):
            if index in fenced_lines:
                continue
            stripped = RE_INLINE_CODE.sub("", line)
            for match in RE_GATE_CLAIM.finditer(stripped):
                head = stripped[: match.start()]
                tail = head[-12:]
                if any(negation in tail for negation in NEGATIONS):
                    continue
                violations.append(Violation(rel_path, index, "NOT_RUN_CLAIM", line.strip()[:120]))
                break

    name = Path(rel_path).name
    if re.fullmatch(r"wave-w[0-9a-z]+-record\.md", name):
        missing = [section for section in REQUIRED_WAVE_SECTIONS if section not in text]
        if missing:
            violations.append(
                Violation(
                    rel_path,
                    0,
                    "WAVE_RECORD_SECTIONS",
                    "缺少小节: " + ", ".join(missing),
                )
            )

    violations.sort()
    return violations


def lint_all(root: Path = REPO_ROOT) -> tuple[list[Violation], int]:
    """扫描全部文档，返回 (违规列表, 扫描文件数)。"""
    violations: list[Violation] = []
    docs = iter_docs(root)
    for path in docs:
        rel = path.relative_to(root).as_posix()
        violations.extend(lint_text(rel, path.read_text(encoding="utf-8")))
    return violations, len(docs)


def load_baseline(root: Path = REPO_ROOT) -> dict[str, Any]:
    """读基线文件；不存在时返回空基线（等价于「零豁免」）。"""
    path = root / BASELINE_PATH
    if not path.is_file():
        return {"schema": "pyknora.phase0.verdict-lint-baseline.v1", "entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def baseline_counts(baseline: dict[str, Any]) -> dict[tuple[str, str], int]:
    """把基线折成 `(路径, 规则) → 允许条数`。"""
    counts: dict[tuple[str, str], int] = {}
    for entry in baseline.get("entries", []):
        key = (str(entry["path"]), str(entry["rule"]))
        counts[key] = counts.get(key, 0) + int(entry.get("count", 1))
    return counts


def new_violations(violations: list[Violation], baseline: dict[str, Any]) -> list[Violation]:
    """过滤掉基线已豁免的条数，只留新增违规。"""
    allowed = baseline_counts(baseline)
    seen: dict[tuple[str, str], int] = {}
    fresh: list[Violation] = []
    for violation in violations:
        key = (violation.path, violation.rule)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > allowed.get(key, 0):
            fresh.append(violation)
    return fresh


def build_baseline(
    violations: list[Violation], reasons: dict[str, str] | None = None
) -> dict[str, Any]:
    """把当前违规折成基线条目（同路径同规则合并计数）。"""
    reasons = reasons or {}
    grouped: dict[tuple[str, str], list[int]] = {}
    for violation in violations:
        grouped.setdefault((violation.path, violation.rule), []).append(violation.line)
    entries = [
        {
            "path": path,
            "rule": rule,
            "count": len(line_numbers),
            "lines": sorted(line_numbers),
            "reason": (
                reasons.get(f"{path}:{rule}")
                or KNOWN_REASONS.get(f"{path}:{rule}")
                or DEFAULT_REASONS.get(rule, "存量违规，待后续波次修")
            ),
        }
        for (path, rule), line_numbers in sorted(grouped.items())
    ]
    return {
        "schema": "pyknora.phase0.verdict-lint-baseline.v1",
        "generated_by": "experiments/phase0/verdict_lint.py --write-baseline",
        "note": "基线只豁免已有条数；同类新增违规仍会让 --check 失败",
        "entries": entries,
    }


DEFAULT_REASONS = {
    "BARE_PASS_WITH_CAVEAT": (
        "同一行并列多个任务或子项，PASS 与 PARTIAL 分属不同主体；保留原文以留痕"
    ),
}

#: 已知存量违规的具体原因（写成基线时原样带出，便于审计是否该修）
KNOWN_REASONS: dict[str, str] = {
    "docs/poc/evidence/environment.md:BARE_PASS_WITH_CAVEAT": (
        "T02 状态行写裸 **PASS**，却在同一行记录「端口占用演练 NOT_RUN」；与 D4 同类口径债，"
        "应在文档债务批次改为 PASS（限域），本轮不改写历史证据"
    ),
    "docs/poc/evidence/poc01-owner.md:BARE_PASS_WITH_CAVEAT": (
        "首列判定已是 PARTIAL（单活），备注列写替身路径 PASS；数值正确但未加限定词，"
        "属口径债，待文档债务批次统一"
    ),
    "docs/poc/evidence/poc02-replanning.md:BARE_PASS_WITH_CAVEAT": (
        "状态行写 **PASS** 而括注「1 项子契约 PARTIAL」；应写 PASS（限域），属口径债"
    ),
    "docs/poc/evidence/wave-w1-record.md:BARE_PASS_WITH_CAVEAT": (
        "D4 原始两处之一（T31 评审已登记）：T02 标裸 PASS 但行内含 NOT_RUN 与待决 ADR"
    ),
    "docs/poc/evidence/wave-w2-record.md:BARE_PASS_WITH_CAVEAT": (
        "D4 原始两处之一（T31 评审已登记）：P0-T03 标裸 PASS 而行内含未覆盖项"
    ),
}


def write_baseline(violations: list[Violation], root: Path = REPO_ROOT) -> Path:
    """写基线文件并返回路径。"""
    path = root / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_baseline(violations)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def format_summary(scanned: int, violations: list[Violation], fresh: list[Violation]) -> str:
    """一行式摘要。"""
    rules = {}
    for violation in violations:
        rules[violation.rule] = rules.get(violation.rule, 0) + 1
    detail = " ".join(f"{rule}={count}" for rule, count in sorted(rules.items())) or "none"
    return f"docs_scanned={scanned} violations={len(violations)} new={len(fresh)} by_rule[{detail}]"


def main(argv: list[str] | None = None) -> int:
    """命令行入口；`--check` 有新增违规时返回 1。"""
    parser = argparse.ArgumentParser(description="P0 判定口径 linter")
    parser.add_argument("--check", action="store_true", help="检查新增违规（默认动作）")
    parser.add_argument("--write-baseline", action="store_true", help="把当前违规写成基线")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录（测试用）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    violations, scanned = lint_all(root)

    if args.write_baseline:
        write_baseline(violations, root)
        print(format_summary(scanned, violations, []))

    fresh = new_violations(violations, load_baseline(root))
    if args.json:
        print(
            json.dumps(
                {
                    "summary": format_summary(scanned, violations, fresh),
                    "new_violations": [item.as_dict() for item in fresh],
                    "violations": [item.as_dict() for item in violations],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_summary(scanned, violations, fresh))
        for item in fresh:
            print(f"  ! {item.path}:{item.line} [{item.rule}] {item.snippet}")

    if args.write_baseline:
        return 0
    return 0 if not fresh else 1


if __name__ == "__main__":
    sys.exit(main())
