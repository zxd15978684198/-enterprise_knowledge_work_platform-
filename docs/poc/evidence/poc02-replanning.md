# P0-T24 证据：Evaluator 与有界重规划（W6）

| 字段 | 值 |
| --- | --- |
| Task ID | P0-T24（Evaluator 与重规划） |
| Owner | `w1-t02-env` |
| Wave | W6（`feature/w6-t13-t24`） |
| 基线 | `dev@cc4a475`，HEAD `cc4a475b46ee3422270e570ad45c7253f50ba6da` |
| 状态 | **PASS**（原子契约 PARTIAL 已于 W17/D4 关闭：`Replanner` 可接真实 `p0_t20` `BudgetLedger`，见 §6 与 `wave-w17-record.md` §3.2） |
| 记录时间 | 2026-10-28（Asia/Shanghai） |
| 复现命令 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_replanning.py -q` |

## 1. 交付文件

| 文件 | 行数 | SHA-256 |
| --- | --- | --- |
| `experiments/phase0/evaluator_replanner.py` | 1622 | `8c866c5203dbdf4af0031499eb6a1315ba200abfafb364a2a15652bd6381857e` |
| `tests/phase0/test_replanning.py` | 665 | `e3cd56737e5f1a82696b245494f10c62c4627615324fb187d9748753c8831998` |

工作区脏差异（`git diff HEAD`，UTF-8 规范化后）SHA-256：`878640e1672973d0fec12c16b1ad6f7a59721e303a927ebf9309eddb1dfca18b`。
该哈希包含同波次 T13 与 Lead 的未提交改动；本任务自身只新增上述两个文件（`git status --porcelain` 中另有 T13 的 `index_publication.py` / `008_publication.sql` / `test_publication_faults.py`，未触碰）。

未新增编号迁移（`007` 预留 T27、`008` 已归 T13）：T24 的账目表在独立 schema `p0_t24` 内用内联 DDL 建立。

## 2. 环境与资源槽

| 项 | 实测值 |
| --- | --- |
| PostgreSQL | `127.0.0.1:5433`，`PostgreSQL 17.11 on x86_64-pc-linux-musl`，库 `pyknora` |
| Redis | `127.0.0.1:6380`，DB 10（本任务的预算/唤醒扩展位） |
| T23 schema | `p0_t23`，由测试 session fixture 调用 `task_scheduler.apply_migration` 建立（`2b58f250f495b3473dc8d8d14c53c920ecd65e96344ad8f66b9988f8fcd6ad15`） |
| T24 schema | `p0_t24`，`evaluator_replanner.ensure_schema` 自建，session 结束 `reset_schema` 清理 |
| 隔离 | 每个用例 `uuid4()` 的 `workspace_id`/`run_id`，结束时按该键删除 `p0_t23` 六表与 `p0_t24` 账目，不 TRUNCATE 邻居 schema |

## 3. 命令与真实结果

### 3.1 测试套件（全量）

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_replanning.py
......................                                                   [100%]
22 passed in 3.88s
PYTEST_EXIT=0
```

- 用例数 22，全绿，0 skip / 0 xfail。
- `rootdir: E:\project_month\pyknora\experiments\phase0`、`configfile: pyproject.toml`、`asyncio: mode=Mode.AUTO`（显式 `-c` 是本项目的已知必要参数，否则会误用仓库根配置）。

### 3.2 关键验收子集（详细名）

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_replanning.py -v \
    -k "third or duplicate or concurrent or fingerprint or budget or replan_replaces"
collected 22 items / 13 deselected / 9 selected
experiments\phase0 .........                                             [100%]
====================== 9 passed, 13 deselected in 2.83s =======================
EXIT=0
```

### 3.3 Lint

```text
$ uv run --project experiments/phase0 ruff check experiments/phase0/evaluator_replanner.py tests/phase0/test_replanning.py
All checks passed!
RUFF_EXIT=0
```

### 3.4 首次运行的真实失败（保留，不用来冒充通过）

1. `ctx` fixture 误把 `t24_schema`（`None`）当返回值断言 → 21 个 ERROR；改为只依赖副作用后消失。
2. 并发用例暴露 `UniqueViolation: phase0_task_graphs_pkey` —— 两个 Replanner 同时通过幂等检查；已在 `Replanner.replan` 外层加 PostgreSQL 会话级 advisory lock（`_lock_key` 由 `blake2b(ws:run)` 派生），后来者读到同一 `request_digest` 返回 `created=False`。修复后 `concurrent` 稳定通过。
3. 同名请求重复调用被幂等吞掉，不能证明 CAS —— 用例改为「不同内容 + 过期 `expected_revision` 才冲突」，并同时断言“完全相同的请求是幂等重试”。

## 4. 量化验收对照

| Backlog / 派单要求 | 结果 | 直接证据（用例） |
| --- | --- | --- |
| 评估输入必须是事实，不信节点自述 | 通过 | `test_self_report_without_receipt_is_partial`（自述“已完成”但无回执 → `partial` + `RECEIPT_MISSING`） |
| 成功判定 | 通过 | `test_success_is_decided_from_facts_and_receipts` |
| 部分成功 | 通过 | `test_partial_success_overall_partial`、`test_missing_fact_key_is_partial`、`test_fact_version_too_low_is_partial` |
| 失败 | 通过 | `test_failed_node_and_blocked_dependents`（A satisfied / B failed / C blocked） |
| 依赖阻塞 | 通过 | 同上（`fail_task` 返回 `["C"]`，评估 `blocked_ids == ("C",)`） |
| 超限终止 | 通过（直接断言） | `test_third_replan_hits_limit_and_terminates_failed`：`decision=limit_exceeded`、`terminal_status=failed`、`replan_count == MAX_REPLANS == 2`、`revisions == [1,2,3]` |
| 幂等重入 | 通过（直接断言） | `test_duplicate_replan_request_is_idempotent`（`created True→False`，`to_revision` 相同，replan 行 1 条）、`test_duplicate_evaluation_is_idempotent`、`test_concurrent_same_request_has_single_winner`（两线程 → 单个 `created=True`） |
| 只替换/追加受影响子任务，不复制已完成 | 通过 | `test_replan_replaces_only_unfinished_and_reuses_completed`：revision 2 节点集合 `{B.r1, C}`，`A` 未登记，`reused == ("A",)` |
| 新 revision 不破坏无环性 | 通过 | 同用例 `dag.validate_graph(...).ok is True`；`test_replan_graph_with_cycle_is_rejected_before_registration`（环在登记前被 T22 拦截，`REPLAN_GRAPH_INVALID`） |
| revision 只有一个活动指针 | 通过 | `test_concurrent_same_request_has_single_winner`（`revisions == [1,2]`、replan 行 1 条）、`test_stale_expected_revision_raises_conflict`（过期 CAS 抛 `REPLAN_REVISION_CONFLICT`） |
| 已确认副作用重做 0 | 通过 | `test_fingerprint_mismatch_with_confirmed_side_effect_goes_review`（`needs_review` + `blocked`，`revisions == [1]`，不重跑 A） |
| 指纹不匹配不复用 | 通过 | `test_fingerprint_mismatch_without_side_effect_replans_node`（无副作用确认 → 重建 `A.r1/B.r1`） |
| 未知用量预留不释放 | 通过（直接断言） | `test_budget_insufficient_blocks_without_releasing_reservation`：`budget_exhausted`/`blocked`、`budget.release_calls == 0`、无新 revision |
| 评价标准未标注 → PARTIAL + 人工评审 | 通过（两级） | `test_unlabeled_acceptance_is_partial`；`ReplanNeedsReview` 路径见 `build_replan_graph` 的 `ACCEPTANCE_UNLABELED` 分支 |
| source 撤销 / 产物失效 | 通过 | `test_cancelled_node_is_not_silently_rerun`（cancelled → `skipped`，`noop`，不重建） |
| 缺生效日期类事实 → 新增/替换节点 | 通过（机制级） | `test_missing_fact_key_is_partial` + `test_replan_replaces_only_unfinished_and_reuses_completed`：缺键 → `partial` → 生成 `<id>.r<n>` 替换节点 |
| 不新建编号迁移 | 通过 | 迁移目录未改；`p0_t24` 内联 DDL |

## 5. 预期 vs 实际

| 项 | 预期 | 实际 |
| --- | --- | --- |
| 评估只用事实 | 回执/事实键缺失即 `partial` | 一致（回执、`min_fact_version`、必需键三条硬规则） |
| 重规划切片 | 只登记未完成 + 替换节点 | 一致（`reused` 不写库；旧 revision 只读） |
| 上限 | 第 3 次 `failed` 且无新 revision | 一致 |
| 并发 | 只有一个赢家，另一方幂等返回 | 一致（advisory lock 后稳定） |
| 预算 | 不足即 `blocked`，绝不 `release` | 一致（`StaticBudget.release` 被调用即抛错，实际 0 次；真实台账侧由 `LedgerBudget` 接线验证，W17/D4） |
| 终态 | 成功/阻塞可区分 | 一致（`runs.terminal_status ∈ {running, succeeded, blocked, failed}`） |

## 6. 未执行 / 部分项（NOT_RUN / PARTIAL）

1. **已关闭（W17/D4）：T20 真实 `BudgetLedger` 台账已接线。** 以下为关闭前的原记录，保留以说明来路：原为 **PARTIAL：T20 真实 `BudgetLedger` 台账未接入。** 本任务只用 `BudgetPort` + `StaticBudget` 验证“超额即 blocked、不 release”的契约，没有连 `p0_t20` 表做充值/结算。原因：派单限定只写 2 文件、且预算台账的写权在 T20；接口契约已冻结（`remaining_microunits` 只读）。
2. **NOT_RUN：kill -9 打断（切 revision 前后）。** 需要真实进程中断编排，属波次级故障注入（T18/T13 领域）；本任务用“旧 revision 只读 + 幂等重入 + 并发 CAS”间接覆盖。
3. **NOT_RUN：旧 revision 结果晚到**（同上，需要调度器端口回放；T23 的 `StaleEpoch` 已覆盖同类语义，T24 未重复测）。
4. **NOT_RUN：HTTP/接口层**。本任务是编排内核，无 API 面。

## 7. 残余风险

| ID | 风险 | 缓解/建议 |
| --- | --- | --- |
| R1 | advisory lock 是**会话级**，只在单活 runner 前提下成立（T05 结论） | 多实例前须换 `pg_advisory_xact_lock` + revision 行锁，或引入 leader election |
| R2 | `p0_t24` 表用内联 DDL，无编号迁移/无版本号 | 集成时把 schema 版本写进 `phase0_schema_migrations` 或并入正式迁移（T13 的 008 之后） |
| R3 | 评估策略由调用方传入，未标注标准时只 `partial` + 评审 | OrchestrationService 必须在建图时强制标注 acceptance，否则上线即大面积 `needs_review` |
| R4 | 复用判据依赖 `phase0_task_inputs` 的指纹完整性 | T25/T26 落库时必须同时写 inputs，否则一律按“未记录”拒绝复用 |
| R5 | `needs_review` 目前只写一条 `blocked` 账目，没有人工审批状态机 | T28 需接审批流（`phase0_replans` 的 `decision` 已可作状态源） |
| R6 | 替换节点 id 形如 `B.r1`，深度受 T22 默认上限 8 约束 | 连续重规划最多 2 次，正常不会触顶；超限路径已显式 `failed` |

## 8. 对 T25 / T26 / T28 的接口建议

1. **T25（RAG 编排接入）**：调用 `evaluate_run` 前必须把工具回执写入 `record_receipt`（同 revision），否则全部 `partial`；`Replanner.replan` 只在 `report.replan_required` 为真时调用，且传 `expected_revision = replanner.current_revision(...)`。
2. **T26（Agent/Research 编排）**：替换节点会继承 `kind` 与 `weight`（`build_replan_graph` 已保留），但 `TaskExecutor.runner` 必须按 `task_id` 而不是节点 index 取输入，避免 `B.r1` 落到旧输入。
3. **T28（审批/人工介入）**：`decision=="needs_review"` 与 `terminal_status=="blocked"` 是唯一需要人工的出口；`phase0_replans.reason` 已带原因码前缀（`REUSE_FINGERPRINT_MISMATCH` 等），可直接作审批单的判定依据；审批通过后以新 `ReplanRequest`（`expected_revision` 不变、`expected_fingerprints` 更新）重入，幂等由 `request_digest` 保证。
4. 统一只读入口：`Replanner.run_status(ws, run)` 返回 `{active_revision, replan_count, terminal_status}`，可作为编排层的单一事实源。
