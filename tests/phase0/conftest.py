"""P0-T04 测试 fixture：控制站起停、故障注入与会话清理。

设计约束（与任务卡一致，其他任务只能通过这里的 fixture API 使用控制站）：

* 起停可重复：每个用例拿到全新的 :class:`ControlSite`，teardown 必须
  ``stop()`` 并断言端口不再 listen、屏障全部释放；
* **不用 sleep 等时序**：健康等待走 ``/healthz`` 轮询，故障窗口走
  :meth:`FaultBarrier.wait_armed`，到达次数走 ``store.wait_for_count``；
* 默认随机端口（``port=0``）保证并行安全；固定 5500 只给"串行 + 可复验"
  的用例，且被占用时显式 skip 并说明原因，不吞掉残留进程这个事实；
* 本文件只依赖控制站与 ``faults`` 两个被测模块，不 import 其他任务代码。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PHASE0_DIR) not in sys.path:  # pragma: no cover - 首次导入即生效
    sys.path.insert(0, str(PHASE0_DIR))

from control_site import ControlSite, request  # noqa: E402
from control_site.client import HttpResult  # noqa: E402
from faults import FaultBarrier  # noqa: E402

FIXED_PORT = 5500
SITE_START_TIMEOUT_S = 5.0

# ---------------------------------------------------------------------------
# 证据目录重定向（W15 修复）
#
# 若干探针（T05/T25/T26/T28/T29）会把实测证据写回仓库 tracked 路径。全量回归时
# 前面的用例先改写证据、后面的 ``test_repo_evidence_ledger_is_clean`` 再按台账校验，
# 同一次运行里必然互相打脸（W14/W15 各复现一次）。这里在**收集任何测试模块之前**
# 把 ``PYKNORA_EVIDENCE_DIR`` 指到临时目录并预置一份 tracked 证据副本：
# 运行期写入落在临时目录，仓库保持干净，探针代码不必知道自己被测试。
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
TRACKED_EVIDENCE_DIR = REPO_ROOT / "docs" / "poc" / "evidence"
EVIDENCE_TMP_DIR = Path(tempfile.mkdtemp(prefix="pyknora-evidence-"))
if TRACKED_EVIDENCE_DIR.is_dir():  # pragma: no branch - 仓库内恒为真
    shutil.copytree(TRACKED_EVIDENCE_DIR, EVIDENCE_TMP_DIR, dirs_exist_ok=True)
os.environ["PYKNORA_EVIDENCE_DIR"] = str(EVIDENCE_TMP_DIR)


def pytest_sessionfinish(session: object, exitstatus: int) -> None:  # noqa: ARG001
    """整轮结束后删掉临时证据目录，不留残留。"""

    shutil.rmtree(EVIDENCE_TMP_DIR, ignore_errors=True)


BARRIER_TIMEOUT_S = 2.0
GATE_WAIT_TIMEOUT_S = 5.0


def port_is_listening(
    host: str = "127.0.0.1", port: int = 0, *, timeout: float = 0.25
) -> bool:
    """用一次 TCP connect 判断端口是否仍有人在 listen（不睡眠、不重试）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


class SiteManager:
    """统一保管用例期间起过的控制站，保证 teardown 一定停干净。"""

    def __init__(self) -> None:
        self._sites: list[ControlSite] = []
        self._lock = threading.Lock()

    def start(self, **kwargs: Any) -> ControlSite:
        site = ControlSite(**kwargs)
        site.start(timeout=SITE_START_TIMEOUT_S)
        with self._lock:
            self._sites.append(site)
        return site

    def stop_all(self) -> list[dict[str, Any]]:
        """逆序停止并返回每站的收尾事实（端口、是否仍 listen、残留屏障）。"""
        report: list[dict[str, Any]] = []
        with self._lock:
            sites = list(reversed(self._sites))
            self._sites.clear()
        for site in sites:
            port = site.port
            site.stop()
            report.append(
                {
                    "port": port,
                    "listening_after_stop": port_is_listening(site.host, port),
                    "armed_barriers": site.fault_armed(),
                }
            )
        return report


@pytest.fixture(scope="session")
def port_probe() -> Callable[..., bool]:
    """暴露端口探测函数，用例不必 import conftest 模块。"""
    return port_is_listening


@pytest.fixture(scope="session")
def phase0_module_path() -> Path:
    """暴露 ``experiments/phase0`` 目录，便于用例读取 fixtures/manifest.json。"""
    return PHASE0_DIR


@pytest.fixture()
def site_manager() -> Iterator[SiteManager]:
    """起停登记簿；teardown 断言端口全部释放、屏障全部放行。"""
    manager = SiteManager()
    try:
        yield manager
    finally:
        report = manager.stop_all()
        for item in report:
            assert not item["listening_after_stop"], (
                f"端口 {item['port']} 在 stop() 后仍在监听"
            )
            assert item["armed_barriers"] == [], (
                f"仍有未释放的屏障：{item['armed_barriers']}"
            )


@pytest.fixture()
def site(site_manager: SiteManager) -> ControlSite:
    """故障入口**关闭**的默认站点（生产默认形态）。"""
    return site_manager.start(allow_fault_injection=False)


@pytest.fixture()
def fault_site(site_manager: SiteManager) -> ControlSite:
    """故障入口打开的站点（仅测试显式开启）。"""
    return site_manager.start(allow_fault_injection=True)


@pytest.fixture()
def fixed_port_site(site_manager: SiteManager) -> ControlSite:
    """固定 ``127.0.0.1:5500`` 的站点；被外部占用则显式 skip 并给出原因。"""
    if port_is_listening("127.0.0.1", FIXED_PORT):
        pytest.skip(
            f"127.0.0.1:{FIXED_PORT} 已被占用（可能是残留进程）；"
            "固定端口用例串行执行，请先确认端口释放"
        )
    return site_manager.start(port=FIXED_PORT, allow_fault_injection=True)


@pytest.fixture()
def barrier() -> Iterator[FaultBarrier]:
    """独立屏障实例（不挂站点），teardown 强制释放并清空。"""
    instance = FaultBarrier(default_timeout=BARRIER_TIMEOUT_S)
    try:
        yield instance
    finally:
        instance.release_all()
        assert instance.armed_names() == []
        instance.reset()


GatedCall = Callable[..., HttpResult]


@pytest.fixture()
def gated_call(fault_site: ControlSite) -> GatedCall:
    """发一次**会被屏障挂住**的请求：先确认服务端真的到达断点，再放行。

    返回的 callable 参数：``path``、``name``（屏障名，缺省取路径最后一段）、
    ``method``、``body``、``headers``、``timeout``、``release``。

    这是「禁止固定 sleep 碰时序」的可复用入口：请求在后台线程发出，主线程用
    ``wait_armed`` 等事实发生，因此无论调度快慢都不会出现"猜时间猜错"。
    """

    def call(
        path: str,
        *,
        name: str | None = None,
        method: str = "GET",
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        release: bool = True,
        wait_timeout: float = GATE_WAIT_TIMEOUT_S,
        params: dict[str, Any] | None = None,
        arm: bool = True,
    ) -> HttpResult:
        assert fault_site.barrier is not None, "fault_site 未挂载屏障"
        barrier_name = name or path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        holder: dict[str, HttpResult] = {}
        failure: dict[str, BaseException] = {}

        if arm:
            # 先布置再发请求：布置是同步动作，不依赖任何 sleep 猜时序。
            fault_site.barrier.arm(
                barrier_name, timeout=wait_timeout + timeout, params=params
            )

        def worker() -> None:
            try:
                holder["result"] = request(
                    method,
                    fault_site.url(path),
                    body=body,
                    headers=headers,
                    timeout=timeout,
                )
            except BaseException as exc:  # noqa: BLE001 - 线程内异常转交给主线程断言
                failure["error"] = exc

        thread = threading.Thread(
            target=worker, name=f"gated-{barrier_name}", daemon=True
        )
        thread.start()
        try:
            reached = fault_site.barrier.wait_armed(barrier_name, timeout=wait_timeout)
            assert reached, f"服务端未在 {wait_timeout}s 内到达断点 {barrier_name!r}"
        finally:
            if release:
                fault_site.barrier.release(barrier_name)
        thread.join(timeout=timeout + 1.0)
        assert not thread.is_alive(), f"{barrier_name!r} 的请求线程未在放行后结束"
        if failure:
            raise failure["error"]
        return holder["result"]

    return call
