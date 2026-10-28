"""测试专用同步屏障 / 故障断点（P0-T04）。

为什么需要它
------------
任务卡与 ``docs/poc/05`` §3 都禁止「用长 sleep 猜时序」定位提交点。所有需要
「业务已提交，但下游还没拿到 receipt」这类窗口的实验，都必须能**确定地**问出
一句「被服务侧真的走到断点了吗」。本模块就是这句话的唯一实现：

* 只用 :class:`threading.Condition` 做**顺序**同步，不参与任何时延猜测；
* 每一次阻塞都有 deadline，到点转入 ``timeout`` 并放行，测试不会挂死；
* ``arm()`` 一次只对应**一次**被服务侧命中；重复注入需要重新 ``arm()``。

状态机（每个 name 独立）::

    idle --arm()--> armed --hit()--> hit --release()--> released
                                     \\--deadline--> timeout

名称既覆盖 6 类注入故障（:data:`FAULT_NAMES`），也接受任意提交点断点名
（:data:`BREAKPOINT_NAMES`，见 ``docs/poc/06`` §5），因此 T05/T06/T21 可以复用
同一个屏障表达「receipt 前后」「checkpoint 前后」等窗口，而不是各造一套计数器。

本模块是**测试 fixture**，不是生产 hook：生产代码不得 import 它。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ARMED",
    "BREAKPOINT_NAMES",
    "DEFAULT_TIMEOUT_S",
    "FAULT_NAMES",
    "HIT",
    "IDLE",
    "MAX_TIMEOUT_S",
    "RELEASED",
    "TIMEOUT",
    "FaultBarrier",
    "FaultError",
    "FaultState",
]

DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 60.0
MAX_NAME_LENGTH = 64

IDLE = "idle"
ARMED = "armed"
HIT = "hit"
RELEASED = "released"
TIMEOUT = "timeout"

_TERMINAL_STATES = frozenset({RELEASED, TIMEOUT})

#: 六类注入故障（任务卡「注入」一行 + TDD08 §5 的提交点语义）。
FAULT_NAMES: tuple[str, ...] = (
    "delay",  # 延迟：响应前按参数等待
    "timeout",  # 超时：挂住连接，客户端先超时，服务端不返回响应
    "server_error",  # 5xx：错误码响应
    "truncate",  # 断流：Content-Length 未写满即正常收尾
    "disconnect_after_success",  # 业务副作用已成功，再主动断连接、无响应
    "login_required",  # 人工登录页/登录墙
)

#: ``docs/poc/06`` §5 的提交点断点名（实验 fixture 协议名，不是生产 hook）。
BREAKPOINT_NAMES: tuple[str, ...] = (
    "after_business_commit",
    "after_publish_before_mark",
    "after_job_claim",
    "after_effect_before_receipt",
    "after_receipt_before_checkpoint",
    "before_activation_commit",
)


class FaultError(ValueError):
    """屏障用法错误：名称非法、重复 arm、timeout 越界。"""


def _check_name(name: object) -> str:
    if not isinstance(name, str):
        raise FaultError(f"屏障名称必须是 str，收到 {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        raise FaultError("屏障名称不得为空")
    if len(stripped) > MAX_NAME_LENGTH:
        raise FaultError(f"屏障名称过长（>{MAX_NAME_LENGTH} 字符）：{stripped[:MAX_NAME_LENGTH]}…")
    if not all(char.isprintable() and not char.isspace() for char in stripped):
        raise FaultError(f"屏障名称只允许可打印、非空白字符：{stripped!r}")
    return stripped


def _clamp_timeout(timeout: float | None, default: float) -> float:
    value = default if timeout is None else float(timeout)
    if value <= 0:
        raise FaultError("timeout 必须为正数")
    return min(value, MAX_TIMEOUT_S)


@dataclass
class FaultState:
    """单个屏障的对外快照（不含不可序列化的时间戳）。"""

    name: str
    state: str = IDLE
    timeout_s: float = DEFAULT_TIMEOUT_S
    params: dict[str, Any] = field(default_factory=dict)
    hits: int = 0
    releases: int = 0
    timeouts: int = 0
    armed_at: float | None = None
    hit_at: float | None = None
    released_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        observed = {"hit_to_release_s": None}
        if self.hit_at is not None and self.released_at is not None:
            observed["hit_to_release_s"] = round(self.released_at - self.hit_at, 3)
        return {
            "name": self.name,
            "state": self.state,
            "timeout_s": round(self.timeout_s, 3),
            "params": dict(self.params),
            "hits": self.hits,
            "releases": self.releases,
            "timeouts": self.timeouts,
            **observed,
        }


class FaultBarrier:
    """按名称管理的同步屏障；线程安全，可被 HTTP 处理器与测试进程同时使用。"""

    def __init__(self, *, default_timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._states: dict[str, FaultState] = {}
        self._default_timeout = _clamp_timeout(default_timeout, DEFAULT_TIMEOUT_S)

    # -- 元信息 -----------------------------------------------------------
    @property
    def default_timeout(self) -> float:
        return self._default_timeout

    @staticmethod
    def fault_names() -> frozenset[str]:
        return frozenset(FAULT_NAMES)

    @staticmethod
    def breakpoint_names() -> frozenset[str]:
        return frozenset(BREAKPOINT_NAMES)

    def state(self, name: str) -> str:
        key = _check_name(name)
        with self._lock:
            state = self._states.get(key)
            return state.state if state else IDLE

    def params_of(self, name: str) -> dict[str, Any]:
        key = _check_name(name)
        with self._lock:
            state = self._states.get(key)
            return dict(state.params) if state else {}

    def armed_names(self) -> list[str]:
        """仍处于 armed/hit（即可能继续阻塞被服务侧）的屏障名，视觉 teardown 证据。"""
        with self._lock:
            return sorted(
                name for name, state in self._states.items() if state.state in (ARMED, HIT)
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "default_timeout_s": round(self._default_timeout, 3),
                "fault_names": list(FAULT_NAMES),
                "breakpoint_names": list(BREAKPOINT_NAMES),
                "barriers": {name: state.as_dict() for name, state in self._states.items()},
            }

    # -- 测试侧：布置/放行/等待命中 ---------------------------------------
    def arm(
        self,
        name: str,
        *,
        timeout: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> FaultState:
        """布置一次故障/断点。

        ``timeout`` 是被服务侧命中后允许阻塞的最长时间（到点自动放行）；
        ``params`` 是故障语义参数（如 ``delay_seconds``、``status``）。
        """
        key = _check_name(name)
        if params is not None and not isinstance(params, dict):
            raise FaultError(f"params 必须是 dict，收到 {type(params).__name__}")
        timeout_s = _clamp_timeout(timeout, self._default_timeout)
        with self._condition:
            state = self._states.setdefault(key, FaultState(name=key))
            if state.state == HIT:
                raise FaultError(f"屏障 {key!r} 正被服务侧阻塞（state=hit），不能重复 arm")
            state.state = ARMED
            state.timeout_s = timeout_s
            state.params = dict(params or {})
            state.armed_at = time.monotonic()
            state.hit_at = None
            state.released_at = None
            self._condition.notify_all()
            return state

    def release(self, name: str) -> bool:
        """放行一次已布置（或已阻塞）的屏障；返回是否真的改变了状态。"""
        key = _check_name(name)
        with self._condition:
            state = self._states.get(key)
            if state is None or state.state not in (ARMED, HIT):
                return False
            state.state = RELEASED
            state.releases += 1
            state.released_at = time.monotonic()
            self._condition.notify_all()
            return True

    def wait_armed(self, name: str, timeout: float | None = None) -> bool:
        """等待被服务侧**真的走到**该断点（state 进入 hit/released/timeout）。

        这是「不许 sleep 碰时序」的核心：测试不猜时间，只等这个事实。
        未 ``arm()`` 过的名称不会命中，因此立即返回 ``False``。
        """
        key = _check_name(name)
        deadline = time.monotonic() + _clamp_timeout(timeout, self._default_timeout)
        with self._condition:
            state = self._states.get(key)
            if state is None:
                return False
            while state.state == ARMED:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return state.state in {HIT, RELEASED, TIMEOUT}

    def hit(self, name: str, *, timeout: float | None = None) -> str:
        """被服务侧调用：到达断点并阻塞，直到测试 ``release()`` 或 deadline 到点。

        返回 ``"idle"``（未布置，直接放行）、``"hit"``（已被另一次命中占用）、
        ``"released"`` 或 ``"timeout"``。
        """
        key = _check_name(name)
        with self._condition:
            state = self._states.get(key)
            if state is None or state.state == IDLE:
                return IDLE
            if state.state != ARMED:
                return state.state
            state.state = HIT
            state.hits += 1
            state.hit_at = time.monotonic()
            self._condition.notify_all()
            deadline = state.hit_at + _clamp_timeout(timeout, state.timeout_s)
            while state.state == HIT:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    state.state = TIMEOUT
                    state.timeouts += 1
                    state.released_at = time.monotonic()
                    self._condition.notify_all()
                    return TIMEOUT
                self._condition.wait(remaining)
            return state.state

    # -- 自动清理 ---------------------------------------------------------
    def release_all(self) -> int:
        """放行所有 armed/hit 屏障（teardown 与 server.stop() 调用）。"""
        with self._condition:
            released = 0
            for state in self._states.values():
                if state.state in (ARMED, HIT):
                    state.state = RELEASED
                    state.releases += 1
                    state.released_at = time.monotonic()
                    released += 1
            self._condition.notify_all()
            return released

    def reset(self) -> None:
        """清空全部屏障状态（跨用例复用同一 barrier 时使用）。"""
        with self._condition:
            self.release_all()
            self._states.clear()
            self._condition.notify_all()

    @contextmanager
    def injection(
        self,
        name: str,
        *,
        timeout: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[FaultState]:
        """``arm`` 的上下文管理器形式：退出时无论如何都放行（自动清理）。"""
        state = self.arm(name, timeout=timeout, params=params)
        try:
            yield state
        finally:
            self.release(name)


def is_terminal(state: str) -> bool:
    """判断状态是否已终止（released/timeout）。"""
    return state in _TERMINAL_STATES
