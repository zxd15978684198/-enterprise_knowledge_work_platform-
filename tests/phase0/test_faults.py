"""P0-T04 故障屏障测试：同步语义、超时自动放行、HTTP 控制器与断连窗口。

这些用例覆盖 `docs/poc/06` §5 的提交点断言方式：**不 sleep 猜时序**，
先让屏障布置，再用 ``wait_armed`` 等"服务端真的到了"这个事实。
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from control_site import ControlSite, request
from faults import (
    ARMED,
    BREAKPOINT_NAMES,
    FAULT_NAMES,
    HIT,
    IDLE,
    RELEASED,
    TIMEOUT,
    FaultBarrier,
    FaultError,
    is_terminal,
)

REQUIRED_FAULTS = {
    "delay",
    "timeout",
    "server_error",
    "truncate",
    "disconnect_after_success",
    "login_required",
}


# --------------------------------------------------------------------------
# 屏障状态机
# --------------------------------------------------------------------------
def test_fresh_barrier_is_idle(barrier: FaultBarrier) -> None:
    assert barrier.state("delay") == IDLE
    assert barrier.armed_names() == []
    assert barrier.params_of("delay") == {}
    assert barrier.wait_armed("delay", timeout=0.05) is False


def test_hit_without_arm_is_a_no_op(barrier: FaultBarrier) -> None:
    assert barrier.hit("delay") == IDLE
    assert barrier.state("delay") == IDLE


def test_arm_hit_blocks_until_release(barrier: FaultBarrier) -> None:
    barrier.arm("delay", params={"delay_seconds": 0})
    observed: dict[str, str] = {}

    def service() -> None:
        observed["gate"] = barrier.hit("delay")

    worker = threading.Thread(target=service, daemon=True)
    worker.start()
    assert barrier.wait_armed("delay", timeout=5.0) is True
    assert barrier.state("delay") == HIT
    assert barrier.release("delay") is True
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert observed["gate"] == RELEASED
    assert barrier.state("delay") == RELEASED
    assert is_terminal(barrier.state("delay"))


def test_hit_times_out_and_releases_itself(barrier: FaultBarrier) -> None:
    barrier.arm("timeout", timeout=0.2)
    observed: dict[str, str] = {}

    worker = threading.Thread(target=lambda: observed.update(gate=barrier.hit("timeout")), daemon=True)
    worker.start()
    assert barrier.wait_armed("timeout", timeout=5.0) is True
    worker.join(timeout=5.0)
    assert observed["gate"] == TIMEOUT
    assert barrier.state("timeout") == TIMEOUT
    snapshot = barrier.snapshot()["barriers"]["timeout"]
    assert snapshot["timeouts"] == 1


def test_wait_armed_times_out_when_service_never_arrives(barrier: FaultBarrier) -> None:
    barrier.arm("delay", timeout=5.0)
    assert barrier.wait_armed("delay", timeout=0.1) is False
    assert barrier.state("delay") == ARMED
    assert barrier.release("delay") is True


def test_arm_rejected_while_service_is_blocked(barrier: FaultBarrier) -> None:
    barrier.arm("delay", timeout=5.0)
    worker = threading.Thread(target=barrier.hit, args=("delay",), daemon=True)
    worker.start()
    assert barrier.wait_armed("delay", timeout=5.0) is True
    with pytest.raises(FaultError):
        barrier.arm("delay")
    barrier.release("delay")
    worker.join(timeout=5.0)
    assert not worker.is_alive()


def test_release_is_idempotent_and_reports_change(barrier: FaultBarrier) -> None:
    barrier.arm("delay")
    assert barrier.release("delay") is True
    assert barrier.release("delay") is False
    assert barrier.release("never-armed") is False


def test_arm_validates_name_timeout_and_params(barrier: FaultBarrier) -> None:
    for bad_name in ("", "   ", "with space", "x" * 65, 123):
        with pytest.raises(FaultError):
            barrier.arm(bad_name)  # type: ignore[arg-type]
    with pytest.raises(FaultError):
        barrier.arm("delay", timeout=0)
    with pytest.raises(FaultError):
        barrier.arm("delay", params=["not", "dict"])  # type: ignore[arg-type]


def test_injection_context_manager_always_releases(barrier: FaultBarrier) -> None:
    with pytest.raises(RuntimeError):
        with barrier.injection("delay") as state:
            assert state.state == ARMED
            worker = threading.Thread(target=barrier.hit, args=("delay",), daemon=True)
            worker.start()
            assert barrier.wait_armed("delay", timeout=5.0) is True
            raise RuntimeError("用例中断")
    assert barrier.state("delay") == RELEASED
    assert barrier.armed_names() == []


def test_release_all_reports_and_clears(barrier: FaultBarrier) -> None:
    barrier.arm("delay")
    barrier.arm("server_error")
    assert barrier.release_all() == 2
    assert barrier.armed_names() == []
    assert barrier.release_all() == 0


def test_reset_clears_barrier_history(barrier: FaultBarrier) -> None:
    barrier.arm("delay")
    barrier.release("delay")
    assert "delay" in barrier.snapshot()["barriers"]
    barrier.reset()
    assert barrier.snapshot()["barriers"] == {}
    assert barrier.state("delay") == IDLE


def test_barrier_names_cover_faults_and_commit_points() -> None:
    assert set(FAULT_NAMES) == REQUIRED_FAULTS
    assert FaultBarrier.fault_names() == REQUIRED_FAULTS
    assert set(BREAKPOINT_NAMES) == {
        "after_business_commit",
        "after_publish_before_mark",
        "after_job_claim",
        "after_effect_before_receipt",
        "after_receipt_before_checkpoint",
        "before_activation_commit",
    }
    assert FaultBarrier.breakpoint_names() == set(BREAKPOINT_NAMES)


def test_barrier_names_are_independent(barrier: FaultBarrier) -> None:
    barrier.arm("delay", timeout=5.0)
    barrier.arm("server_error", timeout=5.0)
    assert set(barrier.armed_names()) == {"delay", "server_error"}

    assert barrier.release("server_error") is True
    assert barrier.state("server_error") == RELEASED
    assert barrier.state("delay") == ARMED, "放行一个屏障不得影响其它独立屏障"

    observed: dict[str, str] = {}
    worker = threading.Thread(
        target=lambda: observed.update(gate=barrier.hit("delay")), daemon=True
    )
    worker.start()
    assert barrier.wait_armed("delay", timeout=5.0) is True
    assert barrier.hit("server_error") == RELEASED, "已放行的屏障再次命中应立即返回终态"
    assert barrier.release("delay") is True
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert observed["gate"] == RELEASED


# --------------------------------------------------------------------------
# HTTP 故障端点（故障入口显式开启的站点）
# --------------------------------------------------------------------------
def test_fault_delay_is_gated_then_succeeds(fault_site: ControlSite, gated_call: Any) -> None:
    result = gated_call("/fault/delay?key=delay-vector", name="delay", timeout=10.0)
    assert result.status == 200
    assert result.header("X-Phase0-Fault") == "delay"
    payload = result.json()
    assert payload["fault"] == "delay"
    assert payload["gate"] == RELEASED


def test_fault_server_error_returns_5xx(fault_site: ControlSite, gated_call: Any) -> None:
    result = gated_call("/fault/server_error", name="server_error")
    assert result.status == 503
    assert result.header("X-Phase0-Fault") == "server_error"
    assert result.json()["error"] == "injected_server_error"


def test_fault_truncate_returns_partial_body(fault_site: ControlSite, gated_call: Any) -> None:
    result = gated_call("/fault/truncate", name="truncate")
    assert result.status == 200
    assert result.header("Content-Length") == "4096"
    assert result.error is not None, "断流必须让客户端读失败，不能被当成完整响应"
    assert len(result.body) < 4096
    assert result.body == b"X" * len(result.body)


def test_fault_disconnect_after_success_lands_effect_without_receipt(
    fault_site: ControlSite, gated_call: Any
) -> None:
    key = "fault-disconnect-1"
    before = request("GET", fault_site.url(f"/nonidem/count?key={key}")).json()
    assert before["applied_calls"] == 0

    result = gated_call(
        f"/fault/disconnect_after_success?key={key}", name="disconnect_after_success"
    )
    assert result.status is None, "主动断连后客户端不得拿到响应"
    assert result.error

    after = request("GET", fault_site.url(f"/nonidem/count?key={key}")).json()
    assert after["applied_calls"] == 1, "副作用已在断连前落地，这是 F3 的关键窗口"

    arrivals = fault_site.store.arrivals_for("/fault/disconnect_after_success")
    assert arrivals[-1]["aborted"] is True
    assert arrivals[-1]["fault_gate"] == RELEASED
    assert arrivals[-1]["key"] == key


def test_fault_timeout_never_looks_like_success(fault_site: ControlSite, gated_call: Any) -> None:
    result = gated_call("/fault/timeout", name="timeout", timeout=2.0)
    assert result.status is None
    assert result.error


def test_fault_login_required_serves_manual_login_page(
    fault_site: ControlSite, gated_call: Any
) -> None:
    result = gated_call("/fault/login_required", name="login_required")
    assert result.status == 200
    assert result.header("X-Phase0-Fault") == "login_required"
    assert "phase0-manual-user" in result.text


def test_unarmed_fault_endpoints_keep_baseline_behavior(fault_site: ControlSite) -> None:
    error = request("GET", fault_site.url("/fault/server_error"), follow_redirects=False)
    assert error.status == 503
    delay = request("GET", fault_site.url("/fault/delay"), follow_redirects=False)
    assert delay.status == 200
    assert delay.json()["gate"] == IDLE


# --------------------------------------------------------------------------
# HTTP 故障控制面（允许跨进程驱动，T05/T07 可直接用）
# --------------------------------------------------------------------------
def test_arm_release_via_http(fault_site: ControlSite) -> None:
    armed = request("POST", fault_site.url("/_fault/arm"), body={"name": "delay"})
    assert armed.status == 200
    assert armed.json()["state"] == ARMED
    state = request("GET", fault_site.url("/_fault/state")).json()
    assert state["barriers"]["delay"]["state"] == ARMED

    released = request("POST", fault_site.url("/_fault/release"), body={"name": "delay"})
    assert released.status == 200
    assert released.json()["released"] is True
    assert released.json()["state"] == RELEASED


def test_arm_endpoint_rejects_invalid_input(fault_site: ControlSite) -> None:
    missing = request("POST", fault_site.url("/_fault/arm"), body={})
    assert missing.status == 400
    assert missing.json()["error"] == "name_required"
    bad_params = request(
        "POST", fault_site.url("/_fault/arm"), body={"name": "delay", "params": ["x"]}
    )
    assert bad_params.status == 400
    assert bad_params.json()["error"] == "params_must_be_object"
    unknown = request("POST", fault_site.url("/_fault/arm"), body={"name": "no_such_fault"})
    assert unknown.status == 400
    assert unknown.json()["error"] == "unknown_fault"


def test_wait_endpoint_observes_real_hit(fault_site: ControlSite) -> None:
    request("POST", fault_site.url("/_fault/arm"), body={"name": "delay", "timeout": 5})
    result_holder: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: result_holder.update(
            r=request("GET", fault_site.url("/fault/delay"), timeout=10.0)
        ),
        daemon=True,
    )
    worker.start()
    waited = request("POST", fault_site.url("/_fault/wait"), body={"name": "delay", "timeout": 5})
    assert waited.status == 200
    assert waited.json()["hit"] is True, "wait_armed 必须在服务端真的到达断点后才返回"
    assert request("POST", fault_site.url("/_fault/release"), body={"name": "delay"}).status == 200
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert result_holder["r"].status == 200


def test_fault_site_healthz_and_state_expose_gate(fault_site: ControlSite) -> None:
    health = request("GET", fault_site.url("/healthz")).json()
    assert health["fault_injection"] is True
    state = request("GET", fault_site.url("/_fault/state")).json()
    assert state["fault_names"] and state["breakpoint_names"]
    assert json.dumps(state)  # 可序列化，便于写进证据


def test_server_stop_releases_pending_barrier(site_manager: Any) -> None:
    site = site_manager.start(allow_fault_injection=True)
    request("POST", site.url("/_fault/arm"), body={"name": "delay", "timeout": 30})
    worker = threading.Thread(
        target=lambda: request("GET", site.url("/fault/delay"), timeout=10.0), daemon=True
    )
    worker.start()
    assert site.barrier.wait_armed("delay", timeout=5.0) is True
    site.stop()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "stop() 必须放行仍在阻塞的故障请求"
    assert site.fault_armed() == []
