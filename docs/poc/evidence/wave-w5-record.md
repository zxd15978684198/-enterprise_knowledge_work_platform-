# Wave W5 记录（P0-T12 / T17 / T21 / T23）

> 按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立；门禁与提交决策在波次末填写。

## 1. 波次声明

```yaml
wave: W5
branch: feature/w5-t12-t17-t21-t23
base: feature/w4-t06-t08-t11-t16-t20-t22@a2a7dfc   # W4 已提交并推送 origin；dev 仍是祖先
started_at: 2026-10-27T21:45+08:00
parallel_tasks: [P0-T12, P0-T21, P0-T23]
blocked_tasks: [P0-T17]
N: 3
serial_tasks: []
per_task:
  - task: P0-T12
    write_scope:
      - experiments/phase0/index_staging.py
      - experiments/phase0/db/migrations/004_version_index.sql
      - tests/phase0/test_index_staging.py
    resource_slot: {pg_schema: p0_t12}
  - task: P0-T21
    write_scope:
      - experiments/phase0/harness_graph.py
      - experiments/phase0/safe_trace.py
      - tests/phase0/test_harness_integration.py
      - docs/poc/evidence/harness-integration.md
    resource_slot: {pg_schema: p0_t21, redis_db: 7}
  - task: P0-T23
    write_scope:
      - experiments/phase0/task_scheduler.py
      - experiments/phase0/task_executor.py
      - experiments/phase0/db/migrations/006_tasks.sql
      - tests/phase0/test_task_coordination.py
    resource_slot: {pg_schema: p0_t23, redis_db: 8}
blocked_reason_P0-T17: >-
  无 pinned upstream BrowserSkill 源码/daemon/extension 产物（T14 已确认仓库只含接入层，
  构建需从 GitHub 克隆 da6bf4ee… 且脚本无 windows 目标、daemon 硬编码 /tmp），亦无真实浏览器设备。
  T17 要求「真扩展配对 + 真实 wire 证据」，缺原料时只能 BLOCKED；按 §4.5.2 在波次启动时移出并行集，
  N 由 4 下调为 3，不派注定 BLOCKED 的 agent 充数。
```

### 1.1 N 的计算依据（§4.5.2 三层规则）

| 层 | 结论 |
| --- | --- |
| 依赖层 | T12←T03(PASS)+T08(PASS 限域)+T11(PASS 限域)；T21←T05(PARTIAL)+T06(PASS)+T08+T20(PASS 限域)；T23←T22(PASS)+T05+T08+T20。T05 为 PARTIAL（单活 runner），T21/T23 只允许在「单活」前提下做限域验证，不得声称横向扩展成立 |
| 写入权层 | 三份写权互不相交；`contracts.py`/`scope.py`/迁移 `001–003`/`005`/`conftest.py`/`fixtures/manifest.json`/`pyproject.toml`/`uv.lock`/`compose.yaml` 只读 |
| 资源层 | PG schema `p0_t12`/`p0_t21`/`p0_t23` 互斥；Redis DB 7/8；core 容器运行中，无人 `up/down` |

→ **N = 3**，`blocked_tasks = [T17]`。

## 1.2 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| C-01 | T12 的 staging/版本分区与 T13 的原子发布是两段：**T12 只产出「不可见分区 + manifest」，不切换 active 指针** |
| C-09 | 同配置重处理与新 Job 重试区分；T12 的 attempt fencing 必须复用 T07/T08 的 epoch 语义，不另造 |
| C-10 | `asset_bindings` 唯一归属：T12 只做用途引用与 checksum；对象存储若不可用，必须记 `NOT_RUN` 而不是用本地路径冒充 S3 |
| T21 关键选择 | T20 指出「gateway 内部重试 vs 逐 attempt 预算归集」冲突：T21 必须**显式选一条**（关闭 gateway 重试由 Harness 逐 attempt 预留，或按 `max_attempts × 单次上限` 预留后拆分结算），并写进代码注释与证据 |
| T21 原生能力 | checkpoint/interrupt 只用 LangGraph 原生；不得自研恢复引擎；`safe_trace` 不得把 prompt/密钥写入 trace |
| T23 状态拥有者 | OrchestrationService 唯一写 Task；Job 归 JobService；checkpoint 归 LangGraph。迁移 `006` 归 T23，禁止新建 `007`（T27 已占用） |
| 已知缺陷（W5 内可顺手修） | `experiments/phase0/docreader_probe/wire_evidence.json` 会被 T10 的测试重写（gate 时被 stash 暂存）；应改为写 `.pytest_cache/`，不污染 tracked 证据 |
| 待合并偏离 | `002`/`003`/`005` 落在独立 schema（`p0_t06`/`p0_t07`/`p0_t20`），生产合并需并回 `p0_t03` 前缀并补 RLS/GRANT；W5 不处理，记入 T31 前置 |
| 迁移编号 | `004`→T12、`006`→T23；T21 不得新建编号迁移 |
| 依赖 | 不得改实验 `pyproject.toml`/`uv.lock`；新包用 `uv run --with` 且**不要用它跑全套** |
| 测试 | 必须 `-c experiments/phase0/pyproject.toml`；全量单进程可直接跑（依赖已进 venv） |
| Git | 波次内禁止 `git add/commit/push`；终端通道：**2026-10-27T21:56 复测 `pwsh` 工具已恢复**（PowerShell 7.6.0，git/uv/docker 均可直接调用），本波次起优先用 `pwsh`；`wsl + cmd.exe` 仅作为它再次失败时的回退 |

### 1.3 本轮暴露的跨组件缺口（比单任务失败更重要）

| 缺口 | 实测证据 | 影响 / 处置 |
| --- | --- | --- |
| **无 `vector` 扩展** | T12 实测 `pg_available_extensions` 无 `vector`；向量用 `float8[] + embedding_dim` | **T25 的前置阻断**：要声明 pgvector ANN/召回必须先引入扩展或外部向量库；本轮不冒充向量检索已通过 |
| **`langgraph` 只有 namespace 包** | T21 实测 `langgraph.graph` / `langgraph.types` 均 `ModuleNotFoundError`（`langgraph.checkpoint.postgres` 可导入） | 原生 `StateGraph`/`interrupt` 未验，T21 的审批等待用的是**持久化等待事实**而非原生 interrupt；C-03/C-04 仍 `NEEDS_POC`，接入原生后必须替换等待实现而不是叠加 |
| **`intent_key` 两套算法** | `tool_registry.compute_intent_key` 的 `schema_version` 是 `str`，`tool_ledger.intent_key` 用 `int=1` → 同一调用算出不同 key | **跨组件去重会静默失效**：T26/T28 之前必须统一（建议以 T06 的持久化版本为准，改 T19 侧） |
| **连接语义三档** | T06/T08 要 `autocommit=True`+显式事务、T20 `BudgetLedger` 拒绝 autocommit、T08 要 `dict_row` | T21 用三条连接并存；生产共用连接池前必须按组件固定连接形态，否则会出现"事务边界争议" |
| **迁移前缀偏离累积** | `002`/`003`/`004`/`005` 均落独立 schema（`p0_t06`/`p0_t07`/`p0_t12`/`p0_t20`） | 生产合并需并回 `p0_t03` 前缀并补 RLS/GRANT，列入 T31 前置；本轮不处理 |

## 2. 状态表

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T12 | 已交报告（待门禁） | w3-t07-outbox | `index_staging.py`(1262) + `004_version_index.sql`(452) + 测试(741，16 例)，共 2455 行；**16 passed（11.97s）/ ruff 通过**（已改用 pwsh）；staging 重放 `inserted=0/skipped=3` 行数不增；旧 attempt `STALE_ATTEMPT` 且 DB 触发器兜底 0 行；ready 后 `search_visible_chunks()==[]`、模拟 T13 激活才可见；manifest 缺项 `MANIFEST_INCOMPLETE`（API+DB 双层）；重复 apply `DuplicateSchema`；`p0_t12` 零残留。**新发现**：本机 PG **无 `vector` 扩展**（实测），向量列用 `float8[] + embedding_dim`，pgvector ANN/召回/延迟全部 `NOT_RUN` → **T25 的前置阻断项** |
| P0-T17 | **BLOCKED** | — | 无 upstream 源码/产物、无真实设备；解除条件见 §1 声明 |
| P0-T21 | 已交报告（待门禁） | w3-t05-checkpoint | `harness_graph.py`(948) + `safe_trace.py`(310) + 测试(544，14 例) + 证据 md(174)，共 1976 行；**14 passed（10.83–21.94s）/ ruff 通过**；6 节点图复用 T05/T06/T08/T19/T20 公开 API；checkpoint 删除后 `recover_from_facts()` 得到 `receipts==1` 且节点返回 `replayed`、`applied_calls` 仍 1；取消/超时零副作用；canary 在 trace/State/checkpoint 全空。**T20 冲突已选方案①**：`max_retries=0` + Harness 逐 attempt `reserve_model_call`（`purpose_key=t21:{step}:attempt:{n}`），代价记入证据。**关键缺口**：`langgraph.graph`/`langgraph.types` 未安装 → 只在「节点驱动 + 持久等待事实」层验证，**原生 StateGraph/interrupt 未验**（验收第 5 项记 PARTIAL） |
| P0-T23 | 已交报告（待门禁） | w1-t02-env | `task_scheduler.py`(1613) + `task_executor.py`(225) + `006_tasks.sql`(230) + 测试(1080，28 例)，共 3148 行；**28 passed（15.23s）/ ruff 通过**（第 40 步起改用 pwsh）；未满足依赖执行 0、required 失败递归 blocked、optional 不阻断；双 scheduler 30 轮×2 线程×4 节点 120 条 claim 无重复；旧 epoch 完成/引用均 `STALE_*` 拒绝；事实先落库、checkpoint 失败只留 `state_ref_pending` 且重入不重跑；真实 T08 Job port 集成通过；迁移重复 apply `MIGRATION_ALREADY_APPLIED`；静态守卫证明无跨域写。限域：单活前提、checkpoint 用端口替身、对账为拉取式、30 轮非 100 轮 |

## 3. 门禁检查（已完成 2026-10-27T22:12–22:18 +08:00，通道 = 原生 pwsh）

### 3.1 逐项结论

| 检查 | 结论 | 证据 |
| --- | --- | --- |
| 各任务四项报告交齐 | ✅ 3/3（T17 记 BLOCKED，未派发） | T12/T21/T23 均按提示词 06 四段汇报 |
| 越界写入核对 | ✅ 无越界 | 新增全部 `??`；受保护文件（`pyproject.toml`/`uv.lock`/`compose.yaml`/`001–003`/`005`/`007`/`conftest.py`/`contracts.py`/`scope.py`）零改动 |
| 全量 ruff | ✅ `All checks passed!` | 首跑 1 个 `I001`（`index_staging.py:44`）→ `ruff check --fix` 后复跑 exit 0 |
| 全量 pytest（单进程） | ✅ **738 passed / 0 failed / 0 skipped** in **262.78 s**（缺口修复后复跑；首次 713 passed/319.70s） | `experiments/phase0/.pytest_cache/w5_final.xml`（`tests=738 failures=0 errors=0 skipped=0`） |
| 端口与临时目录 | ✅ 干净 | 5610/5500/5600 `LISTENING` 计数 0；仓库根无 `tmp*`/`.t0*` |
| 秘密扫描 | ✅ 0 命中 | `experiments/phase0/*.py` 与 `tests/phase0/*.py` 扫 `ghp_`/`sk-`/`AKIA`/私钥 |
| 追加 IMPLEMENTATION_STATUS | ✅ | 「本轮：W5 波次执行」小节 |
| 询问用户是否提交 | ⏳ | 见 §4 |

### 3.2 测试拆解（738 = 655 + 83，缺口修复后复跑）

| 来源 | 用例数 |
| --- | --- |
| W1–W4 既有 | 655 |
| P0-T12 `test_index_staging.py` | 16 |
| P0-T21 `test_harness_integration.py`（含 5 条原生图用例） | 19 |
| P0-T23 `test_task_coordination.py` | 28 |
| 缺口修复新增 `test_cross_component_keys.py` | 20 |
| **合计** | **738**（`738 passed in 262.78s`，junit `tests=738 failures=0 errors=0 skipped=0`） |

> 首次门禁（缺口修复前）为 `713 passed in 319.70s`；修完三个缺口后复跑为 **738 passed in 262.78s**，ruff 仍 `All checks passed!`。

### 3.3 交付哈希与行数（门禁实测）

```text
3abecad316e8c9273f90b659d92b100534dd4b170ef909ba0e51402117816f3f  experiments/phase0/index_staging.py            1106 行
4d17092e5d6fa014a125729a4b79a01b587686e43c32658085bc093e3343919b  experiments/phase0/db/migrations/004_version_index.sql  413 行
864cea28dde9ea0ba12bcfa5d0e0104e0c906faf14afdef62abd494aadc3791b  tests/phase0/test_index_staging.py              642 行
9b0202bda6c2b9f181f349f8dd3034071400035cce957a8e8944fbb91ae6a2d3  experiments/phase0/harness_graph.py             845 行
26df84fd3a0353bc95eefa6e9c9c26318202f15a9601004d74a6bbc5bba1c88b  experiments/phase0/safe_trace.py                259 行
b74d0e9515d662300e5339342fa996b9196c6bf36a5314f67bb14ee39fdc335a  tests/phase0/test_harness_integration.py        476 行
a010a205b200908a9ae4d97e7653ca3d5fb6f9db8bb22e14270cc156bbeed416  docs/poc/evidence/harness-integration.md         139 行
ad4a1b089fc504e9e86192971c1a1a034ed9e0cdc2c303e8c43b02539f3066c4  experiments/phase0/task_scheduler.py            1613 行
0a6b6c39cce5c94dd8ee55c2942406dbbdcbc27c58f4fa8f708d00939827e811  experiments/phase0/task_executor.py              225 行
0d7458ca8c289cfe5f0749de0d9fec5120177637d10cf1cf53de27961187006e  experiments/phase0/db/migrations/006_tasks.sql   230 行
171df66e63381ee640792688bd9bb68611fc02cc07d1b6b4e59dba8189ad8d27  tests/phase0/test_task_coordination.py          1080 行
```

本轮新增合计 **7 108 行**（实现 4 048 + 测试 2 198 + 迁移 643 + 证据 139 等按上表）。

### 3.4 波次判定

| 任务 | 判定 | 限域 / 未完成项 |
| --- | --- | --- |
| P0-T12 | **PASS（限域）** | staging 不可见、attempt/generation fencing、manifest 缺项双层拒绝、空库重建全过 | 对象存储 `NOT_RUN`（`InMemoryObjectStore.backend` 明标 fake）；**pgvector 缺失 → ANN/召回 `NOT_RUN`**（向量用 `float8[]`）；chunk 唯一键不含 `attempt_id`（串行 staging 前提），并行 staging 需契约修订 |
| P0-T21 | **PASS（限域，缺口已修）** | 6 节点图、checkpoint 删除后 `replayed` 且副作用仍 1、取消/超时零副作用、canary 三处为空、T20 冲突选定方案①（`max_retries=0` + 逐 attempt 预留） | **原生主路径已补**：langgraph 1.2.18 下 `StateGraph` + `interrupt()` + `Command(resume=…)` + 原生 `PostgresSaver` 实测通过（新对象+新连接恢复 exactly-once、框架建表、代码零直写）；仍限域：跨连接≠跨进程、原生取消/超时未接、`interrupt` 的 response_schema/time-travel/prune 未用 |
| P0-T23 | **PASS（限域）** | ready 判定、依赖传播、双调度 CAS、事实先于引用、真实 T08 Job 集成、迁移约束全过 | 单活前提；checkpoint 用端口替身（真实 saver 故障未注入）；对账拉取式；30 轮非 100 轮；无 `retry_wait`/`dead_letter`（属 T24/后续） |
| P0-T17 | **BLOCKED** | 无 upstream 源码/产物、无真实设备；解除条件见 §1 | 不冒充真配对 |

## 3.6 门禁后修缺口（用户 2026-10-27 选择「先修缺口再提交」）

| 缺口 | 修复动作 | 真实验证 | 遗留 |
| --- | --- | --- | --- |
| `langgraph` 仅 namespace、原生不可用 | `uv add --project experiments/phase0 --dev langgraph` → **langgraph 1.2.18**（+ langgraph-prebuilt 1.1.1、langgraph-sdk 0.4.10）；`harness_graph.py` 新增原生主路径 `NativeGraphRun`：`StateGraph` 6 节点 + `interrupt()` + `Command(resume=…)` + 原生 `PostgresSaver`（`setup()` 建 `checkpoints/checkpoint_blobs/checkpoint_writes/checkpoint_migrations`） | `test_harness_integration.py` **19 passed**（14 既有 + 5 原生）：暂停后 `next==("approval_wait",)`、下游 `attempts==0`；**新对象+新业务连接+新 saver** 恢复 → `completed`、`plan` 未重跑、`applied_calls==1`、T06 receipt 1 条；重复 resume ×3 仍 1 次；AST 断言模块对原生 checkpoint 表零直写 | 跨连接 ≠ 跨进程（未真起第二个 OS 进程）；原生路径未接取消/超时；`interrupt` 的 `response_schema`/time-travel/prune 未用 |
| `intent_key` 两套算法 | `tool_registry.compute_intent_key` 改为**委托** `tool_ledger.intent_key`；`SCHEMA_VERSION = INTENT_SCHEMA_VERSION`（int），`schema_version` 类型同步 | `test_gateway_registry.py` 98 + 新 `test_cross_component_keys.py` 20 = **118 passed**：同语义同 key、8 字段扰动两侧同步、键序无关、`"1"` 两侧同拒、API 签名保留、复现旧算法分叉 | `run_id=None` 只是草稿键（T21 持久化前必须注入真实 run_id）；`scope_hash` 仍有两套生产者（T21 须统一取值）；`CANONICALIZATION_VERSION` 标签不同（不进 key） |
| `docreader_probe/wire_evidence.json` 被测试回写 | `tests/phase0/test_docreader_wire.py` 的证据写入改到 **`.pytest_cache/wire_evidence.json`**（gitignored），仓库内那份作为静态快照 | `test_docreader_wire.py + test_parser_golden.py` **87 passed**；跑完后 `docreader_probe/` 不再新增改动 | 仓库快照的 `duration_s`/时间戳与最新一次运行不同，随本次提交刷新一次（只差 24 行计时字段） |
| 无 `pgvector` 扩展 | 独立探针：`docker pull pgvector/pgvector:pg17` → **digest `sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`**（PG 17.11 + pgvector 0.8.7），一次性容器在 5434 端口验证后已删除；**未动 core compose** | **PASS（带门禁）**：`CREATE EXTENSION vector` OK、`hnsw`/`ivfflat` 均可建并可被 `EXPLAIN` 走 Index Scan、`<->`/`<=>`/`<#>` 距离算子可用、跨维比较报错；5000×128 HNSW 建 1289.7 ms、IVFFlat 115.8 ms、查询 0.94–1.48 ms、top-5 与精确扫描 5/5 一致；**2000 维硬上限**实测（`vector(2001)` 拒、`halfvec` 到 4000、3072 维需 halfvec 表达式索引）；非超级用户 `CREATE EXTENSION` 被拒 | core profile 换镜像会作废 T02 的 digest/E1/E2 证据，须单独立项授权；musl→glibc 基底切换需备份演练；该 digest 来自镜像源（`docker.1ms.run`），需在直连环境复核；真实 recall/10 万级容量 `NOT_RUN` |

## 4. 提交决策

- 用户答复：**待询问**
- 拟提交（待确认，仅本地）：
  1. `feat(phase0): add W5 probes - index staging (T12), harness integration (T21), task scheduler (T23)`
  2. `docs(phase0): record W5 gate result`
- 不提交：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`
