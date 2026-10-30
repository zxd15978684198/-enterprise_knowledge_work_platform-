# W17 波次记录：PoC-07 残限补测批（D3 轮次强度 + D4 真实预算台账）

- 分支：`feature/w17-d3-d4`（base `dev@1989028`）
- 执行者：Lead（N=1）
- 日期：2026-10-29

## 1. 声明块

### 1.1 并行度判定（为什么 N=1）

| 任务 | 依据 | 依赖 | 与同批任务的耦合 |
| --- | --- | --- | --- |
| D3：ready/revision 竞争从 30 轮提到条款字面 **100 轮** | `poc07-report.md` §5-D3、`wave-w5-record.md:142` | 无（纯测试参数与断言） | 与 D4 **共享** PostgreSQL（`p0_t23`）与 `.env`，但**不共享写权文件** |
| D4：T24 重规划的预算读取从 `StaticBudget` 替身换成**真实 T20 `BudgetLedger`** | `poc07-report.md` §5-D4、`wave-w6-record.md:112` | D1：需 `p0_t20` 迁移在库（已存在，W5 起） | 同上 |

两者写权不相交，理论上可 N=2；但两者都起真实 PG 事务并在同一库上做 TRUNCATE/RESET，
并行会让失败归因变模糊（"是并发干扰还是真缺陷"），而单任务耗时都在分钟级 —— **判定 N=1，串行执行**，
顺序 D3 → D4（D3 是改参数、风险最低，先跑通建立基线）。

### 1.2 写权白名单（越界即回滚）

| 路径 | 用途 |
| --- | --- |
| `tests/phase0/test_task_coordination.py` | D3：`rounds = 30` → 100 + 断言/注释同步 |
| `experiments/phase0/evaluator_replanner.py` | D4：新增 `LedgerBudget` 适配器（只读剩余额度），不改其它逻辑 |
| `tests/phase0/test_replanning_ledger.py` | D4：新建集成测试（真实 `p0_t20` 账本） |
| `docs/poc/evidence/wave-w17-record.md` | 本文件 |
| `docs/poc/evidence/poc07-report.md` | D3/D4 行与 §5、§12 变更记录 |
| `docs/poc/evidence/poc02-replanning.md` | D4 行的「唯一 PARTIAL」表述更新 |
| `docs/IMPLEMENTATION_STATUS.md` | 前沿行 + 本轮段 |

明确**不写**：`docs/poc/03_Phase0_Task_Backlog.md`、`docs/adr/**`、任何迁移文件、任何历史波次记录。

### 1.3 资源槽

| 资源 | 值 |
| --- | --- |
| PostgreSQL | `127.0.0.1:5433`，复用 schema `p0_t23`（T23）与 `p0_t20`（T20），本波次**不新建** schema |
| Redis | 不使用 |
| 端口 | 不占用 |

### 1.4 验收标准

1. D3：竞争实验跑 **100 轮**×2 线程×4 节点 = **400 条 claim**，无重复领取、无提前执行、旧 epoch 结果不覆盖；测试仍通过。
2. D4：`Replanner` 的 `budget=` 可接真实 `BudgetLedger`；三条断言成立 ——（a）剩余额度读数与账本 `available()` 一致；（b）额度不足时 `budget_exhausted` 且 run 终态 `blocked`，账本**预留与已结算计数均不变**（无 release、无透支）；（c）额度充足时 `applied` 且账本计数不变。
3. 全量回归不下降（基线 `1037 passed / 8 skipped`）。
4. 判定口径：`verdict_lint.py --check` 仍 `new=0`；`evidence_meta.py --check` 仍 `with_problem=0`。
5. 文档：`poc07-report.md` 的 D3/D4 残限改为「已补测 + 实测数字」，**不宣称 G-P0/G-FULL 通过**。

### 1.5 禁止事项

- 不把「D3/D4 补测完成」写成「PoC-07 升格为 PASS」——残限还有 D1/D2/D5/D6/D7。
- 不改 `StaticBudget`（它是「被 release 即失败」的守卫，删了等于丢掉一条断言）。
- 不在 D4 里顺手改重规划决策逻辑（本轮只换数据来源，不动算法）。

## 2. 派发记录

| 步骤 | 任务 | 执行者 | 结果 |
| --- | --- | --- | --- |
| D3 | ready/revision 竞争 30 → **100 轮** | Lead | 通过：`test_task_coordination.py` 28 例全绿，`rounds * 4 = 400` 条 claim 断言随参数化 |
| D4 | 重规划预算读数换真实 `BudgetLedger` | Lead | 通过：新增 `LedgerBudget`（只读端口）+ `test_replanning_ledger.py` 5 例全绿 |
| 环境 | 官方 PG 容器再次不在（只剩陌生 `pyknora-pg-p0`） | Lead | `compose --profile core up -d postgres redis` 恢复，PG 17.11 + vector 0.8.7 healthy |
| 工具 | `pwsh` 运行器崩溃 `exit code 1073807364` | Lead | 改用 `Start-Process -RedirectStandardOutput` + 读日志，不再依赖前台管道 |

## 3. 执行与门禁

### 3.1 D3：轮次强度提到条款字面值

`tests/phase0/test_task_coordination.py::test_concurrent_double_scheduler_claims_each_node_once`
只改两处：`rounds = 30` → `rounds = 100`（并加一行注释说明依据），以及把写死的
`120 条 claim` 断言改成 `f"每轮 4 个节点各领一次 = {rounds * 4} 条 claim..."`。

- 为什么之前是 30 轮：`wave-w5-record.md:142` 记录的是「30 轮已能复现双领取」，属于
  够用即止的经验值；Backlog 条款字面写的是 100 轮，本轮按字面补齐，避免"条款与实测口径不一致"。
- 代价：单例耗时上升（100 轮 × 4 节点 × 2 线程 = 400 次 claim），全量回归总时长仍在 8 分钟内。
- 实测：**28 passed**（该文件全部用例），无重复领取、无旧 epoch 覆盖。

### 3.2 D4：把重规划接到真实账本

`experiments/phase0/evaluator_replanner.py` 新增 `LedgerBudget`（`@dataclass(slots=True)`），
实现既有 `BudgetPort` 协议，只做三件事：

1. `remaining_microunits(workspace_id, run_id)` → `BudgetLedger.state(...).available().cost_microunits`；
2. 工作区不匹配 → `ReplanError(CODE_INVALID_REQUEST)`（不静默降级成 0，也不跨租户读）；
3. `release(...)` → `AssertionError`（重规划**不得**释放未知用量的预留，与 `StaticBudget` 同一守卫）。

`StaticBudget` 原样保留：它是"被 release 即失败"的替身守卫，删掉就丢掉一条断言。

新增 `tests/phase0/test_replanning_ledger.py`（5 例，真实 `p0_t20` + `p0_t23`）：

| 用例 | 断言 |
| --- | --- |
| `test_ledger_budget_reports_real_available` | 端口读数 == 账本 `available()`；**在途预留**同步扣减（`spent=0` 但可用已少 400000） |
| `test_insufficient_ledger_blocks_replan` | 需求 > 可用 → `budget_exhausted` + `blocked`；预留/结算/事件三类计数**与调用前完全一致** |
| `test_sufficient_ledger_passes_budget_gate` | 无未满足事实 → `noop`；账本不变；同时记录 noop 不读预算 |
| `test_sufficient_ledger_applies_replan` | claim→fail 后真走重规划：`applied`，不被预算拦截，账本不变 |
| `test_ledger_budget_rejects_foreign_workspace` | 跨工作区读数 → `ReplanError`，不返回数字 |

### 3.3 接线过程中被测试逼出来的三条事实（都已写进测试注释）

1. **行工厂不一致**：`sch.connect()` 配的是 `dict` 行，`bl.connect()` 是元组行 —— 同一个断言里两种都要兼容，否则 `KeyError: 0`。
2. **账本是 append-only**：`p0_t20` 上的 `phase0_reject_append_only_mutation()` 触发器**禁止 DELETE**，所以测试清理只能碰 `p0_t23`/`p0_t24`，账本侧靠每例独立 `run_id` 隔离。这条是被 `CheckViolation` 打回来才发现的，属于"被测系统的正确行为"，不该绕。
3. **`budget_remaining_microunits` 只在被拦时落盘**：放行路径写 `None`，`noop` 路径更在读预算之前就返回。所以"读数正确"不能靠这个字段证明，要靠用例 1（直读端口）与用例 2（被拦时的精确值）。

另外修掉一处**跨连接互等**：清理若用 run 连接删 `p0_t20` 行，会与账本连接的 idle-in-transaction 持锁互等（实测卡死 15 分钟以上）。现在清理按连接分工，并先 `rollback()` 释放读事务。

### 3.4 门禁

| 项 | 命令 | 结果 |
| --- | --- | --- |
| ruff check | `ruff check experiments/phase0/evaluator_replanner.py tests/phase0/test_replanning_ledger.py` | **All checks passed** |
| ruff format | `ruff format --check` 同两文件 | **already formatted**（`evaluator_replanner.py` 顺带做了全文件格式化，41+/20−，纯排版） |
| 定向回归 | `pytest tests/phase0/test_replanning_ledger.py` | **5 passed in 2.20s** |
| 定向回归 | `pytest tests/phase0/test_task_coordination.py` | **28 passed** |
| 全量回归 | `pytest -c experiments/phase0/pyproject.toml tests/phase0 -q` | **1041 passed / 8 skipped / 1 failed in 459.92s**；唯一失败是 `verdict_lint.py` 报本文件缺 `## 2./3./4.`，补齐后 `new=0`（见 §3.5） |
| 判定口径 | `verdict_lint.py --check` | `docs_scanned=58 violations=9 new=1` → 补 §2–§4 后复跑 `new=0` |
| 证据台账 | `evidence_meta.py --check` | `with_problem=0`（无新增证据文件） |

### 3.5 全量失败归因（不是代码缺陷）

`test_verdict_lint.py::test_repo_has_no_new_violations` 失败消息是
`docs/poc/evidence/wave-w17-record.md:0 [WAVE_RECORD_SECTIONS] 缺少小节: ## 2., ## 3., ## 4.`
—— 即 linter 在**本文件写完之前**跑的那一轮。补齐三节后 `new=0`，该用例复跑通过。
这是自证链在工作：文档结构不合规，门禁就红。

### 3.6 环境与残留

- PG 容器曾整体掉线（只剩陌生 `pyknora-pg-p0`，pg16:5434），按波次规程用 `compose --profile core` 拉回；
- 仓库根出现 3 个 `pyknora-evidence-*/` 目录：`conftest.py` 里 `tempfile.mkdtemp` 在本 venv 下返回**仓库根**（已知坑），本轮结束删除；
- 全量回归重写 6 份 tracked 证据 → `git stash` 隔离，不入本波次提交。

## 4. 提交决策

待提交（5 项）：

| 路径 | 性质 |
| --- | --- |
| `experiments/phase0/evaluator_replanner.py` | `LedgerBudget` + 全文件 ruff format |
| `tests/phase0/test_task_coordination.py` | D3：30 → 100 轮 |
| `tests/phase0/test_replanning_ledger.py` | D4：5 例集成测试（新） |
| `docs/poc/evidence/wave-w17-record.md` | 本文件（新） |
| `docs/poc/evidence/poc07-report.md`、`docs/poc/evidence/poc02-replanning.md`、`docs/IMPLEMENTATION_STATUS.md` | D3/D4 残限改写 + 前沿/本轮段 |

不提交：`pyknora-evidence-*/`（已删）、6 份重生成证据（`stash`）、`.idea/`、`CLAUDE.md`、`docs/teacher/`、`docs/plans/README.md`（均属既有未跟踪）。

**判定口径不变**：D3/D4 只是把两条残限补测完，**不改变 PoC-07 的 `PARTIAL`**（D1/D2/D5/D6/D7 仍在），更不构成 G-P0/G-FULL 通过。
