"""P0-T15：Browser port 单入口契约、能力协商与 Scope 注入。

验收依据：
- ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T15（≥20 正负契约 fixture；unsupported 不降级
  偷偷执行；写 action 无 approval 不能 dispatch；secret 字段拒绝；schema 拒未知参数/任意 JS/
  伪 session/旧 locator）。
- ``docs/tdd/05_Browser_Adapter.md`` §3–6（BrowserRequest/BrowserResult/Capabilities、错误
  码集合、超时与重试语义、owner/epoch 约束）。
- ``docs/poc/07_Contract_Closure.md`` C-05（NEEDS_POC：真正兼容性由 T17 真配对证明）。
- ``docs/poc/evidence/browserskill_protocol_manifest.md``（T14）：31 个 ``tool.*`` 动作白名单
  （A）、U1–U9 未知项、错误码表、网关强制改写项。

本模块的边界（照抄任务卡，不许越界）：
- **只统一业务语义**，不重写两套浏览器；不启动 daemon、不连 WS、不做浏览器动作。
- **网络策略不在本任务实现**：``NetworkPolicy`` 只是前置拦截点，由 T16 落地；默认实现
  ``DenyAllNetworkPolicy`` fail-closed，绝不假装 SSRF/域名/速率已被覆盖。
- **31 个动作不各建一个方法**：唯一入口是 :meth:`BrowserPort.execute`，动作由
  :class:`BrowserAction` 枚举表达，未知动作拒绝。
- 能力协商只做「已测试 allowlist ∩ handshake」；未知协议/版本一律
  ``BROWSER_INCOMPATIBLE``，**不以非空版本字符串判定兼容**（TDD05 §3）。

返回形状：``execute(...) -> BrowserResult | RPCError``。成功返回事实 DTO；失败返回
``RPCError``（同时是 ``Exception``，内部校验用 ``raise`` 书写，出口统一 return）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Literal, NoReturn, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

__all__ = [
    "ADAPTER_API_VERSION",
    "HUMAN_TIMEOUT_MAX_MS",
    "MAX_TIMEOUT_MS",
    "ActionSpec",
    "Approval",
    "BrowserAction",
    "BrowserCapabilityError",
    "BrowserPort",
    "BrowserResult",
    "BrowserSession",
    "BrowserSessionState",
    "BrowserTransport",
    "CapabilityId",
    "CapabilityStatus",
    "Capabilities",
    "DenyAllNetworkPolicy",
    "NetworkPolicy",
    "PolicyDecision",
    "RPCError",
    "SideEffect",
    "TransportFailure",
    "action_spec",
    "compute_action_hash",
    "negotiate_capabilities",
    "recovery_hint",
    "ACTION_SPECS",
]

# ---------------------------------------------------------------------------
# 版本与上限（证据：TDD05 §3；T14 manifest §6.5）
# ---------------------------------------------------------------------------
ADAPTER_API_VERSION: Final = "browser-port/1"
"""本 port 的契约版本；与后端 wire 版本分开记录，不做字符串包含比较。"""

MAX_TIMEOUT_MS: Final = 60_000
"""模型可见动作的 deadline 上限（``docs/tdd/05_Browser_Adapter.md:46``）。"""

HUMAN_TIMEOUT_MAX_MS: Final = 300_000
"""人工步上限；upstream 把 ``request_help.timeout_ms`` 夹到 1..300000
（``references/WeKnora/internal/browserskill/human.go:27-35``）。"""

MAX_PARAM_ITEMS: Final = 64

AUTOMATIC_RETRY: Final = False
"""超时/取消一律不自动重试：副作用未知（TDD05 §6；manifest §6.5）。"""


# ---------------------------------------------------------------------------
# 错误码（TDD05 §6 canonical 集合 + 本 port 的契约级扩展）
# ---------------------------------------------------------------------------
class BrowserErrorCode(StrEnum):
    """稳定错误码；TDD05 §6 的 canonical 集是前 9 个，其余是本 port 的契约校验码。"""

    BROWSER_OFFLINE = "BROWSER_OFFLINE"
    BROWSER_PAUSED = "BROWSER_PAUSED"
    BROWSER_INCOMPATIBLE = "BROWSER_INCOMPATIBLE"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    OWNER_LOST = "OWNER_LOST"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    TOOL_UNKNOWN_OUTCOME = "TOOL_UNKNOWN_OUTCOME"
    # ---- 契约级扩展（不改 canonical 字面码，只补校验与超时/取消细节） ----
    INVALID_BROWSER_ACTION = "INVALID_BROWSER_ACTION"
    INVALID_BROWSER_PARAM = "INVALID_BROWSER_PARAM"
    BROWSER_PARAM_UNSUPPORTED = "BROWSER_PARAM_UNSUPPORTED"
    SCOPE_INJECTION_REJECTED = "SCOPE_INJECTION_REJECTED"
    SECRET_IN_PARAMS = "SECRET_IN_PARAMS"
    ARBITRARY_JS_REJECTED = "ARBITRARY_JS_REJECTED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    STALE_EPOCH = "STALE_EPOCH"
    SESSION_REVOKED = "SESSION_REVOKED"
    BROWSER_TIMEOUT = "BROWSER_TIMEOUT"
    BROWSER_CANCELLED = "BROWSER_CANCELLED"
    BROWSER_WIRE_ERROR = "BROWSER_WIRE_ERROR"
    UNTRUSTED_BROWSER_CONSTRUCTION = "UNTRUSTED_BROWSER_CONSTRUCTION"


def recovery_hint(code: BrowserErrorCode, data: Mapping[str, object] | None = None) -> str:
    """模型可见的恢复提示（对应 ``browserskill_result.go:13-26`` 的语义，不复述内部细节）。"""

    data = data or {}
    if code is BrowserErrorCode.BROWSER_PAUSED:
        return "会话已暂停：等待用户显式 resume 后重新授权，不要自行重试。"
    if code is BrowserErrorCode.HUMAN_ACTION_REQUIRED:
        return "需要用户人工处理（登录/验证码）；由用户完成后走 resume，模型不得声明已完成。"
    if code is BrowserErrorCode.TOOL_UNKNOWN_OUTCOME:
        return "动作可能已生效：不要重放；交由人工核对页面后再决定。"
    if code is BrowserErrorCode.BROWSER_TIMEOUT and data.get("action_sent"):
        return "请求已发出但结果未知：不要自动重试写动作。"
    if code is BrowserErrorCode.BROWSER_TIMEOUT:
        return "连接前超时，未发出动作；可重新建连后由调用方显式重试。"
    if code is BrowserErrorCode.BROWSER_INCOMPATIBLE:
        return "后端协议/版本不匹配：用同一 pin 重建 daemon 与扩展后再协商。"
    if code is BrowserErrorCode.CAPABILITY_UNSUPPORTED:
        return "该能力未在此后端验证通过：降级为明确 unsupported，不要改用其它动作凑效果。"
    if code is BrowserErrorCode.SSRF_BLOCKED:
        return "导航被出口策略拒绝（T16 尚未接入真实策略时默认拒绝）。"
    if code is BrowserErrorCode.APPROVAL_REQUIRED:
        return "写动作需要先获得审批：approval 必须绑定同一 session、同一动作与同一参数。"
    if code is BrowserErrorCode.APPROVAL_MISMATCH:
        return "审批与本次调用不匹配（动作/参数/会话/有效期之一不符），拒绝执行。"
    if code is BrowserErrorCode.STALE_EPOCH:
        return "会话 epoch 已变化：旧授权失效，必须重新读取 session 后重新审批。"
    if code is BrowserErrorCode.SESSION_REVOKED:
        return "会话已撤销：不能恢复，需用户重新配对/授权。"
    if code is BrowserErrorCode.OWNER_LOST:
        return "owner 连接丢失：未发动作可取消，已发动作按 unknown_outcome 处理。"
    if code is BrowserErrorCode.SCOPE_INJECTION_REJECTED:
        return "作用域只能由服务端注入：调用方/模型不得传 session_id、workspace_id 等字段。"
    if code is BrowserErrorCode.SECRET_IN_PARAMS:
        return "参数含凭据类字段：cookie/token/Authorization 不得进入浏览器工具参数。"
    if code is BrowserErrorCode.ARBITRARY_JS_REJECTED:
        return "禁止执行任意 JS：只允许注册过的抽取 schema 与结构化解包。"
    return "按错误码处理后重试；不要推测后端内部状态。"


class RPCError(Exception):
    """统一的失败返回体（同时是异常，便于内部 ``raise``、出口 ``return``）。

    ``code`` 与 TDD05 §6 及 T14 manifest §6.6 的同名字面码对齐；``data`` 只放脱敏元数据。
    """

    def __init__(
        self,
        code: BrowserErrorCode | str,
        message: str,
        *,
        data: Mapping[str, object] | None = None,
        action_sent: bool = False,
    ) -> None:
        self.code = BrowserErrorCode(code) if not isinstance(code, BrowserErrorCode) else code
        self.message = message
        self.data = dict(data or {})
        self.data.setdefault("action_sent", action_sent)
        super().__init__(f"{self.code}: {message}")

    @property
    def action_sent(self) -> bool:
        return bool(self.data.get("action_sent", False))

    @property
    def retryable(self) -> bool:
        """只表示「可重试建连」，从不表示「可重放动作」（TDD05 §6）。"""

        if self.action_sent:
            return False
        return self.code in {
            BrowserErrorCode.BROWSER_OFFLINE,
            BrowserErrorCode.BROWSER_TIMEOUT,
        }

    @property
    def replay_safe(self) -> bool:
        """已发动作永不 replay-safe；未发动作本来就没生效。"""

        return not self.action_sent

    def to_envelope(self, method: str) -> dict[str, object]:
        """模型可见信封（形状对齐 ``browserskill_result.go:13-24``）。"""

        return {
            "method": method,
            "error": {"code": str(self.code), "message": self.message, "data": self.data},
            "recovery_hint": recovery_hint(self.code, self.data),
        }


class BrowserCapabilityError(RPCError):
    """能力协商失败（协商期异常；``execute`` 出口统一转 :class:`RPCError`）。"""


# ---------------------------------------------------------------------------
# 动作枚举：31 个 pinned upstream 方法名（A：manifest §6.3 / manager.go:816-848）
# ---------------------------------------------------------------------------
class BrowserAction(StrEnum):
    """``tool.<name>`` 的 ``<name>`` 部分；枚举成员与 T14 证据表逐行对应。"""

    OBSERVE = "observe"
    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"
    NAVIGATE = "navigate"
    NAVIGATE_BACK = "navigate_back"
    NAVIGATE_FORWARD = "navigate_forward"
    RELOAD = "reload"
    CLICK = "click"
    FILL = "fill"
    PRESS = "press"
    HOVER = "hover"
    WHEEL = "wheel"
    SCROLL_TO = "scroll_to"
    FOCUS = "focus"
    BLUR = "blur"
    SELECT = "select"
    TAB_LIST = "tab_list"
    TAB_CREATE = "tab_create"
    TAB_SELECT = "tab_select"
    TAB_CLOSE = "tab_close"
    TAB_BORROW = "tab_borrow"
    TAB_RETURN = "tab_return"
    GET_HTML = "get_html"
    EVALUATE = "evaluate"
    CONSOLE = "console"
    NETWORK = "network"
    WAIT_FOR_NAVIGATION = "wait_for_navigation"
    WAIT_MS = "wait_ms"
    WINDOW_RESIZE = "window_resize"
    EMULATE = "emulate"
    REQUEST_HELP = "request_help"


class SideEffect(StrEnum):
    """副作用分类；``WRITE`` 必须有审批（Backlog：写 action 无 approval 不能 dispatch）。"""

    READ = "read"
    NAVIGATE = "navigate"
    WRITE = "write"
    CONTROL = "control"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """单个动作的契约画像：参数白名单、副作用、必填约束与证据出处。"""

    action: BrowserAction
    effect: SideEffect
    params: frozenset[str]
    required: tuple[str, ...] = ()
    one_of: tuple[tuple[str, ...], ...] = ()
    human_step: bool = False
    arbitrary_js: bool = False
    evidence: str = ""

    @property
    def requires_approval(self) -> bool:
        return self.effect is SideEffect.WRITE

    @property
    def navigational(self) -> bool:
        return self.effect is SideEffect.NAVIGATE

    @property
    def max_timeout_ms(self) -> int:
        return HUMAN_TIMEOUT_MAX_MS if self.human_step else MAX_TIMEOUT_MS


def _spec(
    action: BrowserAction,
    effect: SideEffect,
    params: str,
    *,
    required: tuple[str, ...] = (),
    one_of: tuple[tuple[str, ...], ...] = (),
    human_step: bool = False,
    arbitrary_js: bool = False,
    evidence: str = "",
) -> ActionSpec:
    return ActionSpec(
        action=action,
        effect=effect,
        params=frozenset(params.split()) if params else frozenset(),
        required=required,
        one_of=one_of,
        human_step=human_step,
        arbitrary_js=arbitrary_js,
        evidence=evidence,
    )


# 参数列取自 T14 manifest §6.3 的「参数（B，``browserskill_prompt.go:30-62``）」列；
# 类型校验（``_PARAM_TYPES``）是本 port 自加的保守子集，不是上游 schema（U1 仍为 UNKNOWN）。
_SPEC_LIST: Final[tuple[ActionSpec, ...]] = (
    _spec(
        BrowserAction.OBSERVE,
        SideEffect.READ,
        "tab_id max_text_chars max_depth probe_hover debug_surfaces",
        evidence="manager.go:818",
    ),
    _spec(
        BrowserAction.SNAPSHOT,
        SideEffect.READ,
        "tab_id max_text_chars max_depth",
        evidence="manager.go:817",
    ),
    _spec(BrowserAction.SCREENSHOT, SideEffect.READ, "tab_id ref", evidence="manager.go:838"),
    _spec(
        BrowserAction.NAVIGATE,
        SideEffect.NAVIGATE,
        "url tab_id wait_until",
        required=("url",),
        evidence="manager.go:819,955-959",
    ),
    _spec(
        BrowserAction.NAVIGATE_BACK,
        SideEffect.NAVIGATE,
        "tab_id wait_until",
        evidence="manager.go:820",
    ),
    _spec(
        BrowserAction.NAVIGATE_FORWARD,
        SideEffect.NAVIGATE,
        "tab_id wait_until",
        evidence="manager.go:821",
    ),
    _spec(
        BrowserAction.RELOAD,
        SideEffect.NAVIGATE,
        "tab_id hard wait_until",
        evidence="manager.go:822",
    ),
    _spec(
        BrowserAction.CLICK,
        SideEffect.WRITE,
        "ref selector button click_count modifiers tab_id",
        one_of=(("ref", "selector"),),
        evidence="manager.go:823",
    ),
    _spec(
        BrowserAction.FILL,
        SideEffect.WRITE,
        "ref selector value clear_before tab_id",
        required=("value",),
        one_of=(("ref", "selector"),),
        evidence="manager.go:824",
    ),
    _spec(
        BrowserAction.PRESS,
        SideEffect.WRITE,
        "key ref selector hold_ms modifiers tab_id",
        required=("key",),
        evidence="manager.go:825",
    ),
    _spec(
        BrowserAction.HOVER,
        SideEffect.WRITE,
        "ref selector settle_ms modifiers tab_id",
        one_of=(("ref", "selector"),),
        evidence="manager.go:826",
    ),
    _spec(
        BrowserAction.WHEEL,
        SideEffect.CONTROL,
        "delta_x delta_y ref selector modifiers tab_id",
        evidence="manager.go:827",
    ),
    _spec(
        BrowserAction.SCROLL_TO,
        SideEffect.CONTROL,
        "ref selector tab_id",
        one_of=(("ref", "selector"),),
        evidence="manager.go:828",
    ),
    _spec(
        BrowserAction.FOCUS,
        SideEffect.WRITE,
        "ref selector tab_id",
        one_of=(("ref", "selector"),),
        evidence="manager.go:839",
    ),
    _spec(
        BrowserAction.BLUR,
        SideEffect.WRITE,
        "ref selector tab_id",
        evidence="manager.go:840",
    ),
    _spec(
        BrowserAction.SELECT,
        SideEffect.WRITE,
        "ref selector values tab_id",
        required=("values",),
        one_of=(("ref", "selector"),),
        evidence="manager.go:829",
    ),
    _spec(BrowserAction.TAB_LIST, SideEffect.READ, "scope", evidence="manager.go:830"),
    _spec(
        BrowserAction.TAB_CREATE,
        SideEffect.CONTROL,
        "url active index",
        evidence="manager.go:831",
    ),
    _spec(
        BrowserAction.TAB_SELECT,
        SideEffect.CONTROL,
        "tab_id",
        required=("tab_id",),
        evidence="manager.go:832",
    ),
    _spec(
        BrowserAction.TAB_CLOSE,
        SideEffect.CONTROL,
        "tab_id",
        required=("tab_id",),
        evidence="manager.go:833",
    ),
    _spec(
        BrowserAction.TAB_BORROW,
        SideEffect.CONTROL,
        "tab_id confirm",
        required=("tab_id",),
        human_step=True,
        evidence="manager.go:834,970-974",
    ),
    _spec(
        BrowserAction.TAB_RETURN,
        SideEffect.CONTROL,
        "tab_id",
        required=("tab_id",),
        evidence="manager.go:835",
    ),
    _spec(
        BrowserAction.GET_HTML,
        SideEffect.READ,
        "tab_id ref max_bytes",
        evidence="manager.go:836",
    ),
    _spec(
        BrowserAction.EVALUATE,
        SideEffect.WRITE,
        "expression tab_id await_promise return_by_value",
        required=("expression",),
        arbitrary_js=True,
        evidence="manager.go:837；TDD05 §3 禁止任意 evaluate",
    ),
    _spec(
        BrowserAction.CONSOLE,
        SideEffect.READ,
        "tab_id limit max_text_chars since include_stack",
        evidence="manager.go:841",
    ),
    _spec(
        BrowserAction.NETWORK,
        SideEffect.READ,
        "tab_id limit max_text_chars since",
        evidence="manager.go:842",
    ),
    _spec(
        BrowserAction.WAIT_FOR_NAVIGATION,
        SideEffect.READ,
        "tab_id wait_until",
        evidence="manager.go:843",
    ),
    _spec(
        BrowserAction.WAIT_MS,
        SideEffect.READ,
        "duration_ms",
        required=("duration_ms",),
        evidence="manager.go:844",
    ),
    _spec(
        BrowserAction.WINDOW_RESIZE,
        SideEffect.CONTROL,
        "width height",
        required=("width", "height"),
        evidence="manager.go:845",
    ),
    _spec(
        BrowserAction.EMULATE,
        SideEffect.CONTROL,
        "off overrides tab_id",
        evidence="manager.go:846",
    ),
    _spec(
        BrowserAction.REQUEST_HELP,
        SideEffect.HUMAN,
        "prompt tab_id title targets",
        required=("prompt",),
        human_step=True,
        evidence="manager.go:847,961-966",
    ),
)

ACTION_SPECS: Final[Mapping[BrowserAction, ActionSpec]] = {
    spec.action: spec for spec in _SPEC_LIST
}
"""31 个动作的契约画像；键集合必须与 :class:`BrowserAction` 完全一致。"""

# 参数类型（保守子集，非上游 schema；U1 仍记 UNKNOWN）
_LIST_OF_STR: Final = "list[str]"
_PARAM_TYPES: Final[Mapping[str, Mapping[str, object]]] = {
    "navigate": {"url": str, "tab_id": str, "wait_until": str},
    "navigate_back": {"tab_id": str, "wait_until": str},
    "navigate_forward": {"tab_id": str, "wait_until": str},
    "reload": {"tab_id": str, "wait_until": str, "hard": bool},
    "click": {"ref": str, "selector": str, "button": str, "click_count": int, "tab_id": str},
    "fill": {"ref": str, "selector": str, "value": str, "clear_before": bool, "tab_id": str},
    "press": {"key": str, "ref": str, "selector": str, "hold_ms": int, "tab_id": str},
    "hover": {"ref": str, "selector": str, "settle_ms": int, "tab_id": str},
    "wheel": {"delta_x": int, "delta_y": int, "ref": str, "selector": str, "tab_id": str},
    "scroll_to": {"ref": str, "selector": str, "tab_id": str},
    "focus": {"ref": str, "selector": str, "tab_id": str},
    "blur": {"ref": str, "selector": str, "tab_id": str},
    "select": {"ref": str, "selector": str, "values": _LIST_OF_STR, "tab_id": str},
    "tab_list": {"scope": str},
    "tab_create": {"url": str, "active": bool, "index": int},
    "tab_select": {"tab_id": str},
    "tab_close": {"tab_id": str},
    "tab_borrow": {"tab_id": str, "confirm": bool},
    "tab_return": {"tab_id": str},
    "get_html": {"tab_id": str, "ref": str, "max_bytes": int},
    "console": {"tab_id": str, "limit": int, "max_text_chars": int, "include_stack": bool},
    "network": {"tab_id": str, "limit": int, "max_text_chars": int},
    "wait_for_navigation": {"tab_id": str, "wait_until": str},
    "wait_ms": {"duration_ms": int},
    "window_resize": {"width": int, "height": int},
    "emulate": {"off": bool, "tab_id": str, "overrides": dict},
    "request_help": {"prompt": str, "tab_id": str, "title": str},
    "observe": {"tab_id": str, "max_text_chars": int, "max_depth": int},
    "snapshot": {"tab_id": str, "max_text_chars": int, "max_depth": int},
    "screenshot": {"tab_id": str, "ref": str},
}
_PARAM_TYPES = {key: dict(value) for key, value in _PARAM_TYPES.items()}

_WAIT_UNTIL_VALUES: Final = ("domcontentloaded", "load", "networkidle", "commit")
_DEFAULT_WAIT_UNTIL: Final = "domcontentloaded"
"""网关默认值 A：``manager.go:955-959`` 对 navigate 家族统一填 ``domcontentloaded``。"""

# 只能由服务端注入的键：调用方/模型传入一律拒绝（INV-01 / TDD05 §4）
_RESERVED_PARAM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "session_id",
        "browser_session_id",
        "browser_instance_id",
        "workspace_id",
        "principal_id",
        "expected_epoch",
        "tool_call_id",
        "timeout_ms",
    }
)

# 响应专属/大体积载荷键：不得出现在请求参数（结果只允许 UUID artifact 引用）
_RESPONSE_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {"image_base64", "data_url", "html", "raw", "bytes", "content", "screenshot_base64"}
)

_SECRET_KEY_HINTS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "bearer",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "session_key",
    "device_token",
)
_SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)^bearer\s+\S{8,}$"),
    re.compile(r"(?i)^basic\s+[A-Za-z0-9+/=]{8,}$"),
    re.compile(r"^[A-Za-z0-9_\-]{40,}$"),  # 43 字符配对 token / 长随机串
)
"""值形态启发式：只拦明显凭据，避免把普通文本误判成 secret（误判会阻断正常 fill）。"""

_SECRET_VALUE_KEYS: Final[frozenset[str]] = frozenset({"value", "key", "text", "credential"})
"""只对这些键做值形态判定；`prompt`/`title` 等自由文本不做启发式，避免误伤正常 fill。"""

_MODIFIER_VALUES: Final = (
    "Alt",
    "Control",
    "Meta",
    "Shift",
    "alt",
    "control",
    "meta",
    "shift",
)

_JS_KEYS: Final[frozenset[str]] = frozenset({"expression", "script", "javascript", "eval"})


def action_spec(action: BrowserAction | str) -> ActionSpec:
    """取动作画像；未知动作抛 ``INVALID_BROWSER_ACTION``（不猜、不降级）。"""

    try:
        resolved = BrowserAction(action)
    except ValueError as exc:
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_ACTION,
            f"未知浏览器动作 {action!r}：只接受 {len(BrowserAction)} 个 pinned 动作之一",
            data={"action": str(action)},
        ) from exc
    return ACTION_SPECS[resolved]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _sha256_of_canonical_json(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_int(value: object, *, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 必须是整数，收到 {type(value).__name__}",
            data={"param": name},
        )
    if value < minimum or (maximum is not None and value > maximum):
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 必须在 [{minimum}, {maximum}]，收到 {value}",
            data={"param": name},
        )
    return value


def _require_str(value: object, *, name: str, max_length: int = 8192) -> str:
    if not isinstance(value, str):
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 必须是字符串，收到 {type(value).__name__}",
            data={"param": name},
        )
    if not value:
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM, f"{name} 不能为空字符串", data={"param": name}
        )
    if len(value) > max_length:
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 超长（{len(value)} > {max_length}）",
            data={"param": name},
        )
    return value


def _require_bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 必须是布尔值，收到 {type(value).__name__}",
            data={"param": name},
        )
    return value


def _require_list_of_str(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 必须是字符串数组，收到 {type(value).__name__}",
            data={"param": name},
        )
    if not value or len(value) > MAX_PARAM_ITEMS:
        raise RPCError(
            BrowserErrorCode.INVALID_BROWSER_PARAM,
            f"{name} 元素数必须在 [1, {MAX_PARAM_ITEMS}]",
            data={"param": name},
        )
    for item in value:
        if not isinstance(item, str) or not item:
            raise RPCError(
                BrowserErrorCode.INVALID_BROWSER_PARAM,
                f"{name} 只允许非空字符串元素",
                data={"param": name},
            )
    return tuple(value)


def _is_secret_key(name: str) -> bool:
    """键名归一化后再匹配：`sessionKey` / `session-key` 与 `session_key` 同罪。"""
    lowered = name.lower().replace("_", "").replace("-", "")
    return any(hint.replace("_", "") in lowered for hint in _SECRET_KEY_HINTS)


def _looks_like_secret_value(value: object) -> bool:
    if not isinstance(value, str) or len(value) < 16:
        return False
    return any(pattern.match(value) for pattern in _SECRET_VALUE_PATTERNS)


# ---------------------------------------------------------------------------
# 结果 DTO
# ---------------------------------------------------------------------------
BrowserResultStatus = Literal["succeeded", "failed", "unknown_outcome", "cancelled"]


@dataclass(frozen=True, slots=True)
class BrowserResult:
    """动作事实（不返回原始页面/字节；只返回脱敏 URL、artifact UUID 与 epoch）。

    不变量：
    - ``succeeded`` ⇒ ``error_code is None``；
    - ``unknown_outcome`` / ``failed`` / ``cancelled`` ⇒ ``error_code`` 非空；
    - ``artifact_refs`` 只能是 UUID 引用，禁止内联 base64/观众数据；
    - ``observed_epoch`` 必须 >= 1，调用方不得用它覆盖当前会话 epoch。
    """

    tool_call_id: UUID
    action: BrowserAction
    status: BrowserResultStatus
    final_url: str | None
    artifact_refs: tuple[UUID, ...]
    error_code: str | None
    action_sent: bool
    observed_epoch: int
    redacted: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, UUID):
            raise RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR, "tool_call_id 必须是 UUID"
            )
        if self.status == "succeeded" and self.error_code is not None:
            raise RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR, "status=succeeded 时不得携带 error_code"
            )
        if self.status != "succeeded" and not self.error_code:
            raise RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR,
                f"status={self.status} 必须携带 error_code",
            )
        for ref in self.artifact_refs:
            if not isinstance(ref, UUID):
                raise RPCError(
                    BrowserErrorCode.BROWSER_WIRE_ERROR,
                    "artifact_refs 只允许 UUID 引用（禁止内联内容）",
                )
        if isinstance(self.observed_epoch, bool) or not isinstance(self.observed_epoch, int):
            raise RPCError(BrowserErrorCode.BROWSER_WIRE_ERROR, "observed_epoch 必须是整数")
        if self.observed_epoch < 1:
            raise RPCError(BrowserErrorCode.BROWSER_WIRE_ERROR, "observed_epoch 必须 >= 1")
        if self.final_url is not None:
            _require_str(self.final_url, name="final_url")

    @property
    def retryable(self) -> bool:
        """超时/取消永远不可自动重试（TDD05 §6；manifest §6.5）。"""

        return False

    @property
    def needs_reconciliation(self) -> bool:
        return self.status == "unknown_outcome"


# ---------------------------------------------------------------------------
# 能力协商
# ---------------------------------------------------------------------------
class CapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilityId(StrEnum):
    """U1–U9 的显式能力位（T14 manifest §9.3）；UNKNOWN 绝不等于 supported。"""

    U1_ACTION_PARAM_SCHEMA = "U1_action_param_schema"
    U2_PROTOCOL_VERSION = "U2_protocol_version"
    U3_DAEMON_LISTEN_SCOPE = "U3_daemon_listen_scope"
    U4_UPSTREAM_LICENSE = "U4_upstream_license"
    U5_FULL_REQUEST_NETWORK_CONTROL = "U5_full_request_network_control"
    U6_TAB_BORROW_CONFIRMATION = "U6_tab_borrow_confirmation"
    U7_WINDOWS_RUNTIME = "U7_windows_runtime"
    U8_DAEMON_RESTART_SESSION_SEMANTICS = "U8_daemon_restart_session_semantics"
    U9_WINDOW_CLOSED_DETECTION = "U9_window_closed_detection"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """协商结果：已测试 allowlist ∩ handshake，未知一律显式记录。"""

    adapter_api_version: str
    backend: str
    backend_version: str | None
    protocol_version: str | None
    platform: str
    supported_actions: frozenset[BrowserAction]
    unsupported_actions: frozenset[BrowserAction]
    unknown_actions: frozenset[str]
    ui_channel: CapabilityStatus
    capabilities: Mapping[CapabilityId, CapabilityStatus]
    limits: Mapping[str, int]

    def status_of(self, capability: CapabilityId) -> CapabilityStatus:
        return self.capabilities.get(capability, CapabilityStatus.UNKNOWN)

    def require_capability(self, capability: CapabilityId) -> None:
        status = self.status_of(capability)
        if status is not CapabilityStatus.SUPPORTED:
            raise BrowserCapabilityError(
                BrowserErrorCode.CAPABILITY_UNSUPPORTED,
                f"能力 {capability} 当前为 {status}：未验证通过的能力不得假装成功",
                data={"capability": str(capability), "status": str(status)},
            )

    def supports(self, action: BrowserAction | str) -> bool:
        try:
            resolved = BrowserAction(action)
        except ValueError:
            return False
        return resolved in self.supported_actions

    def require_action(self, action: BrowserAction) -> None:
        if action in self.supported_actions:
            return
        raise BrowserCapabilityError(
            BrowserErrorCode.CAPABILITY_UNSUPPORTED,
            f"动作 {action} 未被该后端声明为已测能力（unsupported/unknown）",
            data={
                "action": str(action),
                "reason": (
                    "arbitrary_js_forbidden" if action is BrowserAction.EVALUATE else "not_verified"
                ),
            },
        )

    @property
    def capabilities_hash(self) -> str:
        return _sha256_of_canonical_json(
            {
                "v": 1,
                "adapter_api_version": self.adapter_api_version,
                "backend": self.backend,
                "backend_version": self.backend_version,
                "protocol_version": self.protocol_version,
                "platform": self.platform,
                "actions": sorted(str(item) for item in self.supported_actions),
                "ui_channel": str(self.ui_channel),
                "capabilities": {
                    str(key): str(self.capabilities[key]) for key in sorted(self.capabilities)
                },
            }
        )


def negotiate_capabilities(
    *,
    declared_actions: Iterable[BrowserAction | str],
    ui_channel: bool | None,
    protocol_version: str | None,
    backend: str = "browserskill",
    backend_version: str | None = None,
    platform: str = "linux",
    tested_protocol_versions: frozenset[str] = frozenset(),
) -> Capabilities:
    """握手 → 能力：**未知协议/版本一律** ``BROWSER_INCOMPATIBLE``。

    ``tested_protocol_versions`` 为空表示「本阶段没有已测版本」——这是 Phase 0 的真实状态
    （T14 U2），因此不传白名单时任何真实后端都会得到 ``BROWSER_INCOMPATIBLE``，而不是被
    「非空版本字符串」放行（TDD05 §3）。
    """

    if not protocol_version or not isinstance(protocol_version, str):
        raise BrowserCapabilityError(
            BrowserErrorCode.BROWSER_INCOMPATIBLE,
            "handshake 未返回 protocol_version：无法判定兼容性",
            data={"protocol_version": protocol_version},
        )
    if protocol_version not in tested_protocol_versions:
        raise BrowserCapabilityError(
            BrowserErrorCode.BROWSER_INCOMPATIBLE,
            f"protocol_version={protocol_version!r} 不在已测白名单内（不以非空版本判定兼容）",
            data={
                "protocol_version": protocol_version,
                "tested": sorted(tested_protocol_versions),
            },
        )

    known: set[BrowserAction] = set()
    unknown: set[str] = set()
    for item in declared_actions:
        try:
            action = BrowserAction(item)
        except ValueError:
            unknown.add(str(item))
            continue
        if action is BrowserAction.EVALUATE:
            continue  # 永远 unsupported：禁止任意 JS
        known.add(action)

    supported = frozenset(known & set(ACTION_SPECS))
    unsupported = frozenset(set(BrowserAction) - supported)

    ui_status = (
        CapabilityStatus.UNKNOWN
        if ui_channel is None
        else (CapabilityStatus.SUPPORTED if ui_channel else CapabilityStatus.UNSUPPORTED)
    )
    capabilities: dict[CapabilityId, CapabilityStatus] = {
        # U1：31 个动作的上游 schema 在参考源码中不可得 → 永远 UNKNOWN（不因版本通过而 SUPPORTED）
        CapabilityId.U1_ACTION_PARAM_SCHEMA: CapabilityStatus.UNKNOWN,
        CapabilityId.U2_PROTOCOL_VERSION: CapabilityStatus.SUPPORTED,
        CapabilityId.U3_DAEMON_LISTEN_SCOPE: CapabilityStatus.UNKNOWN,
        CapabilityId.U4_UPSTREAM_LICENSE: CapabilityStatus.UNKNOWN,
        CapabilityId.U5_FULL_REQUEST_NETWORK_CONTROL: CapabilityStatus.UNKNOWN,
        CapabilityId.U6_TAB_BORROW_CONFIRMATION: CapabilityStatus.UNKNOWN,
        CapabilityId.U7_WINDOWS_RUNTIME: CapabilityStatus.UNSUPPORTED,
        CapabilityId.U8_DAEMON_RESTART_SESSION_SEMANTICS: CapabilityStatus.UNKNOWN,
        CapabilityId.U9_WINDOW_CLOSED_DETECTION: CapabilityStatus.UNKNOWN,
    }
    # Windows：T14 只证明「构建脚本无 windows 目标」，运行期完全未验证 → UNKNOWN，
    # 而不是 unsupported（unsupported 会暗示已测过并确定不可用）。
    if platform.lower().startswith("windows"):
        capabilities[CapabilityId.U7_WINDOWS_RUNTIME] = CapabilityStatus.UNKNOWN

    return Capabilities(
        adapter_api_version=ADAPTER_API_VERSION,
        backend=backend,
        backend_version=backend_version,
        protocol_version=protocol_version,
        platform=platform,
        supported_actions=supported,
        unsupported_actions=unsupported,
        unknown_actions=frozenset(unknown),
        ui_channel=ui_status,
        capabilities=capabilities,
        limits={
            "timeout_ms_max": MAX_TIMEOUT_MS,
            "human_timeout_ms_max": HUMAN_TIMEOUT_MAX_MS,
            "params_max_inline_bytes": 8192,
        },
    )


# ---------------------------------------------------------------------------
# 网络策略前置拦截点（T16 落地；本模块只定义协议与 fail-closed 默认）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


class NetworkPolicy(Protocol):
    """T16 的网络出口策略接口：域名/私网/SSRF/速率都在实现里，不在本 port 里。"""

    def check_navigation(self, *, workspace_id: UUID, url: str) -> PolicyDecision: ...


class DenyAllNetworkPolicy:
    """T16 未接入前的默认策略：拒绝一切导航（fail closed），不假装 SSRF 已覆盖。"""

    def check_navigation(self, *, workspace_id: UUID, url: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            reason=f"T16 网络策略未接入：默认拒绝导航 {url!r}（workspace={workspace_id}）",
        )


_ALLOWED_URL_SCHEMES: Final = ("http", "https")


def _check_url_shape(url: str) -> None:
    """只做形态校验（scheme/凭据位）；域名、私网、重定向链、子资源由 T16 负责。"""

    lowered = url.strip().lower()
    if not lowered.startswith(_ALLOWED_URL_SCHEMES):
        raise RPCError(
            BrowserErrorCode.SSRF_BLOCKED,
            f"只允许 http/https 导航，收到 {url[:32]!r}",
            data={"reason": "scheme_not_allowed"},
        )
    if lowered.count("://") == 1 and "@" in lowered.split("://", 1)[1].split("/", 1)[0]:
        raise RPCError(
            BrowserErrorCode.SSRF_BLOCKED,
            "URL 不得携带用户信息（userinfo）",
            data={"reason": "userinfo_not_allowed"},
        )


def _redact_url(url: str) -> str:
    """脱敏 URL：去掉 userinfo/query/fragment（TDD05 §6 / AGENTS 禁止凭据进上下文）。"""

    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_URL_SCHEMES or not parts.hostname:
        return ""
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# ---------------------------------------------------------------------------
# 审批
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Approval:
    """一次写动作的审批事实；绑定 workspace/session/action/params/有效期。"""

    approval_id: UUID
    workspace_id: UUID
    browser_session_id: UUID
    action: BrowserAction
    params_hash: str
    expires_at: datetime
    granted_by: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, UUID):
            raise RPCError(BrowserErrorCode.APPROVAL_MISMATCH, "approval_id 必须是 UUID")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise RPCError(BrowserErrorCode.APPROVAL_MISMATCH, "approval.expires_at 必须带时区")
        if not re.fullmatch(r"[0-9a-f]{64}", self.params_hash):
            raise RPCError(BrowserErrorCode.APPROVAL_MISMATCH, "params_hash 必须是 sha256 十六进制")


def compute_action_hash(
    *,
    workspace_id: UUID,
    browser_session_id: UUID,
    action: BrowserAction,
    params: Mapping[str, object],
) -> str:
    """审批绑定的动作指纹：覆盖 workspace/session/action/规范化参数。"""

    return _sha256_of_canonical_json(
        {
            "v": 1,
            "workspace_id": str(workspace_id),
            "browser_session_id": str(browser_session_id),
            "action": str(action),
            "params": {str(key): params[key] for key in sorted(params)},
        }
    )


# ---------------------------------------------------------------------------
# 会话（Scope 的唯一可信载体）
# ---------------------------------------------------------------------------
class BrowserSessionState(StrEnum):
    PAIRING = "pairing"
    PAUSED = "paused"
    CONNECTED = "connected"
    HUMAN_HANDOFF = "human_handoff"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    CLOSED = "closed"


_CONTROL_TRANSITIONS: Final[
    Mapping[str, tuple[frozenset[BrowserSessionState], BrowserSessionState]]
] = {
    "pause": (
        frozenset({BrowserSessionState.CONNECTED, BrowserSessionState.HUMAN_HANDOFF}),
        BrowserSessionState.PAUSED,
    ),
    "resume": (
        frozenset({BrowserSessionState.PAUSED, BrowserSessionState.DISCONNECTED}),
        BrowserSessionState.CONNECTED,
    ),
    "close": (
        frozenset(
            {
                BrowserSessionState.PAIRING,
                BrowserSessionState.PAUSED,
                BrowserSessionState.CONNECTED,
                BrowserSessionState.HUMAN_HANDOFF,
                BrowserSessionState.DISCONNECTED,
                BrowserSessionState.REVOKED,
            }
        ),
        BrowserSessionState.CLOSED,
    ),
}


class _TrustToken:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<pyknora.phase0.browser-trusted-scope>"


_TRUST: Final = _TrustToken()


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """会话事实：``workspace_id`` / ``browser_session_id`` 只能由服务端构造。

    与 TDD05 §3 的 BrowserSession DTO 对齐（device_id / capabilities / grant / epoch /
    有效期）；**不含任何凭据**——device token 与 WS 凭据只存在于 secret store 引用。
    """

    workspace_id: UUID
    principal_id: UUID
    browser_session_id: UUID
    backend: str
    state: BrowserSessionState
    capabilities: Capabilities
    lease_epoch: int
    allowed_domains: tuple[str, ...] = ()
    tab_grant_refs: tuple[UUID, ...] = ()
    device_id: UUID | None = None
    run_id: UUID | None = None
    expires_at: datetime | None = None
    owner_instance: str | None = None
    row_version: int = 1
    _trust: object = None

    def __post_init__(self) -> None:
        if self._trust is not _TRUST:
            raise RPCError(
                BrowserErrorCode.UNTRUSTED_BROWSER_CONSTRUCTION,
                "BrowserSession 只能由服务端构造（create_browser_session）；"
                "禁止从请求体、模型输出或队列消息直接构造",
            )
        for name, value in (
            ("workspace_id", self.workspace_id),
            ("principal_id", self.principal_id),
            ("browser_session_id", self.browser_session_id),
        ):
            if not isinstance(value, UUID):
                raise RPCError(
                    BrowserErrorCode.UNTRUSTED_BROWSER_CONSTRUCTION, f"{name} 必须是 UUID"
                )
        if not isinstance(self.capabilities, Capabilities):
            raise RPCError(
                BrowserErrorCode.UNTRUSTED_BROWSER_CONSTRUCTION,
                "capabilities 必须是协商结果（negotiate_capabilities）",
            )
        if isinstance(self.lease_epoch, bool) or not isinstance(self.lease_epoch, int):
            raise RPCError(BrowserErrorCode.STALE_EPOCH, "lease_epoch 必须是整数")
        if self.lease_epoch < 1:
            raise RPCError(BrowserErrorCode.STALE_EPOCH, "lease_epoch 必须 >= 1")
        for ref in self.tab_grant_refs:
            if not isinstance(ref, UUID):
                raise RPCError(
                    BrowserErrorCode.INVALID_BROWSER_PARAM, "tab_grant_refs 只允许 UUID 引用"
                )

    @classmethod
    def from_client_payload(cls, *_args: object, **_kwargs: object) -> NoReturn:
        """deny 默认：客户端不得自报 session/workspace（INV-01）。"""

        raise RPCError(
            BrowserErrorCode.SCOPE_INJECTION_REJECTED,
            "BrowserSession.from_client_payload 一律拒绝："
            "workspace_id/session_id 只能由后端认证授权路径注入",
        )

    @property
    def revoked(self) -> bool:
        return self.state is BrowserSessionState.REVOKED


def create_browser_session(
    *,
    workspace_id: UUID,
    principal_id: UUID,
    capabilities: Capabilities,
    backend: str = "browserskill",
    state: BrowserSessionState = BrowserSessionState.PAUSED,
    lease_epoch: int = 1,
    browser_session_id: UUID | None = None,
    allowed_domains: Iterable[str] = (),
    tab_grant_refs: Iterable[UUID] = (),
    device_id: UUID | None = None,
    run_id: UUID | None = None,
    expires_at: datetime | None = None,
    owner_instance: str | None = None,
) -> BrowserSession:
    """受信构造入口（实验/服务端用）：默认 ``paused``，显式 resume 才 ``connected``。"""

    return BrowserSession(
        workspace_id=workspace_id,
        principal_id=principal_id,
        browser_session_id=browser_session_id or uuid4(),
        backend=backend,
        state=state,
        capabilities=capabilities,
        lease_epoch=lease_epoch,
        allowed_domains=tuple(allowed_domains),
        tab_grant_refs=tuple(tab_grant_refs),
        device_id=device_id,
        run_id=run_id,
        expires_at=expires_at,
        owner_instance=owner_instance,
        _trust=_TRUST,
    )


# ---------------------------------------------------------------------------
# 传输层（真实 daemon 由 T17 接入；本 port 只依赖这个窄接口）
# ---------------------------------------------------------------------------
class TransportFailure(Exception):
    """传输层失败：``code`` 用 T14 manifest §6.6 的 wire 字面码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: Mapping[str, object] | None = None,
        action_sent: bool = False,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = dict(data or {})
        self.action_sent = action_sent


class BrowserTransport(Protocol):
    """``tool.<action>`` 的窄传输接口；实现方负责 WS/IPC 细节（T17）。"""

    def dispatch(
        self, method: str, params: Mapping[str, object], *, timeout_ms: int
    ) -> Mapping[str, object]: ...


_WIRE_ERROR_MAP: Final[Mapping[str, BrowserErrorCode]] = {
    "daemon_incompatible": BrowserErrorCode.BROWSER_INCOMPATIBLE,
    "protocol_error": BrowserErrorCode.BROWSER_INCOMPATIBLE,
    "unknown_method": BrowserErrorCode.CAPABILITY_UNSUPPORTED,
    "gateway_ui_unsupported": BrowserErrorCode.CAPABILITY_UNSUPPORTED,
    "task_paused": BrowserErrorCode.BROWSER_PAUSED,
    "preview_busy": BrowserErrorCode.CAPABILITY_UNSUPPORTED,
    "renderer_read_timeout": BrowserErrorCode.TOOL_UNKNOWN_OUTCOME,
    "not_found": BrowserErrorCode.BROWSER_OFFLINE,
    "owner_lost": BrowserErrorCode.OWNER_LOST,
}
"""wire → canonical 映射（manifest §6.6 → TDD05 §6）。"""


def map_wire_error(failure: TransportFailure) -> RPCError:
    """把 wire 错误映射成稳定码；已发动作一律 ``TOOL_UNKNOWN_OUTCOME``（禁止重放）。"""

    code = failure.code
    data = dict(failure.data)
    if code == "timeout":
        if failure.action_sent:
            return RPCError(
                BrowserErrorCode.TOOL_UNKNOWN_OUTCOME,
                "动作已发出但超时：结果未知，禁止自动重试",
                data={**data, "wire_code": code},
                action_sent=True,
            )
        return RPCError(
            BrowserErrorCode.BROWSER_TIMEOUT,
            "连接前超时：动作未发出",
            data={**data, "wire_code": code},
            action_sent=False,
        )
    if code in {"cancelled", "user_aborted"}:
        if failure.action_sent:
            return RPCError(
                BrowserErrorCode.TOOL_UNKNOWN_OUTCOME,
                "动作已发出后被取消：结果未知，禁止自动重试",
                data={**data, "wire_code": code},
                action_sent=True,
            )
        return RPCError(
            BrowserErrorCode.BROWSER_CANCELLED,
            "动作在发出前被取消",
            data={**data, "wire_code": code},
            action_sent=False,
        )
    if code == "not_found" and "session is not registered" in failure.message:
        return RPCError(
            BrowserErrorCode.BROWSER_OFFLINE,
            "会话在 daemon 侧不存在（视为已断开）",
            data={**data, "wire_code": code},
            action_sent=failure.action_sent,
        )
    mapped = _WIRE_ERROR_MAP.get(code)
    if mapped is None:
        return RPCError(
            BrowserErrorCode.BROWSER_WIRE_ERROR,
            f"未登记的 wire 错误 {code!r}：按未知处理，不得当作成功",
            data={**data, "wire_code": code},
            action_sent=failure.action_sent,
        )
    return RPCError(
        mapped,
        failure.message or f"wire 错误 {code}",
        data={**data, "wire_code": code},
        action_sent=failure.action_sent,
    )


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------
class BrowserPort:
    """Browser port：单入口 ``execute`` + 会话控制；所有校验在这里，不在各动作方法里。"""

    def __init__(
        self,
        transport: BrowserTransport,
        *,
        network_policy: NetworkPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._network_policy: NetworkPolicy = network_policy or DenyAllNetworkPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- 协商 -------------------------------------------------------------
    def negotiate(self, **kwargs: object) -> Capabilities | RPCError:
        try:
            return negotiate_capabilities(**kwargs)  # type: ignore[arg-type]
        except RPCError as err:
            return err

    # -- 唯一执行入口 ------------------------------------------------------
    def execute(
        self,
        session: BrowserSession,
        *,
        action: BrowserAction | str,
        params: Mapping[str, object] | None = None,
        expected_epoch: int,
        timeout_ms: int,
        approval: Approval | None = None,
        tool_call_id: UUID | None = None,
    ) -> BrowserResult | RPCError:
        """``execute(action, params, expected_epoch, timeout_ms) -> result | RPCError``。

        ``session`` 是服务端注入的 Scope 载体（workspace_id/session_id/epoch 都在它上面）；
        ``params`` 里出现这些键一律 ``SCOPE_INJECTION_REJECTED``，绝不静默剥离。
        """

        try:
            return self._execute(
                session,
                action=action,
                params=params,
                expected_epoch=expected_epoch,
                timeout_ms=timeout_ms,
                approval=approval,
                tool_call_id=tool_call_id,
            )
        except RPCError as err:
            return err

    def control(
        self,
        session: BrowserSession,
        command: Literal["pause", "resume", "revoke", "close"],
        *,
        expected_epoch: int,
    ) -> BrowserSession | RPCError:
        """会话控制（TDD05 §3 的 ``control``）。``revoke``/``close`` 幂等且不受旧 epoch 阻塞。"""

        try:
            if command in {"revoke", "close"}:
                return self._terminal(session, command)
            if command not in _CONTROL_TRANSITIONS:
                raise RPCError(
                    BrowserErrorCode.INVALID_BROWSER_ACTION, f"未知控制命令 {command!r}"
                )
            if expected_epoch != session.lease_epoch:
                raise RPCError(
                    BrowserErrorCode.STALE_EPOCH,
                    f"expected_epoch={expected_epoch} 与当前 {session.lease_epoch} 不一致",
                    data={"expected_epoch": expected_epoch, "current": session.lease_epoch},
                )
            allowed, new_state = _CONTROL_TRANSITIONS[command]
            if session.state not in allowed:
                raise RPCError(
                    BrowserErrorCode.BROWSER_PAUSED
                    if command == "pause"
                    else BrowserErrorCode.SESSION_REVOKED,
                    f"会话状态 {session.state} 不允许 {command}",
                    data={"state": str(session.state)},
                )
            epoch = session.lease_epoch + (1 if command == "resume" else 0)
            return replace(session, state=new_state, lease_epoch=epoch)
        except RPCError as err:
            return err

    # -- 内部实现 ---------------------------------------------------------
    def _terminal(
        self, session: BrowserSession, command: Literal["revoke", "close"]
    ) -> BrowserSession:
        target = (
            BrowserSessionState.REVOKED
            if command == "revoke"
            else BrowserSessionState.CLOSED
        )
        if session.state is target:
            return session  # 幂等
        if session.state is BrowserSessionState.REVOKED:
            return session  # 撤销是终态：close 不得把它降级成 closed
        if session.state is BrowserSessionState.CLOSED:
            return session
        return replace(session, state=target, lease_epoch=session.lease_epoch + 1)

    def _execute(
        self,
        session: BrowserSession,
        *,
        action: BrowserAction | str,
        params: Mapping[str, object] | None,
        expected_epoch: int,
        timeout_ms: int,
        approval: Approval | None,
        tool_call_id: UUID | None,
    ) -> BrowserResult | RPCError:
        spec = action_spec(action)
        resolved = spec.action
        self._check_timeout(spec, timeout_ms)

        if params is None:
            caller_params: dict[str, object] = {}
        elif not isinstance(params, Mapping):
            raise RPCError(BrowserErrorCode.INVALID_BROWSER_PARAM, "params 必须是映射")
        else:
            caller_params = dict(params)
        # 顺序即策略：先拒绝注入/凭据/任意 JS，再谈参数白名单；否则注入会被
        # 「未知参数」掩盖成普通校验失败，安全语义就丢了。
        self._check_key_safety(caller_params)
        self._check_special_rejections(spec, caller_params)
        self._check_unknown_params(spec, caller_params)
        normalized = self._apply_defaults(spec, caller_params)
        self._check_required(spec, normalized)
        self._check_types(spec, normalized)

        # --- Scope / epoch / 能力 / 审批：全部在 dispatch 之前 ---
        if session.state is BrowserSessionState.REVOKED:
            raise RPCError(
                BrowserErrorCode.SESSION_REVOKED,
                "会话已撤销：不能执行任何动作",
                data={"session_id": str(session.browser_session_id)},
            )
        if session.state is BrowserSessionState.CLOSED:
            raise RPCError(BrowserErrorCode.BROWSER_OFFLINE, "会话已关闭")
        if session.state is not BrowserSessionState.CONNECTED:
            raise RPCError(
                BrowserErrorCode.BROWSER_PAUSED,
                f"会话状态为 {session.state}：必须由用户显式 resume 后才可执行",
                data={"state": str(session.state)},
            )
        if isinstance(expected_epoch, bool) or not isinstance(expected_epoch, int):
            raise RPCError(BrowserErrorCode.STALE_EPOCH, "expected_epoch 必须是整数")
        if expected_epoch != session.lease_epoch:
            raise RPCError(
                BrowserErrorCode.STALE_EPOCH,
                f"expected_epoch={expected_epoch} 与当前租约 {session.lease_epoch} 不一致",
                data={"expected_epoch": expected_epoch, "current": session.lease_epoch},
            )
        session.capabilities.require_action(resolved)

        params_hash = compute_action_hash(
            workspace_id=session.workspace_id,
            browser_session_id=session.browser_session_id,
            action=resolved,
            params=normalized,
        )
        if spec.requires_approval:
            self._check_approval(session, spec, params_hash, approval)

        if spec.navigational:
            self._check_navigation(session, normalized, approval, spec)

        # --- dispatch：session_id 由服务端注入，调用方不可见 ---
        wire_params: dict[str, object] = {str(k): v for k, v in normalized.items()}
        wire_params.pop("session_id", None)
        wire_params["session_id"] = str(session.browser_session_id)
        method = f"tool.{resolved}"

        try:
            reply = self._transport.dispatch(
                method, wire_params, timeout_ms=timeout_ms
            )
        except TransportFailure as failure:
            return map_wire_error(failure)
        except RPCError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输层任何异常都不得冒充成功
            return RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR,
                f"传输层异常：{type(exc).__name__}",
                data={"method": method},
                action_sent=True,
            )

        return self._build_result(
            session, resolved, reply, tool_call_id=tool_call_id
        )

    # -- 校验分步 ---------------------------------------------------------
    def _check_timeout(self, spec: ActionSpec, timeout_ms: object) -> None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise RPCError(BrowserErrorCode.INVALID_BROWSER_PARAM, "timeout_ms 必须是整数")
        if timeout_ms < 1 or timeout_ms > spec.max_timeout_ms:
            raise RPCError(
                BrowserErrorCode.INVALID_BROWSER_PARAM,
                f"timeout_ms 必须在 [1, {spec.max_timeout_ms}]，收到 {timeout_ms}",
                data={"param": "timeout_ms", "max": spec.max_timeout_ms},
            )

    def _check_unknown_params(self, spec: ActionSpec, params: Mapping[str, object]) -> None:
        unknown = sorted(str(key) for key in params if str(key) not in spec.params)
        if unknown:
            raise RPCError(
                BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED,
                f"动作 {spec.action} 不接受参数 {unknown}：只允许已声明子集"
                "（U1：上游 schema 未公开）",
                data={"action": str(spec.action), "unsupported": unknown},
            )

    def _apply_defaults(
        self, spec: ActionSpec, params: Mapping[str, object]
    ) -> dict[str, object]:
        normalized = {str(key): value for key, value in params.items()}
        if spec.navigational and "wait_until" in spec.params:
            normalized.setdefault("wait_until", _DEFAULT_WAIT_UNTIL)
        return normalized

    def _check_key_safety(self, params: Mapping[str, object]) -> None:
        for key in params:
            lowered = str(key).lower()
            if lowered in _RESERVED_PARAM_KEYS:
                raise RPCError(
                    BrowserErrorCode.SCOPE_INJECTION_REJECTED,
                    f"参数 {key!r} 只能由服务端注入：调用方/模型不得传入",
                    data={"param": str(key)},
                )
            if lowered in _RESPONSE_ONLY_KEYS:
                raise RPCError(
                    BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED,
                    f"参数 {key!r} 是响应载荷键，不是请求参数",
                    data={"param": str(key)},
                )
            if _is_secret_key(lowered):
                raise RPCError(
                    BrowserErrorCode.SECRET_IN_PARAMS,
                    f"参数 {key!r} 属于凭据类字段：禁止进入浏览器工具参数",
                    data={"param": str(key)},
                )
            value = params[key]
            if isinstance(value, Mapping):
                for nested in value:
                    if _is_secret_key(str(nested)):
                        raise RPCError(
                            BrowserErrorCode.SECRET_IN_PARAMS,
                            f"参数 {key}.{nested} 属于凭据类字段",
                            data={"param": f"{key}.{nested}"},
                        )
            elif lowered in _SECRET_VALUE_KEYS and _looks_like_secret_value(value):
                raise RPCError(
                    BrowserErrorCode.SECRET_IN_PARAMS,
                    f"参数 {key!r} 的值形态像凭据（长随机串/Bearer）",
                    data={"param": str(key)},
                )

    def _check_special_rejections(
        self, spec: ActionSpec, params: Mapping[str, object]
    ) -> None:
        if spec.arbitrary_js:
            raise RPCError(
                BrowserErrorCode.ARBITRARY_JS_REJECTED,
                f"动作 {spec.action} 属于任意 JS 执行：公共 port 永久不支持，"
                "抽取请走注册 schema（TDD05 §3）",
                data={"action": str(spec.action)},
            )
        for key in params:
            if str(key) in _JS_KEYS:
                raise RPCError(
                    BrowserErrorCode.ARBITRARY_JS_REJECTED,
                    f"参数 {key!r} 只能用于 evaluate，而任意 JS 被禁止",
                    data={"param": str(key)},
                )
        if spec.action is BrowserAction.REQUEST_HELP and "completion_criteria" in params:
            raise RPCError(
                BrowserErrorCode.BROWSER_PARAM_UNSUPPORTED,
                "request_help 拒绝 completion_criteria：模型不得自证完成"
                "（网关侧强制删除，manifest §6.3 #31）",
                data={"param": "completion_criteria"},
            )

    def _check_required(self, spec: ActionSpec, params: Mapping[str, object]) -> None:
        missing = [name for name in spec.required if name not in params]
        if missing:
            raise RPCError(
                BrowserErrorCode.INVALID_BROWSER_PARAM,
                f"动作 {spec.action} 缺少必填参数 {missing}",
                data={"action": str(spec.action), "missing": missing},
            )
        for group in spec.one_of:
            if not any(name in params for name in group):
                raise RPCError(
                    BrowserErrorCode.INVALID_BROWSER_PARAM,
                    f"动作 {spec.action} 需要 {list(group)} 中至少一个",
                    data={"action": str(spec.action), "one_of": list(group)},
                )

    def _check_types(self, spec: ActionSpec, params: Mapping[str, object]) -> None:
        expected_types = _PARAM_TYPES.get(str(spec.action), {})
        for name, expected in expected_types.items():
            if name not in params:
                continue
            value = params[name]
            if expected is _LIST_OF_STR:
                _require_list_of_str(value, name=name)
            elif expected is str:
                _require_str(value, name=name)
            elif expected is int:
                _require_int(value, name=name, minimum=0, maximum=10_000_000)
            elif expected is bool:
                _require_bool(value, name=name)
            elif expected is dict and not isinstance(value, Mapping):
                raise RPCError(
                    BrowserErrorCode.INVALID_BROWSER_PARAM, f"{name} 必须是映射"
                )
        if "wait_until" in params:
            wait_until = params["wait_until"]
            if wait_until not in _WAIT_UNTIL_VALUES:
                raise RPCError(
                    BrowserErrorCode.INVALID_BROWSER_PARAM,
                    f"wait_until 必须是 {list(_WAIT_UNTIL_VALUES)} 之一，收到 {wait_until!r}",
                    data={"param": "wait_until"},
                )

    def _check_approval(
        self,
        session: BrowserSession,
        spec: ActionSpec,
        params_hash: str,
        approval: Approval | None,
    ) -> None:
        if approval is None:
            raise RPCError(
                BrowserErrorCode.APPROVAL_REQUIRED,
                f"写动作 {spec.action} 必须携带审批（approval_id→参数指纹）",
                data={"action": str(spec.action), "params_hash": params_hash},
            )
        if not isinstance(approval, Approval):
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                f"approval 类型非法：{type(approval).__name__}",
            )
        if approval.workspace_id != session.workspace_id:
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                "审批的 workspace 与会话不一致（跨空间审批一律拒绝）",
                data={"reason": "workspace_mismatch"},
            )
        if approval.browser_session_id != session.browser_session_id:
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                "审批绑定的会话与本次调用的会话不一致",
                data={"reason": "session_mismatch"},
            )
        if approval.action is not spec.action:
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                f"审批动作 {approval.action} ≠ 本次动作 {spec.action}",
                data={"reason": "action_mismatch"},
            )
        if approval.params_hash != params_hash:
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                "审批的参数指纹与本次参数不一致（参数被改写或审批串用）",
                data={"reason": "params_mismatch"},
            )
        if approval.expires_at <= self._clock():
            raise RPCError(
                BrowserErrorCode.APPROVAL_MISMATCH,
                "审批已过期",
                data={"reason": "expired"},
            )

    def _check_navigation(
        self,
        session: BrowserSession,
        params: Mapping[str, object],
        approval: Approval | None,
        spec: ActionSpec,
    ) -> None:
        if spec.action is not BrowserAction.NAVIGATE:
            # 前进/后退/刷新沿用当前 origin，由 T16 在页面级策略里判定
            return
        url = params.get("url")
        assert isinstance(url, str)
        _check_url_shape(url)
        decision = self._network_policy.check_navigation(
            workspace_id=session.workspace_id, url=url
        )
        if not decision.allowed:
            raise RPCError(
                BrowserErrorCode.SSRF_BLOCKED,
                decision.reason or "导航被网络策略拒绝",
                data={"url": url, "policy": "T16"},
            )
        if decision.requires_approval and approval is None:
            raise RPCError(
                BrowserErrorCode.SSRF_BLOCKED,
                f"该 URL 需要审批后才允许导航：{decision.reason}",
                data={"url": url, "requires_approval": True},
            )

    # -- 结果解析 ---------------------------------------------------------
    def _build_result(
        self,
        session: BrowserSession,
        action: BrowserAction,
        reply: Mapping[str, object],
        *,
        tool_call_id: UUID | None,
    ) -> BrowserResult | RPCError:
        if not isinstance(reply, Mapping):
            return RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR, "后端返回体不是映射", action_sent=True
            )
        error = reply.get("error")
        if error:
            if isinstance(error, Mapping) and "code" in error:
                return map_wire_error(
                    TransportFailure(
                        str(error.get("code")),
                        str(error.get("message", "")),
                        data={"data": error.get("data", {})},
                        action_sent=True,
                    )
                )
            return RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR, "后端返回未知错误形状", action_sent=True
            )

        raw_artifacts = reply.get("artifact_refs", ()) or ()
        if not isinstance(raw_artifacts, list | tuple):
            return RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR, "artifact_refs 必须是数组", action_sent=True
            )
        artifacts: list[UUID] = []
        for item in raw_artifacts:
            try:
                artifacts.append(item if isinstance(item, UUID) else UUID(str(item)))
            except (ValueError, AttributeError):
                return RPCError(
                    BrowserErrorCode.BROWSER_WIRE_ERROR,
                    "artifact_refs 含非 UUID 引用（禁止内联内容）",
                    action_sent=True,
                )

        final_url = reply.get("final_url")
        if final_url is not None and not isinstance(final_url, str):
            return RPCError(
                BrowserErrorCode.BROWSER_WIRE_ERROR,
                "final_url 必须是字符串或 null",
                action_sent=True,
            )
        if final_url is not None:
            final_url = _redact_url(final_url)

        observed = reply.get("observed_epoch", session.lease_epoch)
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 1:
            observed = session.lease_epoch

        redacted = tuple(str(item) for item in (reply.get("redacted") or ()))
        try:
            return BrowserResult(
                tool_call_id=tool_call_id or uuid4(),
                action=action,
                status="succeeded",
                final_url=final_url,
                artifact_refs=tuple(artifacts),
                error_code=None,
                action_sent=True,
                observed_epoch=observed,
                redacted=redacted,
            )
        except RPCError as err:
            return err

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network_policy
