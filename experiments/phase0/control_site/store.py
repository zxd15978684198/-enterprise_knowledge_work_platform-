"""进程内计数器与请求到达日志（控制站专用）。

硬约束（P0-T04 任务卡）：

* 只做测试用计数器/故障节点，**绝不写业务 Run/Job/知识库事实表**；
  本模块不 import psycopg / sqlalchemy / redis，也不打开任何文件句柄。
* 状态全部驻留内存，进程退出即消失 —— 所以「10 次 kill/重建无遗留数据」
  不是靠清理脚本保证，而是结构上不可能残留。
* 到达日志只记录**是否出现过敏感头**（名字），绝不记录 Cookie/Authorization
  的值；这是 F16（secret/PII 不得进入证据）在 fixture 侧的落地。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

Surface = Literal["idem", "nonidem"]

KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""控制站 key 的白名单字符集：进证据前不夹带空白/控制字符/超长内容。"""

SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)
"""只记录名字、不记录值的敏感头（覆盖 F16 的 canary 场景）。"""

SESSION_COOKIE = "phase0_manual_session"


class InvalidKeyError(ValueError):
    """key 不满足控制站白名单（超长/空/含非法字符）。"""


def validate_key(key: object) -> str:
    """校验并规整控制站 key；非法即抛 :class:`InvalidKeyError`。"""
    if not isinstance(key, str) or not key:
        raise InvalidKeyError("key 必须是非空字符串")
    if not KEY_PATTERN.match(key):
        raise InvalidKeyError(f"key 只允许 [A-Za-z0-9._:-]{{1,128}}，收到 {key!r}")
    return key


@dataclass
class Counters:
    """单个 key 在某个端点面上的计数。"""

    applied: int = 0
    attempts: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"applied": self.applied, "attempts": self.attempts}


@dataclass
class Arrival:
    """一次到达记录：证明请求真的抵达了目的端点（SSRF 负例证据用）。"""

    seq: int
    method: str
    path: str
    query: dict[str, str]
    received_at: float
    status: int | None = None
    key: str | None = None
    fault_gate: str | None = None
    aborted: bool = False
    credential_headers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "method": self.method,
            "path": self.path,
            "query": dict(self.query),
            "received_at": round(self.received_at, 3),
            "status": self.status,
            "key": self.key,
            "fault_gate": self.fault_gate,
            "aborted": self.aborted,
            "credential_headers": list(self.credential_headers),
        }


class ControlStore:
    """控制站的计数与到达日志；所有写操作在单把锁内完成。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._arrivals: list[Arrival] = []
        self._idem: dict[str, Counters] = {}
        self._nonidem: dict[str, Counters] = {}
        self._last: dict[tuple[str, str], dict[str, Any]] = {}
        self._sessions: set[str] = set()
        self._seq = 0
        self._started_monotonic = time.monotonic()

    # -- 生命周期观测 -----------------------------------------------------
    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self._started_monotonic

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._seq

    # -- 到达日志 ---------------------------------------------------------
    def begin_request(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Any,
    ) -> Arrival:
        """记录一次请求到达，返回可继续填充的 :class:`Arrival`。"""
        present = {str(name).lower() for name in headers}
        credential_headers = sorted(present & SENSITIVE_HEADERS)
        with self._lock:
            self._seq += 1
            record = Arrival(
                seq=self._seq,
                method=method,
                path=path,
                query=dict(query),
                received_at=time.monotonic(),
                credential_headers=credential_headers,
            )
            self._arrivals.append(record)
            self._condition.notify_all()
            return record

    def finish(
        self,
        record: Arrival,
        *,
        status: int | None = None,
        key: str | None = None,
        fault_gate: str | None = None,
        aborted: bool = False,
    ) -> None:
        """补齐一次到达的最终状态（可安全重复调用）。"""
        with self._lock:
            record.status = status if status is not None else record.status
            record.key = key or record.key
            record.fault_gate = fault_gate or record.fault_gate
            record.aborted = record.aborted or aborted
            self._condition.notify_all()

    @property
    def completed_count(self) -> int:
        """已经补齐最终状态（``finish`` 过）的到达条数。"""
        with self._lock:
            return sum(1 for item in self._arrivals if item.status is not None)

    def wait_for_completed(
        self, count: int, *, timeout: float = 5.0
    ) -> list[dict[str, Any]]:
        """条件变量式等待「已完成（带最终状态）」的到达达到 ``count`` 条。

        与 :meth:`wait_for_count` 的区别：到达记录在请求**进站**时就落一条，
        此时 ``status`` 还是 ``None``。任何依赖最终状态（例如 200/aborted）
        的断言都必须等这个，否则会读到半成品记录——这正是"不用 sleep 猜时序"
        在状态维度的含义。
        """
        if count < 0:
            raise ValueError("count 不得为负")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while sum(1 for item in self._arrivals if item.status is not None) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    done = sum(1 for item in self._arrivals if item.status is not None)
                    raise TimeoutError(f"等待 {count} 条已完成到达超时：实际 {done} 条")
                self._condition.wait(remaining)
            return [item.as_dict() for item in self._arrivals if item.status is not None]

    def arrivals(self, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """按 seq 游标读取到达记录（``after`` 之后、最多 ``limit`` 条）。"""
        with self._lock:
            items = [item.as_dict() for item in self._arrivals if item.seq > after]
            return {
                "after": after,
                "next": items[-1]["seq"] if items else after,
                "total": len(self._arrivals),
                "items": items[:limit],
            }

    def arrivals_for(self, path: str) -> list[dict[str, Any]]:
        """按路径过滤到达记录（「最终到达端点」证据的最小查询面）。"""
        with self._lock:
            return [item.as_dict() for item in self._arrivals if item.path == path]

    def wait_for_count(self, count: int, *, timeout: float = 5.0) -> list[dict[str, Any]]:
        """条件变量式等待到达次数达到 ``count``（不用 sleep 碰时序）。

        超时抛 :class:`TimeoutError` 并带上当前计数，便于证据里记「实际到达几次」。
        """
        if count < 0:
            raise ValueError("count 不得为负")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while len(self._arrivals) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"等待 {count} 次到达超时：实际 {len(self._arrivals)} 次"
                    )
                self._condition.wait(remaining)
            return [item.as_dict() for item in self._arrivals]

    # -- 计数端点 ---------------------------------------------------------
    def idempotent_write(self, key: str) -> dict[str, Any]:
        """幂等写：同一 key 只有第一次生效，重复调用只累加 attempts。"""
        key = validate_key(key)
        with self._lock:
            counters = self._idem.setdefault(key, Counters())
            counters.attempts += 1
            applied = counters.applied == 0
            if applied:
                counters.applied = 1
            self._last[("idem", key)] = {
                "surface": "idem",
                "endpoint": "/idem/write",
                "status": 200,
                "applied": applied,
                "at": round(time.monotonic(), 3),
            }
            return {
                "surface": "idem",
                "key": key,
                "applied": applied,
                "applied_calls": counters.applied,
                "attempts": counters.attempts,
                "deduplicated": not applied,
            }

    def non_idempotent_write(self, key: str) -> dict[str, Any]:
        """非幂等写：每次调用都让计数 +1（用于 F3「已成功但未留 receipt」）。"""
        key = validate_key(key)
        with self._lock:
            counters = self._nonidem.setdefault(key, Counters())
            counters.applied += 1
            counters.attempts += 1
            self._last[("nonidem", key)] = {
                "surface": "nonidem",
                "endpoint": "/nonidem/write",
                "status": 200,
                "applied": True,
                "at": round(time.monotonic(), 3),
            }
            return {
                "surface": "nonidem",
                "key": key,
                "applied": True,
                "applied_calls": counters.applied,
                "attempts": counters.attempts,
                "deduplicated": False,
            }

    def counts(self, surface: Surface, key: str) -> dict[str, Any]:
        """只读查单：返回该 key 的调用次数与最后状态。"""
        key = validate_key(key)
        if surface not in ("idem", "nonidem"):
            raise InvalidKeyError(f"未知 surface: {surface!r}")
        with self._lock:
            buckets = self._idem if surface == "idem" else self._nonidem
            counters = buckets.get(key, Counters())
            last = self._last.get((surface, key))
            return {
                "surface": surface,
                "key": key,
                "applied_calls": counters.applied,
                "attempts": counters.attempts,
                "deduplicated": counters.attempts > counters.applied,
                "last_status": dict(last) if last else None,
            }

    # -- 人工登录会话 -----------------------------------------------------
    def create_session(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions.add(token)
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return token in self._sessions

    def drop_sessions(self) -> int:
        with self._lock:
            dropped = len(self._sessions)
            self._sessions.clear()
            return dropped

    # -- 汇总与复位 -------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": self._seq,
                "uptime_s": round(self.uptime_s, 3),
                "idem": {key: value.as_dict() for key, value in self._idem.items()},
                "nonidem": {key: value.as_dict() for key, value in self._nonidem.items()},
                "sessions": len(self._sessions),
                "arrivals": len(self._arrivals),
            }

    def reset(self) -> dict[str, Any]:
        """清空计数/日志/会话（仅内存；跨进程重建本来就是干净状态）。

        注意：``self._lock`` 是**不可重入**的 ``threading.Lock``，复位路径
        不能在持锁时再调用 ``snapshot()``（会自死锁并把处理线程永久挂住，
        拖死 ``server_close()`` 的线程回收）。因此这里手写 after 快照。
        """
        with self._lock:
            cleared = {
                "requests": self._seq,
                "idem_keys": len(self._idem),
                "nonidem_keys": len(self._nonidem),
                "sessions": len(self._sessions),
            }
            self._arrivals.clear()
            self._idem.clear()
            self._nonidem.clear()
            self._last.clear()
            self._sessions.clear()
            self._seq = 0
            self._started_monotonic = time.monotonic()
            after = {
                "requests": self._seq,
                "uptime_s": round(self.uptime_s, 3),
                "idem": {},
                "nonidem": {},
                "sessions": 0,
                "arrivals": 0,
            }
        return {"cleared": cleared, "after": after}
