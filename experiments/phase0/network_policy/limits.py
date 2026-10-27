"""可观测的限流与并发闸门。

* 每个域名一个令牌桶（QPS + burst），每个域名一个并发上限；
* 计数器全部可在 ``snapshot()`` 里读到，测试和运行证据都按计数断言，不靠日志。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """经典令牌桶：``rate`` 个/秒，容量 ``burst``。"""

    def __init__(
        self, *, rate: float, burst: int, clock: Callable[[], float] | None = None
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock or time.monotonic
        self._tokens = float(burst)
        self._updated = self._clock()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill(self._clock())
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill(self._clock())
            return self._tokens


class ConcurrencyGate:
    """按 key（域名）计数的并发闸门；``release`` 必须与成功的 ``acquire`` 配对。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> bool:
        with self._lock:
            current = self._active.get(key, 0)
            if current >= self._limit:
                return False
            self._active[key] = current + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            current = self._active.get(key, 0)
            if current <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = current - 1

    def active(self, key: str) -> int:
        with self._lock:
            return self._active.get(key, 0)

    @property
    def limit(self) -> int:
        return self._limit


class Metrics:
    """进程内计数器（线程安全）；键形如 ``deny.domain_denied`` / ``allow.hop``。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def incr(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def get(self, key: str) -> int:
        with self._lock:
            return self._counters.get(key, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counters.items()))

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
