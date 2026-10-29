# 修复分支记录：`fix/w5-t18`（P0-T18 BrowserSkill owner 唯一性与撤销/恢复）

> 本文件是**单任务修复分支**的记录，不是波次记录。波次记录见 `wave-w<N>-record.md`。
> 生成日期：2026-10-29（Asia/Shanghai）。

## 1. 声明块

```yaml
kind: fix
branch: fix/w5-t18
base: dev@34337c8
task: P0-T18
parallel_tasks: [P0-T18]
N: 1
serial_tasks: []
write_scope:
  P0-T18:
    - experiments/phase0/browserskill_probe/recovery.py
    - tests/phase0/test_bsk_recovery.py
    - docs/poc/evidence/poc05-recovery.md
resources:
  real_device: chrome instance_id=dc8a279d (独占，本分支内不派第二个 agent)
  bsk_daemon: pid=7608 ws=127.0.0.1:52800 protocol=1.3
  postgres: 不使用
  redis: 不使用
```

## 2. 为什么现在是单开修复分支，而不是回到 W5/W6/W7

| 时间点 | T18 状态 | 理由 |
| --- | --- | --- |
| W5/W6/W7 声明时 | `blocked_tasks: [P0-T18]` | T17 未解：无 pinned 上游产物、构建脚本无 windows 目标、本机无真设备 |
| 2026-10-28 `fix/w5-t17` | 阻塞解除 | 本机编译出 `bsk.exe`、装好扩展、真机配对跑通 6 次动作，T17 → PASS（限域） |
| 2026-10-29（本分支） | 单开修复 | 用户指示「单开」，不回到已收口的 W5/W6/W7 波次声明 |

链路：`T17（真协议/真设备）→ T18（owner/撤销/恢复）→ T28/T32`。T18 的两个前置 `T17`、`T06` 均 **PASS（限域）**。

## 3. 前置核验（2026-10-29 实跑）

```text
bsk doctor --json            exit 0   7 项全 ok（含 "extension connected: 1 browser(s) connected"）
bsk browsers --json          exit 0   instance_id=dc8a279d chrome 147.0.0.0 extension 0.3.1 protocol 1.3
bsk status --json            exit 0   daemon_version 0.3.1 / protocol_version 1.3 / ws_port 52800
Get-NetTCPConnection 52800   listeners=1
```

daemon 启动命令（空闲 60 分钟自动退出，重启用同一条）：

```powershell
Start-Process -FilePath 'D:\PyKnora\bsk\bsk.exe' `
  -ArgumentList @('daemon','start','--foreground','--json','--daemon-idle','60m') `
  -RedirectStandardOutput "$env:TEMP\bsk_daemon_28.log" -WindowStyle Hidden
```

## 4. CLI 语义实测（本分支设计依据）

| 步骤 | 命令 | 真实结果 |
| --- | --- | --- |
| 准备请求 | `bsk session request <expiry-ms>:<uuid> --prepare --json` | `{"state":"prepared","session":null}` exit 0 |
| 用请求启动 | `bsk session start --request-id <同 id> --no-focus --json` | 未 prepare 或 daemon 重启过 → `{"code":"invalid_params","message":"request is not prepared or the daemon restarted; ..."}` exit 1 |
| 认领 | `bsk session request <id> --claim` | 可认领态才成功；已取消 → `{"code":"cancelled"}` exit 2；不存在 → `{"code":"not_found"}` exit 1 |
| 撤销 | `bsk session request <id> --cancel --json` | `{"state":"closed"}` exit 0，之后同 id 无法再 start |
| 停会话 | `bsk session stop <id>` / `--all` | 返回 `{"stopped":[...],"failed":[],"returned_tab_ids":[...]}` |
| 标签页借还 | `bsk tab borrow|return --session <s> <tab_id>` | `borrow` 可 `--timeout <s>` 等人工确认 |

## 5. 约束

- 只改 §1 `write_scope` 列出的三个文件；`cli.py`、`adapter.py`、`pyproject.toml`、`uv.lock`、迁移、其它测试一律不动。
- 真机用例必须用 `BSK_LIVE=1` 门控；未设置时记 **NOT_RUN**，不得用 `skip` 冒充通过。
- 所有浏览器动作加 `--no-focus`（不抢用户焦点）；daemon 空闲会自行退出，恢复即重启并重跑 doctor。
- 不做 `git add/commit/push`（提交由 Lead 在门禁后询问用户）。

## 6. 门禁（本文件 §7 回填）

1. `ruff check experiments/phase0/browserskill_probe tests/phase0/test_bsk_recovery.py` → 期望 `All checks passed!`
2. `pytest tests/phase0/test_bsk_recovery.py`（单测）→ 期望全绿
3. `BSK_LIVE=1 pytest tests/phase0/test_bsk_recovery.py`（真机）→ 期望全绿；不可用则 NOT_RUN
4. 全量回归：`pytest tests/phase0`（不减少既有通过数）
5. 越界检查：`git status --porcelain` 只应出现上述三个文件（+ 既有 CRLF 噪声）
6. 秘密扫描：无 token/cookie/凭据入库

## 7. 结论

| 项 | 结果 |
| --- | --- |
| 交付文件 | `experiments/phase0/browserskill_probe/recovery.py`（900 行，sha256 `e9e40df7299784b7f8d0c7690c6247415c53d1e7de7389162be30139a3b9b5cb`）、`tests/phase0/test_bsk_recovery.py`（738 行，`6ed5b933ea2bb3a64904ed5d33aaae194ea6025fe28171cf52416061d8c47243`）、`docs/poc/evidence/poc05-recovery.md`（266 行，`340432e17712c7b86b8dc16d9d012d19effc03f2f2b1729919f46c2f68df1319`） |
| 单测 | `pytest tests/phase0/test_bsk_recovery.py` → **45 用例 / 0 failed / 0 error / 7 skipped**（全部为 `BSK_LIVE` 门控，带 `NOT_RUN` 理由），time 0.335s，exit 0 |
| 真机用例 | `BSK_LIVE=1` + CPython 3.13 → 45 用例 **44 passed / 0 failed / 1 skipped**（`tab borrow` 需人工确认，记 `NOT_RUN`），time 4.243s；原始 wire 证据见 `poc05-recovery.md` §6 |
| 全量回归 | `pytest tests/phase0` → **958 tests / 0 failed / 0 errors / 8 skipped**（均带 `NOT_RUN` 理由），junit `time=458.707`，exit 0；基线 913 → 958（+45 为本分支新增用例，未减少既有通过数） |
| 静态检查 | `ruff check experiments/phase0 tests/phase0` → `All checks passed!`，exit 0 |
| 秘密扫描 | 新增/修改的四个文件 0 命中（`password` / `api_key` / `token` / 私钥头 / `ghp_` / `sk-`） |
| 越界检查 | `git status --porcelain` 仅出现：三份新增交付 + 本记录 + 计划文件（状态刷新）；另有两处 tracked 证据被回归重生成（`poc01_owner_process_level.json`、`retrieval-baseline.json`），差异仅为时间戳/PID/`branch`/`head` 元数据，无语义断言变化，随本次提交一并刷新 |
| 验收三条 | ① owner epoch CAS：**PASS**（同一 `request_id` 双 owner → 单胜者，另一侧 `STALE_EPOCH`）② 撤销后不可执行：**PASS**（`revoke` 后任意 action → `SESSION_REVOKED`，下游 wire 调用数 0）③ 旧 epoch 迟到结果不写当前 session：**PASS**（`apply_result` 按 epoch 丢弃，当前 session 事实不变） |
| 判定 | **PASS（限域）** |

**限域与遗留（不隐瞒）**：

- 真机 1 项 `NOT_RUN`：`tab borrow` / `request_help` 依赖人工确认，CI 不可自动化；已给失败路径单测与人工步骤说明。
- `LeaseRegistry` 为**进程内**状态：多副本网关会各自 CAS（R2），需换持久化实现才算多实例安全。
- 网关重启 epoch 归零（R4）；`bsk` 自身无 owner/fencing 语义（R1），唯一性只能由网关单点保证——这条是设计约束，不是缺陷。
- `bsk` daemon 默认 `--daemon-idle` 会自动退出，CI 需显式拉起；本机需用 CPython ≥ 3.13 才能打开命名管道（uv 托管 3.12 报 `os error 5`）。

**与既有记录的关系**：T18 在 `wave-w6-record.md` 中记为 `⊘ BLOCKED`（当时依赖 T17 真设备/真协议）；T17 于 `fix/w5-t17` 真机跑通后阻塞解除，本分支补齐 T18。W6 的历史记录**不改写**，只在计划 §5 与 `docs/IMPLEMENTATION_STATUS.md` 标注最新状态。

## 8. 提交与合并结果（2026-10-29，真实 git 输出）

| 项 | 值 |
| --- | --- |
| feat 提交 | `cfdc99c` `feat(phase0): close T18 - browser session owner/revoke/recovery`（3 文件 +1904） |
| docs 提交 | `fd19151` `docs(phase0): record T18 gate result and plan status refresh`（5 文件 +168 −59） |
| 分支推送 | `git push -u origin fix/w5-t18` → `* [new branch] fix/w5-t18 -> fix/w5-t18`，exit 0；`refs/heads/fix/w5-t18 = fd191511c4da9c7c8054f49ba7e855ee2c7d2c1c` |
| 合入 dev | `git merge --no-ff fix/w5-t18` → `Merge made by the 'ort' strategy`，合并提交 `f35443c`（父提交 `34337c8` + `fd19151`），8 文件 +2072 −59 |
| dev 推送 | `git push origin dev` → `34337c8..f35443c  dev -> dev`，exit 0 |
| 合并后 dev | `f35443ccfa7c1de5a567b5397dc96eb23106be8a`（`refs/heads/dev` 实测一致） |
| 未合入项 | `feature/*` 与 `fix/w5-t17`、`fix/w7r-t05-t09` 仍保留，未删除、未改写历史 |

**证据文件说明**：`poc01_owner_process_level.json`、`retrieval-baseline.json` 为回归跑动时由用例重生成（时间戳/PID/`branch`/`head` 元数据变化），随 `fd19151` 一并刷新，断言语义未变。
