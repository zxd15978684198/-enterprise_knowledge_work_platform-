"""P0-T16 Playwright 受控读取探针 + 把 T15 ``BrowserPort`` 的导航动作接到网络策略。

两个职责，刻意分开：

1. ``PolicyGuardedBrowserPort``：T15 的 ``BrowserPort`` 只对 ``navigate`` 调策略
   （``browser_contracts._check_navigation`` 对 ``navigate_back/forward/reload`` 直接 return），
   这里补上后三个动作的接线：从页面上下文推出目标 URL，过 ``HopGuard`` 授权，再转发。
2. ``PlaywrightNavigator``：真实浏览器读取（每 run 独立 context、每请求过策略、超限中止）。
   本机未安装 Playwright 时 ``detect_playwright()`` 返回不可用并给出原因，调用方必须把
   浏览器部分记 **NOT_RUN**，不得用 skip 或 mock 冒充；替代验证是 ``substitute_http_probe``。

策略只在这里"用"，不在这里"实现"：域名/IP/限流/预算全部在 ``network_policy`` 包里。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from browser_contracts import (
    BrowserAction,
    BrowserErrorCode,
    BrowserPort,
    BrowserResult,
    BrowserSession,
    RPCError,
)
from network_policy import (
    FetchResult,
    NetworkPolicyEngine,
    PolicyViolation,
    Reason,
    SafeFetcher,
)

NAVIGATIONAL_ACTIONS: Final[frozenset[BrowserAction]] = frozenset(
    {
        BrowserAction.NAVIGATE,
        BrowserAction.NAVIGATE_BACK,
        BrowserAction.NAVIGATE_FORWARD,
        BrowserAction.RELOAD,
    }
)
HISTORY_BACK_ACTIONS: Final[frozenset[BrowserAction]] = frozenset(
    {BrowserAction.NAVIGATE_BACK, BrowserAction.NAVIGATE_FORWARD}
)


# ---------------------------------------------------------------------------
# Playwright 可用性
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlaywrightAvailability:
    available: bool
    reason: str
    executable: str | None = None


class PlaywrightUnavailable(RuntimeError):
    """浏览器不可用时**显式失败**：绝不返回空结果冒充"读取成功"。"""


def detect_playwright() -> PlaywrightAvailability:
    """探测 Playwright SDK 与 chromium 二进制；任何一步缺失都给出可写进证据的原因。"""

    if importlib.util.find_spec("playwright") is None:
        return PlaywrightAvailability(
            available=False,
            reason=(
                "playwright 未安装（importlib.util.find_spec('playwright')=None）；"
                "本机无法执行真实浏览器导航，Playwright 部分记 NOT_RUN"
            ),
        )
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都算不可用
        return PlaywrightAvailability(
            available=False,
            reason=f"playwright 已安装但无法导入 sync_api：{type(exc).__name__}: {exc}",
        )
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
    except Exception as exc:  # noqa: BLE001 - 浏览器缺失/驱动失败都记原因
        return PlaywrightAvailability(
            available=False,
            reason=f"chromium 启动器不可用：{type(exc).__name__}: {exc}",
        )
    if not executable or not Path(executable).exists():
        cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "默认缓存目录"
        return PlaywrightAvailability(
            available=False,
            reason=f"chromium 二进制缺失（{cache}）：{executable!r}",
        )
    return PlaywrightAvailability(
        available=True, reason="playwright + chromium 可用", executable=executable
    )


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """一次真实读取的事实；``blocked_requests`` 是被策略中止的子资源 URL。"""

    requested_url: str
    final_url: str
    title: str
    text: str
    screenshot_bytes: int
    blocked_requests: tuple[str, ...] = ()


class PlaywrightNavigator:
    """每 run 独立 context；**每个请求**（含子资源）先过 ``NetworkPolicyEngine.authorize``。"""

    def __init__(
        self,
        engine: NetworkPolicyEngine,
        *,
        headless: bool = True,
        max_text_chars: int = 8000,
        max_screenshot_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        availability = detect_playwright()
        if not availability.available:
            raise PlaywrightUnavailable(availability.reason)
        self._engine = engine
        self._headless = headless
        self._max_text_chars = max_text_chars
        self._max_screenshot_bytes = max_screenshot_bytes

    def navigate(
        self,
        url: str,
        *,
        workspace_id: UUID,
        timeout_ms: int = 15_000,
    ) -> PageSnapshot:
        from playwright.sync_api import sync_playwright

        blocked: list[str] = []
        with self._engine.open_guard(
            workspace_id=workspace_id, total_budget_ms=timeout_ms
        ) as guard:
            start = guard.check(url)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self._headless)
                try:
                    context = browser.new_context(java_script_enabled=True)
                    page = context.new_page()

                    def route_handler(route: object) -> None:
                        request_url = getattr(getattr(route, "request", None), "url", "")
                        try:
                            self._engine.authorize(str(request_url), workspace_id=workspace_id)
                        except PolicyViolation:
                            blocked.append(str(request_url))
                            route.abort()  # type: ignore[attr-defined]
                            return
                        route.continue_()  # type: ignore[attr-defined]

                    page.route("**/*", route_handler)
                    page.goto(start.url, timeout=timeout_ms, wait_until="domcontentloaded")
                    title = page.title()
                    text = page.inner_text("body", timeout=timeout_ms)
                    shot = page.screenshot(type="png")
                    if len(shot) > self._max_screenshot_bytes:
                        raise PolicyViolation(
                            Reason.BODY_TOO_LARGE,
                            f"截图 {len(shot)} 字节超过上限 {self._max_screenshot_bytes}",
                            url=url,
                        )
                    final_url = page.url
                    context.close()
                finally:
                    browser.close()
            guard.check_deadline()
            return PageSnapshot(
                requested_url=url,
                final_url=final_url,
                title=title,
                text=text[: self._max_text_chars],
                screenshot_bytes=len(shot),
                blocked_requests=tuple(blocked),
            )


def substitute_http_probe(
    url: str, *, workspace_id: UUID, engine: NetworkPolicyEngine
) -> FetchResult:
    """Playwright 缺失时的**替代验证**：证明出口策略在连接层生效。

    它只能证明"策略 + 连接层"这一半；JS 渲染、子资源、截图属 NOT_RUN，不得以此宣称 T16 全通。
    """

    return SafeFetcher(engine).fetch(url, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# T15 导航动作接线（navigate_back / navigate_forward / reload）
# ---------------------------------------------------------------------------
@dataclass
class PageContext:
    """一个会话的页面位置；``history`` 是所有**已授权**的 URL，不允许由调用方写入。"""

    history: list[str] = field(default_factory=list)
    cursor: int = -1

    @property
    def current_url(self) -> str | None:
        if 0 <= self.cursor < len(self.history):
            return self.history[self.cursor]
        return None

    def record(self, url: str) -> None:
        del self.history[self.cursor + 1 :]
        self.history.append(url)
        self.cursor = len(self.history) - 1

    def candidate(self, action: BrowserAction) -> str | None:
        if action is BrowserAction.NAVIGATE_BACK:
            return self.history[self.cursor - 1] if self.cursor >= 1 else None
        if action is BrowserAction.NAVIGATE_FORWARD:
            if self.cursor + 1 < len(self.history):
                return self.history[self.cursor + 1]
            return None
        if action is BrowserAction.RELOAD:
            return self.current_url
        return None

    def move(self, action: BrowserAction) -> None:
        if action is BrowserAction.NAVIGATE_BACK and self.cursor >= 1:
            self.cursor -= 1
        elif action is BrowserAction.NAVIGATE_FORWARD and self.cursor + 1 < len(self.history):
            self.cursor += 1


class PolicyGuardedBrowserPort:
    """``BrowserPort`` 的策略前置层：四个导航动作都必须先过网络策略。

    ``navigate`` 在 ``BrowserPort`` 内部已经过一遍策略（静态判定），这里再过一遍带预算的
    ``HopGuard``（域名/IP/端口/限流），可理解为"契约闸 + 出口闸"双保险；
    ``navigate_back/forward/reload`` 的目标 URL 由 ``PageContext`` 推出，契约层不检查，
    只有这里检查。
    """

    def __init__(
        self,
        port: BrowserPort,
        engine: NetworkPolicyEngine,
        *,
        workspace_id: UUID,
        context: PageContext | None = None,
    ) -> None:
        self._port = port
        self._engine = engine
        self._workspace_id = workspace_id
        self._context = context or PageContext()

    @property
    def context(self) -> PageContext:
        return self._context

    def execute(
        self,
        session: BrowserSession,
        *,
        action: BrowserAction | str,
        params: dict[str, object] | None = None,
        expected_epoch: int,
        timeout_ms: int,
        **kwargs: object,
    ) -> BrowserResult | RPCError:
        try:
            resolved = BrowserAction(action)
        except ValueError:
            # 未知动作交给契约层报 INVALID_BROWSER_ACTION，这里不抢错误语义。
            return self._port.execute(
                session,
                action=action,
                params=params,
                expected_epoch=expected_epoch,
                timeout_ms=timeout_ms,
                **kwargs,  # type: ignore[arg-type]
            )

        candidate: str | None = None
        if resolved in NAVIGATIONAL_ACTIONS:
            candidate = self._target_url(resolved, params or {})
            if candidate is None:
                return self._deny(
                    PolicyViolation(
                        Reason.NO_PAGE_CONTEXT,
                        f"{resolved} 需要服务器侧页面上下文（先 navigate 成功一次）",
                        data={"action": str(resolved)},
                    )
                )
            denial = self._authorize(resolved, candidate)
            if denial is not None:
                return denial

        result = self._port.execute(
            session,
            action=action,
            params=params,
            expected_epoch=expected_epoch,
            timeout_ms=timeout_ms,
            **kwargs,  # type: ignore[arg-type]
        )
        if isinstance(result, BrowserResult):
            self._commit(resolved, candidate, result)
        return result

    # -- 内部 -------------------------------------------------------------
    def _target_url(self, action: BrowserAction, params: dict[str, object]) -> str | None:
        if action is BrowserAction.NAVIGATE:
            url = params.get("url")
            return url if isinstance(url, str) else None
        return self._context.candidate(action)

    def _authorize(self, action: BrowserAction, url: str) -> RPCError | None:
        try:
            with self._engine.open_guard(workspace_id=self._workspace_id) as guard:
                guard.check(url)
        except PolicyViolation as violation:
            return self._deny(violation, action=action)
        return None

    def _deny(
        self, violation: PolicyViolation, *, action: BrowserAction | None = None
    ) -> RPCError:
        return RPCError(
            BrowserErrorCode.SSRF_BLOCKED,
            f"{violation.reason}: {violation.message}",
            data={
                "policy_reason": str(violation.reason),
                "action": str(action) if action is not None else "",
                **violation.data,
            },
        )

    def _commit(
        self, action: BrowserAction, candidate: str | None, result: BrowserResult
    ) -> None:
        landed = result.final_url or candidate
        if action is BrowserAction.NAVIGATE and landed:
            self._context.record(landed)
        elif action in HISTORY_BACK_ACTIONS:
            self._context.move(action)
        elif action is BrowserAction.RELOAD and landed:
            self._context.record(landed)
