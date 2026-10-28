"""把 T15 的 `BrowserAction` 映射到 `bsk` CLI，并在发出前做 allowlist / 秘密 / 作用域三道闸。

映射只覆盖本任务**逐个 `bsk ... --help` 核对过签名**的子命令（会话、标签页、快照、截图、
求值、控制台、导航、点击、填写）。其余动作（`observe`、`hover`、`press`、`wheel`、`select`、
`get_html`、`network`、`tab_borrow`、`tab_return` 等）一律抛 `ActionNotMapped`，
不用猜测的 argv 去赌——它们留给 T18/T16 顺着 `bsk <cmd> --help` 补齐。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from browser_contracts import BrowserAction

from .cli import BskCli, BskCliError, parse_json, redact

_TAB_SCOPED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        BrowserAction.TAB_CLOSE.value,
        BrowserAction.TAB_SELECT.value,
    }
)
_SECRET_PARAM_MARKERS: Final[tuple[str, ...]] = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)
ADAPTER_SUPPORTED: Final[frozenset[str]] = frozenset(
    {
        BrowserAction.SNAPSHOT.value,
        BrowserAction.SCREENSHOT.value,
        BrowserAction.NAVIGATE.value,
        BrowserAction.NAVIGATE_BACK.value,
        BrowserAction.NAVIGATE_FORWARD.value,
        BrowserAction.CLICK.value,
        BrowserAction.FILL.value,
        BrowserAction.EVALUATE.value,
        BrowserAction.CONSOLE.value,
        BrowserAction.TAB_LIST.value,
        BrowserAction.TAB_CREATE.value,
        BrowserAction.TAB_CLOSE.value,
        BrowserAction.TAB_SELECT.value,
    }
)


class ActionNotMapped(LookupError):
    """该动作的 CLI 签名尚未核对，按契约拒绝而不是猜。"""


class AdapterRefused(PermissionError):
    """闸门拒绝：秘密参数、越界标签页、未知动作。"""


def _is_secret_param(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_PARAM_MARKERS)


def _as_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterRefused(f"参数 {name!r} 必须是非空字符串")
    return value


def _opt(argv: list[str], flag: str, value: Any, *, name: str | None = None) -> None:
    if value is None or value == "":
        return
    if isinstance(value, bool):
        if value:
            argv.append(flag)
        return
    argv.extend([flag, _as_text(str(value), name=name or flag)])


def build_argv(
    action: BrowserAction | str,
    params: Mapping[str, Any],
    *,
    session: str,
    tab_id: str | None = None,
) -> list[str]:
    """纯函数：动作 + 参数 → `bsk` 子命令 argv（不含二进制与 `--json`，由 `BskCli` 补）。"""

    name = action.value if isinstance(action, BrowserAction) else str(action)
    if name not in ADAPTER_SUPPORTED:
        raise ActionNotMapped(f"动作 {name!r} 的 bsk CLI 签名未核对，拒绝派发")
    for key in params:
        if _is_secret_param(key):
            raise AdapterRefused(f"动作 {name!r} 携带秘密参数 {key!r}，拒绝下发给浏览器")
    _as_text(session, name="session")
    target_tab = params.get("tab_id", tab_id)
    if target_tab is not None:
        target_tab = _as_text(target_tab, name="tab_id")

    argv: list[str] = []
    if name == BrowserAction.SNAPSHOT.value:
        argv = ["snapshot", "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--max-depth", params.get("max_depth"), name="max_depth")
        _opt(argv, "--max-tokens", params.get("max_tokens"), name="max_tokens")
    elif name == BrowserAction.SCREENSHOT.value:
        argv = ["screenshot", "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--out", params.get("out"), name="out")
        _opt(argv, "--ref", params.get("ref"))
        if params.get("full_page"):
            argv.append("--full-page")
    elif name == BrowserAction.NAVIGATE.value:
        argv = ["navigate", _as_text(params.get("url"), name="url"), "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--wait-until", params.get("wait_until"), name="wait_until")
        _opt(argv, "--timeout", params.get("timeout"), name="timeout")
    elif name == BrowserAction.NAVIGATE_BACK.value:
        argv = ["navigate", "back", "--session", session]
        _opt(argv, "--tab-id", target_tab)
    elif name == BrowserAction.NAVIGATE_FORWARD.value:
        argv = ["navigate", "forward", "--session", session]
        _opt(argv, "--tab-id", target_tab)
    elif name == BrowserAction.CLICK.value:
        argv = ["click", _as_text(params.get("target"), name="target"), "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--button", params.get("button"))
        _opt(argv, "--click-count", params.get("click_count"), name="click_count")
        _opt(argv, "--modifiers", params.get("modifiers"))
    elif name == BrowserAction.FILL.value:
        argv = [
            "fill",
            _as_text(params.get("target"), name="target"),
            "--value",
            _as_text(params.get("value"), name="value"),
            "--session",
            session,
        ]
        _opt(argv, "--tab-id", target_tab)
        if params.get("no_clear"):
            argv.append("--no-clear")
    elif name == BrowserAction.EVALUATE.value:
        expression = _as_text(params.get("expression"), name="expression")
        argv = ["evaluate", expression, "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--timeout", params.get("timeout"), name="timeout")
    elif name == BrowserAction.CONSOLE.value:
        argv = ["console", "--session", session]
        _opt(argv, "--tab-id", target_tab)
        _opt(argv, "--limit", params.get("limit"), name="limit")
        _opt(argv, "--since", params.get("since"), name="since")
    elif name == BrowserAction.TAB_LIST.value:
        argv = ["tab", "list", "--session", session]
        _opt(argv, "--scope", params.get("scope"))
    elif name == BrowserAction.TAB_CREATE.value:
        argv = ["tab", "create", "--session", session]
        _opt(argv, "--url", params.get("url"))
        if params.get("no_active"):
            argv.append("--no-active")
    elif name in _TAB_SCOPED_ACTIONS:
        keyword = "close" if name == BrowserAction.TAB_CLOSE.value else "select"
        tab = _as_text(params.get("tab_id", tab_id), name="tab_id")
        argv = ["tab", keyword, "--session", session, tab]
    return argv


@dataclass(slots=True)
class BrowserskillAdapter:
    """会话生命周期 + 动作派发；`agent_tab_ids` 由 `refresh_tabs()` 从 `scope=agent` 拉取。"""

    cli: BskCli
    session_id: str | None = None
    agent_tab_ids: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    # ---- 会话与观测 ----
    def doctor(self) -> Any:
        # doctor 在有 `status="na"` 项时退出码为 1，但仍输出完整 JSON；不能因此判失败。
        _code, stdout, stderr = self.cli.run(["doctor"], check=False)
        try:
            return redact(parse_json(stdout))
        except BskCliError as exc:
            raise BskCliError(f"doctor 输出不可解析: {exc}", stdout=stdout, stderr=stderr) from exc

    def browsers(self) -> Any:
        return self.cli.run_json(["browsers"])

    def list_sessions(self) -> Any:
        return self.cli.run_json(["session", "list"])

    def start_session(self, *, name: str | None = None, browser: str | None = None,
                      no_focus: bool = True) -> str:
        argv = ["session", "start"]
        _opt(argv, "--name", name)
        _opt(argv, "--browser", browser)
        if no_focus:
            argv.append("--no-focus")
        payload = self.cli.run_json(argv)
        session_id = payload.get("session_id") if isinstance(payload, Mapping) else None
        if not isinstance(session_id, str) or not session_id:
            raise AdapterRefused(f"session start 未返回 session_id: {payload!r}")
        self.session_id = session_id
        return session_id

    def stop_session(self, *, all_sessions: bool = False) -> Any:
        if all_sessions:
            return self.cli.run_json(["session", "stop", "--all"])
        if not self.session_id:
            raise AdapterRefused("没有活动 session，无法 stop")
        payload = self.cli.run_json(["session", "stop", self.session_id])
        self.session_id = None
        self.agent_tab_ids.clear()
        return payload

    def refresh_tabs(self, *, scope: str = "agent") -> list[dict[str, Any]]:
        if scope not in {"agent", "user", "all"}:
            raise AdapterRefused(f"scope 非法: {scope!r}")
        self._require_session()
        payload = self.cli.run_json(["tab", "list", "--session", self.session_id, "--scope", scope])
        # CLI 返回 {"tabs": [...]}；兼容裸数组形式。
        tabs = payload.get("tabs", []) if isinstance(payload, Mapping) else payload
        if scope == "agent" and isinstance(tabs, list):
            self.agent_tab_ids = {
                str(tab["tab_id"])
                for tab in tabs
                if isinstance(tab, Mapping) and "tab_id" in tab
            }
        return tabs if isinstance(tabs, list) else []

    # ---- 动作 ----
    def execute(self, action: BrowserAction | str, params: Mapping[str, Any] | None = None, *,
                expected_session: str | None = None) -> Any:
        params = dict(params or {})
        self._require_session()
        if expected_session is not None and expected_session != self.session_id:
            raise AdapterRefused("session 不匹配（epoch/session 漂移）")
        name = action.value if isinstance(action, BrowserAction) else str(action)
        tab_id = params.get("tab_id")
        if (
            name in _TAB_SCOPED_ACTIONS
            and tab_id is not None
            and str(tab_id) not in self.agent_tab_ids
        ):
            raise AdapterRefused(f"tab {tab_id} 不在本 session 的 agent 作用域内")
        argv = build_argv(action, params, session=self.session_id or "", tab_id=tab_id)
        payload = self.cli.run_json(argv)
        self.calls.append(
            {"action": name, "argv": redact(argv), "session": self.session_id,
             "ok": True, "tab_id": tab_id}
        )
        return payload

    def _require_session(self) -> None:
        if not self.session_id:
            raise AdapterRefused("尚未 start_session()")


def supported_actions() -> Iterable[str]:
    """证据表与测试共用：当前可派发的动作名（排序后稳定）。"""

    return sorted(ADAPTER_SUPPORTED)


__all__ = [
    "ADAPTER_SUPPORTED",
    "ActionNotMapped",
    "AdapterRefused",
    "BrowserskillAdapter",
    "build_argv",
    "supported_actions",
]
