"""P0-T17 真机取证脚本（必须用系统 CPython 3.13 跑，见下方说明）。

为什么不用实验 venv 的 3.12：
    uv 托管的 CPython 3.12.13 在本机既打不开 daemon 的命名管道（`PermissionError 13`），
    也让它派生的 `bsk.exe` 子进程拿不到管道（`connect IPC named pipe ... 拒绝访问 (os error 5)`）。
    同一份 `bsk.exe` 由系统 CPython 3.13.3 派生则 `doctor` 全绿。这是运行时差异，不是代码差异。

用法（在仓库根）：
    C:\\Python313\\python.exe experiments\\phase0\\browserskill_probe\\live_run.py --json-out <path>

输出 JSON 里的每一项都来自真实 CLI stdout，不做任何加工；失败即非零退出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_PHASE0 = Path(__file__).resolve().parent.parent
if str(_REPO_PHASE0) not in sys.path:
    sys.path.insert(0, str(_REPO_PHASE0))

from browser_contracts import BrowserAction  # noqa: E402
from browserskill_probe import BrowserskillAdapter, BskCli  # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-T17 BrowserSkill 真机取证")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--url", default="https://example.com")
    parser.add_argument("--shot", type=Path, default=None)
    args = parser.parse_args(argv)

    cli = BskCli.autodetect()
    adapter = BrowserskillAdapter(cli=cli)
    evidence: dict[str, Any] = {
        "task": "P0-T17",
        "collected_at": _now(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "bsk_binary": str(cli.binary),
        "steps": {},
    }

    evidence["steps"]["doctor"] = adapter.doctor()
    browsers = adapter.browsers()
    evidence["steps"]["browsers"] = browsers
    if not isinstance(browsers, list) or not browsers:
        raise SystemExit("没有已配对的浏览器：先启动 daemon 并在 Chrome 里加载扩展")

    session_id = adapter.start_session(name="p0-t17-live")
    evidence["steps"]["session_start"] = {"session_id": session_id}
    try:
        created = adapter.execute(BrowserAction.TAB_CREATE, {"url": args.url})
        tab_id = str(created["tab_id"])
        evidence["steps"]["tab_create"] = created

        adapter.refresh_tabs()
        evidence["steps"]["tab_list_agent"] = adapter.refresh_tabs(scope="agent")

        evidence["steps"]["snapshot"] = adapter.execute(
            BrowserAction.SNAPSHOT, {"tab_id": tab_id, "max_depth": 6}
        )
        evaluated = adapter.execute(
            BrowserAction.EVALUATE,
            {"expression": "({title: document.title, href: location.href})", "tab_id": tab_id},
        )
        evidence["steps"]["evaluate"] = evaluated

        shot = args.shot or Path(
            f"{Path(__file__).resolve().parent.parent}/.pytest_cache/p0-t17-live.png"
        )
        shot.parent.mkdir(parents=True, exist_ok=True)
        evidence["steps"]["screenshot"] = adapter.execute(
            BrowserAction.SCREENSHOT, {"out": str(shot), "tab_id": tab_id}
        )
        evidence["steps"]["screenshot_artifact"] = {
            "path": str(shot),
            "bytes": shot.stat().st_size if shot.is_file() else 0,
            "sha256": hashlib.sha256(shot.read_bytes()).hexdigest().upper()
            if shot.is_file()
            else None,
        }

        evidence["steps"]["console"] = adapter.execute(
            BrowserAction.CONSOLE, {"tab_id": tab_id, "limit": 20}
        )
        evidence["steps"]["tab_close"] = adapter.execute(
            BrowserAction.TAB_CLOSE, {"tab_id": tab_id}
        )
    finally:
        evidence["steps"]["session_stop"] = adapter.stop_session()
        evidence["calls"] = adapter.calls

    payload = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload, encoding="utf-8")
        print(f"evidence written: {args.json_out} ({len(payload)} bytes)")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
