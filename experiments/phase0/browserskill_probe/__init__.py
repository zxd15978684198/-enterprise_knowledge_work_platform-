"""P0-T17: BrowserSkill 真配对探针。

只做两件事：
1. `cli` 把 `bsk` CLI 当成受控传输层（子进程 + JSON + 超时 + 脱敏），不做 IPC 直连
   （uv 托管的 CPython 3.12 在本机打不开 Windows 命名管道，见 evidence/poc01-owner.md 的同源限制）。
2. `adapter` 把 T15 `BrowserAction` 的受控动作映射到 `bsk` 子命令，并在发出前做
   allowlist / 秘密 / 作用域 三道闸；未核对过 CLI 签名的动作直接拒绝，不猜 argv。
"""

from __future__ import annotations

from .adapter import (
    ADAPTER_SUPPORTED,
    ActionNotMapped,
    AdapterRefused,
    BrowserskillAdapter,
    build_argv,
    supported_actions,
)
from .cli import BskCli, BskCliError, find_bsk_binary, parse_json, redact

__all__ = [
    "ADAPTER_SUPPORTED",
    "ActionNotMapped",
    "AdapterRefused",
    "BrowserskillAdapter",
    "BskCli",
    "BskCliError",
    "build_argv",
    "find_bsk_binary",
    "parse_json",
    "redact",
    "supported_actions",
]
