# ADR-0006：P0 单活 Checkpoint Runner

- 日期：2026-10-28
- 状态：**提议**（依赖 ADR-0001；评审通过前不改其正文）
- 依据：`poc01-owner.md` §5.2/§6.1-E/§7/§7.1；`poc01_owner_process_level.json`（T05R）；C-03；TDD 01 §6.4、TDD 04 §7–9

## 背景

P0-T05 实测到原生 `PostgresSaver` 的写入不校验 advisory lock 归属：锁 backend 被终止后原生 put 仍然成功（`poc01-owner.md` :179、:186）。T05R 补齐进程级证据：写进程存活、锁已释放（阻塞拿锁成功且 owner_epoch 推进到 2）之后，原生 put 仍返回 `ACCEPTED`；同一进程内的替身 fenced 路径则被 `STALE_OWNER_EPOCH` 拒绝。结论是锁只能做互斥，不能做写入授权，因此不能把“谁持有锁”当作“谁有权写”。

## 决策提议

P0 唯一可上线形态是**单活 checkpoint runner**：同一 run 同时最多一个写入进程，由部署与调度保证唯一性；runner 崩溃后由人工或编排显式重启，新 runner 必须先阻塞取得 advisory lock、再 claim 新的 owner epoch，**禁止自动接管**。多实例横扩不在 P0 范围；若未来要保留该方向，必须先把所有 checkpoint 写入收归宿主持锁代理，并另立 PoC 验证，本 ADR 不批准。T05 的判定口径固定为 `PASS（限域：单活）`。

## 替代方案

1. 自动接管：检测锁丢失后自愈。已实测原生 saver 在失锁后继续写，自动接管会产生双写，拒绝。
2. 多 runner 共享同一 thread 并靠 advisory lock 互斥：锁不约束原生 saver 的写入路径，等于没有 fence，拒绝。
3. 宿主持锁代理统一写入：可以恢复单写者语义，但需要新增服务、故障模型与性能证据，T05 未验证，留待独立 PoC 评审。

## 后果与验证条件

后果：单 run 吞吐上限为单 runner；滚动升级与故障恢复必须先停旧进程再启新进程，恢复时延可观测；`owner_epoch` 与 attempt 账本继续用于审计和诊断，但不承担跨进程写入授权。验证条件：(1) 接管判据只认“阻塞 `pg_advisory_lock` 成功返回”，不用 `pg_locks` 快照；(2) 旧进程仍存活时原生写入一律记为缺口，不得宣称已 fence；(3) 替身 fenced 路径必须拒绝过期 epoch；(4) 进程级证据每次重跑都落盘，并与本 ADR 的口径一致。失败时降级为“停旧进程后重启”，不引入自动接管。

实现契约：[01](../tdd/01_System_Architecture.md) §6.4、[04](../tdd/04_LangGraph_Workflows.md) §7–9、[07](../poc/07_Contract_Closure.md) C-03。替代关系：补充并限域修订 ADR-0001 的运行时边界，不改其正文。
