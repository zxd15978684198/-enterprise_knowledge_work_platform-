"""P0-T21 安全 trace：只记录**脱敏字段**，prompt / 密钥 / cookie / 参数原文都不上报。

为什么需要它
------------
``docs/tdd/02_Agent_Harness.md`` §6 与 ``AGENTS.md`` 的硬约束：
「禁止密钥、凭据、cookie、Authorization、个人敏感信息进入普通日志、trace、Graph State
或模型上下文」。而 Harness 恰恰是**最容易顺手把整段 prompt 塞进 trace** 的地方：
节点名、模型响应、工具参数、错误串都在手边。

所以这里用 allowlist 而不是 denylist：

* :meth:`SafeTrace.record` 只接受 :data:`ALLOWED_FIELDS` 里的字段名，其它字段名直接
  抛 :class:`TraceFieldRejected`（写错了就当场失败，而不是"先记下来再过滤"）；
* 值只允许标量/小整数计数；字符串统一截断并做 canary 扫描，命中即替换 ``<redacted>``；
* 错误只记 ``error_class`` + ``error_code``，**不记 message**（message 里常带 URL/密钥）；
* :meth:`SafeTrace.to_state_fragment` 只输出计数与最后状态，这是唯一允许进入 Graph
  State 的形状——canary 测试同时扫描 trace 序列化结果与这份 fragment。

本模块不依赖 LangGraph、不依赖 DB：它是纯内存记录器，可被任何 Harness 节点复用。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

__all__ = [
    "ALLOWED_FIELDS",
    "MAX_STRING_LENGTH",
    "REDACTED",
    "SafeTrace",
    "TraceEvent",
    "TraceFieldRejected",
    "TraceOverflowError",
    "contains_any",
    "redact_text",
]

REDACTED: Final = "<redacted>"
MAX_STRING_LENGTH: Final = 64
MAX_EVENTS: Final = 2000

#: **唯一**允许出现在 trace 里的字段名。新增字段必须先改这里并补 canary 测试。
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "node",
        "action",
        "status",
        "attempt",
        "tool",
        "tool_version",
        "intent_key_prefix",
        "request_digest_prefix",
        "error_class",
        "error_code",
        "duration_ms",
        "counts",
        "note_code",
    }
)

#: 明显不该出现的字段名（做二次拦截，给出更可读的错误）。
_FORBIDDEN_HINTS: Final[tuple[str, ...]] = (
    "prompt",
    "content",
    "message",
    "argument",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "body",
    "header",
    "base_url",
    "dsn",
    "email",
)

_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9+/=_-]{16,}")


class TraceFieldRejected(ValueError):
    """trace 字段名/值不在 allowlist 内：宁可失败，也不把敏感值写进去。"""

    code = "TRACE_FIELD_REJECTED"


class TraceOverflowError(RuntimeError):
    """事件数超上限：说明某个循环在刷 trace，先停下来而不是无限增长。"""

    code = "TRACE_OVERFLOW"


def contains_any(haystack: str, needles: Iterable[str]) -> bool:
    """子串命中检测（canary 断言与脱敏共用同一实现）。"""

    return any(needle and needle in haystack for needle in needles)


def redact_text(text: str, *, secrets: Iterable[str] = (), canaries: Iterable[str] = ()) -> str:
    """把文本里的秘密/canary 子串替换成 ``<redacted>``，并截断长度。

    这是**兜底**，不是主要防线：主要防线是 allowlist（根本不让原文进来）。
    """

    if not isinstance(text, str):
        raise TraceFieldRejected(f"redact_text 需要 str，收到 {type(text).__name__}")
    cleaned = text
    for needle in list(secrets) + list(canaries):
        if needle:
            cleaned = cleaned.replace(needle, REDACTED)
    cleaned = _TOKEN_RE.sub(REDACTED, cleaned) if len(cleaned) > MAX_STRING_LENGTH else cleaned
    return cleaned[:MAX_STRING_LENGTH]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """一条 trace 事件；字段集合静态可审（与 :data:`ALLOWED_FIELDS` 一致）。"""

    seq: int
    at: str
    run_id: str
    node: str
    action: str
    status: str
    attempt: int = 0
    tool: str | None = None
    tool_version: str | None = None
    intent_key_prefix: str | None = None
    request_digest_prefix: str | None = None
    error_class: str | None = None
    error_code: str | None = None
    duration_ms: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)
    note_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "run_id": self.run_id,
            "node": self.node,
            "action": self.action,
            "status": self.status,
            "attempt": self.attempt,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "intent_key_prefix": self.intent_key_prefix,
            "request_digest_prefix": self.request_digest_prefix,
            "error_class": self.error_class,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
            "counts": dict(self.counts),
            "note_code": self.note_code,
        }


class SafeTrace:
    """内存 trace 记录器：allowlist + 脱敏 + 可审计的 Graph State 片段。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        secrets: Iterable[str] = (),
        canaries: Iterable[str] = (),
        max_events: int = MAX_EVENTS,
    ) -> None:
        if not isinstance(run_id, UUID):
            raise TraceFieldRejected("run_id 必须是 UUID（避免调用方把任意串塞进来）")
        self.run_id = run_id
        self._secrets = tuple(item for item in secrets if item)
        self._canaries = tuple(item for item in canaries if item)
        self._max_events = max_events
        self._events: list[TraceEvent] = []
        self._counts: dict[str, int] = {}

    # -- 写入 -------------------------------------------------------------
    def record(self, *, node: str, action: str, status: str = "ok", **fields: Any) -> TraceEvent:
        """记录一条事件；字段名/类型不在 allowlist 内立即失败。"""

        if len(self._events) >= self._max_events:
            raise TraceOverflowError(f"trace 事件数超过上限 {self._max_events}")
        rejected = [name for name in fields if name not in ALLOWED_FIELDS]
        if rejected:
            raise TraceFieldRejected(
                f"trace 字段 {rejected} 不在 allowlist 内（禁止把 prompt/密钥/参数原文写进 trace）"
            )
        for name in fields:
            lowered = name.lower()
            if any(hint in lowered for hint in _FORBIDDEN_HINTS):  # pragma: no cover - 双保险
                raise TraceFieldRejected(f"trace 字段 {name!r} 命中禁止词")
        counts = self._safe_counts(fields.pop("counts", None))
        event = TraceEvent(
            seq=len(self._events) + 1,
            at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            run_id=str(self.run_id),
            node=self._safe_str(node, name="node"),
            action=self._safe_str(action, name="action"),
            status=self._safe_str(status, name="status"),
            attempt=int(fields.pop("attempt", 0) or 0),
            tool=self._maybe(fields.pop("tool", None), name="tool"),
            tool_version=self._maybe(fields.pop("tool_version", None), name="tool_version"),
            intent_key_prefix=self._prefix(fields.pop("intent_key_prefix", None)),
            request_digest_prefix=self._prefix(fields.pop("request_digest_prefix", None)),
            error_class=self._maybe(fields.pop("error_class", None), name="error_class"),
            error_code=self._maybe(fields.pop("error_code", None), name="error_code"),
            duration_ms=int(fields.pop("duration_ms", 0) or 0),
            counts=counts,
            note_code=self._maybe(fields.pop("note_code", None), name="note_code"),
        )
        self._events.append(event)
        for key, value in counts.items():
            self._counts[key] = self._counts.get(key, 0) + value
        return event

    def record_error(self, *, node: str, action: str, error: BaseException) -> TraceEvent:
        """只记异常**类型名**与（若有）稳定 code，绝不记 message。"""

        code = getattr(error, "code", None)
        return self.record(
            node=node,
            action=action,
            status="error",
            error_class=type(error).__name__,
            error_code=code if isinstance(code, str) and code.isupper() else None,
        )

    # -- 读取 -------------------------------------------------------------
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def as_list(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._events]

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def to_state_fragment(self) -> dict[str, Any]:
        """唯一允许进入 Graph State 的形状：事件数 + 计数 + 最后状态。"""

        last = self._events[-1] if self._events else None
        fragment: dict[str, Any] = {
            "trace_events": len(self._events),
            "trace_counts": dict(self._counts),
            "last_node": last.node if last else None,
            "last_status": last.status if last else None,
            "trace_digest": self.digest(),
        }
        return fragment

    def digest(self) -> str:
        payload = "\n".join(event.at + event.action + event.status for event in self._events)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- 自检 -------------------------------------------------------------
    def scan(self, needles: Iterable[str]) -> list[str]:
        """扫描序列化后的全部字段，返回命中的 needle 列表（canary 断言用）。"""

        blob = repr(self.as_list()) + repr(self.to_state_fragment())
        return [needle for needle in needles if needle and needle in blob]

    # -- 内部 -------------------------------------------------------------
    def _safe_str(self, value: Any, *, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise TraceFieldRejected(f"trace 字段 {name} 必须是非空 str，收到 {value!r}")
        if len(value) > MAX_STRING_LENGTH:
            raise TraceFieldRejected(f"trace 字段 {name} 超过 {MAX_STRING_LENGTH} 字符")
        return redact_text(value, secrets=self._secrets, canaries=self._canaries)

    def _maybe(self, value: Any, *, name: str) -> str | None:
        if value is None:
            return None
        return self._safe_str(value, name=name)

    def _prefix(self, value: Any) -> str | None:
        """哈希只允许记前缀（12 位），避免把完整 hash 当指纹外泄。"""

        if value is None:
            return None
        text = self._safe_str(value, name="hash")
        return text[:12]

    def _safe_counts(self, value: Any) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TraceFieldRejected("counts 必须是映射")
        cleaned: dict[str, int] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 24:
                raise TraceFieldRejected(f"counts 的 key 非法: {key!r}")
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise TraceFieldRejected(f"counts[{key}] 必须是非负 int")
            cleaned[key] = item
        return cleaned


def safe_event_stream(events: Sequence[TraceEvent]) -> list[dict[str, Any]]:
    """把事件序列规约成可直接落盘/上报的形状（保留给证据与 T30 复用）。"""

    return [event.as_dict() for event in events]
