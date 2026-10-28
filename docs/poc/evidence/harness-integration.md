# P0-T21 证据：Harness 节点集成、取消与安全 trace（含原生 LangGraph 补证）

> 波次 W5 / 任务 P0-T21。验收依据 `docs/poc/03_Phase0_Task_Backlog.md`（P0-T21）、
> `docs/poc/07_Contract_Closure.md` C-04、`docs/tdd/02_Agent_Harness.md` §3–§6、
> `docs/poc/05_Phase0_Acceptance.md` F3/F4/F5。
> 本文件只记录**实跑**结果；ADR 全部保持 Proposed，本任务没有把任何一条改成"已接受"。
>
> **本版修订**：主线已 `uv add --project experiments/phase0 --dev langgraph`，实装
> **langgraph 1.2.18**。原「原生 graph/interrupt 不可用 → PARTIAL」的结论已被**实测替换**
> （见 §6）；fallback 路径保留但明确不在主路径上（见 §3、§6.4）。

## 1. 任务声明

```yaml
wave: W5
task: P0-T21
branch: feature/w5-t12-t17-t21-t23
base: a2a7dfc
write_scope:
  - experiments/phase0/harness_graph.py
  - tests/phase0/test_harness_integration.py
  - docs/poc/evidence/harness-integration.md
resource_slot: {pg_schema: p0_t21, redis_db: 7}
native_stack: {langgraph: 1.2.18, langgraph-checkpoint: 4.3.1,
               langgraph-checkpoint-postgres: 3.2.0, psycopg: 3.3.6, psycopg-pool: 3.3.3}
reused_readonly:
  - checkpoint_probe.py   # T05, p0_t05 —— 仅 fallback 路径使用
  - approval_probe.py     # T06, p0_t06
  - tool_ledger.py        # T06
  - job_probe.py          # T08, p0_t08
  - model_gateway.py      # T19
  - tool_registry.py      # T19
  - budget_ledger.py      # T20, p0_t20
redis_note: 未使用（Job 唤醒只需 T08 的 PG 事实；DB 7 保持空闲）
```

## 2. T20 指出的契约冲突：显式二选一（方案 ①）

**冲突**：`model_gateway.ModelGateway.call` 内部自带重试
（`total_attempts = config.max_retries + 1`，`model_gateway.py:955`），而
`budget_ledger.reserve_model_call` 的 `purpose_key` 是「一次预留 = 一个 purpose_key」。
两者叠加 → N 次真实 provider 调用只落 1 条预留，成本无法逐 attempt 归集。

**选方案 ①**：关掉 gateway 内部重试（`ProviderConfig(max_retries=0)`），Harness 自己按
attempt 循环，`purpose_key = f"t21:{model_step_key}:attempt:{n}"`，失败 `release`、
成功 `settle`。**代价**：退避/错误分类/`Retry-After` 从 gateway 移到 Harness；本实现只做
有界循环（`max_model_attempts`），**不读 `Retry-After`**——这是明示取舍，不是遗漏。

**证据**（`test_budget_reserves_each_model_attempt`，实测通过）：脚本化 transport 第一次
网络错误、第二次成功 → `transport.calls == 2`，账本恰好 2 条 reservation（`:attempt:1` /
`:attempt:2`），两条状态不同（一条 released、一条 settled）。即「1 次 provider 请求 =
1 条预留」。**原生图与 fallback 共用同一个 `run_model_step_with_budget`**，该语义在两条
路径上都成立。

## 3. 两条实现路径（主路径 / fallback）

| | **原生主路径** `NativeGraphRun` | **fallback** `GraphRun`（保留） |
| --- | --- | --- |
| 图执行 | `langgraph.graph.StateGraph` 编译 | 本模块顺序调用节点函数 |
| 审批等待 | **原生 `langgraph.types.interrupt()`** | `p0_t21.phase0_wait_facts` 持久化等待事实 |
| 恢复 | **`Command(resume=...)`** | `run.resume()` 方法调用 |
| checkpoint | **原生 `PostgresSaver`（框架建表、框架读写）** | T05 `checkpoint_probe` 替身存储 |
| 节点数 | 6（`plan`→`read_fanout`→`approval_wait`→`write_execute`→`job_wake`→`finalize`） | 同 6 个 |
| 用途 | **验收主证据** | 兼容既有 14 条断言（预算/canary/取消/超时/事实恢复） |

保留 fallback 的理由与边界：

1. 既有 14 条用例覆盖的是**与图引擎无关**的语义（逐 attempt 预算、canary、取消/超时、
   事实恢复），改写成原生图不会提高这些断言的强度，反而扩大回归面；
2. fallback **不在主路径上**——`test_native_checkpoints_are_owned_by_framework` 实测断言：
   原生路径跑完后 `phase0_wait_facts` 与 `phase0_graph_runs` 中该 run 的行数**均为 0**。
   「原生 interrupt 已验证」与「fallback 未用于主路径」是两条可独立复验的事实；
3. 取消/超时注入点（`request_cancel` / `set_deadline`）目前只挂在 fallback 的 `GraphRun`
   上；原生路径尚未接这两个事实（见 §9.2）。

## 4. 组件复用表（本任务没有重写任何已有能力）

| 能力 | 委托给 | 调用点 |
| --- | --- | --- |
| 图执行/中断/恢复 | **langgraph 1.2.18 原生** | `NativeGraphRun._compile` / `start` / `resume` |
| checkpoint 持久化 | **原生 `PostgresSaver`** | `ensure_native_checkpoint_schema` / `_native_saver_connection` |
| 审批登记/批准/六项校验/执行/账本 | T06 `ensure_approval` / `approve` / `require_approval` / `execute` | `_node_approval_wait` / `_node_write_execute` |
| receipt 查询 | T06 `tool_ledger.receipts_for` | `recover_from_facts`（fallback） |
| Job 唤醒 | T08 `enqueue` / `record_result` | `_node_job_wake` |
| 模型调用 | T19 `ModelGateway.call`（`max_retries=0`） | `run_model_step_with_budget` |
| 预算预留/结算/释放 | T20 `reserve_model_call` / `settle` / `release` | `run_model_step_with_budget` |
| trace 脱敏 | `safe_trace.SafeTrace` | 所有节点 |

本任务自己的 DDL 只有 `p0_t21` 三张表（`phase0_graph_runs` / `phase0_wait_facts` /
`phase0_node_events`，服务于 fallback 与审计）。**checkpoint 表不在此列**：
`checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations`
四张表由 `PostgresSaver.setup()` 创建。

## 5. 验收逐项（实测）

| # | 验收条件 | 判定 | 实测依据 |
| --- | --- | --- | --- |
| 1 | 最小可运行图：只读并行 + 写前屏障 + 审批等待 + 预算预留 + Job 唤醒，组件全部复用 | **PASS（原生）** | 原生 6 节点图；`test_native_graph_pauses_at_interrupt_and_persists_state` 证明暂停点与零副作用；`test_minimal_graph_uses_every_reused_component` 覆盖 T05/T06/T08/T19/T20 全链 |
| 2 | checkpoint 丢失后重入：读已提交事实恢复，不回退已提交状态 | **PASS（单活前提）** | `test_checkpoint_loss_reentry_reads_committed_facts_and_does_not_roll_back`：删该 thread 全部 checkpoint + 归零序列指针 → `recover_from_facts()` 得到 receipts；再执行返回 `replayed`，下游 `applied_calls` 仍为 1 |
| 3 | 取消后不再执行新工具调用；超时不伪装成功 | **PASS（fallback 路径）** | 取消后 `attempts==0`、终态 `cancelled`；启动即取消时 `transport.calls==0`；超时终态 `timeout != completed`、零副作用 |
| 4 | `safe_trace` 只记脱敏字段；canary 不进 trace / Graph State | **PASS** | 三类 canary 同时注入 → trace、`to_state_fragment()`、最新 checkpoint 载荷均不含；**原生路径**另测 interrupt payload / State / trace 三重扫描通过 |
| 5 | checkpoint/interrupt 只用原生接口，不自研恢复引擎 | **PASS（原生 1.2.18 已验证）** | 见 §6；AST 断言只 import 原生 API、无自研 interrupt 入口、**零** `INSERT/UPDATE/DELETE` 打到原生 checkpoint 表 |

## 6. 原生 LangGraph 实测（langgraph 1.2.18）

### 6.1 能力核对

| API | 结果 |
| --- | --- |
| `from langgraph.graph import StateGraph, START, END` | 可导入、可编译 |
| `from langgraph.types import interrupt, Command` | 可导入；`interrupt()` 在节点内暂停生效 |
| `langgraph.checkpoint.postgres.PostgresSaver` | 可实例化；`setup()` 建表；读写正常 |

### 6.2 暂停 → 跨连接恢复 → 恰好一次副作用

用例：`test_native_graph_resumes_from_new_connection_exactly_once`

1. `NativeGraphRun.start()` → 图跑到 `approval_wait`，节点内 `interrupt({...})`；
   返回值含 `__interrupt__`，payload 为
   `{kind, approval_id, tool_call_id, intent_key_prefix}`（**只有引用，无 prompt/参数**）。
2. `get_state()` → `next == ("approval_wait",)`；框架 `checkpoints` 表已有该 thread 的行
   （`checkpoint_rows() >= 1`）；此刻下游 `attempts == 0`（写工具一次都没执行）。
3. **新宿主对象 + 新业务连接 + 新 saver 连接**（跨连接恢复）：只给 `thread_id` +
   `Command(resume={"decision": "approved"})` → 图继续跑完。
4. 实测：`status == completed`、`write_outcome == succeeded`；`plan` 只跑过一次
   （`starter.transport.calls == 1`、`resumer.transport.calls == 0`）；
   下游 `attempts == 1`、`applied_calls == 1`；T06 receipt 恰好 1 条 `succeeded`。

### 6.3 重复 resume 不重复副作用

用例：`test_native_repeated_resume_does_not_duplicate_side_effects`

再连发 3 次 `Command(resume=...)`：每次返回 `completed`；下游仍 `applied_calls == 1`、
receipt 仍 1 条。原因两层：图已到 END（LangGraph 不再执行节点），且 T06 的稳定
`intent_key` + receipt 即便重入也会返回 `replayed`。

### 6.4 checkpoint 归框架所有

用例：`test_native_checkpoints_are_owned_by_framework`

| 事实 | 实测 |
| --- | --- |
| 框架建的四张表 | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` 全部存在于 `p0_t21` |
| 框架写入了 checkpoint | `SELECT count(*) FROM p0_t21.checkpoints >= 1` |
| 本模块直写？ | AST 断言：源码中不存在 `INSERT INTO`/`UPDATE`/`DELETE FROM` 打到这四张表（含带 schema 前缀写法） |
| 原生路径是否偷偷用 fallback | `phase0_wait_facts` 与 `phase0_graph_runs` 中该 run 行数均为 **0** |

### 6.5 原生 interrupt 的一个真实行为（务必记牢）

LangGraph 在 resume 时**会从头重跑 `interrupt()` 所在节点**。因此：

- 节点里 `interrupt()` **之前**的逻辑必须幂等。本实现的 `persist_response` /
  `prepare_call` / `ensure_approval` 都幂等（T06 partial UNIQUE + 稳定 intent_key），
  重跑两次得到同一个 approval_id；
- **副作用必须放在 `interrupt()` 之后**（`write_execute` 是下一个节点），否则恢复会重放。

## 7. 门禁实跑记录

环境：PowerShell 7.6.0，`pwsh` 工具直跑。

| # | 命令原文 | exit | 真实结果 |
| --- | --- | --- | --- |
| 1 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_harness_integration.py -q -o addopts=` | 0 | **`19 passed in 14.37s`**（总耗时 15.84 s） |
| 2 | `uv run --project experiments/phase0 ruff check experiments/phase0/harness_graph.py tests/phase0/test_harness_integration.py` | 0 | `All checks passed!` |

用例构成：**原有 14 条全部保留通过** + **新增 5 条原生用例**
（暂停/持久化、跨连接恢复恰好一次、重复 resume、checkpoint 归框架、interrupt payload 无
canary）。既有 AST 用例按新前提更新：从「不得 import langgraph」改为「必须用原生 API +
不得直写原生 checkpoint 表」。

受保护文件零改动：`pyproject.toml`/`uv.lock` 由主线安装依赖，本任务未写。

## 8. 交付哈希（`Get-FileHash -Algorithm SHA256`）

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/harness_graph.py` | 1425 | `86586954cc31b5a2947196b933e78cde50701a912a017fbf070a0118dd3cb3a4` |
| `tests/phase0/test_harness_integration.py` | 747 | `4681184f6d94f2ccd7bc90d19ad216c2f7f195350634bd21d1c4520ae8ed3754` |

本文件自身不在表内（自我哈希不稳定），由 `wave-w5-record.md` 门禁统一记录。
Git：`branch=feature/w5-t12-t17-t21-t23`、`HEAD=a2a7dfc`，交付文件均为 `??` 未跟踪。

## 9. 仍未验证 / 风险

1. **跨进程 ≠ 跨连接**：本轮"跨连接恢复"是**新对象 + 新连接 + 新 saver 实例**，没有真起
   第二个 OS 进程再 `Command(resume=...)`。真进程级证明需要子进程 + 同一 `thread_id`，
   属 T30 故障矩阵范围。
2. **原生路径未接取消/超时**：`request_cancel`/`set_deadline` 目前只作用于 fallback 的
   `GraphRun`；原生图悬挂在 `interrupt()` 时的取消语义（`update_state` / 线程级取消）未验证。
3. **两套 intent_key 分歧仍在**：`tool_registry.compute_intent_key` 的 `schema_version`
   是 `str`，`tool_ledger.intent_key` 是 `int=1`，同一调用算出的 key 不相等；本任务持久化
   统一用 T06 版本。建议 T28/T30 收敛，否则跨组件去重会静默失效。
4. **连接形态三档**：T06/T08 要 `autocommit=True` + 显式事务，T20 `BudgetLedger` 拒绝
   autocommit，T08 还要 `row_factory=dict_row`；原生路径每次调用开 4 条连接
   （业务 / 预算 / Job / saver）。生产必须按组件固定连接形态。
5. `interrupt()` 的 `response_schema`、time-travel/fork、`prune` 策略均未使用/未验证。

## 10. 给 T26/T28/T30 的建议

- **T26（预算/观测）**：沿用 `purpose_key = t21:{model_step_key}:attempt:{n}`；接 OTel 时
  只映射 `safe_trace.TraceEvent` 的 allowlist 字段，**不要**改成"先记全量再过滤"。
- **T28（安全/越权）**：`safe_trace.ALLOWED_FIELDS` 是唯一出口清单；原生 interrupt payload
  已实测只带引用，新增字段必须同时补 canary 断言。工具调用不得绕过
  `approval_probe.require_approval`。
- **T30（故障矩阵）**：原生 interrupt 的注入点是「`interrupt()` 已落 checkpoint、resume
  尚未到达」；可直接复用 `NativeGraphRun` 的 `start()`/`resume()` 两步与 `get_state()`
  观测，并补**真子进程**恢复（见 §9.1）。多实例不要基于本证据放宽：T05 是单活结论，
  本任务同样只在单活下验证。
