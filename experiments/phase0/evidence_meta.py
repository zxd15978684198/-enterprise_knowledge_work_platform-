"""P0 证据元数据台账（W14 / M2）。

背景与目标
----------
`P0_Core_Gate_Review.md` §2.4 D2 指出：`docs/poc/evidence/` 下的机器证据（JSON）**没有统一的
`task` / `wave` / `derived_from` / `sha256` 台账**，于是评审只能靠人工猜「这份 JSON 属于哪个任务、
由哪份报告复述」。M2 的目标是把这层元数据**变成可机检的事实**：

* 每份 JSON 都声明它属于哪个任务（`task`）、哪个波次（`wave`）；
* 每份 JSON 都来自哪份人工报告/哪段探针代码（`derived_from`），且报告确实引用了该任务；
* 每份 JSON 的内容指纹（规范化后的 `sha256`）登记在 `SHA256SUMS`，可一次校验是否被手改。

「规范化」的含义
----------------
对 `json.loads` 之后的对象按 `sort_keys=True`、紧凑分隔符、`ensure_ascii=False` 重新序列化，
再对 UTF-8 字节求 sha256。因此**缩进、键序、行尾风格都不影响指纹**，只有值变化才会变——
这正是「能否逐字节复算」与「语义等价」的分界：前者做不到（时间戳每次跑都变），后者可以。

用法
----
    python experiments/phase0/evidence_meta.py --check     # 校验，出问题则 exit 1
    python experiments/phase0/evidence_meta.py --write     # 重写 SHA256SUMS 与台账 JSON
    python experiments/phase0/evidence_meta.py --check --json  # 机器可读输出

副作用：`--write` 覆盖 `docs/poc/evidence/SHA256SUMS` 与
`docs/poc/evidence/evidence_manifest.json`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR_NAME = Path("docs") / "poc" / "evidence"
LEDGER_NAME = "SHA256SUMS"
MANIFEST_NAME = "evidence_manifest.json"

#: 证据目录里**不是证据**的 JSON：工具自身产物（content 是派生配置，不是被审计对象）。
#: 漏登记一项就会让 `--check` 报 `UNREGISTERED`，因此新增此类产物必须同步加到这里。
NON_EVIDENCE_JSON: frozenset[str] = frozenset({MANIFEST_NAME, "verdict_lint_baseline.json"})

#: 证据 JSON 里可能承载生成时间的字段（不同探针历史写法不同，按顺序取值）
TIMESTAMP_FIELDS = ("generated_at", "collected_at", "created_at")


@dataclass(frozen=True)
class EvidenceSpec:
    """一份机器证据的期望元数据（人工维护，工具只校验不推断）。"""

    name: str
    task: str
    wave: str
    derived_from: tuple[str, ...]
    timestamp_field: str | None = "generated_at"
    note: str = ""


#: 台账本体。新增证据 JSON 时必须在此登记，否则工具报 `UNREGISTERED`。
SPECS: tuple[EvidenceSpec, ...] = (
    EvidenceSpec(
        name="ann-bench.json",
        task="P0-T25",
        wave="W13",
        derived_from=(
            "docs/poc/evidence/wave-w13-record.md",
            "experiments/phase0/ann_bench.py",
        ),
        note="ANN 补跑（ADR-0010 步骤 3）；载荷自带 wave=W13",
    ),
    EvidenceSpec(
        name="docreader_image_inventory.json",
        task="P0-T09R",
        wave="W7R",
        derived_from=(
            "docs/poc/evidence/wave-w7r-record.md",
            "experiments/phase0/audit_docreader_image.ps1",
        ),
        note="载荷无 wave 字段，波次由台账给出",
    ),
    EvidenceSpec(
        name="p0-t17-live-run.json",
        task="P0-T17",
        wave="W5",
        derived_from=(
            "docs/poc/evidence/wave-w5-record.md",
            "tests/phase0/test_bsk_live.py",
        ),
        timestamp_field="collected_at",
        note="真配对实跑记录，时间字段为 collected_at",
    ),
    EvidenceSpec(
        name="poc01_owner_process_level.json",
        task="P0-T05R",
        wave="W7R",
        derived_from=(
            "docs/poc/evidence/wave-w7r-record.md",
            "experiments/phase0/checkpoint_probe.py",
        ),
    ),
    EvidenceSpec(
        name="rag-citations.json",
        task="P0-T26",
        wave="W8",
        derived_from=(
            "docs/poc/evidence/wave-w8-record.md",
            "experiments/phase0/rag_probe.py",
        ),
        note="载荷缺 task 字段（D 级缺口，见 M2 报告 §3）",
    ),
    EvidenceSpec(
        name="research-batch.json",
        task="P0-T28",
        wave="W9",
        derived_from=(
            "docs/poc/evidence/wave-w9-record.md",
            "experiments/phase0/research_probe.py",
        ),
        timestamp_field=None,
        note="载荷既无 task 也无时间戳（D 级缺口，见 M2 报告 §3）",
    ),
    EvidenceSpec(
        name="retrieval-baseline.json",
        task="P0-T25",
        wave="W7",
        derived_from=(
            "docs/poc/evidence/wave-w7-record.md",
            "experiments/phase0/retrieval_probe.py",
        ),
    ),
    EvidenceSpec(
        name="security-probes.json",
        task="P0-T29",
        wave="W9",
        derived_from=(
            "docs/poc/evidence/wave-w9-record.md",
            "experiments/phase0/security_probe.py",
        ),
    ),
)

#: 问题码 → 人类可读说明
PROBLEM_TEXT = {
    "MISSING_FILE": "证据文件不存在",
    "UNREGISTERED": "证据目录里出现了未登记的 JSON",
    "TASK_MISMATCH": "载荷 task 与台账不一致",
    "WAVE_MISMATCH": "载荷 wave 与台账不一致",
    "DERIVED_MISSING": "derived_from 指向的文件不存在",
    "DERIVED_NO_CITATION": "derived_from 里的报告未引用该任务编号",
    "NO_TIMESTAMP": "缺少时间戳字段（台账声明该证据本就没有时间戳，仅提示）",
    "LEDGER_MISSING": "SHA256SUMS 中没有该文件",
    "LEDGER_MISMATCH": "SHA256SUMS 与当前规范化指纹不一致",
}


def canonical_bytes(payload: Any) -> bytes:
    """把 JSON 载荷规范化成稳定字节串（键排序、紧凑分隔、不转义非 ASCII）。"""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    """规范化载荷的 sha256 十六进制摘要。"""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    """原始字节的 sha256（用于对人工报告/代码做定位，不用于 JSON 指纹）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    """读取 JSON 文件；失败时抛出带路径的 ValueError，便于定位。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 只在文件损坏时触发
        raise ValueError(f"{path}: 不是合法 JSON: {exc}") from exc


def pick_timestamp(payload: Any, preferred: str | None) -> tuple[str | None, str]:
    """取时间戳，返回 (值, 来源说明)。

    `preferred` 为 None 表示台账声明「该证据本就没有时间戳」，此时不尝试其他字段。
    """
    if preferred is None:
        return None, "absent"
    if isinstance(payload, dict) and isinstance(payload.get(preferred), str):
        return str(payload[preferred]), "payload"
    for alt in TIMESTAMP_FIELDS:
        if isinstance(payload, dict) and isinstance(payload.get(alt), str):
            return str(payload[alt]), f"payload:{alt}"
    return None, "absent"


@dataclass
class Entry:
    """单份证据的核对结果。"""

    name: str
    task: str
    wave: str
    sha256: str = ""
    timestamp: str | None = None
    timestamp_source: str = "absent"
    task_source: str = "payload"
    wave_source: str = "payload"
    derived_from: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """转成可序列化字典。"""
        return {
            "name": self.name,
            "task": self.task,
            "wave": self.wave,
            "sha256": self.sha256,
            "timestamp": self.timestamp,
            "timestamp_source": self.timestamp_source,
            "task_source": self.task_source,
            "wave_source": self.wave_source,
            "derived_from": self.derived_from,
            "problems": self.problems,
        }


def collect(spec: EvidenceSpec, root: Path) -> Entry:
    """核对单份证据：存在性、载荷 task/wave、时间戳、derived_from、指纹。"""
    entry = Entry(name=spec.name, task=spec.task, wave=spec.wave)
    path = root / EVIDENCE_DIR_NAME / spec.name
    if not path.is_file():
        entry.problems.append("MISSING_FILE")
        return entry

    payload = load_json(path)
    entry.sha256 = canonical_sha256(payload)

    if isinstance(payload, dict) and isinstance(payload.get("task"), str):
        if str(payload["task"]) != spec.task:
            entry.problems.append("TASK_MISMATCH")
    else:
        entry.task_source = "manifest"

    if isinstance(payload, dict) and isinstance(payload.get("wave"), str):
        if str(payload["wave"]) != spec.wave:
            entry.problems.append("WAVE_MISMATCH")
    else:
        entry.wave_source = "manifest"

    entry.timestamp, entry.timestamp_source = pick_timestamp(payload, spec.timestamp_field)
    if entry.timestamp is None and spec.timestamp_field is not None:
        entry.problems.append("NO_TIMESTAMP")

    for rel in spec.derived_from:
        target = root / rel
        item: dict[str, Any] = {"path": rel, "exists": target.is_file()}
        if not item["exists"]:
            entry.problems.append("DERIVED_MISSING")
            entry.derived_from.append(item)
            continue
        item["sha256"] = file_sha256(target)
        if target.suffix.lower() == ".md":
            text = target.read_text(encoding="utf-8")
            cited = spec.task in text
            item["cites_task"] = cited
            if not cited:
                entry.problems.append("DERIVED_NO_CITATION")
        entry.derived_from.append(item)

    return entry


def read_ledger(root: Path) -> dict[str, str]:
    """读 SHA256SUMS，返回 {证据文件名: 指纹}。"""
    path = root / EVIDENCE_DIR_NAME / LEDGER_NAME
    if not path.is_file():
        return {}
    ledger: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        ledger[name.rsplit("/", 1)[-1]] = digest
    return ledger


def render_ledger(entries: list[Entry]) -> str:
    """渲染 SHA256SUMS 文本（按文件名排序，行尾 LF）。"""
    lines = [
        "# P0 机器证据指纹（规范化 JSON 的 sha256）；由 evidence_meta.py --write 生成，勿手改",
        "# 复算命令：python experiments/phase0/evidence_meta.py --check",
    ]
    for entry in sorted(entries, key=lambda item: item.name):
        lines.append(f"{entry.sha256}  {EVIDENCE_DIR_NAME.as_posix()}/{entry.name}")
    return "\n".join(lines) + "\n"


def check(root: Path = REPO_ROOT) -> tuple[list[Entry], list[str]]:
    """核对全部已登记证据，返回 (条目, 全局问题)。"""
    entries = [collect(spec, root) for spec in SPECS]
    ledger = read_ledger(root)
    for entry in entries:
        if not entry.sha256:
            continue
        recorded = ledger.get(entry.name)
        if recorded is None:
            entry.problems.append("LEDGER_MISSING")
        elif recorded != entry.sha256:
            entry.problems.append("LEDGER_MISMATCH")

    global_problems: list[str] = []
    evidence_dir = root / EVIDENCE_DIR_NAME
    if evidence_dir.is_dir():
        registered = {spec.name for spec in SPECS}
        for path in sorted(evidence_dir.glob("*.json")):
            if path.name not in registered and path.name not in NON_EVIDENCE_JSON:
                global_problems.append(f"UNREGISTERED:{path.name}")
    return entries, global_problems


def write_artifacts(entries: list[Entry], root: Path = REPO_ROOT) -> None:
    """写入 SHA256SUMS 与台账 JSON。"""
    evidence_dir = root / EVIDENCE_DIR_NAME
    (evidence_dir / LEDGER_NAME).write_text(render_ledger(entries), encoding="utf-8")
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest = {
        "schema": "pyknora.phase0.evidence-manifest.v1",
        "generated_by": "experiments/phase0/evidence_meta.py",
        "generated_at": generated_at,
        "fingerprint_rule": (
            "json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=False) → sha256(utf-8)"
        ),
        "entries": [entry.as_dict() for entry in entries],
    }
    (evidence_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def format_summary(entries: list[Entry], global_problems: list[str]) -> str:
    """一行式摘要，便于粘进波次记录。"""
    total = len(entries)
    with_problem = sum(1 for entry in entries if entry.problems)
    manifest_only = sum(1 for entry in entries if entry.task_source == "manifest")
    no_ts = sum(1 for entry in entries if entry.timestamp is None)
    return (
        f"evidence={total} with_problem={with_problem} "
        f"task_from_manifest={manifest_only} no_timestamp={no_ts} "
        f"global_problems={len(global_problems)}"
    )


def main(argv: list[str] | None = None) -> int:
    """命令行入口；`--check` 有问题返回 1。"""
    parser = argparse.ArgumentParser(description="P0 证据元数据台账校验/生成")
    parser.add_argument("--check", action="store_true", help="校验台账与指纹（默认动作）")
    parser.add_argument("--write", action="store_true", help="重写 SHA256SUMS 与台账 JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录（测试用）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.write:
        # 先落盘再核对：否则 check() 读到的是上一版台账，会全部报 LEDGER_MISSING
        write_artifacts([collect(spec, root) for spec in SPECS], root)
    entries, global_problems = check(root)

    report = {
        "summary": format_summary(entries, global_problems),
        "global_problems": global_problems,
        "entries": [entry.as_dict() for entry in entries],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["summary"])
        for item in global_problems:
            print(f"  ! {item}")
        for entry in entries:
            if entry.problems:
                print(f"  ! {entry.name}: {', '.join(entry.problems)}")

    if args.write:
        return 0
    return 0 if not any(entry.problems for entry in entries) and not global_problems else 1


if __name__ == "__main__":
    sys.exit(main())
