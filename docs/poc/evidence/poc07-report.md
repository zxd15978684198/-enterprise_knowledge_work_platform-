# PoC-07 验收报告：DAG 规划与状态协调（T22 / T23 / T24）

| 项 | 值 |
| --- | --- |
| PoC | **PoC-07**（研究 H3，PRD R-09/R-14、F-10、AC-E01/AC-E09） |
| 覆盖任务 | `P0-T22` 结构化 Planner 与无环校验、`P0-T23` ready 调度与依赖传播、`P0-T24` Evaluator 与有界重规划 |
| 判定 | **PASS（限域）** |
| 判定依据 | 三任务各自 `PASS（限域）`（`wave-w4-record.md:159`、`wave-w5-record.md:142`、`wave-w6-record.md:68`）+ 本波次独立复跑 `101 passed in 15.16s` |
| 报告性质 | **聚合验收报告**（不改写任何历史波次判定，只做「可复算性核对 + 残限汇总」）；不是新实验 |
| 生成波次 | W16（声明块 `wave-w16-record.md` §1，N=1，Lead 自执行） |
| 基线 | `dev@f9a7455`，分支 `feature/w16-poc07-report` |
| 记录时间 | 2026-10-29（Asia/Shanghai） |

## 1. 为什么需要这份报告

`Phase0_Full_Gate_Review.md:31` 把 `poc07-report.md` 登记为**已知输入缺口**：PoC-07 的结论此前只散落在三份波次记录的表格行里，
没有一份能独立回答「这条 PoC 的验收条款逐条落在哪里、哪些边界没覆盖」的文件。G-FULL 的 PoC-08（Research 全链）反过来依赖
PoC-07 的关键项，缺口不闭合会让 PoC-08 的依赖判定悬空。

本报告不新增实验，只做三件事：**①把 PoC-07 的验收条款逐条对到用例；②把残限项集中列清；③核对证据本身能不能复算**。

## 2. 覆盖范围与证据来源

| 任务 | 交付文件（当前 `HEAD`） | 行数 | 原始波次 |
| --- | --- | --- | --- |
| `P0-T22` | `experiments/phase0/dag_contracts.py`、`experiments/phase0/planner_probe.py`、`tests/phase0/test_dag_validation.py` | 799 / 461 / 501 | W4（`wave-w4-record.md`） |
| `P0-T23` | `experiments/phase0/task_scheduler.py`、`experiments/phase0/task_executor.py`、`experiments/phase0/db/migrations/006_tasks.sql`、`tests/phase0/test_task_coordination.py` | 1787 / 261 / 257 / 1192 | W5（`wave-w5-record.md`，含 W6 缺口修复） |
| `P0-T24` | `experiments/phase0/evaluator_replanner.py`、`tests/phase0/test_replanning.py`、`docs/poc/evidence/poc02-replanning.md` | 1622 / 665 / 130 | W6（`wave-w6-record.md`） |

PoC-07 的验收条款原文在 `docs/poc/02_Phase0_PoC_Plan.md:108-115`（§10）；三任务的量化验收在
`docs/poc/03_Phase0_Task_Backlog.md:371`（T22）、`:388`（T23）、`:405`（T24）。

## 3. 命令与真实结果

### 3.1 本波次独立复跑（W16，2026-10-29）

```bash
# 仓库根执行；-o addopts= 清掉项目默认 addopts（否则 -q 会叠成 -qq 不出汇总行）
experiments/phase0/.venv/Scripts/python.exe -m pytest \
  -c experiments/phase0/pyproject.toml \
  tests/phase0/test_dag_validation.py \
  tests/phase0/test_task_coordination.py \
  tests/phase0/test_replanning.py \
  -q -o addopts=
# → 101 passed in 15.16s      (PYTEST_EXIT=0)
```

`101 = 51 (T22) + 28 (T23) + 22 (T24)`，与三份波次记录各自报的数字逐项相符。

### 3.2 原始波次记录的数字（不改写，原样引用）

| 任务 | 原始结果 | 位置 |
| --- | --- | --- |
| `P0-T22` | `51 passed`；32 节点 / **72 边** / 66 点；最长链 12 节点 11 跳 24 点；核心链 23 点与 `poc/04` 一致；28 个非法计划全拒；RAG 路径 planner 调用 0 | `wave-w4-record.md:91` |
| `P0-T23` | `28 passed（15.23s）`；未满足依赖执行 0；required 失败递归 `blocked`、optional 不阻断；双 scheduler **30 轮 × 2 线程 × 4 节点 = 120 条 claim 无重复**；旧 epoch 完成/引用均 `STALE_*` 拒绝；迁移重复 apply → `MIGRATION_ALREADY_APPLIED` | `wave-w5-record.md:88` |
| `P0-T24` | `22 passed`；超限 `decision=limit_exceeded`、`terminal_status=failed`、`replan_count == MAX_REPLANS == 2`、`revisions == [1,2,3]`；重规划只替换未完成且 `reused == ("A",)`；幂等三例（重复请求/重复评估/并发单赢家） | `wave-w6-record.md:68`、`poc02-replanning.md:74-86` |

## 4. 验收逐条对照

### 4.1 PoC-07 层面（`02_Phase0_PoC_Plan.md:113`）

| 验收条款 | 结果 | 直接证据 | 备注 |
| --- | --- | --- | --- |
| ≥20 个非法计划 **100% 在执行前拒绝** | 通过（超额） | `test_dag_validation.py` 28 个参数化非法计划（环 / 自依赖 / 缺节点 / 超规模 / 越界 Scope），首错可定位 | 28 > 20 |
| **100 轮** ready / revision 竞争，无提前执行、无双领取、无旧结果覆盖 | 通过 | 双 scheduler **100 轮** × 2 线程 × 4 节点 = **400 条 claim**，无重复；旧 epoch 完成/引用 `STALE_*` 拒绝 | W17/D3 按条款字面补测（原为 30 轮），见 §5-D3 |
| 已确认成功产物且 input/profile/policy 相同才复用，成本归父 run 一次 | 通过（限域） | `test_replan_replaces_only_unfinished_and_reuses_completed`：revision 2 节点集 `{B.r1, C}`、`A` 未重新登记、`reused == ("A",)` | 预算侧成本归父 run 已在真实 `p0_t20` 台账上验证（W17/D4，见 §5-D4） |
| 重规划到上限必须停止 | 通过 | `test_third_replan_hits_limit_and_terminates_failed`：`replan_count == 2`、终态 `failed`、`revisions == [1,2,3]` | — |
| Evaluator 缺证据不视通过；模型输出不能覆盖硬规则 | 通过 | `test_self_report_without_receipt_is_partial`（自述「已完成」无回执 → `partial` + `RECEIPT_MISSING`）；`test_success_is_decided_from_facts_and_receipts` | 判定输入是事实与回执，不是节点自述 |
| 普通 RAG 不调用 Planner | 通过 | T22：RAG 路径 planner 调用计数 0 | — |

### 4.2 任务层面量化验收

| 任务 | 条款 | 结果 |
| --- | --- | --- |
| T22 | 非法计划 100% 调度前拒绝 | 通过（28/28） |
| T22 | 合法九节点拓扑可复现 | 通过（`poc/04` 拓扑一致，72/72 边一致；曾误报 Mermaid 缺边，已澄清） |
| T22 | 普通 RAG 路径 Planner 调用 0 | 通过 |
| T23 | 未满足依赖提前执行 0 | 通过 |
| T23 | 每逻辑节点同 epoch 双领取 0 | 通过（400 claim 无重复；CAS 保证） |
| T23 | 旧事实不会被 checkpoint 覆盖 | 通过（`STALE_*` 拒绝；事实先落库，checkpoint 失败只留 `state_ref_pending` 且重入不重跑） |
| T23 | 所有 fixed 作业都走 Job port | 通过（真实 T08 Job port 集成） |
| T24 | revision 只有一活动指针 | 通过 |
| T24 | 已确认副作用重做 0 | 通过 |
| T24 | 未知用量预留不释放 | 通过（真实 `p0_t20` 台账，W17/D4；`StaticBudget` 守卫保留） |
| T24 | 最多 2 次重规划后停止 | 通过 |
| T24 | input/profile/policy 不匹配不复用 | 通过 |

## 5. 残限与不一致（必须随判定一起引用）

| 编号 | 项 | 事实 | 影响 |
| --- | --- | --- | --- |
| **D1** | 单活 runner 前提 | T05 为 `PARTIAL`（原生 checkpoint 已通，但「跨连接 ≠ 跨进程」），T23 的全部并发结论都建立在**单活调度器**上 | 多实例横向扩展未验证，不得据此宣称弹性 |
| **D2** | checkpoint 用端口替身 | T23 的 checkpoint 故障注入走端口替身，真实 saver 故障未注入 | 故障域结论限于替身语义 |
| **D3** | ~~30 轮 ≠ 100 轮~~ **已补测关闭（W17）** | 现为 **100 轮** × 2 线程 × 4 节点 = **400 条 claim**，无重复领取、无提前执行、无旧 epoch 覆盖（`test_task_coordination.py` 28 passed） | 条款字面已满足；证据 `wave-w17-record.md` §3.1 |
| **D4** | ~~T20 真实预算台账未接线~~ **已补测关闭（W17）** | `Replanner` 的 `budget=` 现可接 `LedgerBudget`（真实 `p0_t20`）：读数含在途预留、不足即 `budget_exhausted`+`blocked` 且账本三类计数不变、充足放行、跨工作区拒绝（`test_replanning_ledger.py` 5 passed） | 「成本归父 run 一次」已在真实台账成立；证据 `wave-w17-record.md` §3.2 |
| **D5** | 真实模型 plan `NOT_RUN` | T22 的 planner 用确定性 fixture，真实模型输出（JSON 截断/未知 kind/越权 workspace）未打真实模型 | 模型鲁棒性未证 |
| **D6** | 回放类注入 `NOT_RUN` | T24 的「切 revision 前后 kill」「旧 revision 结果晚到回放」「HTTP 层」三项记 `NOT_RUN` | 崩溃一致性未证 |
| **D7** | 对账为拉取式 | T23 对账不推模式，缺 `retry_wait` / `dead_letter`（属 T24 及后续） | 与 T08 死信语义需在集成期对齐 |

## 6. 证据可复算性核对（本波次实测）

复核时发现：**同一仓库里 sha256 有两种计算口径**，且 `core.autocrlf=true`（仓库无 `.gitattributes`）会让两者必然不同。

| 口径 | 含义 | 示例 |
| --- | --- | --- |
| **blob（LF）** | `git show HEAD:<path>` 的字节（提交内容） | `evaluator_replanner.py` 记录值 `8c866c52…` = blob 哈希，**匹配**；worktree 哈希 `f219e173…` 不匹配 |
| **worktree（CRLF）** | 磁盘上检出后的字节 | `006_tasks.sql` 记录值 `2b58f250…` = worktree 哈希，**匹配**；blob 哈希 `0d7458ca…` 不匹配（后者才是 `wave-w5-record.md` 记的值） |

实测统计（一次性只读脚本，扫描 `docs/**/*.md` 中「64 位十六进制 + 同行/上行可解析路径」的组合）：

```
match_blob_lf        89
match_worktree_crlf  27
drift                47
hash_without_path    82   # 路径不在同行/上行，未参与判定
```

结论三条：

1. 可解析的 163 条里，**116 条（71%）在某种口径下是匹配的**——也就是说 T31 §2.3 记的「119 处漂移」中相当一部分是 **CRLF/LF 口径差异，不是内容变更**。
2. 因此 **hash 相等不能直接当门禁判据**：必须先声明口径。建议统一为 **blob 口径**（`git show HEAD:<path> | sha256sum`），因为它在任何机器上一致。
3. 剩余 47 条真漂移需单独一案一查（多为后续波次合法修改后未回填），本波次**不代改**任何历史记录。

同时核对到两处**行数口径**差异，均可解释、非文件漂移：

| 文件 | 波次记录 | 当前实测 | 解释 |
| --- | --- | --- | --- |
| `task_scheduler.py` | 1613 | 1787 | 1613 是**非空行**（`Measure-Object -Line` 口径），1787 是总行（`Get-Content` / `wc -l`）；同一 commit `f0cf3e1` 内容一致 |
| `task_executor.py` / `006_tasks.sql` / `test_task_coordination.py` | 225 / 230 / 1080 | 261 / 257 / 1192 | 同上（空行分别为 36 / 27 / 112） |

## 7. 对门级判定的影响

**不改变任何门级判定。**

- `P0_Core_Gate_Review.md` 的 G1–G4 仍为 `PARTIAL`、G5 仍为 `NOT_RUN`；本报告只是把 PoC-07 的分散结论收成一份可审查文件。
- `Phase0_Full_Gate_Review.md` 中 PoC-07 的「输入缺口」一条可以关闭，但 PoC-07 的判定**不因此升级**（残限 D1/D2/D5/D6/D7 仍在；D3/D4 已于 W17 补测关闭，判定仍为 `PASS（限域）`）。
- PoC-08 对 PoC-07 的依赖可以按 `PASS（限域）` 引用，但必须连带引用 D1/D2/D5。

## 8. 复现

```bash
# 1) 三条任务测试（仓库根执行；需要 core profile 的 PG/Redis）
docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core up -d postgres redis
experiments/phase0/.venv/Scripts/python.exe -m pytest -c experiments/phase0/pyproject.toml \
  tests/phase0/test_dag_validation.py tests/phase0/test_task_coordination.py tests/phase0/test_replanning.py \
  -q -o addopts=     # 期望 101 passed

# 2) hash 口径复核（任取一个记录值）
git show HEAD:experiments/phase0/evaluator_replanner.py | sha256sum   # → 8c866c52…（blob 口径）
sha256sum experiments/phase0/evaluator_replanner.py                   # → f219e173…（CRLF 口径，不等）

# 3) 行数口径复核
git show f0cf3e1:experiments/phase0/task_scheduler.py | wc -l         # → 1787（总行）
```

## 9. 遗留

1. ~~D3（30 轮 → 100 轮）与 D4（真实预算台账接线）是可补测的两项~~ **已于 W17 补测关闭**：D3 提到 100 轮（400 claim）、D4 接真实 `p0_t20` 台账（5 例集成测试）。残余 D1/D2/D5/D6/D7 不变。
2. §6 的 47 条真漂移需要一次专门的「证据 hash 回填批次」，本波次只做定性说明。
3. 本报告未覆盖 P0-T22/T23/T24 之外的调度能力（如生产定时器、多站点连接器），不在 PoC-07 范围内。

## 10. 变更记录

| 日期 | 波次 | 改动 |
| --- | --- | --- |
| 2026-10-29 | W16 | 首次落盘：把 T22/T23/T24 的条款级结论收成一份可审查文件（判定 `PASS（限域）`，残限 D1–D7） |
| 2026-10-30 | W17 | **D3 关闭**：竞争实验按条款字面提到 100 轮（400 claim），`test_task_coordination.py` 28 passed；**D4 关闭**：`Replanner` 新增 `LedgerBudget` 接真实 `p0_t20` 台账，`test_replanning_ledger.py` 5 passed。判定**仍为 `PASS（限域）`**，门级不变 |
