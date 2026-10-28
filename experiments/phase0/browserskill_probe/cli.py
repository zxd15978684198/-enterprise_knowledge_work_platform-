"""`bsk` CLI 的受控进程封装：定位二进制、限时执行、JSON 解析、输出脱敏。

设计约束（来自 T17 现场实测，见 docs/poc/evidence/poc05-browserskill-live.md）：
- daemon 的 IPC 是 Windows 命名管道，uv 托管的 CPython 3.12 打开即 `PermissionError 13`
  （ctypes `CreateFileW` 也是 `last_error=5`），所以探针**不直连 IPC**，一律走 CLI 子进程。
- CLI 每次调用是新进程；session 由 daemon 侧持有，跨进程仍可用，但会被 `--daemon-idle`
  或用户关闭 Agent Window 回收，因此 adapter 的全部动作必须在同一脚本内连续完成。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
)
REDACTED: Final[str] = "<redacted>"

# 本机已编译的 bsk 落点（按顺序探测）；也可用 BSK_BIN 覆盖。
_CANDIDATE_PATHS: Final[tuple[str, ...]] = (
    r"D:\PyKnora\bsk\bsk.exe",
    r"%LOCALAPPDATA%\PyKnora\bsk\bsk.exe",
)


class BskCliError(RuntimeError):
    """CLI 调用失败：非零退出、超时或输出不是期望的 JSON。"""

    def __init__(self, message: str, *, argv: Sequence[str] = (), exit_code: int | None = None,
                 stderr: str = "", stdout: str = "") -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout


def find_bsk_binary(explicit: str | os.PathLike[str] | None = None) -> Path:
    """按 BSK_BIN → 候选路径 → PATH 的顺序定位 bsk 可执行文件。"""

    candidates: list[Path] = []
    if explicit is not None:
        strict = Path(explicit)
        if not strict.is_file():
            raise BskCliError(f"找不到 bsk 可执行文件：显式指定的路径不存在 {strict}")
        return strict
    env = os.environ.get("BSK_BIN")
    if env:
        candidates.append(Path(env))
    for raw in _CANDIDATE_PATHS:
        candidates.append(Path(os.path.expandvars(raw)))
    found = shutil.which("bsk") or shutil.which("bsk.exe")
    if found:
        candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    raised = ", ".join(str(p) for p in candidates)
    raise BskCliError(f"找不到 bsk 可执行文件；已探测: {raised}（可用 BSK_BIN 指定）")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def redact(value: Any) -> Any:
    """递归脱敏：命中的键整值替换，字符串里不做事后正则（避免误伤正文）。"""

    if isinstance(value, Mapping):
        return {
            key: (REDACTED if isinstance(key, str) and _is_secret_key(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def parse_json(text: str) -> Any:
    """从 CLI stdout 里取出第一个 JSON 文档；允许前面有信息行。"""

    stripped = text.strip()
    if not stripped:
        raise BskCliError("CLI 输出为空，无法解析 JSON", stdout=text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    starts = [index for index in (stripped.find("{"), stripped.find("[")) if index >= 0]
    if not starts:
        raise BskCliError(f"CLI 输出不含 JSON: {stripped[:200]!r}", stdout=text)
    start = min(starts)
    decoder = json.JSONDecoder()
    try:
        parsed, _end = decoder.raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        raise BskCliError(f"CLI 输出 JSON 损坏: {exc}", stdout=text) from exc
    return parsed


@dataclass(slots=True)
class BskCli:
    """把一次 `bsk ... --json` 调用收敛成「有超时、有退出码、可脱敏」的纯函数。"""

    binary: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def autodetect(cls, explicit: str | os.PathLike[str] | None = None, *,
                   timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> BskCli:
        return cls(binary=find_bsk_binary(explicit), timeout_seconds=timeout_seconds)

    def argv(self, args: Sequence[str]) -> list[str]:
        argv = [str(self.binary), *[str(item) for item in args]]
        if "--json" not in argv:
            argv.append("--json")
        return argv

    def run(self, args: Sequence[str], *, timeout_seconds: float | None = None,
            check: bool = True) -> tuple[int, str, str]:
        argv = self.argv(args)
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BskCliError(f"CLI 超时（{timeout}s）", argv=argv) from exc
        except OSError as exc:
            raise BskCliError(f"CLI 启动失败: {exc}", argv=argv) from exc
        if check and completed.returncode != 0:
            raise BskCliError(
                f"CLI 退出码 {completed.returncode}",
                argv=argv,
                exit_code=completed.returncode,
                stderr=completed.stderr,
                stdout=completed.stdout,
            )
        return completed.returncode, completed.stdout, completed.stderr

    def run_json(self, args: Sequence[str], *, timeout_seconds: float | None = None) -> Any:
        _code, stdout, _stderr = self.run(args, timeout_seconds=timeout_seconds)
        return redact(parse_json(stdout))
