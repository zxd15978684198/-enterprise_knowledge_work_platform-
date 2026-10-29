# P0-T18 证据：BrowserSkill owner 唯一性、撤销与迟到结果丢弃

> 波次 W5 / 任务 P0-T18。本文件是 Backlog `docs/poc/03_Phase0_Task_Backlog.md`（P0-T18）与
> `docs/poc/07_Contract_Closure.md` C-05 要求的 DoD 交付物：「D0 + 原始 wire 证据 + 是否支持多 owner 的明确结论」。
> 契约依据：`docs/tdd/05_Browser_Adapter.md` §3（BrowserSession/epoch）、§5（owner 路由与人工接管）、§6（网络与隐私）；
> 上游协议事实来源：`docs/poc/evidence/browserskill_protocol_manifest.md`（T14，commit `662abaf9`）。

## 1. 任务声明

```yaml
wave: W5
task: P0-T18
agent: w1-t14-browserskill
branch: fix/w5-t18
head: 34337c8
started_at: 2026-10-29T09:32+08:00
write_scope:
  - experiments/phase0/browserskill_probe/recovery.py
  - tests/phase0/test_bsk_recovery.py
  - docs/poc/evidence/poc05-recovery.md
deps: [P0-T14（协议清单）, P0-T17（真机链路）]
reused_by: [P0-T19（Browser port 接线）, P0-T25（多实例/多 owner 部署）]
```

## 2. 已知约束

| 约束 | 内容 |
| --- | --- |
| 写权 | 只写上面三个文件；`cli.py`、`adapter.py`、`live_run.py`、`pyproject.toml`、`uv.lock`、迁移、其它测试一律只读。 |
| 真机礼仪 | 所有 `bsk session start` 一律带 `--no-focus`，不抢用户窗口焦点；结束后 `session stop`，`session list` 必须回到 `[]`。 |
| 唯一性边界 | 本任务是**网关侧** owner 唯一性（CAS + fencing token）。daemon 本身不提供 owner/epoch 语义（见 §4.3 实测），不得据此宣称 daemon 有 fencing。 |
| 秘密 | 不记录 token、cookie、页面正文；本文件只摘录 `session_id`、`tab_id`、错误码等元数据。 |
| Git | 波次内不做任何 `git add/commit/push/switch`。 |
| 测试纪律 | 不用 `mock` 假装真机；真机用例以 `BSK_LIVE=1` 门控，未满足时记 **NOT_RUN**，不写 `xfail` 冒充通过。 |

## 3. 被测命题与设计

C-05 与 P0-T18 卡片把问题拆成两个必须分开回答的命题：

| 命题 | 实验载体 | 结论位置 |
| --- | --- | --- |
| P1：如果唯一的写入口径由网关持有，「epoch CAS」能否保证两个 owner 中**恰好一个**生效，并让旧 epoch 的写入被拒？ | `LeaseRegistry`（纯内存、线程安全）+ `test_bsk_recovery.py` 单测 | §5.1 |
| P2：daemon / wire 层是否自带 owner 或 fencing（即不靠 CAS 也能防双 owner）？ | 真机 `bsk` 原始输出 | §4.3、§6 |

### 3.1 epoch 语义（`recovery.py:244` 起）

| 操作 | 前置 | 成功语义 | 失败码 |
| --- | --- | --- | --- |
| `claim(subject, expected_epoch, owner_id, ttl)` | `expected_epoch == current` 且无他人活动租约 | epoch = current + 1，旧租约被覆盖 | `STALE_EPOCH`（落后）、`EPOCH_CONFLICT`（超前或他人持有）、`SESSION_REVOKED` |
| `takeover(subject, expected_epoch, owner_id, ttl)` | 同上（允许旧租约仍活着） | epoch + 1，旧 owner 立刻只能拿 `STALE_EPOCH` | 同上 |
| `assert_current(subject, epoch)` | — | 返回活动租约 | `STALE_EPOCH` / `EPOCH_CONFLICT` / `LEASE_EXPIRED` / `SESSION_REVOKED` |
| `revoke(subject, reason)` | — | 置 revoked、epoch + 1、清租约；**幂等** | 之后任意 epoch 均 `SESSION_REVOKED` |
| `release(subject, epoch)` | 必须是当前 epoch 持有者 | 归还租约但不推进 epoch | `STALE_EPOCH` |

错误码常量与 `browser_contracts` 的 `BROWSER_INCOMPATIBLE`/`CAPABILITY_UNSUPPORTED` 分属两层：
本模块产出的是 **owner/epoch 层** 的 4 个稳定码（`recovery.py:60-80`），不冒充浏览器动作错误码。

### 3.2 迟到结果的处理（`apply_result`）

`apply_result(session_id, epoch, action, result)` 不抛异常，固定返回
`{"applied": bool, "dropped_reason": <code>|None, ...}`：
`applied=False` 时记录只进 `dropped` 账本，**当前会话状态零改动**；原因码就是 `assert_current` 的错误码
（`STALE_EPOCH` / `SESSION_REVOKED` / `LEASE_EXPIRED` / `EPOCH_CONFLICT`）。

## 4. 真机原始证据（wire 层）

### 4.1 环境

```text
bsk 0.3.1（D:\PyKnora\bsk\bsk.exe）
bsk doctor: daemon running pid 7608 at ws://127.0.0.1:52800
            (sock \\.\pipe\bsk-daemon-e974436b93237db7)
            protocol compatible: daemon protocol 1.3 (app 0.3.1)
            extension connected: 1 browser(s) connected
Chrome instance_id = dc8a279d，extension 0.3.1
采集时间 2026-10-29T09:44:27+08:00
```

### 4.2 recoverable start 的正常与幂等路径

```text
=== A1 prepare 1793238807368:b81b11ed-… | exit=0
{ "cleanup_error": null, "request_id": "1793238807368:b81b11ed-…",
  "session": null, "state": "prepared" }

=== A2 start | exit=0
{ "agent_window_id": 270465317, "browser_instance_id": "dc8a279d",
  "interaction": { "borrow_confirmation": "always", "request_help": "enabled" },
  "session_id": "zhyv" }

=== A3 start-again（同一 request_id） | exit=0
{ … "session_id": "zhyv" }          # 与 A2 完全相同
=== A3b same_session=True session=zhyv
```

**结论（A3）**：对同一个 `request_id` 重复 `session start` 是**幂等**的——返回同一 session 且 exit 0。
daemon 没有「已消费」栅栏，也不校验调用者身份。

### 4.3 claim 语义（不是 fencing）

```text
=== A4 claim-active（request 处于 active） | exit=0
{ "request_id": "1793238807368:…", "state": "active",
  "session": { "session_id": "zhyv", "agent_window_id": 270465317, … } }

=== C2 claim-prepared（request 处于 prepared） | exit=2
{ "code": "cancelled", "message": "start request cannot be claimed", "exit_code": 2 }

=== C3 claim-unknown（不存在的 request_id） | exit=1
{ "code": "not_found", "message": "start request not found", "exit_code": 1 }

=== B3 claim-cancelled | exit=2
{ "code": "cancelled", "message": "start request cannot be claimed", "exit_code": 2 }
```

**结论（A4/C2/C3/B3）**：`--claim` 只是「读一次请求并把 session 交回」，**不排他、不推进 epoch**；
prepared 态根本不可认领，unknown 走 `not_found`。因此 owner 唯一性不可能由 wire 层提供。

### 4.4 撤销与「撤销后不可执行」

```text
=== A6 stop | exit=0
{ "failed": [], "return_failures": [], "returned_tab_ids": [], "stopped": ["zhyv"] }

=== A7 停止后仍按旧 session 发动作 | exit=1
{ "code": "not_found", "message": "session not registered or already stopped", "exit_code": 1 }

=== A8 停止后再 start 同一 request | exit=2
{ "code": "cancelled", "message": "start request cancelled; inspect session request for cleanup status",
  "exit_code": 2 }

=== B2 cancel（未消费的 request） | exit=0
{ "request_id": "1793238809431:e87122cf-…", "session": null, "state": "closed" }

=== B4 cancel 之后 start | exit=2
{ "code": "cancelled", "message": "start request cancelled; …", "exit_code": 2 }
```

**结论**：wire 侧确实会拒绝已停止/已取消的会话，但**拒绝发生在动作时**（`not_found` exit 1），
不是「撤销即失能」的本地保证；而且它**不区分新旧 owner**——任何能拿到 session_id 的进程都能继续发动作，
直到 stop 生效。这就是本任务必须加本地闸（`assert_current` 先于派发）的原因。

### 4.5 借标签页需要人工确认（不可自动化）

```text
=== D2 tab list --scope user | exit=0
{ "tabs": [ { "tab_id": 270465135, "title": "Go | OpenCode Console",
              "url": "https://opencode.ai/console/…", "window_id": 270465134,
              "active": true, "scope": "user" } ] }

=== D4 tab borrow --timeout 3 | exit=4
{ "code": "timeout", "message": "Timed out waiting for tab borrow confirmation",
  "exit_code": 4, "data": { "reason": "confirmation_timeout" } }

=== D5 tab return（借入未完成） | exit=1
{ "code": "not_found", "message": "tab_return: tab 270465135 is not borrowed by this session" }

=== E1 session list（收尾） | exit=0
[]
```

## 5. 单测证据（不依赖 daemon）

### 5.1 命令与结果

```powershell
cd E:\project_month\pyknora
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_bsk_recovery.py -q
# EXIT=0；45 个用例：38 passed + 7 skipped（7 个真机用例门控未开）
```

计数由同口径 JUnit 复核（3.12 venv 写 `%TEMP%` 遇 `PermissionError`，改用 3.13 生成 XML）：

```powershell
C:\Python313\python.exe -m pytest -c experiments/phase0/pyproject.toml tests/phase0/test_bsk_recovery.py `
  -q --junitxml C:\Users\Administrator\AppData\Local\Temp\p18_unit.xml
# tests=45 failures=0 errors=0 skipped=7 time=0.098  EXIT=0
```

### 5.2 用例 → 命题映射

| 组 | 用例（节选） | 证明了什么 |
| --- | --- | --- |
| epoch CAS | `test_first_claim_starts_at_epoch_one_and_is_alive`、`test_claim_with_stale_expected_epoch_is_rejected`、`test_claim_with_epoch_ahead_of_current_is_conflict` | 首次 claim = epoch 1；落后 → `STALE_EPOCH`，超前 → `EPOCH_CONFLICT` |
| 并发单胜者 | `test_concurrent_claims_have_exactly_one_winner`（8 线程同抢 `expected_epoch=0`） | **恰好 1 个赢家**、7 个输家码 ⊆ {`EPOCH_CONFLICT`,`STALE_EPOCH`}、终态 epoch=1 |
| 双 owner | `test_second_owner_cannot_claim_a_live_lease`、`test_recoverable_start_second_owner_is_rejected_before_wire` | 第二 owner 在 CAS 处即被拒，**`stub.calls == []`**（零 wire 调用） |
| 接管 | `test_takeover_fences_the_old_owner`、`test_takeover_requires_cas_hit` | 接管后旧 epoch 一律 `StaleEpoch`；接管本身也要 CAS 命中 |
| TTL | `test_expired_lease_requires_takeover_not_silent_reuse`、`test_claim_after_expiry_by_another_owner_succeeds_and_old_epoch_is_stale` | 过期后必须显式 takeover，静默复用被拒 |
| 撤销 | `test_revoke_blocks_every_epoch_afterwards`、`test_revoke_is_idempotent`、`test_execute_is_blocked_after_revoke_and_makes_no_wire_call` | 撤销后 claim/takeover/assert 全拒；`execute` 拒发且 **0 次 wire** |
| 迟到结果 | `test_apply_result_drops_stale_epoch`、`…drops_after_revoke`、`…drops_when_lease_expired`、`…drops_future_epoch_as_conflict` | 四种原因分别落 `dropped_reason`，`applied == []` |
| 失败回退 | `test_recoverable_start_maps_cancelled_to_start_rejected_and_releases_lease` | start 被拒时释放租约→ epoch 保持 1、租约为空，可重试 |
| 借出清理 | `test_revoke_reports_wire_stop_failure_without_hiding_it` | stop 失败不掩盖：`wire_error.exit_code`/`stdout` 原样进记录 |
| 契约一致 | `test_supported_recovery_actions_are_contract_subset` | 本模块只派发 `snapshot`/`tab_list`，是 `BrowserAction` 子集 |

## 6. 真机证据（`BSK_LIVE=1` + CPython 3.13）

`experiments/phase0` 的 uv 托管解释器是 3.12，派生 `bsk.exe` 时拿不到 daemon 命名管道（T17 已记录 `os error 5`），
故真机层用系统 3.13 运行、被测代码与 3.12 完全一致（纯 Python、无版本分支）。

```powershell
$env:BSK_LIVE = "1"
C:\Python313\python.exe -m pytest -c experiments/phase0/pyproject.toml tests/phase0/test_bsk_recovery.py `
  -q --junitxml C:\Users\Administrator\AppData\Local\Temp\p18_live.xml
# tests=45 failures=0 errors=0 skipped=1 time=4.243  EXIT=0 → 44 passed, 1 skipped
```

| 真机用例 | 真实结果 |
| --- | --- |
| `test_live_recoverable_start_executes_and_stop_blocks_old_session` | prepare→start（epoch 1）→ snapshot 成功；stop 后同 session snapshot 得 `not_found` exit 1 |
| `test_live_double_owner_same_request_single_winner` | owner-b 在 CAS 处 `StaleEpoch`；**wire 复跑 start 仍返回同一 session（A3）**，证明唯一性只能来自 CAS |
| `test_live_cancel_then_start_is_rejected` | cancel → `closed`；随后 start 得 `cancelled` exit 2，租约已释放 |
| `test_live_claim_semantics_are_not_fencing` | prepared→claim `cancelled` exit 2；unknown→`not_found` exit 1；active→exit 0 带回 session |
| `test_live_takeover_drops_old_epoch_result` | takeover 后旧 epoch 结果 `applied=False/dropped_reason=STALE_EPOCH`；新 epoch `applied=True` |
| `test_live_revoke_makes_actions_impossible` | 本地 `SessionRevoked`（0 wire）+ wire `not_found` 双证据 |
| `test_live_tab_borrow_and_return` | **NOT_RUN**：`tab borrow --timeout 3` 返回 `timeout/confirmation_timeout`（exit 4），用户未确认 |

## 7. 验收逐条核对（Backlog P0-T18）

| 验收项 | 结论 | 证据 |
| --- | --- | --- |
| owner epoch CAS：两个 owner 抢同一会话**恰好一个**生效 | **PASS** | §5.2 并发组（8 线程 1 胜 7 负）+ `test_live_double_owner…`；单进程语义严格，跨进程见 §9 R2 |
| 旧 epoch 写被拒 | **PASS** | `test_claim_with_stale_expected_epoch_is_rejected`、`test_takeover_fences_the_old_owner`、§4.3 证明 wire 不代劳 |
| 撤销后不可执行（本地闸 + 真实 wire 双证据） | **PASS** | `test_execute_is_blocked_after_revoke…`（0 wire）+ §4.4 A7 `not_found` + `test_live_revoke…` |
| 旧 epoch 迟到结果不写当前 session | **PASS** | §5.2 迟到组 4 例 + `test_live_takeover_drops_old_epoch_result` |
| 单测全绿 | **PASS** | 45 用例，0 failed / 0 error（38 passed + 7 门控 skipped） |
| live 用例在 `BSK_LIVE=1` 下全绿 | **PASS（含 1 NOT_RUN）** | 44 passed / 0 failed；`tab borrow` 因需人工确认记 NOT_RUN |

## 8. NOT_RUN 清单

| 项 | 原因 | 复现命令 |
| --- | --- | --- |
| `tab borrow`/`tab return` 完整闭环 | 借入需用户在 Chrome 中点击确认，`--timeout 3` 内无人确认 → `timeout/confirmation_timeout` exit 4（§4.5 已取证该失败路径） | `bsk tab borrow --session <s> <tab_id> --timeout 30`（需人工在场） |
| **跨进程**双 owner | 本任务实现的是单进程 CAS；跨进程唯一性需要共享存储（DB/Redis）或单点网关，未纳入 3 文件写权 | —（见 §9 R2） |
| 3.12 下真机 | uv 托管解释器（3.12）派生 `bsk.exe` 时报 `os error 5`（T17 同源）；用 3.13 跑，代码相同 | — |

## 9. 残余风险

| ID | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | daemon 无 owner 语义：任何进程拿到 `session_id` 都能发动作（§4.3/§4.4），wire 只在 stop 后拒绝 | 多写者部署会互相踩踏 | 唯一入口放在网关；`session_id` 不出网关 |
| R2 | `LeaseRegistry` 是进程内状态，多副本网关各自 CAS → 仍可能双 owner | 水平扩容后唯一性失效 | T19/T25 需把 epoch 落到共享存储（或单写者路由 + 粘性） |
| R3 | `stop` 后 start 的 wire 错误是 `cancelled`(exit 2) 而非 `invalid_params` | 按 code 而非文案判断即可；已写进契约测试 | 契约层只认 code |
| R4 | `monotonic` 时钟 + 内存租约：网关重启后 epoch 归零，旧 owner 的迟到结果在重启后可能被误接受 | 崩溃恢复场景 | 持久化 epoch（同 R2） |
| R5 | `request_help`/`tab borrow` 依赖人工确认，CI 不可自动化 | 真机回归只能半自动 | 保留人工步骤说明 + 失败路径单测 |

## 10. 交付文件

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/browserskill_probe/recovery.py` | 900 | `e9e40df7299784b7f8d0c7690c6247415c53d1e7de7389162be30139a3b9b5cb` |
| `tests/phase0/test_bsk_recovery.py` | 738 | `6ed5b933ea2bb3a64904ed5d33aaae194ea6025fe28171cf52416061d8c47243` |
| `docs/poc/evidence/poc05-recovery.md` | 本文件 | — |

```text
ruff check experiments/phase0/browserskill_probe tests/phase0/test_bsk_recovery.py
→ All checks passed!  (EXIT=0)
```

## 11. 对后续任务的接口建议

- **P0-T19（Browser port 接线）**：`execute()` 必须先 `assert_current(epoch)` 再落到 transport；
  迟到结果统一走 `apply_result()`，不要各自实现「丢弃」。
- **P0-T25（多实例）**：把 `LeaseRegistry` 换成同一接口的持久化实现（`claim/takeover/revoke/assert_current` 签名不变），
  测试文件里共享 registry 的双实例用法可直接复用。
- **P0-T17（真配对）补测**：`tab borrow` 的人工确认路径建议录一段人工操作 + 一次 `--timeout 30` 成功样例；
  当前只覆盖了超时失败分支。
