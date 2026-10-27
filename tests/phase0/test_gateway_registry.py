"""P0-T19 验收测试：单 provider ModelGateway + 窄工具 Registry。

原则
----
* **不调用真实模型 API**：所有 provider 都是本地假传输（``FakeTransport``）或
  127.0.0.1 回环 HTTP 服务器；没有任何外网请求、没有真实 key。
* 协议矩阵覆盖 429/4xx/5xx/超时/网络/截断/半截参数/未知 finish_reason/坏 usage。
* 密钥只出现在 transport 的 ``Authorization`` 头里；日志、异常、安全视图一律脱敏，
  用固定 canary 字符串做反例断言（``sk-canary-...``）。
* 工具侧只验证「白名单解析 + 参数校验 + 稳定 intent + 审批门」，**没有执行器**；
  审批链路本身归 T06/T21，本文件只证明「未批准不得执行」的拒绝路径。
"""

from __future__ import annotations

import http.server
import json
import re
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import contracts  # noqa: E402
import model_gateway as gw  # noqa: E402
import tool_registry as tools  # noqa: E402

WS_A = UUID("0a0a0a0a-0000-4000-8000-00000000000a")
WS_B = UUID("0b0b0b0b-0000-4000-8000-00000000000b")
PRINCIPAL_A = UUID("a11a11a1-0000-4000-8000-00000000000a")
POLICY_A = UUID("a9a9a9a9-0000-4000-8000-0000000000aa")
ARTIFACT_ID = UUID("c0dec0de-0000-4000-8000-00000000000c")
RUN_ID = UUID("22222222-0000-4000-8000-000000000002")
CANARY = "sk-canary-t19-4f1d0c7a9b2e"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 测试替身与构造助手
# ---------------------------------------------------------------------------
class FakeTransport:
    """按顺序回放预置响应；记录每个请求，便于断言重试次数与请求体。"""

    def __init__(self, *responses: object, default: object | None = None) -> None:
        self._responses = list(responses)
        self._default = default
        self.requests: list[gw.TransportRequest] = []

    def send(self, request: gw.TransportRequest) -> gw.TransportResponse:
        self.requests.append(request)
        item = self._responses.pop(0) if self._responses else self._default
        if item is None:
            raise AssertionError("FakeTransport 没有剩余响应")
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, gw.TransportResponse)
        return item


def _scope(
    *, workspace_id: UUID = WS_A, principal_id: UUID = PRINCIPAL_A, role: str = "owner"
) -> contracts.WorkspaceScope:
    principal = contracts.synthesize_principal(
        workspace_id=workspace_id,
        principal_id=principal_id,
        membership_subject_ref="subject-t19",
        kind="service",
        role=role,
    )
    return contracts.issue_scope(principal, policy_snapshot_id=POLICY_A)


def _config(**overrides: Any) -> gw.ProviderConfig:
    params: dict[str, Any] = {
        "base_url": "http://127.0.0.1:9",
        "model": "phase0-fake-model",
        "timeout_seconds": 2.0,
        "max_retries": 1,
        "backoff_base_seconds": 0.01,
        "backoff_cap_seconds": 0.05,
    }
    params.update(overrides)
    return gw.ProviderConfig(**params)


def _gateway(
    *responses: object,
    config: gw.ProviderConfig | None = None,
    default: object | None = None,
) -> tuple[gw.ModelGateway, FakeTransport, list[float]]:
    transport = FakeTransport(*responses, default=default)
    sleeps: list[float] = []
    gateway = gw.ModelGateway(
        config or _config(),
        transport=transport,
        api_key=gw.Secret(CANARY),
        sleep=sleeps.append,
        clock=lambda: 100.0,
    )
    return gateway, transport, sleeps


def _call(*, workspace_id: UUID = WS_A, tools: tuple[Any, ...] = (), **overrides: Any) -> gw.ModelCall:
    params: dict[str, Any] = {
        "workspace_id": workspace_id,
        "messages": (gw.ChatMessage(role="user", content="ping"),),
        "tools": tools,
    }
    params.update(overrides)
    return gw.ModelCall(**params)


def _response(
    status_code: int = 200,
    body: bytes = b"{}",
    *,
    headers: dict[str, str] | None = None,
    request_ref: str = "req-fake-1",
) -> gw.TransportResponse:
    # 传输层契约：回包头一律小写（真实 HttpJsonTransport 也做同样归一化）。
    return gw.TransportResponse(
        status_code=status_code,
        body=body,
        headers={key.lower(): value for key, value in (headers or {}).items()},
        request_ref=request_ref,
    )


def _completion(
    *,
    finish_reason: str = "stop",
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "phase0-fake-model",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 7},
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _tool_call(name: str, arguments: object, *, call_id: str = "call_1") -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


def _registry() -> tools.ToolRegistry:
    return tools.build_phase0_registry()


# ---------------------------------------------------------------------------
# 1) 单 provider 显式配置 + 密钥只从环境读取
# ---------------------------------------------------------------------------
def test_single_provider_config_is_explicit_and_describable() -> None:
    config = _config(provider_name="provider-under-test")
    described = config.describe()
    assert described["provider_name"] == "provider-under-test"
    assert described["model"] == "phase0-fake-model"
    assert described["api_key_env"] == "PHASE0_MODEL_API_KEY"
    assert described["max_retries"] == 1
    assert "api_key" not in described
    assert config.url_for("/v1/chat/completions") == "http://127.0.0.1:9/v1/chat/completions"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"base_url": "ftp://127.0.0.1"}, "scheme"),
        ({"base_url": "http://user:pass@127.0.0.1:9"}, "userinfo"),
        ({"base_url": "http://127.0.0.1:9?debug=1"}, "query"),
        ({"timeout_seconds": 0.0}, "timeout"),
        ({"timeout_seconds": 601.0}, "timeout"),
        ({"max_retries": 6}, "retries"),
        ({"max_retries": -1}, "retries"),
        ({"backoff_cap_seconds": 0.001}, "backoff"),
        ({"chat_path": "v1/chat"}, "path"),
        ({"api_key_env": "lower-case-env"}, "env name"),
    ],
    ids=[
        "scheme",
        "userinfo",
        "query",
        "timeout-zero",
        "timeout-too-large",
        "retries-too-many",
        "retries-negative",
        "backoff-cap-below-base",
        "chat-path-without-slash",
        "env-name-lowercase",
    ],
)
def test_provider_config_rejects_unsafe_or_implicit_values(
    overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(gw.ConfigError) as excinfo:
        _config(**overrides)
    assert excinfo.value.code == gw.CODE_BY_CLASS[gw.ErrorClass.CONFIG]
    assert reason  # 说明性标签，避免参数未使用


def test_api_key_comes_from_environment_only_and_never_leaks() -> None:
    config = _config()
    with pytest.raises(gw.ConfigError) as missing:
        gw.load_api_key(config, environ={})
    assert config.api_key_env in str(missing.value)
    assert "None" not in str(missing.value)

    secret = gw.load_api_key(config, environ={config.api_key_env: CANARY})
    assert secret.reveal() == CANARY
    assert repr(secret) == "Secret(***)"
    assert str(secret) == "Secret(***)"
    assert CANARY not in repr(secret)

    gateway = gw.ModelGateway(config, transport=FakeTransport(), api_key=secret)
    assert gateway.describe()["api_key_configured"] is True
    assert CANARY not in repr(gateway)
    assert CANARY not in json.dumps(gateway.describe(), ensure_ascii=False)
    with pytest.raises(TypeError):
        secret.__reduce__()  # pickle 泄密路径必须关闭


# ---------------------------------------------------------------------------
# 2) 协议 / 故障矩阵（≥20 组 fixture）
# ---------------------------------------------------------------------------
_RATE = gw.ErrorClass.RATE_LIMITED
_UNAVAILABLE = gw.ErrorClass.PROVIDER_5XX
_AUTH = gw.ErrorClass.AUTH
_BAD = gw.ErrorClass.BAD_REQUEST
_TIMEOUT = gw.ErrorClass.TIMEOUT
_NETWORK = gw.ErrorClass.NETWORK


@pytest.mark.parametrize(
    ("response", "error_class", "retryable"),
    [
        (_response(429, b'{"error":"slow down"}'), _RATE, True),
        (_response(401), _AUTH, False),
        (_response(403), _AUTH, False),
        (_response(400), _BAD, False),
        (_response(404), _BAD, False),
        (_response(409), _BAD, False),
        (_response(422), _BAD, False),
        (_response(408), _TIMEOUT, True),
        (_response(500), _UNAVAILABLE, True),
        (_response(502), _UNAVAILABLE, True),
        (_response(503), _UNAVAILABLE, True),
        (_response(504), _UNAVAILABLE, True),
        (gw.TransportTimeout("boom"), _TIMEOUT, True),
        (gw.TransportNetworkError("boom"), _NETWORK, True),
    ],
    ids=[
        "429",
        "401",
        "403",
        "400",
        "404",
        "409",
        "422",
        "408",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "network",
    ],
)
def test_http_and_transport_errors_are_classified_and_bounded(
    response: object, error_class: gw.ErrorClass, retryable: bool
) -> None:
    gateway, transport, sleeps = _gateway(config=_config(max_retries=1))
    transport._default = response  # 同一个错误重复出现，验证重试上限

    with pytest.raises(gw.GatewayError) as excinfo:
        gateway.call(_scope(), _call())

    error = excinfo.value
    assert error.error_class is error_class
    assert error.code == gw.CODE_BY_CLASS[error_class]
    assert error.retryable is retryable
    expected_attempts = 2 if retryable else 1
    assert error.attempts == expected_attempts
    assert len(transport.requests) == expected_attempts
    assert len(sleeps) == expected_attempts - 1
    assert len(error.retry_delays) == expected_attempts - 1
    if retryable:
        assert all(0 < delay <= gateway.config.backoff_cap_seconds for delay in error.retry_delays)


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"not-json", gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE]),
        (b"[]", gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE]),
        (json.dumps({"choices": []}).encode(), gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE]),
        (
            _completion(finish_reason="stop", tool_calls=[_tool_call("site.read_counter", {})]),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE],
        ),
        (
            _completion(finish_reason="tool_calls", tool_calls=[]),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE],
        ),
        (
            _completion(
                finish_reason="tool_calls",
                tool_calls=[_tool_call("site.read_counter", "{not-json")],
            ),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_TOOL_ARGUMENTS],
        ),
        (
            _completion(
                finish_reason="tool_calls",
                tool_calls=[{"id": "c1", "function": {"name": "site.read_counter", "arguments": {}}}],
            ),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_TOOL_ARGUMENTS],
        ),
        (
            _completion(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call("site.read_counter", {"path": "/idem/count"}, call_id="c1"),
                    _tool_call("site.read_counter", {"path": "/nonidem/count"}, call_id="c2"),
                ],
            ),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE],
        ),
        (
            _completion(
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 3},
            ),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE],
        ),
        (
            _completion(finish_reason="weird"),
            gw.CODE_BY_CLASS[gw.ErrorClass.INVALID_RESPONSE],
        ),
    ],
    ids=[
        "body-not-json",
        "body-top-level-array",
        "choices-empty",
        "stop-with-tool-calls",
        "tool-calls-without-payload",
        "arguments-not-json",
        "arguments-not-string",
        "duplicate-tool-name",
        "usage-total-too-small",
        "unknown-finish-reason",
    ],
)
def test_invalid_response_shapes_fail_closed(body: bytes, expected_code: str) -> None:
    gateway, transport, _ = _gateway(_response(200, body))
    with pytest.raises(gw.GatewayError) as excinfo:
        gateway.call(_scope(), _call())
    assert excinfo.value.code == expected_code
    assert len(transport.requests) == 1


def test_empty_tool_calls_list_with_stop_is_a_plain_text_turn() -> None:
    """`tool_calls: []` 等价于「没有工具调用」：接受为空文本轮，不算协议违规。"""

    body = _completion(finish_reason="stop", tool_calls=[], content="done")
    gateway, _, _ = _gateway(_response(200, body))
    result = gateway.call(_scope(), _call())
    assert result.finish_reason == "stop"
    assert result.tool_proposals == ()
    assert result.text == "done"


def test_structured_tool_call_is_parsed_without_execution() -> None:
    body = _completion(
        finish_reason="tool_calls",
        tool_calls=[_tool_call("site.read_counter", {"path": "/idem/count"})],
    )
    gateway, _, _ = _gateway(_response(200, body))
    result = gateway.call(_scope(), _call(tools=_registry().declarations(scope=_scope())))

    assert result.finish_reason == "tool_calls"
    assert result.attempts == 1
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 18
    assert result.usage.cost_source == "unavailable"
    assert result.truncated is False
    assert len(result.tool_proposals) == 1
    proposal = result.tool_proposals[0]
    assert proposal.name == "site.read_counter"
    assert proposal.arguments == {"path": "/idem/count"}
    assert proposal.call_ordinal == 0
    assert proposal.provider_call_id == "call_1"
    safe = result.to_safe_dict(secrets=[CANARY])
    assert safe["tool_proposals"] == [
        {"name": "site.read_counter", "call_ordinal": 0, "argument_keys": ["path"]}
    ]
    assert "arguments" not in json.dumps(safe["tool_proposals"])


def test_truncated_response_drops_tool_proposals() -> None:
    body = _completion(
        finish_reason="length",
        content="partial answer",
        tool_calls=[_tool_call("site.read_counter", '{"path": "/idem')],
    )
    gateway, _, _ = _gateway(_response(200, body))
    result = gateway.call(_scope(), _call())
    assert result.truncated is True
    assert result.tool_proposals == ()
    assert result.text == "partial answer"


# ---------------------------------------------------------------------------
# 3) 有界退避 / Retry-After / 健康探测
# ---------------------------------------------------------------------------
def test_retry_uses_bounded_exponential_backoff() -> None:
    gateway, transport, sleeps = _gateway(
        _response(503), _response(503), _response(200, _completion(content="ok")),
        config=_config(max_retries=2, backoff_base_seconds=0.01, backoff_cap_seconds=0.05),
    )
    result = gateway.call(_scope(), _call())
    assert result.text == "ok"
    assert result.attempts == 3
    assert len(transport.requests) == 3
    assert sleeps == [0.01, 0.02]
    assert gateway.backoff_delay(4) == 0.05
    assert gateway.backoff_delay(0) == 0.01


@pytest.mark.parametrize(("retry_after", "expected"), [("0.03", 0.03), ("99", 0.05)])
def test_retry_after_header_is_honoured_and_capped(retry_after: str, expected: float) -> None:
    gateway, _, sleeps = _gateway(
        _response(429, headers={"Retry-After": retry_after}),
        _response(200, _completion(content="ok")),
        config=_config(max_retries=1, backoff_cap_seconds=0.05),
    )
    result = gateway.call(_scope(), _call())
    assert result.text == "ok"
    assert sleeps == [expected]


def test_non_retryable_errors_do_not_repeat_the_request() -> None:
    gateway, transport, sleeps = _gateway(_response(401), _response(200, _completion(content="late")))
    with pytest.raises(gw.GatewayError) as excinfo:
        gateway.call(_scope(), _call())
    assert excinfo.value.error_class is _AUTH
    assert len(transport.requests) == 1
    assert sleeps == []


def test_exhausted_retries_report_attempts_and_delays() -> None:
    gateway, transport, _ = _gateway(
        _response(500), _response(500), config=_config(max_retries=1, backoff_base_seconds=0.02)
    )
    with pytest.raises(gw.GatewayError) as excinfo:
        gateway.call(_scope(), _call())
    error = excinfo.value
    assert error.attempts == 2
    assert error.retry_delays == (0.02,)
    assert error.status_code == 500
    assert error.provider_request_ref == "req-fake-1"
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("response", "ok", "expected_error"),
    [
        (_response(200, b'{"status":"ok"}'), True, None),
        (_response(503), False, gw.CODE_BY_CLASS[gw.ErrorClass.PROVIDER_5XX]),
        (gw.TransportTimeout("slow"), False, gw.CODE_BY_CLASS[gw.ErrorClass.TIMEOUT]),
        (gw.TransportNetworkError("refused"), False, gw.CODE_BY_CLASS[gw.ErrorClass.NETWORK]),
    ],
    ids=["ok", "503", "timeout", "network"],
)
def test_health_probe_reports_status_without_retrying(
    response: object, ok: bool, expected_error: str | None
) -> None:
    gateway, transport, sleeps = _gateway(default=response)
    status = gateway.health()
    assert status.ok is ok
    assert status.error_code == expected_error
    assert status.attempts == 1
    assert len(transport.requests) == 1
    assert sleeps == []
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url.endswith("/healthz")
    assert transport.requests[0].body is None
    assert set(status.as_dict()) == {
        "ok",
        "latency_ms",
        "attempts",
        "status_code",
        "error_code",
        "provider_request_ref",
    }


def test_health_timeout_is_bounded_by_override() -> None:
    gateway, transport, _ = _gateway(default=_response(200, b"{}"))
    gateway.health(timeout_seconds=0.5)
    assert transport.requests[0].timeout_seconds == 0.5
    gateway.health()
    assert transport.requests[1].timeout_seconds == 2.0


# ---------------------------------------------------------------------------
# 4) Scope 注入拒绝 + 请求体不含密钥/作用域
# ---------------------------------------------------------------------------
def test_scope_injection_is_rejected_by_default() -> None:
    with pytest.raises(gw.ScopeInjectionRejected) as bad_call:
        gw.ModelCall(workspace_id="ws-evil", messages=(gw.ChatMessage("user", "hi"),))
    assert bad_call.value.code == "MODEL_SCOPE_INJECTION"

    gateway, transport, _ = _gateway(_response(200, _completion(content="ok")))
    with pytest.raises(gw.ScopeInjectionRejected):
        gateway.call(_scope(), _call(workspace_id=WS_B))
    assert transport.requests == []

    with pytest.raises(gw.ScopeInjectionRejected):
        gateway.call(object(), _call())  # type: ignore[arg-type]
    assert transport.requests == []


def test_request_body_has_no_scope_or_key_and_auth_is_header_only() -> None:
    gateway, transport, _ = _gateway(_response(200, _completion(content="ok")))
    declarations = _registry().declarations(scope=_scope())
    gateway.call(_scope(), _call(tools=declarations))

    request = transport.requests[0]
    payload = json.loads(request.body or b"{}")
    assert set(payload) == {"model", "messages", "max_tokens", "temperature", "stream", "tools"}
    for forbidden in ("workspace_id", "principal_id", "policy_snapshot_id", "scope"):
        assert forbidden not in payload
    assert CANARY.encode() not in (request.body or b"")
    assert request.headers["Authorization"] == f"Bearer {CANARY}"
    # registry 的声明按名字排序（与注册顺序无关），这里断言集合而非顺序。
    exported = {item["function"]["name"] for item in payload["tools"]}
    assert exported == {"site.read_counter", "site.idem_write", "site.error_probe"}
    for item in payload["tools"]:
        assert item["type"] == "function"
        assert item["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# 5) 回环 HTTP：真实传输适配 + 错误体 canary 脱敏
# ---------------------------------------------------------------------------
class _ProviderHandler(http.server.BaseHTTPRequestHandler):
    """本地假 provider；行为由 ``STATE`` 决定，记录每个请求。"""

    protocol_version = "HTTP/1.1"
    STATE: dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准签名
        return

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.STATE.setdefault("requests", []).append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )

    def _send(self, status_code: int, payload: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端提前断开
            return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._record()
        if self.path == "/healthz":
            self._send(200, b'{"status":"ok"}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._record()
        mode = self.STATE.get("mode", "ok")
        if mode == "ok":
            self._send(200, self.STATE["body"])
        elif mode == "500_canary":
            leaked = (
                b'{"error":"invalid api key ' + CANARY.encode() + b'","authorization":"Bearer '
                + CANARY.encode() + b'"}'
            )
            self._send(500, leaked)
        elif mode == "429":
            self._send(429, b'{"error":"rate limited"}')
        else:  # pragma: no cover - 仅供排查
            self._send(500, b'{"error":"unknown test mode"}')


@pytest.fixture()
def live_provider() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"requests": [], "mode": "ok", "body": b"{}"}
    _ProviderHandler.STATE = state
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_transport_round_trip_against_loopback_provider(
    live_provider: dict[str, Any],
) -> None:
    live_provider["body"] = _completion(
        finish_reason="tool_calls",
        tool_calls=[_tool_call("site.read_counter", {"path": "/idem/count"})],
    )
    config = _config(base_url=live_provider["base_url"], max_retries=0)
    gateway = gw.ModelGateway(config, transport=gw.HttpJsonTransport(), api_key=gw.Secret(CANARY))
    registry = _registry()

    result = gateway.call(_scope(), _call(tools=registry.declarations(scope=_scope())))

    assert result.finish_reason == "tool_calls"
    assert result.provider_request_ref.startswith("POST ")
    assert result.tool_proposals[0].arguments == {"path": "/idem/count"}
    # usage 也来自真实回包（缺 total 时按 input+output 补齐，成本缺省标记 unavailable）。
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 18
    assert result.usage.cost_microunits == 0
    assert result.usage.cost_source == "unavailable"

    seen = live_provider["requests"]
    assert len(seen) == 1
    assert seen[0]["method"] == "POST"
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["headers"]["Authorization"] == f"Bearer {CANARY}"
    assert CANARY.encode() not in seen[0]["body"]

    intent, validated = tools.prepare_intent(
        registry,
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=result.tool_proposals[0].call_ordinal,
        name=result.tool_proposals[0].name,
        arguments=result.tool_proposals[0].arguments,
    )
    assert intent.requires_approval is False
    assert validated.arguments_hash == tools.compute_arguments_hash({"path": "/idem/count"})

    health = gateway.health()
    assert health.ok is True
    assert health.status_code == 200
    assert live_provider["requests"][-1]["method"] == "GET"
    assert live_provider["requests"][-1]["path"] == "/healthz"


def test_provider_error_body_is_redacted_in_exception(live_provider: dict[str, Any]) -> None:
    live_provider["mode"] = "500_canary"
    config = _config(base_url=live_provider["base_url"], max_retries=0)
    gateway = gw.ModelGateway(config, transport=gw.HttpJsonTransport(), api_key=gw.Secret(CANARY))

    with pytest.raises(gw.GatewayError) as excinfo:
        gateway.call(_scope(), _call())

    error = excinfo.value
    assert error.code == gw.CODE_BY_CLASS[gw.ErrorClass.PROVIDER_5XX]
    assert error.status_code == 500
    rendered = str(error)
    assert CANARY not in rendered
    assert "***" in rendered


# ---------------------------------------------------------------------------
# 6) Registry：白名单、重复注册、版本、schema 漂移
# ---------------------------------------------------------------------------
def test_phase0_registry_is_a_three_tool_whitelist() -> None:
    registry = _registry()
    assert registry.names() == ("site.error_probe", "site.idem_write", "site.read_counter")
    assert registry.versions("site.read_counter") == ("1.0.0",)
    assert len(registry.specs) == 3
    declarations = registry.declarations(scope=_scope())
    assert {item.name for item in declarations} == set(registry.names())
    assert all(HEX64.match(item.schema_hash) for item in declarations)
    assert registry.specs[0].schema_hash == registry.specs[0].schema_hash


def test_unknown_tool_is_rejected_instead_of_returning_empty() -> None:
    registry = _registry()
    with pytest.raises(tools.UnknownToolError) as excinfo:
        registry.resolve(scope=_scope(), name="site.sql_exec")
    assert excinfo.value.code == "TOOL_UNKNOWN"
    assert "site.read_counter" in str(excinfo.value)  # 报错要给出白名单，而不是空结果

    with pytest.raises(tools.UnknownToolError):
        registry.declarations(scope=_scope(), names=["site.sql_exec"])


def test_duplicate_registration_is_rejected() -> None:
    registry = _registry()
    with pytest.raises(tools.DuplicateToolRegistrationError) as excinfo:
        registry.register(tools.READ_COUNTER)
    assert excinfo.value.code == "TOOL_DUPLICATE_REGISTRATION"

    drifted = replace(tools.READ_COUNTER, description="同一 name/version 的漂移定义")
    with pytest.raises(tools.DuplicateToolRegistrationError):
        registry.register(drifted)


def test_unknown_version_and_ambiguous_version() -> None:
    registry = _registry()
    with pytest.raises(tools.UnknownToolVersionError) as excinfo:
        registry.resolve(scope=_scope(), name="site.read_counter", version="9.9.9")
    assert excinfo.value.code == "TOOL_VERSION_UNKNOWN"

    registry.register(replace(tools.READ_COUNTER, version="1.1.0"))
    with pytest.raises(tools.AmbiguousToolVersionError) as ambiguous:
        registry.resolve(scope=_scope(), name="site.read_counter")
    assert ambiguous.value.code == "TOOL_VERSION_AMBIGUOUS"
    assert registry.resolve(scope=_scope(), name="site.read_counter", version="1.1.0").schema_hash


def test_schema_drift_changes_hash_and_declaration() -> None:
    spec = tools.READ_COUNTER
    drifted = replace(
        spec,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 8}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    assert spec.schema_hash != drifted.schema_hash
    assert spec.declaration().schema_hash != drifted.declaration().schema_hash
    assert spec.schema_hash == replace(spec).schema_hash


@pytest.mark.parametrize(
    "kwargs",
    [
        {"parameters": {"type": "object", "properties": {"a": {"type": "string"}}}},
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string", "pattern": "^x$"}},
                "required": ["a"],
                "additionalProperties": False,
            }
        },
        {"readonly": False},
        {"requires_approval": False, "side_effect": "write", "readonly": False},
        {"name": "Bad Name"},
        {"version": "1.0"},
        {"timeout_seconds": 0.01},
        {"max_output_bytes": 0},
    ],
    ids=[
        "object-without-additional-properties",
        "unsupported-keyword-pattern",
        "readonly-without-readonly-flag",
        "write-without-approval",
        "bad-name",
        "bad-version",
        "timeout-too-small",
        "max-output-too-small",
    ],
)
def test_tool_spec_rejects_unsafe_or_incomplete_declarations(kwargs: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "name": "site.probe",
        "version": "1.0.0",
        "description": "probe",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        "readonly": True,
        "side_effect": "none",
        "requires_approval": False,
    }
    if kwargs.get("side_effect") == "write":
        base["requires_approval"] = True
        kwargs = dict(kwargs)
        kwargs.pop("requires_approval", None)
        base["requires_approval"] = False
    params = {**base, **kwargs}
    with pytest.raises(tools.ToolSchemaInvalidError) as excinfo:
        tools.ToolSpec(**params)
    assert excinfo.value.code == "TOOL_SCHEMA_INVALID"


def test_declarations_drive_tool_approval_flags() -> None:
    registry = _registry()
    read = registry.resolve(scope=_scope(), name="site.read_counter")
    write = registry.resolve(scope=_scope(), name="site.idem_write")
    external = registry.resolve(scope=_scope(), name="site.error_probe")
    assert (read.readonly, read.requires_approval) == (True, False)
    assert (write.readonly, write.requires_approval) == (False, True)
    assert (external.readonly, external.requires_approval) == (False, True)


# ---------------------------------------------------------------------------
# 7) 参数校验：缺必填 / 未知字段 / 类型 / 范围 / 超大
# ---------------------------------------------------------------------------
def test_arguments_missing_required_field_is_rejected() -> None:
    with pytest.raises(tools.InvalidToolArgumentsError) as excinfo:
        tools.validate_arguments(tools.READ_COUNTER, {})
    errors = excinfo.value.errors
    assert [item.code for item in errors] == ["MISSING_REQUIRED"]
    assert errors[0].field_path == "path"


def test_arguments_unknown_field_is_rejected_explicitly() -> None:
    with pytest.raises(tools.InvalidToolArgumentsError) as excinfo:
        tools.validate_arguments(
            tools.READ_COUNTER, {"path": "/idem/count", "force": True}
        )
    assert [item.code for item in excinfo.value.errors] == ["UNKNOWN_FIELD"]
    assert excinfo.value.errors[0].field_path == "force"


def test_arguments_type_mismatch_including_bool_as_integer() -> None:
    with pytest.raises(tools.InvalidToolArgumentsError) as excinfo:
        tools.validate_arguments(
            tools.IDEM_WRITE,
            {"idempotency_key": 12345678, "body": {"text": "x"}},
        )
    assert "TYPE_MISMATCH" in [item.code for item in excinfo.value.errors]

    with pytest.raises(tools.InvalidToolArgumentsError):
        tools.validate_arguments(tools.ERROR_PROBE, {"fault": "http_500", "timeout_ms": True})


@pytest.mark.parametrize(
    ("spec", "arguments", "expected_code"),
    [
        (tools.ERROR_PROBE, {"fault": "drop_table"}, "ENUM_MISMATCH"),
        (tools.ERROR_PROBE, {"fault": "http_500", "timeout_ms": 0}, "RANGE_VIOLATION"),
        (tools.ERROR_PROBE, {"fault": "http_500", "timeout_ms": 30001}, "RANGE_VIOLATION"),
        (tools.IDEM_WRITE, {"idempotency_key": "short", "body": {"text": "x"}}, "LENGTH_VIOLATION"),
        (
            tools.IDEM_WRITE,
            {"idempotency_key": "k" * 9, "body": {"text": "x" * 513}},
            "LENGTH_VIOLATION",
        ),
    ],
    ids=["enum", "min-range", "max-range", "short-key", "long-body-text"],
)
def test_arguments_enum_and_bounds_are_enforced(
    spec: tools.ToolSpec, arguments: dict[str, Any], expected_code: str
) -> None:
    with pytest.raises(tools.InvalidToolArgumentsError) as excinfo:
        tools.validate_arguments(spec, arguments)
    assert expected_code in [item.code for item in excinfo.value.errors]


def test_arguments_oversized_payload_is_rejected() -> None:
    # 体积检查先于字段级校验：>16KiB 直接拒绝，不做「截断后再校验」。
    arguments = {"idempotency_key": "k" * 64, "body": {"text": "x" * 20000}}
    with pytest.raises(tools.ArgumentsTooLargeError) as excinfo:
        tools.validate_arguments(tools.IDEM_WRITE, arguments)
    assert excinfo.value.code == "TOOL_ARGUMENTS_TOO_LARGE"


def test_arguments_hash_is_order_independent_and_stable() -> None:
    validated_one = tools.validate_arguments(
        tools.IDEM_WRITE, {"idempotency_key": "key-12345678", "body": {"text": "hi"}}
    )
    validated_two = tools.validate_arguments(
        tools.IDEM_WRITE, {"body": {"text": "hi"}, "idempotency_key": "key-12345678"}
    )
    assert validated_one.arguments_hash == validated_two.arguments_hash
    assert validated_one.canonical == '{"body":{"text":"hi"},"idempotency_key":"key-12345678"}'
    assert HEX64.match(validated_one.arguments_hash)
    assert validated_one.size_bytes == len(validated_one.canonical.encode("utf-8"))


def test_model_supplied_scope_argument_is_rejected() -> None:
    with pytest.raises(tools.ToolScopeInjectionError) as excinfo:
        tools.validate_arguments(
            tools.READ_COUNTER, {"path": "/idem/count", "workspace_id": str(WS_B)}
        )
    assert excinfo.value.code == "TOOL_SCOPE_INJECTION"

    with pytest.raises(tools.ToolScopeInjectionError):
        tools.prepare_intent(
            _registry(),
            scope=object(),  # type: ignore[arg-type]
            resource_refs=(),
            response_artifact_id=ARTIFACT_ID,
            call_ordinal=0,
            name="site.read_counter",
            arguments={"path": "/idem/count"},
        )


# ---------------------------------------------------------------------------
# 8) 稳定 intent key（C-04 公式）+ 审批准入
# ---------------------------------------------------------------------------
def test_intent_key_is_stable_and_matches_c04_formula() -> None:
    registry = _registry()
    intent_one, _ = tools.prepare_intent(
        registry,
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=0,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
        run_id=RUN_ID,
    )
    intent_two, _ = tools.prepare_intent(
        registry,
        scope=_scope(role="member"),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=0,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
        run_id=RUN_ID,
    )
    assert intent_one.intent_key == intent_two.intent_key  # 同一语义 → 同一键
    assert intent_one.requires_approval is False
    assert intent_one.schema_version == "tool-intent/1"
    assert intent_one.canonicalization_version == "json-c14n/1"

    manual = gw.sha256_hex(
        gw.canonical_json(
            {
                "schema_version": "tool-intent/1",
                "run_id": str(RUN_ID),
                "response_artifact_id": str(ARTIFACT_ID),
                "call_ordinal": 0,
                "name": "site.read_counter",
                "tool_version": "1.0.0",
                "arguments_hash": tools.compute_arguments_hash({"path": "/idem/count"}),
                "scope_hash": tools.compute_scope_hash(_scope(), ()),
            }
        )
    )
    assert manual == intent_one.intent_key
    assert intent_one.recompute_key() == intent_one.intent_key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", None),
        ("response_artifact_id", uuid4()),
        ("call_ordinal", 3),
        ("name", "site.error_probe"),
        ("tool_version", "1.0.1"),
        ("arguments_hash", "0" * 64),
        ("scope_hash", "f" * 64),
        ("schema_version", "tool-intent/2"),
    ],
)
def test_intent_key_changes_with_every_bound_field(field: str, value: object) -> None:
    intent, _ = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=0,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
        run_id=RUN_ID,
    )
    mutated = replace(intent, **{field: value})
    assert mutated.recompute_key() != intent.intent_key


def test_intent_verify_detects_tampering() -> None:
    intent, _ = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=0,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
    )
    tampered = replace(intent, arguments_hash="1" * 64)
    with pytest.raises(tools.ToolContractError) as excinfo:
        tampered.verify()
    assert excinfo.value.code == "TOOL_CONTRACT_ERROR"


def test_prepare_intent_rejects_unknown_tool_and_bad_arguments() -> None:
    with pytest.raises(tools.UnknownToolError):
        tools.prepare_intent(
            _registry(),
            scope=_scope(),
            resource_refs=(),
            response_artifact_id=ARTIFACT_ID,
            call_ordinal=0,
            name="site.rm_rf",
            arguments={},
        )
    with pytest.raises(tools.InvalidToolArgumentsError):
        tools.prepare_intent(
            _registry(),
            scope=_scope(),
            resource_refs=(),
            response_artifact_id=ARTIFACT_ID,
            call_ordinal=0,
            name="site.read_counter",
            arguments={},
        )


def test_readonly_tool_needs_no_approval() -> None:
    intent, _ = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=0,
        name="site.read_counter",
        arguments={"path": "/idem/count"},
    )
    assert tools.assert_executable(intent) is None


def test_write_tool_is_blocked_without_approval() -> None:
    intent, _ = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=1,
        name="site.idem_write",
        arguments={"idempotency_key": "key-12345678", "body": {"text": "hi"}},
    )
    assert intent.requires_approval is True
    with pytest.raises(tools.ApprovalRequiredError) as excinfo:
        tools.assert_executable(intent)
    assert excinfo.value.code == "TOOL_APPROVAL_REQUIRED"

    approval = tools.ApprovalRef(
        approval_id=uuid4(), intent_key=intent.intent_key, status="approved"
    )
    assert tools.assert_executable(intent, approval=approval) == approval.approval_id


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        ("pending", "status"),
        ("rejected", "status"),
        ("consumed", "status"),
        ("revoked", "status"),
        ("expired", "expiry"),
        ("mismatch", "intent"),
    ],
    ids=["pending", "rejected", "consumed", "revoked", "expired", "wrong-intent"],
)
def test_approval_gate_rejects_stale_or_mismatched_approvals(status: str, kind: str) -> None:
    intent, _ = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=2,
        name="site.idem_write",
        arguments={"idempotency_key": "key-12345678", "body": {"text": "hi"}},
    )
    if kind == "intent":
        approval = tools.ApprovalRef(approval_id=uuid4(), intent_key="a" * 64, status="approved")
        expected: type[Exception] = tools.ApprovalMismatchError
    elif kind == "expiry":
        from datetime import UTC, datetime, timedelta

        approval = tools.ApprovalRef(
            approval_id=uuid4(),
            intent_key=intent.intent_key,
            status="approved",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        expected = tools.ApprovalExpiredError
    else:
        approval = tools.ApprovalRef(
            approval_id=uuid4(), intent_key=intent.intent_key, status=status  # type: ignore[arg-type]
        )
        expected = tools.ApprovalMismatchError
    with pytest.raises(expected):
        tools.assert_executable(intent, approval=approval)


def test_intent_arguments_are_exactly_what_was_validated() -> None:
    intent, validated = tools.prepare_intent(
        _registry(),
        scope=_scope(),
        resource_refs=(),
        response_artifact_id=ARTIFACT_ID,
        call_ordinal=4,
        name="site.error_probe",
        arguments={"fault": "http_429", "timeout_ms": 1000},
    )
    assert dict(intent.arguments) == {"fault": "http_429", "timeout_ms": 1000}
    assert intent.arguments_hash == validated.arguments_hash
    assert intent.workspace_id == WS_A
    assert intent.principal_id == PRINCIPAL_A
    assert intent.policy_snapshot_id == POLICY_A
