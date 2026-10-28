# poc01-owner 修订 01：T05 判定重述与限域准入

- 日期：2026-10-28
- 修订对象：`docs/poc/evidence/poc01-owner.md`（原文不改，本文件为追加式修订）
- 依据：原文 §5.2/§6.1-E/§7/§7.1；本修订配套证据 `poc01_owner_process_level.json`（T05R）；ADR-0006（提议）
- 口径：本修订只使用判定字符串 `PASS（限域：单活）`

## 1. 判定重述

P0-T05 判定由原记录的 `PARTIAL（单活）` 正式化为 **`PASS（限域：单活）`**。

限域的含义是：替身（fenced）路径在锁失效、owner 已推进的场景下仍拒绝过期 epoch（`poc01-owner.md` :219），因此 P0 可以准入“同一 run 单活 runner”这一形态；而原生 `PostgresSaver` 的写入路径在锁释放后仍被接受（:179、:186，本修订补进程级证据），所以不能宣称“checkpoint 写入已被跨进程 fence”。两项事实缺一不可，结论只能是单活。

原 PARTIAL 记录中的多实例问题不因本修订而消失，见 §4。

## 2. 原文行号依据（逐条）

- `poc01-owner.md` :179 —— 锁 backend 被 terminate 后，`put_after_lock_loss = ok`（连接级证据：原生写入仍成功）。
- `poc01-owner.md` :186 —— 结论句“锁释放不 fence 原生 saver（实测成立）”。
- `poc01-owner.md` :187 —— saver 连接死亡时 `put` 抛 `OperationalError`，不会静默续写；风险边界是“连接还活着”。
- `poc01-owner.md` :191–192 —— 接管确认的正确判据是“已持有锁”，不是 `pg_locks` 快照。
- `poc01-owner.md` :213 —— §6.1-E 明说缺口：未测“旧进程未死但失锁后仍在写”的进程级存活证据。
- `poc01-owner.md` :219 —— item1 记 `PARTIAL（单活）`：替身路径 0/50 被拒、对照组 50/50 成功、原生路径 FAIL。
- `poc01-owner.md` :220–223 —— item2–5 PASS（epoch CAS、恢复一致性、版本不兼容、探测无 sleep）。
- `poc01-owner.md` :227 —— 原文明确判定。
- `poc01-owner.md` :229 —— 唯一可上线形态：单活 runner；并建议做 ADR-0001 限域修订。
- `poc01-owner.md` :230 —— 若保留多实例方向，须把所有 checkpoint 写入收归宿主持锁代理，原文标注“未验证，属新增 PoC”。

## 3. 本修订新增的进程级证据（T05R）

- 子进程（真实 Python 进程）import `checkpoint_probe`，自建 distributor（advisory lock + owner claim）与**原生** `PostgresSaver`（独立连接），报告自身 PID 与两个 backend pid 后阻塞读 stdin；父进程用 `SELECT pg_terminate_backend(<lock backend>)` 只杀锁连接，**不杀写进程**。
- 锁释放判据：父进程的阻塞 `pg_advisory_lock` 成功返回（判据只看这个事件，不采信 `pg_locks` 快照）；`holds_lock()` 仅用于确认该锁记录已落在本连接名下，不用来判断“别人是否已释放”。随后 claim 把 owner_epoch 从 1 推进到 2。
- 原生路径结果：`accepted=true`、`code=ACCEPTED`，checkpoint 行数 1→2，且按 `checkpoint_id` 在 `p0_t05.checkpoints` 查回该行。
- 同进程替身路径（同一故障场景）：`accepted=false`、`code=STALE_OWNER_EPOCH`、detail `token epoch=1 已过期，当前=2`。
- 本次真实读数：reported_pid=19588（Popen 启动器 16784，Windows venv 转启动器）、lock backend=20843、saver backend=20847、接管后 lock backend=20848、阻塞拿锁 4.19ms、子进程退出码 0。
- 测试函数：`test_process_level_native_saver_accepts_write_after_lock_loss_is_fail_evidence`。它是**负面证据**用例：用例“通过”只表示“已实测并记录原生写入仍被接受”，不表示原生路径安全。
- 证据文件：`docs/poc/evidence/poc01_owner_process_level.json`（含 PID、backend pid、accepted、时间戳、命令、退出码与 `gate_run`）。

真实命令与退出码：

```text
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_checkpoint_owner.py
# 输出：12 passed in 18.52s
# exit 0

uv run --project experiments/phase0 ruff check tests/phase0
# 输出：All checks passed!
# exit 0
```

## 4. DoD 中仍为 PARTIAL 的项

DoD 第 3 项“多实例问题的明确结论”仍为 **PARTIAL**：T05 只证明了“advisory lock 不能作为跨进程写入授权、不能自动接管”，并没有验证 `poc01-owner.md` :230 所说的“宿主持锁代理”形态，也没有多写者故障注入证据。本修订不改变该项状态，也不把多实例形态写成可用。

## 5. G-P0 限域准入条件

- G-P0-1 单活部署：同一 run 同一时刻至多一个 runner 进程，由部署/编排保证，不靠锁自证。
- G-P0-2 接管流程：旧 runner 先停止（或由编排确认退出）→ 新 runner 阻塞拿到 advisory lock → claim 新 owner epoch；禁止自动接管。
- G-P0-3 授权口径：任何门禁、摘要、评审都不得把“持有锁”当作“有权写 checkpoint”；锁只用于互斥与接管排序。
- G-P0-4 观测：owner_epoch 跳变、attempt 账本中 `STALE_OWNER_EPOCH`/连接中断要可告警，用于发现双启动。
- G-P0-5 可回归：本修订配套测试与证据文件在 P0 期间保持可重跑；重跑只更新 `gate_run`，判定口径不变。

以上五项同时满足，T05 才维持 `PASS（限域：单活）`；任一项不满足则退回 PARTIAL。

## 6. G-FULL 不可宣称的理由

- 原生 saver 写入路径不校验 owner token（:186 加本修订的进程级证据），锁释放后写入仍被接受。
- 自动接管没有验证，且现有机制在旧进程存活时会产生双写。
- `poc01-owner.md` :230 提出的宿主持锁代理没有实现、没有故障注入、没有性能证据。
- 因此 G-FULL（多写者安全横扩）缺三类证据：写入处 fence、接管协议、代理/服务级故障模型；不能宣称。

## 7. 取代与补充声明

- **取代**：`poc01-owner.md` §7 item1 的判定措辞，由 `PARTIAL（单活）` 更新为 `PASS（限域：单活）`；依据不变，仅正式化。
- **补充**：`poc01-owner.md` §6.1-E 的缺口已由本修订补齐（进程存活 + 锁已释放 + 原生仍写 + 替身被拒）。
- **补充**：`poc01-owner.md` §7.1 :229 的建议已落实为 `docs/adr/0006_Single_Active_Checkpoint_Runner.md`（状态：提议）；ADR-0001 正文不改。
- **不取代**：原文 §5 的连接级时序与读数、§6 契约表、§8 交付哈希、§9 复用建议仍以原文为准。
- 本修订、ADR-0006、进程级证据 JSON 三份文件口径一致，判定字符串统一为 `PASS（限域：单活）`。
