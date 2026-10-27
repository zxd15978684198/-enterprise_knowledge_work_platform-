"""P0-T19 单 Provider ModelGateway：显式配置、有界重试、错误分类与用量事实。

契约来源
--------
* ``docs/poc/07_Contract_Closure.md`` C-04：ModelGateway 登记脱敏响应与稳定步骤绑定；
  模型步骤键来自已持久输入/逻辑步骤，不从临时计数生成（本任务只落 DTO 字段）。
* ``docs/tdd/02_Agent_Harness.md`` §2 ``ModelGateway``/``ModelRequest``/``ModelResult`` port；
  §5「模型 429 / 暂时 5xx / 连接失败可取消指数退避，建议最多 2 次额外尝试」；
  §6「ModelGateway 运行时读取凭据；异常 message 采用 allowlist 安全错误，
  不转储 SDK 请求/响应」。
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T19 卡：一个 provider + 受控工具 typed
  契约，只验单一 provider，不构建完整 Harness。

边界（本任务**不做**，写入证据）
--------------------------------
* 不调用真实模型：``HttpJsonTransport`` 只是成熟 HTTP 的薄适配；本任务全部用例
  打本地回环假 provider，真实模型能力 = ``NOT_RUN``（无联网、无真实 key）。
* 不实现 ToolExecutor / AgentRuntime 循环：模型响应只规范化为 ``ModelResult``
  + ``ToolProposal``；工具执行与审批链路归 T21/T06。
* 不写 Graph/Run 状态：本模块只记录请求结果与用量事实（Backlog 状态负责人）。
* 同步实现：TDD 的 async port 由 T21 适配；P0 先验证同步契约形状。

安全约束
--------
* 密钥只从环境变量读取，并以 :class:`Secret` 包装；``repr``/``str`` 恒为 ``***``，
  只有 transport 在发请求前显式 :meth:`Secret.reveal`。
* 所有对外文本（异常、safe dict、日志）经 :func:`redact_secrets`：先精确替换已知
  密钥值，再替换 ``Bearer <x>`` / ``api_key: <x>`` 模式；provider 错误体先截断再脱敏。
* provider 请求体**不含** workspace/principal（作用域只留在服务端）；provider 回包
  若带 ``workspace_id``/``principal_id`` 字段一律忽略并记录，绝不传播。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Never, Protocol
from uuid import UUID

from contracts import ClientScopeRejectedError, WorkspaceScope

__all__ = [
    "ChatMessage",
    "ConfigError",
    "ErrorClass",
    "FinishReason",
    "GatewayError",
    "HealthStatus",
    "HttpJsonTransport",
    "ModelCall",
    "ModelGateway",
    "ModelResult",
    "ModelTransport",
    "ProviderConfig",
    "RequestPayload",
    "ScopeInjectionRejected",
    "Secret",
    "ToolDeclaration",
    "ToolProposal",
    "TransportNetworkError",
    "TransportRequest",
    "TransportResponse",
    "TransportTimeout",
    "Usage",
    "build_request_payload",
    "canonical_json",
    "classify_status",
    "load_api_key",
    "parse_chat_completion",
    "redact_secrets",
    "safe_excerpt",
    "sha256_hex",
]


# ---------------------------------------------------------------------------
# 错误分类（稳定 code；证据与断言只引用这些字符串）
# ---------------------------------------------------------------------------
class ErrorClass(StrEnum):
    """provider 失败的分类；决定是否可重试与对外错误码。"""

    CONFIG = "config"
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    PROVIDER_5XX = "provider_5xx"
    BAD_REQUEST = "bad_request"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    SCOPE = "scope"


CODE_BY_CLASS: dict[ErrorClass, str] = {
    ErrorClass.CONFIG: "MODEL_CONFIG_INVALID",
    ErrorClass.AUTH: "MODEL_PROVIDER_AUTH",
    ErrorClass.RATE_LIMITED: "MODEL_PROVIDER_RATE_LIMITED",
    ErrorClass.PROVIDER_5XX: "MODEL_PROVIDER_UNAVAILABLE",
    ErrorClass.BAD_REQUEST: "MODEL_PROVIDER_BAD_REQUEST",
    ErrorClass.TIMEOUT: "MODEL_PROVIDER_TIMEOUT",
    ErrorClass.NETWORK: "MODEL_PROVIDER_NETWORK",
    ErrorClass.INVALID_RESPONSE: "MODEL_RESPONSE_INVALID",
    ErrorClass.INVALID_TOOL_ARGUMENTS: "INVALID_TOOL_ARGUMENTS",
    ErrorClass.SCOPE: "MODEL_SCOPE_INJECTION",
}

RETRYABLE_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.RATE_LIMITED,
        ErrorClass.PROVIDER_5XX,
        ErrorClass.TIMEOUT,
        ErrorClass.NETWORK,
    }
)

MAX_ERROR_BODY_CHARS = 200
MAX_ERROR_BODY_BYTES = 64 * 1024


class GatewayError(RuntimeError):
    """provider/契约失败；``code`` 稳定，``safe_message`` 已脱敏且不含 provider 原文头。"""

    def __init__(
        self,
        error_class: ErrorClass,
        safe_message: str,
        *,
        attempts: int = 0,
        status_code: int | None = None,
        provider_request_ref: str | None = None,
        retry_delays: tuple[float, ...] = (),
    ) -> None:
        super().__init__(safe_message)
        self.error_class = error_class
        self.code = CODE_BY_CLASS[error_class]
        self.safe_message = safe_message
        self.attempts = attempts
        self.retryable = error_class in RETRYABLE_CLASSES
        self.status_code = status_code
        self.provider_request_ref = provider_request_ref
        self.retry_delays = retry_delays

    def __str__(self) -> str:
        return (
            f"{self.code}: {self.safe_message} "
            f"(attempts={self.attempts}, retryable={str(self.retryable).lower()})"
        )


class ConfigError(GatewayError):
    """显式配置缺失/非法；发生在任何网络调用之前。"""

    def __init__(self, safe_message: str, *, attempts: int = 0) -> None:
        super().__init__(ErrorClass.CONFIG, safe_message, attempts=attempts)


class ScopeInjectionRejected(ClientScopeRejectedError):
    """deny 默认：作用域只能由服务端注入，模型/客户端提交的 workspace 一律拒绝。"""

    code = "MODEL_SCOPE_INJECTION"


# ---------------------------------------------------------------------------
# 秘密与脱敏
# ---------------------------------------------------------------------------
class Secret:
    """环境变量里的密钥包装：``repr``/``str`` 恒为 ``***``，只有 transport 能 reveal。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("密钥不能为空")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    __hash__ = None  # 含秘密的对象不得进入 set/dict key

    def __reduce__(self) -> Never:  # pragma: no cover - 禁止 pickle 泄密
        raise TypeError("Secret 不可序列化")


_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{4,}")
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|x-api-key|access[_-]?token|token)\b"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{4,}"
)
_SECRET_FIELD_REPLACEMENT = r"\1: ***"


def redact_secrets(text: str, *, secrets: Iterable[str | Secret] = ()) -> str:
    """先精确替换已知密钥值，再替换常见凭据字段模式。"""

    if not isinstance(text, str):
        raise TypeError("redact_secrets 只接受 str")
    redacted = text
    for item in secrets:
        raw = item.reveal() if isinstance(item, Secret) else item
        if isinstance(raw, str) and raw:
            redacted = redacted.replace(raw, "***")
    redacted = _BEARER_RE.sub(r"\1 ***", redacted)
    redacted = _SECRET_FIELD_RE.sub(_SECRET_FIELD_REPLACEMENT, redacted)
    return redacted


def safe_excerpt(
    text: str,
    limit: int = MAX_ERROR_BODY_CHARS,
    *,
    secrets: Iterable[str | Secret] = (),
) -> str:
    """脱敏后截断；用于 provider 错误体与模型文本摘录。"""

    if not isinstance(text, str):
        raise TypeError("safe_excerpt 只接受 str")
    cleaned = redact_secrets(text, secrets=secrets).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


# ---------------------------------------------------------------------------
# 显式 provider 配置
# ---------------------------------------------------------------------------
_URL_SCHEMES = frozenset({"http", "https"})
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """**一个** provider 的显式配置；没有自动发现、没有多 provider 路由。

    ``base_url``/``model``/``timeout_seconds``/``max_retries`` 必须由调用方显式给出，
    不从环境推断（除密钥值本身）。``api_key_env`` 只保存环境变量名，不保存值。
    """

    base_url: str
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.2
    backoff_cap_seconds: float = 2.0
    chat_path: str = "/v1/chat/completions"
    health_path: str = "/healthz"
    api_key_env: str = "PHASE0_MODEL_API_KEY"
    provider_name: str = "phase0-single-provider"

    def __post_init__(self) -> None:
        self._validate_base_url(self.base_url)
        _require_text(self.model, "ProviderConfig.model", 1, 128)
        _require_positive_float(self.timeout_seconds, "ProviderConfig.timeout_seconds", 600.0)
        _require_int_range(self.max_retries, "ProviderConfig.max_retries", 0, 5)
        _require_positive_float(
            self.backoff_base_seconds, "ProviderConfig.backoff_base_seconds", 30.0
        )
        _require_positive_float(
            self.backoff_cap_seconds, "ProviderConfig.backoff_cap_seconds", 60.0
        )
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ConfigError("backoff_cap_seconds 必须 >= backoff_base_seconds")
        for name, value in (("chat_path", self.chat_path), ("health_path", self.health_path)):
            if not isinstance(value, str) or not _PATH_RE.match(value):
                raise ConfigError(f"{name} 必须以 / 开头且只含 URL 安全字符")
        if not isinstance(self.api_key_env, str) or not _ENV_NAME_RE.match(self.api_key_env):
            raise ConfigError("api_key_env 必须是合法环境变量名（大写字母/数字/下划线）")
        _require_text(self.provider_name, "ProviderConfig.provider_name", 1, 64)

    @staticmethod
    def _validate_base_url(value: object) -> None:
        if not isinstance(value, str) or not value:
            raise ConfigError("base_url 不能为空")
        # 只允许 http(s)、无 userinfo（禁止把凭据写进 URL 再进日志）
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in _URL_SCHEMES:
            raise ConfigError(f"base_url 只支持 http/https，收到 scheme={parts.scheme!r}")
        if not parts.hostname:
            raise ConfigError("base_url 缺少 host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ConfigError("base_url 不得包含 userinfo/凭据")
        if parts.query or parts.fragment:
            raise ConfigError("base_url 不得包含 query/fragment")

    def describe(self) -> dict[str, Any]:
        """安全描述（可进日志/证据）：只含环境变量名与是否已配置。"""

        return {
            "provider_name": self.provider_name,
            "base_url": self.base_url.rstrip("/"),
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_cap_seconds": self.backoff_cap_seconds,
            "chat_path": self.chat_path,
            "health_path": self.health_path,
            "api_key_env": self.api_key_env,
        }

    def url_for(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


def load_api_key(config: ProviderConfig, *, environ: Mapping[str, str] | None = None) -> Secret:
    """只从环境变量读取密钥；缺失/空值立即失败，且错误消息只含变量名。"""

    source = os.environ if environ is None else environ
    value = source.get(config.api_key_env)
    if value is None or not value.strip():
        raise ConfigError(f"环境变量 {config.api_key_env} 未设置；密钥不入代码/配置文件")
    return Secret(value)


# ---------------------------------------------------------------------------
# 传输层：薄 HTTP 适配 + 假 provider 可注入
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TransportRequest:
    method: Literal["POST", "GET"]
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]
    request_ref: str


class TransportTimeout(Exception):
    """连接/读取超时（可重试）。"""


class TransportNetworkError(Exception):
    """DNS/连接重置/其它传输失败（可重试）。"""


class ModelTransport(Protocol):
    def send(self, request: TransportRequest) -> TransportResponse: ...


class HttpJsonTransport:
    """成熟 HTTP 的最小适配：不重试、不解析业务语义，只把状态/字节/头带回来。

    分类、重试与脱敏都在 :class:`ModelGateway`，因此换 provider 只换这一层。
    """

    __slots__ = ("_counter", "_max_body_bytes")

    def __init__(self, *, max_body_bytes: int = MAX_ERROR_BODY_BYTES) -> None:
        self._counter = 0
        self._max_body_bytes = max_body_bytes

    def send(self, request: TransportRequest) -> TransportResponse:
        self._counter += 1
        request_ref = f"{request.method} {request.url.split('?', 1)[0]} #{self._counter}"
        http_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                body = response.read(self._max_body_bytes)
                return TransportResponse(
                    status_code=int(response.status),
                    body=body,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    request_ref=request_ref,
                )
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(self._max_body_bytes)
            finally:
                exc.close()
            return TransportResponse(
                status_code=int(exc.code),
                body=body,
                headers={key.lower(): value for key, value in (exc.headers or {}).items()},
                request_ref=request_ref,
            )
        except TimeoutError as exc:
            raise TransportTimeout(f"{request_ref} 超时") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TransportTimeout(f"{request_ref} 超时") from exc
            raise TransportNetworkError(f"{request_ref} 连接失败") from exc
        except ConnectionError as exc:
            raise TransportNetworkError(f"{request_ref} 连接失败") from exc


# ---------------------------------------------------------------------------
# 请求 DTO（provider 侧形状；不含作用域）
# ---------------------------------------------------------------------------
CHAT_ROLES: tuple[str, ...] = ("system", "user", "assistant", "tool")
_STEP_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in CHAT_ROLES:
            raise ConfigError(f"不支持的消息角色 {self.role!r}")
        if not isinstance(self.content, str):
            raise ConfigError("消息内容必须是 str")
        if len(self.content) > 100_000:
            raise ConfigError("单条消息内容超过 100k 字符上限")

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """授权后的工具声明（由 ToolRegistry 提供；gateway 不反查 registry）。"""

    name: str
    version: str
    description: str
    parameters_schema: Mapping[str, Any]
    schema_hash: str

    def __post_init__(self) -> None:
        _require_text(self.name, "ToolDeclaration.name", 1, 64)
        _require_text(self.version, "ToolDeclaration.version", 1, 32)
        if not re.match(r"^[0-9a-f]{64}$", str(self.schema_hash)):
            raise ConfigError("ToolDeclaration.schema_hash 必须是 sha256 hex")
        if not isinstance(self.parameters_schema, Mapping):
            raise ConfigError("ToolDeclaration.parameters_schema 必须是映射")

    def as_provider_payload(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters_schema),
            },
        }


@dataclass(frozen=True, slots=True)
class ModelCall:
    """一次模型步骤的输入；``workspace_id`` 必须由服务端从 Scope 注入。"""

    workspace_id: UUID
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDeclaration, ...] = ()
    max_output_tokens: int = 512
    temperature: float = 0.0
    model_step_key: str = "step-1"
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, UUID):
            raise ScopeInjectionRejected("ModelCall.workspace_id 必须是服务端注入的 UUID")
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ConfigError("ModelCall.messages 必须是非空 tuple")
        if not all(isinstance(item, ChatMessage) for item in self.messages):
            raise ConfigError("ModelCall.messages 只接受 ChatMessage")
        if not isinstance(self.tools, tuple) or not all(
            isinstance(item, ToolDeclaration) for item in self.tools
        ):
            raise ConfigError("ModelCall.tools 只接受 ToolDeclaration tuple")
        names = [item.name for item in self.tools]
        if len(set(names)) != len(names):
            raise ConfigError("ModelCall.tools 的 name 不得重复")
        _require_int_range(self.max_output_tokens, "ModelCall.max_output_tokens", 1, 32768)
        if not isinstance(self.temperature, int | float) or not (
            0.0 <= float(self.temperature) <= 2.0
        ):
            raise ConfigError("ModelCall.temperature 必须在 [0, 2]")
        if not isinstance(self.model_step_key, str) or not _STEP_KEY_RE.match(self.model_step_key):
            raise ConfigError("model_step_key 必须是 1-128 位 [A-Za-z0-9_.:@-]（来自已持久输入）")
        if self.timeout_seconds is not None:
            _require_positive_float(self.timeout_seconds, "ModelCall.timeout_seconds", 600.0)


@dataclass(frozen=True, slots=True)
class RequestPayload:
    """发往 provider 的 JSON 形状；**不含** workspace/principal/密钥。"""

    model: str
    messages: tuple[Mapping[str, str], ...]
    tools: tuple[Mapping[str, Any], ...]
    max_output_tokens: int
    temperature: float
    stream: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(item) for item in self.messages],
            "max_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "stream": self.stream,
        }
        if self.tools:
            payload["tools"] = [dict(item) for item in self.tools]
        return payload


def build_request_payload(config: ProviderConfig, call: ModelCall) -> RequestPayload:
    """构造 provider 请求体；作用域字段在此处**没有任何**来源。"""

    return RequestPayload(
        model=config.model,
        messages=tuple(item.as_payload() for item in call.messages),
        tools=tuple(item.as_provider_payload() for item in call.tools),
        max_output_tokens=call.max_output_tokens,
        temperature=float(call.temperature),
    )


# ---------------------------------------------------------------------------
# 响应 DTO
# ---------------------------------------------------------------------------
FinishReason = Literal["stop", "tool_calls", "length"]
FINISH_REASONS: tuple[str, ...] = ("stop", "tool_calls", "length")
_IGNORED_RESPONSE_FIELDS: tuple[str, ...] = (
    "workspace_id",
    "principal_id",
    "policy_snapshot_id",
    "scope",
    "role",
)


@dataclass(frozen=True, slots=True)
class Usage:
    """固定结构的用量事实；未知成本用 0 占位并显式标注 ``cost_source``。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_microunits: int = 0
    currency: str = "USD"
    cost_source: Literal["provider_reported", "estimated", "unavailable"] = "unavailable"

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens", "cost_microunits"):
            _require_int_range(getattr(self, name), f"Usage.{name}", 0, 10**12)
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise _invalid_response("usage.total_tokens 小于 input+output")
        if not isinstance(self.currency, str) or not re.match(r"^[A-Z]{3}$", self.currency):
            raise _invalid_response("usage.currency 必须是 3 位大写币种")
        if self.cost_source not in ("provider_reported", "estimated", "unavailable"):
            raise _invalid_response("usage.cost_source 非法")

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_microunits": self.cost_microunits,
            "currency": self.currency,
            "cost_source": self.cost_source,
        }


@dataclass(frozen=True, slots=True)
class ToolProposal:
    """模型提出的一次工具调用；本任务只解析，不执行（执行归 T21）。"""

    name: str
    arguments: Mapping[str, Any]
    raw_arguments: str
    call_ordinal: int
    provider_call_id: str | None = None
    declared_version: str | None = None

    def safe_arguments_keys(self) -> tuple[str, ...]:
        return tuple(sorted(str(key) for key in self.arguments))


@dataclass(frozen=True, slots=True)
class ModelResult:
    """规范化模型响应；``usage`` 固定存在，``text`` 只留在内存（safe dict 会脱敏）。"""

    model: str
    finish_reason: FinishReason
    text: str
    tool_proposals: tuple[ToolProposal, ...]
    usage: Usage
    provider_request_ref: str
    attempts: int
    latency_ms: float
    truncated: bool = False
    ignored_response_fields: tuple[str, ...] = ()

    def to_safe_dict(self, *, secrets: Iterable[str | Secret] = ()) -> dict[str, Any]:
        """可进日志/证据的脱敏视图（不含原始 arguments 值，只含键名）。"""

        return {
            "model": self.model,
            "finish_reason": self.finish_reason,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 3),
            "truncated": self.truncated,
            "text_excerpt": safe_excerpt(self.text, 120, secrets=secrets),
            "tool_proposals": [
                {
                    "name": item.name,
                    "call_ordinal": item.call_ordinal,
                    "argument_keys": list(item.safe_arguments_keys()),
                }
                for item in self.tool_proposals
            ],
            "usage": self.usage.as_dict(),
            "provider_request_ref": self.provider_request_ref,
            "ignored_response_fields": list(self.ignored_response_fields),
        }


# ---------------------------------------------------------------------------
# 响应解析（OpenAI 兼容 chat.completions 形状）
# ---------------------------------------------------------------------------
def _invalid_response(message: str) -> GatewayError:
    return GatewayError(ErrorClass.INVALID_RESPONSE, message)


def _invalid_tool_arguments(message: str) -> GatewayError:
    return GatewayError(ErrorClass.INVALID_TOOL_ARGUMENTS, message)


def parse_chat_completion(
    body: bytes,
    *,
    model: str,
    provider_request_ref: str,
    attempts: int,
    latency_ms: float,
) -> ModelResult:
    """把 provider 回包规范化为 :class:`ModelResult`；任何歧义都 fail closed。"""

    if not isinstance(body, bytes | bytearray):
        raise _invalid_response("回包不是字节串")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid_response("回包不是合法 JSON（可能被截断）") from exc
    if not isinstance(parsed, dict):
        raise _invalid_response("回包顶层必须是 JSON object")

    ignored = tuple(sorted(key for key in _IGNORED_RESPONSE_FIELDS if key in parsed))

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _invalid_response("回包缺少非空 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise _invalid_response("choices[0] 必须是 object")

    finish_reason_raw = first.get("finish_reason")
    if finish_reason_raw not in FINISH_REASONS:
        raise _invalid_response(f"未知 finish_reason: {finish_reason_raw!r}")
    finish_reason: FinishReason = finish_reason_raw  # type: ignore[assignment]

    message = first.get("message", {})
    if not isinstance(message, dict):
        raise _invalid_response("choices[0].message 必须是 object")
    content = message.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        raise _invalid_response("message.content 必须是 str 或 null")

    truncated = finish_reason == "length"
    proposals: tuple[ToolProposal, ...] = ()
    if finish_reason == "length" and message.get("tool_calls"):
        # TDD02 §5：截断的工具参数不「猜修复后执行」。
        proposals = ()
    elif finish_reason == "tool_calls":
        if not isinstance(message.get("tool_calls"), list) or not message["tool_calls"]:
            raise _invalid_response("finish_reason=tool_calls 但缺少 tool_calls")
        proposals = _parse_tool_calls(message["tool_calls"])
    elif message.get("tool_calls"):
        raise _invalid_response("finish_reason 非 tool_calls 却带 tool_calls")

    usage = _parse_usage(parsed.get("usage"))
    return ModelResult(
        model=model,
        finish_reason=finish_reason,
        text=text,
        tool_proposals=proposals,
        usage=usage,
        provider_request_ref=provider_request_ref,
        attempts=attempts,
        latency_ms=latency_ms,
        truncated=truncated,
        ignored_response_fields=ignored,
    )


def _parse_tool_calls(raw_calls: list[Any]) -> tuple[ToolProposal, ...]:
    proposals: list[ToolProposal] = []
    seen: set[str] = set()
    for ordinal, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            raise _invalid_response(f"tool_calls[{ordinal}] 必须是 object")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _invalid_response(f"tool_calls[{ordinal}].function 缺失")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise _invalid_response(f"tool_calls[{ordinal}].function.name 缺失")
        if name in seen:
            raise _invalid_response(f"同一响应重复工具名 {name!r}")
        seen.add(name)
        raw_arguments = function.get("arguments")
        if not isinstance(raw_arguments, str):
            raise _invalid_tool_arguments(
                f"tool_calls[{ordinal}] 的 arguments 必须是 JSON 字符串（截断/半截参数不执行）"
            )
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise _invalid_tool_arguments(
                f"tool_calls[{ordinal}] 的 arguments 不是合法 JSON（可能被截断）"
            ) from exc
        if not isinstance(arguments, dict):
            raise _invalid_tool_arguments(f"tool_calls[{ordinal}] 的 arguments 必须是 JSON object")
        provider_id = item.get("id")
        proposals.append(
            ToolProposal(
                name=name,
                arguments=arguments,
                raw_arguments=raw_arguments,
                call_ordinal=ordinal,
                provider_call_id=provider_id if isinstance(provider_id, str) else None,
            )
        )
    return tuple(proposals)


def _parse_usage(raw: object) -> Usage:
    if raw is None:
        return Usage()
    if not isinstance(raw, dict):
        raise _invalid_response("usage 必须是 object")
    try:
        input_tokens = int(raw.get("prompt_tokens", 0))
        output_tokens = int(raw.get("completion_tokens", 0))
        total_tokens = int(raw.get("total_tokens", input_tokens + output_tokens))
        cost = int(raw.get("cost_microunits", 0))
    except (TypeError, ValueError) as exc:
        raise _invalid_response("usage 字段必须是整数") from exc
    currency = raw.get("currency", "USD")
    cost_source = raw.get("cost_source", "provider_reported" if cost else "unavailable")
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_microunits=cost,
        currency=currency,
        cost_source=cost_source,
    )


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    latency_ms: float
    attempts: int
    status_code: int | None = None
    error_code: str | None = None
    provider_request_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 3),
            "attempts": self.attempts,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "provider_request_ref": self.provider_request_ref,
        }


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
class ModelGateway:
    """单 provider 薄网关：显式配置、有界指数退避、分类错误、用量事实。

    重试只覆盖 ``RATE_LIMITED`` / ``PROVIDER_5XX`` / ``TIMEOUT`` / ``NETWORK``；
    认证失败、4xx、回包非法、工具参数截断都**不重试**（避免重复计费/重复副作用）。
    """

    __slots__ = ("config", "_api_key", "_clock", "_sleep", "_transport")

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: ModelTransport | None = None,
        api_key: Secret | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, ProviderConfig):
            raise ConfigError("ModelGateway 需要显式 ProviderConfig")
        self.config = config
        self._transport: ModelTransport = transport or HttpJsonTransport()
        self._api_key = api_key
        self._sleep = sleep
        self._clock = clock

    # -- 配置/安全视图 -------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """安全描述：不含密钥值，只说明是否已配置。"""

        info = self.config.describe()
        info["api_key_configured"] = self._api_key is not None
        return info

    def __repr__(self) -> str:
        return (
            f"ModelGateway(provider={self.config.provider_name!r}, "
            f"model={self.config.model!r}, base_url={self.config.base_url!r}, key=***)"
        )

    def _require_key(self) -> Secret:
        if self._api_key is None:
            self._api_key = load_api_key(self.config)
        return self._api_key

    # -- 健康探测 -----------------------------------------------------
    def health(self, *, timeout_seconds: float | None = None) -> HealthStatus:
        """单次探测；不重试、不抛异常，返回可记录的失败分类。"""

        key = self._require_key()
        timeout = min(self.config.timeout_seconds, 5.0)
        if timeout_seconds is not None:
            _require_positive_float(timeout_seconds, "health.timeout_seconds", 600.0)
            timeout = timeout_seconds
        request = TransportRequest(
            method="GET",
            url=self.config.url_for(self.config.health_path),
            headers={
                "Authorization": f"Bearer {key.reveal()}",
                "Accept": "application/json",
            },
            body=None,
            timeout_seconds=timeout,
        )
        started = self._clock()
        try:
            response = self._transport.send(request)
        except TransportTimeout:
            return HealthStatus(
                ok=False,
                latency_ms=(self._clock() - started) * 1000.0,
                attempts=1,
                error_code=CODE_BY_CLASS[ErrorClass.TIMEOUT],
            )
        except TransportNetworkError:
            return HealthStatus(
                ok=False,
                latency_ms=(self._clock() - started) * 1000.0,
                attempts=1,
                error_code=CODE_BY_CLASS[ErrorClass.NETWORK],
            )
        latency_ms = (self._clock() - started) * 1000.0
        if 200 <= response.status_code < 300:
            return HealthStatus(
                ok=True,
                latency_ms=latency_ms,
                attempts=1,
                status_code=response.status_code,
                provider_request_ref=response.request_ref,
            )
        error_class = classify_status(response.status_code)
        return HealthStatus(
            ok=False,
            latency_ms=latency_ms,
            attempts=1,
            status_code=response.status_code,
            error_code=CODE_BY_CLASS[error_class],
            provider_request_ref=response.request_ref,
        )

    # -- 模型调用 -----------------------------------------------------
    def call(
        self,
        scope: WorkspaceScope,
        call: ModelCall,
        *,
        reservation_id: UUID | None = None,
    ) -> ModelResult:
        """执行一次模型步骤（含重试）；失败抛 :class:`GatewayError`。

        ``reservation_id`` 只记录在错误/结果的关联字段中，预算账本归 T20。
        """

        if not isinstance(scope, WorkspaceScope):
            raise ScopeInjectionRejected("call() 需要服务端签发的 WorkspaceScope")
        if not isinstance(call, ModelCall):
            raise GatewayError(ErrorClass.BAD_REQUEST, "call() 需要 ModelCall")
        if call.workspace_id != scope.workspace_id:
            raise ScopeInjectionRejected(
                "模型/客户端提交的 workspace 与授权 Scope 不一致，已拒绝（deny 默认）"
            )
        del reservation_id  # 目前只在此处确认调用方语义，账本归 T20
        key = self._require_key()
        payload = build_request_payload(self.config, call)
        body = json.dumps(payload.as_dict(), ensure_ascii=False, allow_nan=False).encode("utf-8")
        timeout = call.timeout_seconds or self.config.timeout_seconds
        request = TransportRequest(
            method="POST",
            url=self.config.url_for(self.config.chat_path),
            headers={
                "Authorization": f"Bearer {key.reveal()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=body,
            timeout_seconds=timeout,
        )

        retry_delays: list[float] = []
        total_attempts = self.config.max_retries + 1
        last_error: GatewayError | None = None
        for attempt in range(1, total_attempts + 1):
            started = self._clock()
            try:
                response = self._transport.send(request)
            except TransportTimeout:
                last_error = GatewayError(
                    ErrorClass.TIMEOUT,
                    "provider 请求超时",
                    attempts=attempt,
                    retry_delays=tuple(retry_delays),
                )
            except TransportNetworkError:
                last_error = GatewayError(
                    ErrorClass.NETWORK,
                    "provider 连接失败",
                    attempts=attempt,
                    retry_delays=tuple(retry_delays),
                )
            else:
                latency_ms = (self._clock() - started) * 1000.0
                if 200 <= response.status_code < 300:
                    return parse_chat_completion(
                        response.body,
                        model=self.config.model,
                        provider_request_ref=response.request_ref,
                        attempts=attempt,
                        latency_ms=latency_ms,
                    )
                error_class = classify_status(response.status_code)
                last_error = GatewayError(
                    error_class,
                    self._describe_http_failure(response, key),
                    attempts=attempt,
                    status_code=response.status_code,
                    provider_request_ref=response.request_ref,
                    retry_delays=tuple(retry_delays),
                )
                if error_class in (ErrorClass.RATE_LIMITED, ErrorClass.PROVIDER_5XX):
                    retry_after = _retry_after_seconds(response.headers)
                    if retry_after is not None and attempt < total_attempts:
                        delay = min(max(retry_after, 0.0), self.config.backoff_cap_seconds)
                        retry_delays.append(delay)
                        self._sleep(delay)
                        continue
            if attempt >= total_attempts or last_error is None or not last_error.retryable:
                raise last_error
            delay = self.backoff_delay(attempt - 1)
            retry_delays.append(delay)
            self._sleep(delay)
        raise last_error or GatewayError(ErrorClass.NETWORK, "provider 调用失败")

    def backoff_delay(self, retry_index: int) -> float:
        """有界指数退避：``min(base * 2**retry_index, cap)``（retry_index 从 0 起）。"""

        if retry_index < 0:
            raise ConfigError("retry_index 不能为负")
        raw = self.config.backoff_base_seconds * (2 ** retry_index)
        return min(raw, self.config.backoff_cap_seconds)

    def _describe_http_failure(self, response: TransportResponse, key: Secret) -> str:
        text = response.body.decode("utf-8", errors="replace")
        excerpt = safe_excerpt(text, secrets=(key,))
        if not excerpt:
            return f"provider 返回 HTTP {response.status_code}"
        return f"provider 返回 HTTP {response.status_code}: {excerpt}"


def classify_status(status_code: int) -> ErrorClass:
    """HTTP 状态 → 错误分类（未知状态 fail closed 为非重试的 BAD_REQUEST）。"""

    if status_code == 429:
        return ErrorClass.RATE_LIMITED
    if status_code in (401, 403):
        return ErrorClass.AUTH
    if status_code == 408:
        return ErrorClass.TIMEOUT
    if 500 <= status_code <= 599:
        return ErrorClass.PROVIDER_5XX
    if 200 <= status_code <= 299:  # pragma: no cover - 调用方已先判成功
        raise ConfigError("classify_status 不接受 2xx")
    return ErrorClass.BAD_REQUEST


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


# ---------------------------------------------------------------------------
# 校验助手
# ---------------------------------------------------------------------------
def _require_text(value: object, name: str, min_length: int, max_length: int) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} 必须是 str")
    if not min_length <= len(value) <= max_length:
        raise ConfigError(f"{name} 长度必须在 [{min_length}, {max_length}]")
    return value


def _require_positive_float(value: object, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name} 必须是数字")
    number = float(value)
    if not 0 < number <= maximum:
        raise ConfigError(f"{name} 必须在 (0, {maximum}]")
    return number


def _require_int_range(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必须是 int")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} 必须在 [{minimum}, {maximum}]")
    return value


def canonical_json(payload: object) -> str:
    """稳定 JSON 序列化（排序键、无空白、禁 NaN）；用于哈希而非传输。"""

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
