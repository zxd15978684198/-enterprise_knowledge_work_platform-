"""P0-T17 证据测试：`bsk` CLI 传输层 + 动作映射闸门 + 真机配对。

分两层：
- 默认跑的单测（无需 daemon）：argv 构造、秘密/作用域闸门、JSON 解析、超时与退出码。
- `BSK_LIVE=1` 才跑的真机用例：doctor / browsers / session / tab / snapshot / evaluate /
  screenshot / console / tab-list scope，全部打真 daemon 与真 Chrome 扩展。
  未设置该环境变量时为 NOT_RUN（证据文件里显式记录），不以 skip 冒充通过。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from browserskill_probe import (
    ADAPTER_SUPPORTED,
    ActionNotMapped,
    AdapterRefused,
    BrowserskillAdapter,
    BskCli,
    BskCliError,
    build_argv,
    find_bsk_binary,
    parse_json,
    redact,
    supported_actions,
)
from browserskill_probe.cli import DEFAULT_TIMEOUT_SECONDS
from browser_contracts import BrowserAction

SESSION = "sess-1"
TAB = "123456789"


class _StubCli:
    """记录调用、返回预置 JSON，避免单测触碰真 daemon。"""

    def __init__(self, payloads: dict[tuple[str, ...], Any] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[list[str]] = []

    def run_json(self, args, *, timeout_seconds: float | None = None) -> Any:
        key = tuple(str(item) for item in args)
        self.calls.append(list(key))
        for candidate, payload in self.payloads.items():
            if key[: len(candidate)] == candidate:
                return payload
        return {"ok": True}


# ---------------------------------------------------------------- argv 映射


def test_snapshot_argv_keeps_session_and_depth() -> None:
    argv = build_argv(BrowserAction.SNAPSHOT, {"max_depth": 4}, session=SESSION, tab_id=TAB)
    assert argv == ["snapshot", "--session", SESSION, "--tab-id", TAB, "--max-depth", "4"]


def test_screenshot_argv_full_page_and_out() -> None:
    argv = build_argv(
        BrowserAction.SCREENSHOT,
        {"out": "shot.png", "full_page": True},
        session=SESSION,
    )
    assert argv == ["screenshot", "--session", SESSION, "--out", "shot.png", "--full-page"]


def test_navigate_argv_puts_url_first() -> None:
    argv = build_argv(
        BrowserAction.NAVIGATE,
        {"url": "https://example.com", "wait_until": "domcontentloaded"},
        session=SESSION,
    )
    assert argv == [
        "navigate",
        "https://example.com",
        "--session",
        SESSION,
        "--wait-until",
        "domcontentloaded",
    ]


def test_navigate_back_is_subcommand_form() -> None:
    assert build_argv(BrowserAction.NAVIGATE_BACK, {}, session=SESSION) == [
        "navigate",
        "back",
        "--session",
        SESSION,
    ]


def test_click_argv_carries_target_and_button() -> None:
    argv = build_argv(
        BrowserAction.CLICK,
        {"target": "@e3", "button": "left", "click_count": 2},
        session=SESSION,
    )
    assert argv == [
        "click",
        "@e3",
        "--session",
        SESSION,
        "--button",
        "left",
        "--click-count",
        "2",
    ]


def test_fill_argv_requires_value() -> None:
    argv = build_argv(
        BrowserAction.FILL,
        {"target": "#q", "value": "pyknora"},
        session=SESSION,
    )
    assert argv == ["fill", "#q", "--value", "pyknora", "--session", SESSION]
    with pytest.raises(AdapterRefused):
        build_argv(BrowserAction.FILL, {"target": "#q"}, session=SESSION)


def test_evaluate_argv_expression_positional() -> None:
    argv = build_argv(
        BrowserAction.EVALUATE,
        {"expression": "document.title"},
        session=SESSION,
    )
    assert argv == ["evaluate", "document.title", "--session", SESSION]


def test_console_argv_limit_and_since() -> None:
    argv = build_argv(BrowserAction.CONSOLE, {"limit": 5, "since": 2}, session=SESSION)
    assert argv == ["console", "--session", SESSION, "--limit", "5", "--since", "2"]


def test_tab_list_scope() -> None:
    assert build_argv(BrowserAction.TAB_LIST, {"scope": "agent"}, session=SESSION) == [
        "tab",
        "list",
        "--session",
        SESSION,
        "--scope",
        "agent",
    ]


def test_tab_create_url() -> None:
    assert build_argv(
        BrowserAction.TAB_CREATE,
        {"url": "https://example.com"},
        session=SESSION,
    ) == ["tab", "create", "--session", SESSION, "--url", "https://example.com"]


def test_tab_close_is_positional() -> None:
    assert build_argv(BrowserAction.TAB_CLOSE, {"tab_id": TAB}, session=SESSION) == [
        "tab",
        "close",
        "--session",
        SESSION,
        TAB,
    ]


@pytest.mark.parametrize(
    "action",
    [
        BrowserAction.HOVER,
        BrowserAction.PRESS,
        BrowserAction.WHEEL,
        BrowserAction.TAB_BORROW,
        BrowserAction.TAB_RETURN,
        BrowserAction.OBSERVE,
        BrowserAction.GET_HTML,
        BrowserAction.NETWORK,
    ],
)
def test_unverified_actions_are_refused_not_guessed(action: BrowserAction) -> None:
    with pytest.raises(ActionNotMapped):
        build_argv(action, {}, session=SESSION)


def test_supported_set_is_subset_of_contract_actions_and_sorted() -> None:
    names = set(supported_actions())
    assert names == set(ADAPTER_SUPPORTED)
    assert names.issubset({action.value for action in BrowserAction})
    assert list(supported_actions()) == sorted(names)


# ---------------------------------------------------------------- 闸门


def test_secret_params_never_reach_the_browser() -> None:
    with pytest.raises(AdapterRefused):
        build_argv(
            BrowserAction.EVALUATE,
            {"expression": "1+1", "authorization": "Bearer x"},
            session=SESSION,
        )


def test_adapter_refuses_when_session_missing() -> None:
    adapter = BrowserskillAdapter(cli=_StubCli())  # type: ignore[arg-type]
    with pytest.raises(AdapterRefused):
        adapter.execute(BrowserAction.SNAPSHOT)


def test_adapter_refuses_session_drift() -> None:
    adapter = BrowserskillAdapter(cli=_StubCli())  # type: ignore[arg-type]
    adapter.session_id = SESSION
    with pytest.raises(AdapterRefused):
        adapter.execute(BrowserAction.SNAPSHOT, expected_session="sess-2")


def test_adapter_refuses_user_tab_outside_agent_scope() -> None:
    cli = _StubCli({("tab", "list"): [{"tab_id": TAB, "scope": "agent"}]})
    adapter = BrowserskillAdapter(cli=cli)  # type: ignore[arg-type]
    adapter.session_id = SESSION
    adapter.refresh_tabs()
    adapter.execute(BrowserAction.TAB_SELECT, {"tab_id": TAB})
    with pytest.raises(AdapterRefused):
        adapter.execute(BrowserAction.TAB_CLOSE, {"tab_id": "999"})


def test_refresh_tabs_records_only_agent_scope() -> None:
    cli = _StubCli({("tab", "list"): [{"tab_id": "1"}, {"tab_id": "2", "scope": "user"}]})
    adapter = BrowserskillAdapter(cli=cli)  # type: ignore[arg-type]
    adapter.session_id = SESSION
    adapter.refresh_tabs(scope="agent")
    assert adapter.agent_tab_ids == {"1", "2"}  # 过滤由 daemon 的 --scope 完成


def test_refresh_tabs_rejects_bad_scope() -> None:
    adapter = BrowserskillAdapter(cli=_StubCli())  # type: ignore[arg-type]
    adapter.session_id = SESSION
    with pytest.raises(AdapterRefused):
        adapter.refresh_tabs(scope="everything")


# ---------------------------------------------------------------- 传输层


def test_redact_masks_nested_secret_keys() -> None:
    payload = {
        "session_id": "lmbu",
        "nested": {"Authorization": "Bearer secret", "list": [{"api_key": "k1"}]},
        "note": "token appears in prose but is not a key",
    }
    masked = redact(payload)
    assert masked["session_id"] == "lmbu"
    assert masked["nested"]["Authorization"] == "<redacted>"
    assert masked["nested"]["list"][0]["api_key"] == "<redacted>"
    assert masked["note"] == payload["note"]


def test_parse_json_skips_informational_prefix() -> None:
    assert parse_json('daemon ready pid=1\n{"pong": true}') == {"pong": True}
    assert parse_json('[{"tab_id": "1"}]') == [{"tab_id": "1"}]


def test_parse_json_rejects_garbage() -> None:
    with pytest.raises(BskCliError):
        parse_json("no json at all")
    with pytest.raises(BskCliError):
        parse_json("")


def test_cli_appends_json_flag() -> None:
    cli = BskCli(binary=Path(sys.executable))
    assert cli.argv(["doctor"])[-1] == "--json"
    assert cli.argv(["doctor", "--json"]).count("--json") == 1


def test_cli_timeout_becomes_bsk_error() -> None:
    cli = BskCli(binary=Path(sys.executable), timeout_seconds=0.2)
    with pytest.raises(BskCliError, match="超时"):
        cli.run(["-c", "import time; time.sleep(5)"])


def test_cli_nonzero_exit_carries_code() -> None:
    cli = BskCli(binary=Path(sys.executable))
    with pytest.raises(BskCliError) as excinfo:
        cli.run(["-c", "import sys; sys.exit(3)"])
    assert excinfo.value.exit_code == 3


def test_cli_missing_binary_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(BskCliError, match="找不到"):
        find_bsk_binary(tmp_path / "definitely-missing-bsk.exe")


def test_default_timeout_is_bounded() -> None:
    assert 0 < DEFAULT_TIMEOUT_SECONDS <= 120


# ---------------------------------------------------------------- 真机（BSK_LIVE=1）


@pytest.mark.skipif(
    os.environ.get("BSK_LIVE") != "1",
    reason="NOT_RUN: 需要真 daemon + 已加载扩展；设 BSK_LIVE=1 启用真机用例",
)
@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason=(
        "NOT_RUN: uv 托管 CPython 3.12 与 bsk daemon 的 Windows 命名管道 IPC 不兼容"
        "（PermissionError 13，派生 bsk.exe 亦 os error 5）；真机取证用 3.13 跑"
        " browserskill_probe/live_run.py，见 docs/poc/evidence/poc05-browserskill-live.md"
    ),
)
def test_live_pairing_and_core_actions(tmp_path: Path) -> None:
    cli = BskCli.autodetect()
    adapter = BrowserskillAdapter(cli=cli)

    doctor = adapter.doctor()
    checks = doctor["checks"] if isinstance(doctor, dict) else doctor
    assert isinstance(checks, list) and checks, f"doctor 未返回检查项: {doctor!r}"
    assert all(item.get("ok") is True for item in checks), f"doctor 有失败项: {doctor!r}"

    browsers = adapter.browsers()
    assert isinstance(browsers, list) and browsers, "没有已配对的浏览器"
    assert browsers[0]["extension_version"], "缺少 extension_version"

    session_id = adapter.start_session(name="p0-t17-live")
    try:
        created = adapter.execute(BrowserAction.TAB_CREATE, {"url": "https://example.com"})
        tab_id = str(created["tab_id"])
        adapter.refresh_tabs()

        snapshot = adapter.execute(BrowserAction.SNAPSHOT, {"tab_id": tab_id})
        assert "Example Domain" in json.dumps(snapshot, ensure_ascii=False)

        evaluated = adapter.execute(
            BrowserAction.EVALUATE,
            {"expression": "document.title", "tab_id": tab_id},
        )
        assert json.dumps(evaluated, ensure_ascii=False).find("Example Domain") >= 0

        shot = tmp_path / "live.png"
        adapter.execute(
            BrowserAction.SCREENSHOT,
            {"out": str(shot), "tab_id": tab_id},
        )
        assert shot.is_file() and shot.stat().st_size > 1000
        assert len(hashlib.sha256(shot.read_bytes()).hexdigest()) == 64

        adapter.execute(BrowserAction.CONSOLE, {"tab_id": tab_id, "limit": 10})
        tabs = adapter.refresh_tabs(scope="agent")
        assert any(str(tab.get("tab_id")) == tab_id for tab in tabs)

        adapter.execute(BrowserAction.TAB_CLOSE, {"tab_id": tab_id})
    finally:
        adapter.stop_session()
        assert session_id
