# PoC-03 证据：新索引完成后原子切换可见版本（P0-T13）

| 项 | 值 |
| --- | --- |
| 任务 | P0-T13「原子切换可见版本」（W6 波次，与 P0-T24 并行） |
| 分支 / 基线 | `feature/w6-t13-t24` @ `cc4a475` |
| 波次资源槽 | `pg_schema = p0_t13`，`redis_db = 9`（发布路径不经 broker，本轮未使用 Redis） |
| 迁移编号 | `008_publication.sql`（W6 记录 §1.2 判给 T13；001–006 已占用，007 预留 T27） |
| 执行环境 | PostgreSQL `127.0.0.1:5433`（库 `pyknora`）、uv 0.12.24、Python 3.12.13、pytest 9.1.1、ruff 0.14.4 |
| 凭据处理 | 连接串由 `scope.read_dsn()` 从 `experiments/phase0/.env.example` 读取；**本文件不含任何口令/令牌** |
| 结论 | **PASS（限域）** — 18/18 用例通过；未测项见 §9，不以通过项覆盖 NOT_RUN |

## 1. 交付物

| 文件 | 行数 | SHA-256（全文件） |
| --- | --- | --- |
| `experiments/phase0/index_publication.py` | 1129 | `e2b92c018fb936331d20809622068d5d26d32cf59e6bc20dee0a1ab838688d3f` |
| `experiments/phase0/db/migrations/008_publication.sql` | 271 | `52d2739f378bf9bcec61e30cd61346addd4c7271caa2f1fe97d558ce36a36f67` |
| `tests/phase0/test_publication_faults.py` | 1062 | `36020792facb17e7c45f4e5a2921ea2a9415d4d14c2619f465a5451b7d8c4bcf` |
| `docs/poc/evidence/poc03-publication.md` | 本文件 | — |

三个代码文件均为**新增未跟踪**（`git status` 显示 `??`），本轮未修改任何受保护文件：
`004_version_index.sql`、`index_staging.py`、`contracts.py`、`scope.py`、`tests/phase0/conftest.py`、
`experiments/phase0/pyproject.toml`、`uv.lock`、`compose.yaml`、`.env.example` 全部只读。

## 2. 执行命令与真实结果

```text
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
    tests/phase0/test_publication_faults.py -q -o addopts= -p no:cacheprovider
```

```text
..................                                                       [100%]
18 passed in 29.70s
PYTEST_EXIT=0
```

```text
uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
    experiments/phase0/index_publication.py tests/phase0/test_publication_faults.py
All checks passed!
RUFF_EXIT=0
```

- `-v` 另跑一次：`18 passed in 28.12s`，exit 0（即连续两次全绿，无间歇失败）。
- 迁移台账（用例 1 内断言）：`p0_t13.phase0_schema_migrations` 恰一行，`migration_id='008_publication'`，
  `sha256='52d2739f…36f67'`，与 `migration_sha256(MIGRATION_PATH)`、落盘文件哈希三者一致；
  重复 apply 抛 `psycopg.errors.DuplicateSchema`（显式失败，不静默跳过）。
- 清场：session teardown `DROP SCHEMA IF EXISTS p0_t13 CASCADE`；若 `p0_t12` 由本轮创建则一并 drop，
  跑完 `information_schema.schemata` 中两 schema 计数为 0（不残留状态）。
- 失败历史（如实记录）：首轮收集期 `psycopg.TransactionStatus` 导入错误；第二轮 16 个 ERROR（`ensure_staging_schema`
  在事务未提交时调 `apply_migration`）；第三轮 15 failed（publish 要求调用方已开事务，测试却在 IDLE 调用）；
  第四轮 10 passed / 6 failed，原因均为**测试侧**取样与场景错误（`documents.row_version` 基线取在
  `bump_desired_generation` 之前、并发用例选了两个都发布已 active 版本、回滚轮次猜版本号）。修正测试后 18/18，
  实现侧未为通过而放宽断言。

## 3. 验收逐项核对

### 3.1 提示词六条

| # | 验收点 | 结论 | 证据 |
| --- | --- | --- | --- |
| A1 | 发布前 chunk 写全但 `search_visible_chunks()` 为空 | **PASS** | 用例 3：`ready` 版本 `chunk_count=3`、`documents.active_version IS NULL`、可见行 0；发布后同查询得 3 行 |
| A2 | 单事务原子切换（指针与可见性同时生效） | **PASS** | 用例 4：提交后 `active_version=2`、v1 `retired`、v2 `active`、`documents.row_version` 恰 +1、可见集合 == v2 checksum 集合 |
| A3 | 中途注入故障后可见版本保持不变 | **PASS** | 用例 7/8/9/5：模拟提交前崩溃、`pg_terminate_backend`、`statement_timeout`、切换时刻并发读；四者的可见集合/指针/row_version/事实计数全部等于注入前 |
| A4 | 并发：旧 worker/旧 attempt 在切换后写入被拒（沿用 `STALE_ATTEMPT`） | **PASS** | 用例 12：旧 attempt `stage_chunk_batch` 抛 `ji.StaleAttemptError(code='STALE_ATTEMPT')`，chunk 行数不变（未新造错误码） |
| A5 | 幂等：重复发布不产生第二次可见性变化、不二次投递 Job | **PASS** | 用例 10：同 key 复放同一 `publication_id`/`job_id`、`job_created=False`、`document_row_version` 不变；`phase0_publications` 1 行、`phase0_publication_jobs` 1 行；同 key 换版本 → `IdempotencyConflict` |
| A6 | 回滚到上一版本可执行且证据完整 | **PASS** | 用例 16：回滚 v2→v1 内容生成 `to_version=3`（`kind='rollback'`、`rollback_of_version=1`），可见集合 == v1、v2 `retired`、审计有 `rolled_back`；用例 17：5 轮发布/回滚交替后 `publication_count=11`、`job_count=11` |
| A7 | ≥6 个故障注入用例，禁止固定 sleep | **PASS** | 8 类注入见 §4；同步点全用 `FaultBarrier`（`arm/wait_armed/release`），唯一 `sleep` 是被 `statement_timeout` 打断的 `pg_sleep` |

### 3.2 任务卡（`docs/poc/03` L218–233）量化项

| # | 验收点 | 结论 | 证据 |
| --- | --- | --- | --- |
| B1 | 100 轮竞态，每轮 ≥100 次读取，混版本/半成品/跨空间命中 0 | **PASS** | 用例 6：100×100 = **10 000 次采样**，每次 checksum 集合精确等于上一已提交版本；同轮另一 workspace 恒空集 |
| B2 | v1 晚到不覆盖 v2 | **PASS** | 用例 13：`desired_generation=2` 后发布 v1 → `StaleGenerationError(code='STALE_GENERATION')`，active 不变 |
| B3 | 核心成功不被富化改为 failed | **PASS** | 用例 15：发布 Job 置 `failed` 后版本仍 `active`、可见 3 行不变 |
| B4 | 同配置 failed Job 受控重试不改内容版本 | **PARTIAL** | 只能证明「Job 状态变化不影响版本」；真实重试器属 T08/T25，本轮无 worker |
| B5 | 删除提交后新查询 0 / 取消/重建不复活 / 历史引用与 GC | **NOT_RUN** | T12 未提供删除/取消/重建 API；`assets`/`asset_bindings` 属 D10 只读域，本任务无权访问 |
| B6 | 对象丢失注入 | **NOT_RUN** | 对象存储为 `InMemoryObjectStore`（T12 遗留假实现），无真实对象生命周期可注入 |
| B7 | DoD：D0 + 完整时间线/读采样元组/最终指针/对象引用/残余风险 | **PARTIAL** | 时间线由审计表 `phase0_publication_events`（8 种 outcome）给出，读采样元组、最终指针、残余风险见 §4–§6；对象引用未实现 |

## 4. 故障注入清单

| # | 注入 | 机制 | 断言（真实观测） |
| --- | --- | --- | --- |
| 1 | 提交前崩溃 | 线程停在 `after_activation_before_commit` 后抛 `_SimulatedCrash`，不提交 | 独立连接看不到中间态；指针/`row_version`/可见集合/事实计数 = 注入前 |
| 2 | 后端被杀 | 同步点阻塞时另一连接 `pg_terminate_backend(pid)` | worker 抛连接错误；事务整体消失；四项基线不变 |
| 3 | 语句超时 | `SET LOCAL statement_timeout='80ms'` + `pg_sleep(0.3)` | 抛 `QueryCanceled`；rollback 后四项基线不变 |
| 4 | 并发争抢 | 两发布者争同一新版本，第二方阻塞在 `documents` 行锁（按 pid 查 `pg_stat_activity.wait_event_type='Lock'`） | 只有一方切换；另一方式幂等复放（`replayed=True`、同 `publication_id`、`row_version` 只 +1） |
| 5 | 旧 attempt 写入 | 切换后拿旧 `Attempt` 调 `stage_chunk_batch` | `STALE_ATTEMPT`；chunk 行数不变 |
| 6 | 旧版本晚到 | 推进 `desired_generation` 后发布旧版本 | `STALE_GENERATION`；active 不变；审计留 `stale_generation` |
| 7 | manifest 篡改 | 直接改版本行 `manifest_checksum` | `ManifestMismatch`；不建事实、不切指针、版本仍 `ready` |
| 8 | 富化失败 | 发布后把 Job 置 `failed` | 核心版本仍 `active`、可见性不变 |

> 同步点集合：`before_validation` → `before_activation_commit` → `after_activation_before_commit`，
> 由 `SyncPoint` 协议注入；`index_publication` 生产代码不 import `faults`。

## 5. 用例清单（18 条，真实全绿）

1. `test_migration_008_shape_ledger_and_reapply` — 5 张表、约束与触发器存在；台账哈希三处一致；重复 apply 显式失败。
2. `test_publication_facts_are_immutable_and_jobs_require_publication` — 发布事实/审计 UPDATE 抛 `CheckViolation`；孤立 Job 抛 `ForeignKeyViolation`。
3. `test_staging_complete_but_invisible_until_publish` — 写全但不可见（A1）。
4. `test_publish_switches_pointer_and_visibility_in_one_commit` — 指针+可见性同提交生效（A2）。
5. `test_readers_never_see_partial_switch_during_hold` — 切换被挂起期间 120 次读只见旧版本。
6. `test_hundred_rounds_of_contended_reads_never_see_mixed_versions` — 100×100 = 10 000 次采样零混版本（B1）。
7. `test_crash_before_commit_leaves_visible_version_unchanged` — 提交前崩溃无痕迹（A3）。
8. `test_backend_kill_mid_transaction_leaves_no_partial_switch` — 后端被杀无部分切换（A3）。
9. `test_statement_timeout_in_transaction_rolls_back_switch` — 超时回滚（A3）。
10. `test_duplicate_publish_is_idempotent_no_second_switch_no_second_job` — 幂等（A5）。
11. `test_concurrent_publishers_only_one_wins` — 并发只一方生效，另一方复放（§4-4）。
12. `test_old_attempt_write_after_switch_is_rejected` — 旧 attempt 被拒（A4）。
13. `test_late_old_version_publication_is_rejected` — 旧版本晚到被拒（B2）。
14. `test_rejected_attempt_audit_is_persisted_when_committed` — 拒绝原因在提交后可从审计复验（`detail` 含 `desired=`）。
15. `test_enrichment_job_failure_does_not_change_core_version` — 富化失败不改核心（B3）。
16. `test_rollback_republishes_history_as_new_version` — 回滚生成新版本并复现历史内容（A6）。
17. `test_repeated_publish_rollback_cycles` — 5 轮发布/回滚，版本与事实计数线性增长、无重复事实。
18. `test_manifest_tamper_blocks_publish_without_switch` — manifest 篡改挡住发布（§4-7）。

## 6. 设计要点与量化数据

**三件事一次事务**：旧 `active`→`retired`、目标 `ready`→`active`、`documents.active_version` CAS 指针
（`row_version` 严格 +1）；发布事实 `phase0_publications`、发布内 Job `phase0_publication_jobs`、审计
`phase0_publication_events` 与三件事同一事务提交，任一步失败整笔回滚。

**锁顺序**：`documents` 行 `FOR UPDATE` → 版本行 `FOR UPDATE`。T12 的 `begin_attempt`/`bump_desired_generation`
先锁同一 `documents` 行，因此 staging 侧与发布侧天然串行，publish 后不可能被旧 attempt 追写。

**幂等**：`(workspace_id, document_id, idempotency_key)` 唯一；复放直接返回既有事实并追加
`idempotent_replay` 审计，不写指针、不写 Job；同 key 不同目标 → `IdempotencyConflict`。

**Job 唯一写入者**：Job 只在 `_activate`（发布路径、事务内）出现，`dedup_key = f"{kind}:{document_id}:{version}"`
带 `ON CONFLICT DO NOTHING`；幂等键与 dedup_key 双向防二次投递（C-01）。

**回滚语义**：004 状态机只允许 `ready→active`，`retired→active` 非法；因此回滚 = `desired_generation+1`
新建版本行，按目标版本 chunk 重建内容与 manifest，再走同一条 `_activate`。既保持历史引用指向旧版本，
又让可见内容回到旧版本。

**可见性探针**：`read_visibility()` 直接调用 T12 的 `search_visible_chunks()`，与生产读路径同一条 SQL
（`documents.active_version = index_version AND versions.status='active'`），不另写一套口径。

| 量化项 | 实测值 |
| --- | --- |
| 并发读采样 | 10 000 次（100 轮 × 100 次），零混版本、零跨空间命中 |
| 每次切换的指针写入 | 1 次（`documents.row_version` 恰 +1） |
| 重复发布新增事实 | 0（`publication_count`、`job_count` 不变） |
| 100 轮结束状态 | `active_version=101`、`publication_count=101`、`job_count=101` |
| 5 轮发布/回滚 | `publication_count=11`、`job_count=11`，无重复事实 |
| 回滚内容一致性 | 回滚后可见 checksum 集合逐行等于目标版本 chunk 集合 |

**schema `p0_t13` 五张表**：`phase0_schema_migrations`、`phase0_publications`（不可变，含 kind 形状 CHECK、
idempotency/target 双唯一键）、`phase0_publication_jobs`（FK 指向 publications）、
`phase0_publication_events`（只追加，`event_seq` identity 提供全序）、以及由触发器实现的三条不变式
（发布事实不可变、审计只追加、Job 状态机 `pending/claimed/succeeded/failed`）。版本状态机仍完全归 004，
本轮不复刻。

## 7. 契约偏离与冲突（交 lead 裁决，未改 ADR 与 `docs/poc/00–07`）

1. **schema 落位**：001 §10 留位说 T13 的指针切换在 `p0_t03` 内增量；本轮按 W6 资源槽落在独立 schema
   `p0_t13`（发布事实）+ 只读写 `p0_t12`（版本/指针）。原因同 T07/T12：并行波次各占 schema 槽，001 会话
   teardown 会 `DROP SCHEMA p0_t03 CASCADE`。**生产合并前**需并入共享前缀并补 RLS policy/GRANT。
2. **跨 schema 写无 FK**：`index_publication` 直接 UPDATE `p0_t12` 的 `phase0_documents` /
   `phase0_document_versions`；008 未建跨 schema 外键（保持 `p0_t13` 可独立重建），「版本行存在」由代码校验。
   生产建议加 FK 或同 schema 放置。
3. **回滚消耗 generation**：契约未定义 `retired→active` 是否合法，004 禁止该边，本轮用「新版本行重放」，
   因此回滚会推进 `desired_generation`。若 T25/T28 期望回滚不推进版本号，需契约修订。
4. **Job 语义最小**：发布内 Job 只有 `kind='index.enrichment'` 与四态事实，证明「由发布路径在事务内写、
   不二次投递」；真实执行/重试/死信归 T08/T25，本轮不实现（不重复造状态机）。
5. **单 active 唯一性无 DB 约束**：004 无「每 document 一个 active」约束，本轮由「锁 documents 行 + 版本状态机 +
   提交前读自检」保证；生产建议加部分唯一索引 `UNIQUE (workspace_id, document_id) WHERE status='active'`。
6. **被拒尝试的审计**：发布抛出前的审计行随事务回滚，故「被拒绝的发布」只有在调用方**提交**该失败事务后才留痕
   （用例 13/14 显式演示）。生产上建议失败路径用独立短事务或日志通道记录，否则拒绝原因可能丢失。

## 8. NOT_RUN / PARTIAL 汇总

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 删除提交后新查询 0、取消/重建不复活、源撤销 | **NOT_RUN** | T12 无删除/取消/重建/撤销 API；`assets`/`asset_bindings` 属 D10 只读域 |
| 对象丢失 → 发布前 `OBJECT_MISSING` | **NOT_RUN** | 假对象存储 `InMemoryObjectStore` 无真实删除路径 |
| 富化真实执行、ACK 丢、enrichment 重试 | **PARTIAL** | 只有 Job 状态事实与「失败不改核心」断言；真实 worker/ACK 属 T08/T25 |
| 跨空间越权（RLS/GRANT 层） | **PARTIAL** | 只断言 `search_visible_chunks` 按 `workspace_id` 隔离；RLS 在 `p0_t03`，本轮未复用 |
| 真实 MinIO/S3、pgvector 检索 | **NOT_RUN** | 环境无 `vector` 扩展（T12 已记录）；对象存储为内存假实现 |
| Redis DB 9 | **NOT_RUN** | 发布路径不经 broker（Job 由发布事务写入），本任务未使用 Redis |
| 100 轮 × 100 读之外的长时间 soak | **NOT_RUN** | 本轮只跑到 100 轮（10 000 采样），未做分钟级 soak |

## 9. 对 T25 / T26 的接口建议

1. **T25（读路径）**：读者必须单快照取「指针 + chunk」。本轮可见性断言是**单条 join**；T25 若拆成多步查询，
   必须用 `REPEATABLE READ` 或同一语句，否则可能读到指针切换前后的混合视图。
2. **T25（Job 终态）**：发布只创建 `pending` Job，终态必须由 JobService CAS 提交，**不得由 broker 或发布模块直写**；
   建议以 `publication_id` 作为 Job 的父事实，`job_id` 与 `publication_id` 一一对应（本轮已满足）。
3. **T25（拒绝原因）**：失败尝试的审计需在独立短事务写入（见 §7.6），否则 `stale_*` / `conflict` 原因会随回滚消失。
4. **T26（删除/GC）**：删除必须先确保「无 active 指针 + 历史引用释放」，再回收版本行；回滚会推进
   `desired_generation`，GC 不得把「generation 跳号」误判为空洞而提前回收被引用的版本。
5. **T26（重试/死信）**：富化 Job 的 retry/dead-letter 建议复用 T08 的 `phase0_deliveries` 五态与 `repair_count`，
   不要为发布再造一套投递状态机。
6. **生产化清单**：`p0_t13` 并入共享 schema；补 `UNIQUE (workspace_id, document_id) WHERE status='active'`；
   补 RLS policy/GRANT；发布事实与审计按 workspace 分区（当前仅有 workspace 前导索引）。

## 10. 复现

```bash
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
    tests/phase0/test_publication_faults.py -v -o addopts=
uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
    experiments/phase0/index_publication.py tests/phase0/test_publication_faults.py
```
