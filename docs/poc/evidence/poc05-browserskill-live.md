# P0-T17 证据：BrowserSkill 真配对与真实动作

- **任务**：P0-T17（BrowserSkill 真配对与最小动作验证）｜关联 PoC：PoC-05｜关联契约：C-05（NEEDS_POC）、C-04（工具意图/审批面）
- **状态**：`PASS（限域）`——真 daemon + 真 Chrome 扩展 + 真动作全部走通；限域项见 §6
- **采集时间**：2026-10-28（Asia/Shanghai）
- **执行人**：Lead（人工在环：扩展加载由用户在 Chrome 完成）
- **分支**：`fix/w5-t17`（基线 `dev@cc4a475`）

## 1. 结论

`bsk` 三件套在本机可完整运行：Rust daemon（自编译 `bsk.exe`）+ 未打包 Chrome 扩展（自构建 0.3.1）
+ 本仓 `browserskill_probe` 探针。探针通过 **CLI 受控子进程**驱动 daemon，完成
`session start → tab create → snapshot → evaluate → screenshot → console → tab close → session stop`
全链路，未使用任何 mock、桩或假实现。

## 2. 环境与版本（真实值）

| 项 | 值 |
| --- | --- |
| OS | Windows（本机开发环境） |
| Chrome | 147.0.7727.117（真实用户配置文件 + 未打包扩展） |
| bsk daemon | 0.3.1，`pid 1768`，`ws://127.0.0.1:52800`，`--daemon-idle 60m` |
| IPC 管道 | `\\.\pipe\bsk-daemon-e974436b93237db7` |
| 扩展 | `version 0.3.1`，`location=4`（unpacked），ID `ipodpljpdchoicenpokpiakgdjafjimn`，路径 `D:\PyKnora\browser-skill-extension\0.3.1` |
| 扩展协议 | `extension_protocol_version=1.3`，`version_skew=false` |
| daemon 协议 | `1.3`（doctor: `protocol compatible` ok） |
| `bsk.exe` | 15,925,760 B，`sha256=DE16E23C30FACD36AF7FC54244DC6E9EFFC69253F13CCB5A3F23C80842412C64` |
| 扩展 zip | 860,920 B，`sha256=050211FDE1827E332F23DBA54F0BDF970C2C1ED0D1AD6E4939CCE394716839DB` |
| 上游源码 | `Tencent/BrowserSkill@da6bf4eed2dd7256567e152df8c903c87f6598c3`（release/0.3.1），LICENSE = **MIT** |
| 取证 Python | **3.13.3**（`C:\Python313\python.exe`） |

## 3. 命令与真实输出

### 3.1 `bsk doctor --json`（摘录关键项）

| check | status | detail |
| --- | --- | --- |
| bsk home writable | ok | `C:\Users\Administrator\.bsk` |
| daemon running | ok | `pid 1768 at ws://127.0.0.1:52800` |
| daemon local process identity | ok | `IPC peer PID matches daemon identity` |
| protocol compatible | ok | `daemon protocol 1.3 (app 0.3.1)` |
| extension connected | ok | `1 browser(s) connected` |
| browser protocol compatible | ok | `all 1 online browser(s) are compatible with the daemon` |

`doctor` 存在 `status="na"` 项时退出码为 **1**（不是失败）；探针对 doctor 用 `check=False` 解析 stdout，
其余子命令仍按非零即失败处理。

### 3.2 `bsk browsers --json`

```json
{"instance_id":"dc8a279d","browser_name":"chrome","browser_version":"147.0.0.0",
 "extension_version":"0.3.1","session_count":0,"version_skew":false,
 "extension_protocol_version":"1.3"}
```

### 3.3 真机全链路（`browserskill_probe/live_run.py`）

```powershell
C:\Python313\python.exe experiments\phase0\browserskill_probe\live_run.py `
  --json-out docs\poc\evidence\p0-t17-live-run.json
# exit=0
# evidence written: docs/poc/evidence/p0-t17-live-run.json (7683 bytes)
```

| 步骤 | 真实结果 |
| --- | --- |
| `session start` | `session_id=ivff`，`agent_window_id=270464464`，`interaction={borrow_confirmation:always, request_help:enabled}` |
| `tab create` | `tab_id=270464479`，`url=https://example.com` |
| `tab list --scope agent` | 两个 agent tab：`about:blank`（inactive）与 `https://example.com/`（active） |
| `snapshot` | aria 树含 `RootWebArea "Example Domain"` 与其正文 |
| `evaluate` | `{title:"Example Domain", href:"https://example.com/"}` |
| `screenshot` | **56,940 B**，`sha256=BE95C974110DCA9F9E0755A019A1B22C8EB911F8731EBB2EFF7104D8CA599476`（1903×800 PNG） |
| `console` | 缓冲区 1 条 `net::ERR_FAILED (chrome-extension://invalid/)` |
| `tab close` | `{"tab_id": 270464479}` |
| `session stop` | `{"stopped":["ivff"], "failed":[], "return_failures":[]}` |

证据文件：`docs/poc/evidence/p0-t17-live-run.json`（8,693 B，`sha256=27C40FCA6F08FF36944727BB6ABA489224E8960B9CB89DAB37052F61BB9D694A`）。

### 3.4 作用域隔离（真机）

同一时刻 `tab list --scope user` 只返回用户自己的窗口（hao123、`chrome://extensions` 等），
`--scope agent` 只返回 Agent Window 内的两个 tab。探针在 `execute()` 前用它校验
`tab_close/tab_select` 的目标必须落在 `agent` 集合内，越界直接 `AdapterRefused`。

## 4. 代码与测试

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `experiments/phase0/browserskill_probe/cli.py` | 约 175 | 定位 `bsk`、限时子进程、JSON 解析、递归脱敏 |
| `experiments/phase0/browserskill_probe/adapter.py` | 约 250 | 13 个动作 → argv 映射；allowlist / 秘密 / 作用域三道闸；会话生命周期 |
| `experiments/phase0/browserskill_probe/live_run.py` | 约 140 | 真机取证驱动（3.13 执行） |
| `tests/phase0/test_bsk_live.py` | 约 330 | 34 个单测（argv/闸门/传输层）+ 1 个 `BSK_LIVE=1` 真机用例 |

单测实跑（无 daemon 依赖）：

```powershell
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml `
  tests/phase0/test_bsk_live.py -q
# 34 passed, 1 skipped
uv run --project experiments/phase0 ruff check experiments/phase0/browserskill_probe `
  tests/phase0/test_bsk_live.py
# All checks passed!
```

## 5. 现场发现（写入约束）

1. **CPython 3.12 与 daemon IPC 不兼容**：uv 托管的 3.12.13 打开命名管道即 `PermissionError 13`
   （ctypes `CreateFileW` → `last_error=5`）；由它派生的 `bsk.exe` 也拿不到管道
   （`connect IPC named pipe ... 拒绝访问 (os error 5)`）。同一 `bsk.exe` 由系统 CPython 3.13.3
   派生则全部正常。→ 真机取证必须用 3.13；探针本身不直连 IPC，避免把这个限制带进产品路径。
2. **session 由 daemon 持有、跨进程可见，但会被 `--daemon-idle` 或用户关闭 Agent Window 回收**；
   因此所有动作必须在同一驱动脚本内连续完成（`live_run.py` 即此形态）。
3. `bsk status` 的“自动拉起 daemon”在本机失败（`cannot start an independent Windows daemon;
   the host may prohibit Job Object breakaway ... os error 5`），必须
   `bsk daemon start --foreground --json --daemon-idle <dur>` 显式拉起。这属于部署事实，不进产品代码。
4. WeKnora 官方 `scripts/build_browserskill.sh` 平台白名单无 windows、daemon 硬编码 `/tmp`；
   但 BrowserSkill 自身的 Rust 工程支持 Windows（命名管道实现 + `tests/windows_parent_cancel.rs`），
   本机已用 `cargo build --locked --release -p bsk` 编出可用二进制。

## 6. 限域与未覆盖（不得对外声称已通过）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 31 个受控动作全覆盖 | **PARTIAL** | 适配层目前映射并单测了 **13/31**（会话/标签页/快照/截图/求值/控制台/导航/点击/填写）；其余 18 个（`observe`、`hover`、`press`、`wheel`、`select`、`get_html`、`network`、`tab_borrow`、`tab_return` 等）在映射前需逐一 `bsk <cmd> --help` 核对签名，现按契约直接 `ActionNotMapped` 拒绝 |
| 交互类动作真机验证 | **PARTIAL** | 真机跑了读类动作（snapshot/evaluate/screenshot/console/tab）；`click/fill/navigate` 仅单测 argv，未在真实页面上点过 |
| `owner` / 撤销 / 借还 | **NOT_RUN** | 属 P0-T18（`recovery.py`），本任务不覆盖；`tab_borrow/tab_return` 未映射 |
| 网络策略（域名/SSRF/速率） | **NOT_RUN** | 属 P0-T16；上游代码确认**完全没有**该策略，必须网关前置 |
| 多浏览器 / 多实例 | **NOT_RUN** | 本机只有 1 个 Chrome 实例（`dc8a279d`） |
| Linux/macOS 目标 | **NOT_RUN** | 只在 Windows 验证；CI 无 GUI 环境 |
| 审批面接入 | **NOT_RUN** | 本探针不实现审批；`C-04` 账本在 P0-T06，接线留 T21 |

## 7. 复现步骤

```powershell
# 1) daemon（一次性）
D:\PyKnora\bsk\bsk.exe daemon start --foreground --json --daemon-idle 60m

# 2) Chrome：chrome://extensions → 开发者模式 → 加载已解压的扩展程序
#    → D:\PyKnora\browser-skill-extension\0.3.1

# 3) 配对自检
D:\PyKnora\bsk\bsk.exe doctor --json      # 6 项 ok

# 4) 真机取证（必须 3.13）
C:\Python313\python.exe experiments\phase0\browserskill_probe\live_run.py `
  --json-out docs\poc\evidence\p0-t17-live-run.json

# 5) 回归
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml `
  tests/phase0/test_bsk_live.py -q
```
