"""P0-T04 控制站验收测试：页面/计数/人工登录/故障门默认关闭，以及 manifest 自洽。

这些用例同时充当"控制向量清单可复验"的证据：manifest 里的 sha256 必须
与真实服务字节一致，否则测试失败（不允许伪造 PASS）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from control_site import ControlSite, request
from control_site.store import SESSION_COOKIE

MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "experiments" / "phase0" / "fixtures" / "manifest.json"
)
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
VECTORS: list[dict[str, Any]] = MANIFEST["vectors"]
VECTOR_BY_ID = {vector["id"]: vector for vector in VECTORS}
FAULT_MATRIX_IDS = {f"F{index}" for index in range(1, 19)}

SHA256_VECTORS = [
    vector for vector in VECTORS if vector["integrity"]["mode"] == "sha256"
]
LIVE_GET_VECTORS = [
    vector
    for vector in VECTORS
    if vector["method"] == "GET"
    and not vector["path"].startswith(("/fault", "/_fault"))
    and vector["path"] not in {"/protected"}
]


# --------------------------------------------------------------------------
# manifest 自洽性
# --------------------------------------------------------------------------
def test_manifest_has_at_least_twenty_vectors() -> None:
    assert len(VECTORS) >= 20, f"控制向量不足：{len(VECTORS)}"
    ids = [vector["id"] for vector in VECTORS]
    assert len(set(ids)) == len(ids), "控制向量 id 重复"


def test_manifest_entries_are_complete() -> None:
    required = {
        "id",
        "type",
        "polarity",
        "method",
        "path",
        "license",
        "source",
        "integrity",
        "expected",
        "fault_matrix",
        "consumers",
    }
    for vector in VECTORS:
        missing = required - set(vector)
        assert not missing, f"{vector.get('id')} 缺少字段 {sorted(missing)}"
        assert vector["polarity"] in {"positive", "negative"}
        assert vector["method"] in {"GET", "POST"}
        assert vector["path"].startswith("/")
        assert vector["license"], f"{vector['id']} 无许可说明"
        assert "TODO" not in json.dumps(vector) and "TBD" not in json.dumps(vector)
        assert vector["integrity"]["mode"] in {"sha256", "generation", "structural"}
        if vector["integrity"]["mode"] == "sha256":
            digest = vector["integrity"]["sha256"]
            assert len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
            assert vector["expected"]["body_sha256"] == digest
        assert set(vector["fault_matrix"]) <= FAULT_MATRIX_IDS
        assert vector["consumers"], f"{vector['id']} 未标注消费任务"


def test_manifest_covers_every_required_endpoint_kind() -> None:
    kinds = {vector["type"] for vector in VECTORS}
    required_kinds = {
        "static_page",
        "js_page",
        "iframe_page",
        "redirect",
        "download_html",
        "download_binary",
        "login_page",
        "counter_idempotent",
        "counter_non_idempotent",
        "counter_read",
        "fault_delay",
        "fault_timeout",
        "fault_server_error",
        "fault_truncate",
        "fault_disconnect_after_success",
        "fault_login_required",
        "error_unknown",
    }
    assert required_kinds <= kinds, f"缺少向量类型：{sorted(required_kinds - kinds)}"
    redirects = {vector["path"] for vector in VECTORS if vector["type"] == "redirect"}
    assert {"/redirect/301", "/redirect/302"} <= redirects
    assert MANIFEST["control_site"]["fault_injection_default"] is False


@pytest.mark.parametrize("vector", SHA256_VECTORS, ids=[v["id"] for v in SHA256_VECTORS])
def test_manifest_hash_matches_live_bytes(vector: dict[str, Any], site: ControlSite) -> None:
    result = request("GET", site.url(vector["path"]), follow_redirects=False)
    assert result.status == vector["expected"]["status"]
    assert result.header("Content-Type") == vector["expected"]["content_type"]
    assert hashlib.sha256(result.body).hexdigest() == vector["integrity"]["sha256"]
    assert len(result.body) == vector["integrity"]["bytes"]


@pytest.mark.parametrize("vector", LIVE_GET_VECTORS, ids=[v["id"] for v in LIVE_GET_VECTORS])
def test_manifest_expected_status_matches_live(vector: dict[str, Any], site: ControlSite) -> None:
    result = request("GET", site.url(vector["path"]), follow_redirects=False)
    assert result.status == vector["expected"]["status"], vector["id"]


# --------------------------------------------------------------------------
# 页面 / 跳转 / 附件
# --------------------------------------------------------------------------
def test_redirects_are_served_with_location(site: ControlSite) -> None:
    for path, status in (("/redirect/301", 301), ("/redirect/302", 302)):
        result = request("GET", site.url(path), follow_redirects=False)
        assert result.status == status
        assert result.location == "/static/page.html"
        assert result.body == b""


def test_downloads_declare_attachment(site: ControlSite) -> None:
    html = request("GET", site.url("/download/html"))
    binary = request("GET", site.url("/download/binary"))
    assert html.header("Content-Disposition").startswith("attachment;")
    assert html.header("Content-Type") == "text/html; charset=utf-8"
    assert binary.header("Content-Disposition").startswith("attachment;")
    assert binary.header("Content-Type") == "application/octet-stream"
    assert len(binary.body) == 4096


def test_js_page_is_served_as_html(site: ControlSite) -> None:
    result = request("GET", site.url("/static/js.html"))
    assert result.status == 200
    assert "/js/beacon" in result.text


# --------------------------------------------------------------------------
# 计数器
# --------------------------------------------------------------------------
def test_idempotent_write_deduplicates_by_key(site: ControlSite) -> None:
    key = "acceptance-idem-1"
    first = request("POST", site.url("/idem/write"), body={"key": key}).json()
    second = request("POST", site.url("/idem/write"), body={"key": key})
    payload = second.json()
    assert first["applied"] is True
    assert first["applied_calls"] == 1 and first["attempts"] == 1
    assert payload["applied"] is False
    assert payload["applied_calls"] == 1, "重复 key 不允许再计一次副作用"
    assert payload["attempts"] == 2
    assert payload["deduplicated"] is True
    assert second.header("X-Idempotent-Replayed") == "true"


def test_non_idempotent_write_counts_every_call(site: ControlSite) -> None:
    key = "acceptance-ni-1"
    first = request("POST", site.url("/nonidem/write"), body={"key": key}).json()
    second = request("POST", site.url("/nonidem/write"), body={"key": key}).json()
    assert first["applied_calls"] == 1
    assert second["applied_calls"] == 2, "非幂等写每次都必须 +1"
    assert second["deduplicated"] is False


def test_count_endpoints_report_last_status(site: ControlSite) -> None:
    key = "acceptance-count-1"
    request("POST", site.url("/idem/write"), body={"key": key})
    request("POST", site.url("/nonidem/write"), body={"key": key})
    idem = request("GET", site.url(f"/idem/count?key={key}")).json()
    nonidem = request("GET", site.url(f"/nonidem/count?key={key}")).json()
    assert idem["surface"] == "idem"
    assert idem["applied_calls"] == 1
    assert idem["last_status"]["endpoint"] == "/idem/write"
    assert nonidem["surface"] == "nonidem"
    assert nonidem["applied_calls"] == 1
    assert nonidem["last_status"]["status"] == 200


def test_invalid_key_rejected_without_side_effect(site: ControlSite) -> None:
    result = request("POST", site.url("/nonidem/write"), body={"key": "bad key!"})
    assert result.status == 400
    assert result.json()["error"] == "invalid_key"
    snapshot = request("GET", site.url("/_control/state")).json()
    assert snapshot["nonidem"] == {}


def test_write_rejects_wrong_method(site: ControlSite) -> None:
    result = request("GET", site.url("/idem/write"), follow_redirects=False)
    assert result.status == 405
    assert result.header("Allow") == "POST"


# --------------------------------------------------------------------------
# 人工登录
# --------------------------------------------------------------------------
def test_protected_requires_session(site: ControlSite) -> None:
    result = request("GET", site.url("/protected"), follow_redirects=False)
    assert result.status == 401
    assert result.json()["error"] == "login_required"


def test_login_flow_uses_manual_test_account(site: ControlSite) -> None:
    bad = request(
        "POST", site.url("/login"), body={"username": "phase0-manual-user", "password": "nope"}
    )
    assert bad.status == 401

    ok = request(
        "POST",
        site.url("/login"),
        body={"username": "phase0-manual-user", "password": "phase0-manual-pass"},
        follow_redirects=False,
    )
    assert ok.status == 303
    assert ok.location == "/protected"
    cookie_header = ok.header("Set-Cookie") or ""
    assert SESSION_COOKIE in cookie_header
    token = cookie_header.split(";", 1)[0]

    authed = request("GET", site.url("/protected"), headers={"Cookie": token})
    assert authed.status == 200
    assert authed.json()["user"] == "phase0-manual-user"


def test_unknown_path_is_404(site: ControlSite) -> None:
    result = request("GET", site.url("/nope"), follow_redirects=False)
    assert result.status == 404
    assert result.json()["error"] == "not_found"


# --------------------------------------------------------------------------
# 故障门默认关闭（生产默认形态）
# --------------------------------------------------------------------------
def test_fault_endpoints_absent_by_default(site: ControlSite) -> None:
    baseline = request("GET", site.url("/nope"), follow_redirects=False).json()
    probes = [
        ("GET", "/fault/server_error"),
        ("GET", "/fault/delay"),
        ("GET", "/fault/truncate"),
        ("GET", "/_fault/state"),
    ]
    for method, path in probes:
        result = request(method, site.url(path), follow_redirects=False)
        assert result.status == 404, f"{path} 在默认站点必须不存在"
        assert result.json()["error"] == baseline["error"] == "not_found"
    arm = request("POST", site.url("/_fault/arm"), body={"name": "delay"}, follow_redirects=False)
    assert arm.status == 404


def test_healthz_reports_fault_gate_state(site: ControlSite, fault_site: ControlSite) -> None:
    closed = request("GET", site.url("/healthz")).json()
    opened = request("GET", fault_site.url("/healthz")).json()
    assert closed["status"] == "ok" and closed["fault_injection"] is False
    assert opened["fault_injection"] is True
    assert closed["service"] == opened["service"]


def test_fault_site_exposes_admin_state(fault_site: ControlSite) -> None:
    state = request("GET", fault_site.url("/_fault/state")).json()
    assert state["default_timeout_s"] > 0
    assert state["barriers"] == {}
    assert set(state["fault_names"]) == {
        "delay",
        "timeout",
        "server_error",
        "truncate",
        "disconnect_after_success",
        "login_required",
    }


# --------------------------------------------------------------------------
# 到达日志：最终到达端点 + 不记录敏感头的值
# --------------------------------------------------------------------------
def test_arrival_log_gives_final_endpoint_for_outbound_vector(site: ControlSite) -> None:
    baseline = site.store.completed_count
    page = request("GET", site.url("/static/iframe.html"))
    beacon = request("GET", site.url("/js/beacon?key=iframe-frame"))
    assert page.status == 200 and beacon.status == 200

    # 到达记录在**进站**时就落一条（status=None），最终状态由 finish() 补齐；
    # 这里等"已完成"的记录，避免读到半成品——这是禁止 sleep 猜时序在状态维度的体现。
    arrivals = site.wait_for_completed(baseline + 2)[-2:]
    assert [item["path"] for item in arrivals] == ["/static/iframe.html", "/js/beacon"]
    assert arrivals[-1]["key"] == "iframe-frame"
    assert arrivals[-1]["status"] == 200


def test_arrival_log_never_stores_credential_values(site: ControlSite) -> None:
    canary_authorization = "canary-auth-7c1f4d2b9e"
    canary_cookie = "canary-cookie-51ab77cd"
    canary_api_key = "canary-apikey-2f9e63"
    request(
        "GET",
        site.url("/static/page.html"),
        headers={
            "Authorization": f"Bearer {canary_authorization}",
            "Cookie": f"{SESSION_COOKIE}={canary_cookie}",
            "X-Api-Key": canary_api_key,
        },
    )
    log = request("GET", site.url("/_control/requests")).json()
    # 到达日志会把本次查询自身也记入，因此按路径定位被测请求，而不是取最后一条。
    entry = next(
        item for item in reversed(log["items"]) if item["path"] == "/static/page.html"
    )
    assert entry["path"] == "/static/page.html"
    assert "authorization" in entry["credential_headers"]
    assert "cookie" in entry["credential_headers"]
    assert "x-api-key" in entry["credential_headers"]

    serialized = json.dumps(log, ensure_ascii=False)
    for canary in (canary_authorization, canary_cookie, canary_api_key):
        assert canary not in serialized, "到达日志不得记录凭据值"


def test_control_state_and_reset(site: ControlSite) -> None:
    request("POST", site.url("/idem/write"), body={"key": "reset-idem"})
    before = request("GET", site.url("/_control/state")).json()
    assert before["idem"]["reset-idem"]["applied"] == 1
    assert before["fault_injection"] is False
    assert before["requests"] >= 2

    cleared = request("POST", site.url("/_control/reset")).json()
    assert cleared["cleared"]["idem_keys"] == 1
    after = request("GET", site.url("/_control/state")).json()
    assert after["idem"] == {}
    assert after["requests"] == 1, "reset 只保留 reset 这一次到达记录"


def test_arrival_log_pagination(site: ControlSite) -> None:
    for index in range(3):
        request("GET", site.url(f"/static/page.html?n={index}"))
    page = request("GET", site.url("/_control/requests?after=0&limit=2")).json()
    assert len(page["items"]) == 2
    assert page["total"] >= 3
    second = request("GET", site.url(f"/_control/requests?after={page['next']}&limit=2")).json()
    assert second["items"], "游标翻页不应为空"


# --------------------------------------------------------------------------
# 端口只绑回环、起停可重复
# --------------------------------------------------------------------------
def test_only_loopback_host_allowed() -> None:
    with pytest.raises(ValueError):
        ControlSite(host="0.0.0.0")  # type: ignore[arg-type]


def test_ten_start_stop_cycles_leave_no_listener(
    site_manager: Any, port_probe: Any
) -> None:
    for cycle in range(10):
        instance = site_manager.start(allow_fault_injection=True)
        port = instance.port
        assert instance.host == "127.0.0.1"
        assert request("GET", instance.url("/healthz")).status == 200
        instance.stop()
        assert not port_probe(instance.host, port), f"第 {cycle + 1} 轮 stop 后端口仍在监听"


def test_fixed_port_site_is_served_and_released(
    fixed_port_site: ControlSite, port_probe: Any
) -> None:
    assert fixed_port_site.port == 5500
    assert request("GET", fixed_port_site.url("/healthz")).status == 200
    fixed_port_site.stop()
    assert not port_probe("127.0.0.1", 5500)
