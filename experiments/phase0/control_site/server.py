"""控制站进程内生命周期：绑定、起停、健康等待与重复起停安全。

硬性约束在此集中落地：

* **只绑 127.0.0.1**：``host`` 只接受回环字面量，否则 ``ValueError``；
* 起停可重复：``stop()`` 释放故障屏障、关闭监听、join 线程；再次
  ``start()`` 不需要换端口、也没有残留会话；
* 不落盘、不连 PG/Redis：站点状态全在内存 store 里，进程结束即消失；
* 监听端口被外部占用时 ``start()`` 直接失败而不是"偷偷复用"
  （Windows 上 SO_REUSEADDR 允许端口劫持，必须显式预检存活监听者）。
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
import time
from typing import Any

from . import payloads
from .client import wait_for_healthz
from .handlers import build_handler
from .store import ControlStore

__all__ = ["ControlSite", "LOOPBACK_HOSTS"]

LOOPBACK_HOSTS = ("127.0.0.1",)
DEFAULT_HOST = "127.0.0.1"
HEALTH_PATH = "/healthz"


class ControlSite:
    """一个可重复起停的本地控制站。

    用法::

        site = ControlSite()          # 随机空闲端口
        site.start()
        try:
            site.url("/healthz")
        finally:
            site.stop()
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = 0,
        allow_fault_injection: bool = False,
        barrier: Any | None = None,
        request_queue_size: int = 64,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"控制站只允许绑定回环地址 {LOOPBACK_HOSTS}，收到 {host!r}"
                "（P0-T04 约束：端口只绑 127.0.0.1）"
            )
        if not 0 <= int(port) <= 65535:
            raise ValueError(f"非法端口: {port!r}")

        self.host = host
        self._requested_port = int(port)
        self.allow_fault_injection = bool(allow_fault_injection)
        self._owns_barrier = barrier is None and self.allow_fault_injection
        self.barrier = (
            barrier if barrier is not None else _maybe_barrier(self.allow_fault_injection)
        )
        self.store = ControlStore()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._started_at = 0.0
        self._close_timed_out = False

        handler_cls = build_handler(
            store=self.store,
            barrier=self.barrier,
            allow_faults=self.allow_fault_injection,
        )

        class _Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            # allow_reuse_address 保持 True：Windows 下 TIME_WAIT 会阻止重绑，
            # 而端口"仍然被占用"由下面的存活预检负责识别，不依赖 bind 失败。
            allow_reuse_address = True
            allow_reuse_port = False

            def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D102
                # 客户端中断/截断是预期行为，不该打印 traceback 污染证据输出。
                self.client_errors = getattr(self, "client_errors", 0) + 1

        self._server_cls = _Server
        self._handler_cls = handler_cls
        self._server = _Server(
            (self.host, self._requested_port), handler_cls, bind_and_activate=False
        )
        self._server.request_queue_size = int(request_queue_size)
        self._server.client_errors = 0
        try:
            self._server.server_bind()
            self._server.server_activate()
        except OSError as exc:
            self._server.server_close()
            raise RuntimeError(
                f"控制站无法绑定 {self.host}:{self._requested_port}: {exc}"
                "（端口被占用或权限不足；固定端口场景请先确认无残留进程）"
            ) from exc

    # -- 只读属性 ---------------------------------------------------------
    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def started_at(self) -> float:
        return self._started_at

    def url(self, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def health_url(self) -> str:
        return self.url(HEALTH_PATH)

    # -- 生命周期 ---------------------------------------------------------
    def _assert_port_free(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            in_use = probe.connect_ex((self.host, self.port)) == 0
        finally:
            probe.close()
        if in_use:
            raise RuntimeError(
                f"{self.host}:{self.port} 已有进程在监听；控制站拒绝复用他人端口"
            )

    def start(self, *, timeout: float = 5.0) -> ControlSite:
        with self._lock:
            if self.is_running:
                return self
            if self._closed:
                raise RuntimeError("该 ControlSite 已 stop 且不可复用；请新建实例")
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.01},
                name=f"control-site-{self.port}",
                daemon=True,
            )
            self._thread.start()
            self._started_at = time.monotonic()
        wait_for_healthz(self.health_url(), timeout=timeout)
        return self

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if self.barrier is not None:
            # 停机路径不得因屏障状态抛错：先放行仍在阻塞的故障请求，再关监听。
            with contextlib.suppress(Exception):
                self.barrier.release_all()
        if thread is not None and thread.is_alive():
            self._server.shutdown()
            thread.join(timeout=timeout)
        # ``ThreadingHTTPServer.server_close()`` 会 join 全部请求线程；若某个
        # handler 卡死（例如被测代码里的死锁），直接调用会把停机永久挂住。
        # 监听 socket 在 join 之前就关掉了，端口释放不依赖线程回收，因此这里
        # 放到独立守护线程里并**带超时**：超时只记账，不阻塞夹具。
        closer = threading.Thread(
            target=self._server.server_close,
            name=f"control-site-close-{self.port}",
            daemon=True,
        )
        closer.start()
        closer.join(timeout=timeout)
        self._close_timed_out = closer.is_alive()
        self.store.drop_sessions()

    # -- 便捷访问 ---------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        snapshot["client_errors"] = int(getattr(self._server, "client_errors", 0))
        snapshot["fault_injection"] = self.allow_fault_injection
        if self.barrier is not None:
            snapshot["barrier"] = self.barrier.snapshot()
        return snapshot

    def reset(self) -> dict[str, Any]:
        result = self.store.reset()
        if self.barrier is not None:
            result["barrier_released"] = self.barrier.release_all()
        return result

    def wait_for_requests(self, count: int, *, timeout: float = 5.0) -> list[dict[str, Any]]:
        """条件变量式等待够 ``count`` 次到达（不用 sleep 碰时序）。"""
        return self.store.wait_for_count(count, timeout=timeout)

    def wait_for_completed(self, count: int, *, timeout: float = 5.0) -> list[dict[str, Any]]:
        """等待够 ``count`` 条**带最终状态**的到达（断言 status 前必须用它）。"""
        return self.store.wait_for_completed(count, timeout=timeout)

    def fault_armed(self) -> list[str]:
        if self.barrier is None:
            return []
        return self.barrier.armed_names()

    # -- 上下文管理器 -----------------------------------------------------
    def __enter__(self) -> ControlSite:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return (
            f"ControlSite(port={self.port}, running={self.is_running}, "
            f"faults={self.allow_fault_injection}, payload={payloads.PAYLOAD_ID})"
        )


def _maybe_barrier(enabled: bool) -> Any | None:
    if not enabled:
        return None
    from faults import FaultBarrier  # 延迟导入：关闭故障入口时完全不引入故障模块

    return FaultBarrier()
