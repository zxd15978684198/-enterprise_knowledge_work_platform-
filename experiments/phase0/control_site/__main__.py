"""控制站命令行入口：``python -m control_site --port 5500``。

两道门禁保证「生产默认不可启用故障入口」：

1. 命令行不传 ``--allow-fault-injection`` → ``/fault/*`` 与 ``/_fault/*`` 一律 404；
2. 即使传了，也必须同时满足环境变量 ``PYKNORA_CONTROL_SITE_ALLOW_FAULTS=1``，
   且 ``PYKNORA_ENV`` 不是 production/prod —— 否则直接拒绝启动。

``--self-check N`` 会做 N 轮「起 → healthz → 停」并在结束时验证端口已释放，
用于生成可复验的起停证据，不留下后台进程。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
from typing import Any

from .client import request
from .payloads import PAYLOAD_ID
from .server import DEFAULT_HOST, LOOPBACK_HOSTS, ControlSite

PRODUCTION_ENVS = frozenset({"production", "prod"})
FAULT_ENV_FLAG = "PYKNORA_CONTROL_SITE_ALLOW_FAULTS"
ENV_FLAG = "PYKNORA_ENV"


def _faults_allowed_by_env() -> tuple[bool, str]:
    env_name = (os.environ.get(ENV_FLAG) or "").strip().lower()
    if env_name in PRODUCTION_ENVS:
        return False, f"{ENV_FLAG}={env_name} 禁止启用故障注入"
    flag = (os.environ.get(FAULT_ENV_FLAG) or "").strip()
    if flag != "1":
        return False, f"需要显式设置 {FAULT_ENV_FLAG}=1 才允许启用故障注入"
    return True, ""


def _port_is_listening(host: str, port: int, *, timeout: float = 0.25) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, port)) == 0
    finally:
        probe.close()


def _self_check(*, host: str, port: int, cycles: int, allow_faults: bool) -> int:
    """N 轮起停，逐轮验证 healthz 与端口释放；返回进程退出码。"""
    results: list[dict[str, Any]] = []
    for index in range(1, cycles + 1):
        site = ControlSite(host=host, port=port, allow_fault_injection=allow_faults)
        declared_port = site.port
        site.start()
        try:
            health = request("GET", site.health_url(), timeout=2.0)
            listening = _port_is_listening(host, declared_port)
            body = health.json() if health.status == 200 else None
            results.append(
                {
                    "cycle": index,
                    "port": declared_port,
                    "health_status": health.status,
                    "health_body": body,
                    "port_listening_while_running": listening,
                }
            )
        finally:
            site.stop()
        results[-1]["port_listening_after_stop"] = _port_is_listening(host, declared_port)
        print(json.dumps(results[-1], ensure_ascii=False, sort_keys=True), flush=True)

    leaks = [item for item in results if item["port_listening_after_stop"]]
    healthy = [item for item in results if item["health_status"] == 200]
    summary = {
        "cycles": cycles,
        "healthy_rounds": len(healthy),
        "port_leaks_after_stop": len(leaks),
        "status": "PASS" if len(healthy) == cycles and not leaks else "FAIL",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["status"] == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m control_site",
        description="PyKnora Phase 0 控制站（只绑回环；故障注入默认关闭）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, choices=list(LOOPBACK_HOSTS))
    parser.add_argument("--port", type=int, default=5500, help="0 表示随机空闲端口")
    parser.add_argument(
        "--allow-fault-injection",
        action="store_true",
        help=f"挂载 /fault/* 与 /_fault/*（还需 {FAULT_ENV_FLAG}=1）",
    )
    parser.add_argument(
        "--self-check",
        type=int,
        metavar="N",
        default=0,
        help="做 N 轮起停自检后退出（不常驻）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.allow_fault_injection:
        allowed, reason = _faults_allowed_by_env()
        if not allowed:
            print(f"[control-site] 拒绝启用故障注入：{reason}", file=sys.stderr, flush=True)
            return 2

    if args.self_check:
        return _self_check(
            host=args.host,
            port=args.port,
            cycles=args.self_check,
            allow_faults=args.allow_fault_injection,
        )

    site = ControlSite(
        host=args.host,
        port=args.port,
        allow_fault_injection=args.allow_fault_injection,
    )
    site.start()
    banner = {
        "status": "listening",
        "base_url": site.base_url,
        "health": site.health_url(),
        "fault_injection": site.allow_fault_injection,
        "payload_id": PAYLOAD_ID,
    }
    print(json.dumps(banner, ensure_ascii=False, sort_keys=True), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("[control-site] 收到中断，正在停止", file=sys.stderr, flush=True)
    finally:
        site.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
