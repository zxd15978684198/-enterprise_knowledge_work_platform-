# P0-T28 Research 全链控制站集成 — 证据报告

- 波次：W9（`docs/poc/evidence/wave-w9-record.md`），分支 `feature/w9-t28-t29`，HEAD `f7b9676`（base `feature/w8-t26@f7b9676`）
- 任务卡：`docs/poc/03_Phase0_Task_Backlog.md` P0-T28（L473 起）；依赖 T27/T18/T26/T13/T21
- 资源槽：PG schema `p0_t28`（结束时 DROP）、Redis DB 15（本任务不使用）、控制站端口随机/5501（未占 T29 的 5600）
- 凭据：一律经 `scope.read_dsn()` / `.env.example` 读取，本文件不含口令；DSN 不落日志
- 限域：**无真实 chat 模型** → 注入式 `ModelPort` 确定性替身，因此「真实模型链路」`NOT_RUN`，结论最高 `PASS（限域）`；BSK 真机需 `BSK_LIVE=1`，未开启 → 用户浏览器通道用固定快照，**真机 NOT_RUN**

## 1. 交付物

| 文件 | 行数 | SHA-256 | 职责 |
| --- | --- | --- | --- |
| `experiments/phase0/research_probe.py` | 1464 | `faa61045abaab5cc8e89e3eaeb33a68f338fc61ccef7786e7d5d59c99b37a055` | schema `p0_t28` 事实层：DDL/触发器、run/来源/证据/候选/引用/审批/Job/事件、finalize/publish/cancel、可追溯性校验 |
| `experiments/phase0/research_flow.py` | 1273 | `0dc917b4ad2ee843d315b6c70d5f288b58fc583b463e4e4a3bd54b68573e488a` | 编排层：三读取通道适配、ModelPort 替身、LangGraph 状态图（plan→schedule→fetch/analyze→approve→publish→finalize）、resume/cancel、30 任务批量 |
| `tests/phase0/test_research_e2e.py` | 738 | `ef1da8dad508c347c938dd079b0151c00affd4ad1c06608fdd014f04d5812d0f` | 20 用例（含 30 任务批量与逐 run 回源校验） |
| `docs/poc/evidence/research-batch.json` | 22,329 B | — | 30 任务批量真实结果（每行 run_id/expect/actual/status/published/external_applied） |

迁移内联在 `research_probe.MIGRATION_SQL`（`MIGRATION_ID="t28_research_inline"`）：`sha256=a46d28e9ac0d6eb9d450997c753459561fd597de22c8335287c8eeb7629b073c`，10 张表、12 种 `event_type`、每 run 唯一 terminal 部分唯一索引、发布候选至少一条引用的延迟约束触发器。未新增编号迁移文件（写域禁止，且 schema 独占）。

## 2. 命令与真实结果

```bash
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
  tests/phase0/test_research_e2e.py -q -o addopts= -p no:cacheprovider
# → 20 passed in 67.53s  (PYTEST_EXIT=0)

uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
  experiments/phase0/research_probe.py experiments/phase0/research_flow.py tests/phase0/test_research_e2e.py
# → All checks passed!  (RUFF_EXIT=0)
```

批量结果（`rf.seed_tasks()` → `rf.run_batch(...)`，30 任务）：

```json
{"succeeded": 12, "approval_rejected": 6, "budget_exceeded": 4,
 "browser_down": 4, "plan_invalid": 4, "unexpected": 0}
```

单次正路径实测（含审批中断）：首次 `waiting_approval`（返回原生 interrupt），resume 后 `succeeded`、`published=1`、`candidates=1`、`citations=3`、`sources=4`（search/http/js/user_browser 四通道）、`events=16`、`terminal=1`、`answer_kind=grounded`；外部写幂等键 `t28-research:{run_id}:{candidate_id}` 仅 1 个 key、`applied_calls=1`、`attempts=1`。

失败→修复历史（真实，供回归对照）：

1. `phase0_t28_candidates_guard` 抛 `P0-T28 candidate row_version 必须严格 +1`：`decide_approval` 对 rejected 走两条 UPDATE，第二条未递增 row_version → 合并为单条 UPDATE + `CASE`。
2. 控制站幂等写从 **JSON body 的 `key`** 记账（非 header），计数端点 `GET /idem/count?key=`，快照端点 `GET /_control/state`；`run_batch` 原先按不完整的 key 前缀查询导致 `external_applied` 恒为空 → 改为全量快照按 run 前缀过滤。
3. LangGraph 1.2 在 `interrupt()` 时 `app.invoke` 不抛异常而是返回含 `__interrupt__` 的 dict → 调度层同时处理返回值与异常两种形态。
4. `tool_ledger.intent_key` 含随机 `response_artifact_id`，导致审批与发布 intent_key 不一致 → 改确定性 `uuid5`，并在发布前用同一 candidate body 复算并比对。

## 3. 验收逐项

| # | 验收项（任务卡/波次口径） | 结论 | 证据 |
| --- | --- | --- | --- |
| A1 | 目标→DAG→搜索/HTTP/JS/用户浏览器→质量/去重/审阅→Job→发布→RAG 引用的全链，不新写子系统 | PASS | `test_run_executes_three_channels_and_waits_for_approval`、`test_resume_approves_and_publishes_in_one_transaction`；复用 T22 planner、T23 scheduler、T20 budget、T19 tool_registry、T04 control_site |
| A2 | 30 任务覆盖正负路径与三读取通道 | PASS | `test_batch_thirty_tasks_classifies_all_outcomes`；`unexpected=0` |
| A3 | 已发布候选 100% 有快照/时间/通道/许可/证据/审核 | PASS | 12 个已发布 run 逐个 `verify_traceability`：`published_candidates=1`、`complete_chains=1`、`incomplete=0`、`problems=[]`；`test_traceability_report_is_complete`、`test_citations_trace_back_to_authorized_sources` |
| A4 | 禁止/越权/无证据发布 0 | PASS | `test_publish_requires_approval_reference`（无审批 → `ApprovalRequired`）、`test_approval_revoked_after_decision_blocks_publish`（撤权后 0 发布/0 indexed/0 Job）、`test_candidate_without_citation_cannot_commit`（延迟约束触发器抛 `CheckViolation`） |
| A5 | 精确重复不重索引 | PASS | `test_exact_duplicate_content_reuses_index_job`（同 `dedupe_key` 复用同一 job_id，外部写仍 1 次） |
| A6 | 两轮变化/未变化状态正确 | PASS | 上条 + `test_changed_content_creates_new_job`（内容变化生成第二个 job） |
| A7 | 任务成功率分类实报 | PASS | 批量 summary 六类计数，`unexpected=0`；`expect == actual` 逐行断言 |
| A8 | 审核后撤权 | PASS | `test_approval_revoked_after_decision_blocks_publish` |
| A9 | 批准后 DB/Redis 故障 | PARTIAL | DB：`test_finalize_writes_terminal_only_on_commit`（未提交不可见、提交后唯一 terminal、第二条 terminal 抛 `UniqueViolation`）；`test_cancel_after_interrupt_keeps_single_terminal`。Redis：本任务不经 Redis，故 Redis 故障 **NOT_RUN**（见 §6） |
| A10 | 浏览器断线 | PASS | `test_broken_js_channel_fails_without_publication`（`SOURCE_UNAVAILABLE`、0 发布）；批量 `browser_down=4/4` |
| A11 | 旧版本晚到 | NOT_RUN | 属 T13/T26 的版本可见性域；T28 只消费「已发布授权版本」，不自建版本竞态（见 §6） |
| A12 | 最后预算竞争/秘密 canary | PARTIAL | 预算：`test_tight_budget_stops_before_any_publication`（`BUDGET_EXCEEDED`、0 来源/0 发布）+ 批量 `budget_exceeded=4/4`；「预算竞争」并发抢占**未构造**（PARTIAL）。秘密：`test_event_payloads_contain_no_credentials`（事件 payload 无凭据/canary）PASS |
| A13 | 取消可复现 | PASS | `test_cancel_after_interrupt_keeps_single_terminal`（取消后 resume 复放、不发布、terminal 唯一） |
| A14 | 最终引用回源验证 | PASS | 引用 quote 的 sha256 与授权来源正文一致、`license` 非空、chunk 属于该 workspace 的活动版本（`test_citations_trace_back_to_authorized_sources`） |
| A15 | DoD：D0 + 三通道/审核/入库/引用全链证据 + 残余风险 + P1 范围建议 | PASS | 本报告 §1–§7 |

## 4. 故障注入矩阵

| # | 注入 | 用例 | 观测事实 | 结论 |
| --- | --- | --- | --- | --- |
| F1 | 审批前 DB 事务未提交 | `test_finalize_writes_terminal_only_on_commit` | 回滚后 Run 仍 running、草稿 partial、terminal=0 | PASS |
| F2 | 第二 terminal 并发提交 | 同上 | `UniqueViolation`（DB 部分唯一索引兜底） | PASS |
| F3 | 无引用的已发布候选 | `test_candidate_without_citation_cannot_commit` | COMMIT 时延迟约束触发器 `CheckViolation` | PASS |
| F4 | 审批后撤权 | `test_approval_revoked_after_decision_blocks_publish` | 0 发布、0 indexed 事件、0 Job、0 外部写 | PASS |
| F5 | 无审批引用直接发布 | `test_publish_requires_approval_reference` | `ApprovalRequired` | PASS |
| F6 | 审批拒绝 | `test_rejected_approval_keeps_research_out_of_business_facts` | run `succeeded`、`published=0`、`answer_kind=insufficient_evidence`、candidate `rejected`、无 Job | PASS |
| F7 | 预算紧张 | `test_tight_budget_stops_before_any_publication` + 批量 4 例 | `BUDGET_EXCEEDED`、0 来源、0 发布 | PASS |
| F8 | JS 通道断线 | `test_broken_js_channel_fails_without_publication` + 批量 4 例 | `SOURCE_UNAVAILABLE`、无发布 | PASS |
| F9 | 非法计划（契约违规） | `test_invalid_plan_fails_before_side_effects` + 批量 4 例 | `PLAN_INVALID`、0 来源（无副作用） | PASS |
| F10 | 中断后取消再恢复 | `test_cancel_after_interrupt_keeps_single_terminal` | 复放、不发布、terminal 唯一 | PASS |
| F11 | 重复提交（run/resume） | `test_repeated_run_and_resume_are_idempotent` | `replayed=True`、事件不增、外部写仍 1 次 | PASS |
| F12 | 完全相同内容二次发布 | `test_exact_duplicate_content_reuses_index_job` | 复用同一 `job_id` / `dedupe_key` | PASS |
| F13 | 内容变化后二次发布 | `test_changed_content_creates_new_job` | 新 `job_id`、旧 job 不变 | PASS |
| F14 | 事件/引用的篡改 | `test_events_and_citations_are_append_only` | `UPDATE/DELETE` 抛 `CheckViolation` | PASS |

## 5. 契约偏离（均不修改 ADR/TDD，交评审）

1. **schema 前缀**：本任务使用独占 `p0_t28`，而非 `001_scope_core.sql` §10 预留的 `p0_t03` 增量。原因：W9 资源槽隔离 + 001 的 teardown 会 `DROP p0_t03 ... CASCADE`。生产合并前需换共享前缀并补 RLS policy/GRANT。
2. **不建角色/RLS**：作用域在 SQL 内以 workspace + 委托关系 + 活动版本过滤；RLS 证据由 T03 提供，未在 `p0_t28` 复刻。
3. **LangGraph checkpoint 表也落在 `p0_t28`**（saver 连接 `search_path=p0_t28`），随 schema 一并清理；生产应使用共享 checkpoint schema，避免每业务域一套。
4. **不建 `p0_t26`**：T26 的 schema 由 T26 测试自行 DROP，且 W9 平行任务 T29 可能共用其槽位；本任务只在 `p0_t28` 自建事实表，复用 T26 的事件枚举常量与「每 run 唯一 terminal」口径（`phase0_t28_run_terminal_unique`）。
5. **审批拒绝/撤权后 Run 仍以 `succeeded` 结束**（`published=0`、`answer_kind=insufficient_evidence`）：研究报告本身是有价值产物，「未入库」由候选状态与 Job 缺失表达；该终态口径契约未明确，待评审。
6. **Job 只由发布路径在事务内写**（`publish_candidate` + `complete_task` 同事务），且 Job 只有 `pending` 初始态；真实执行/终态归 T08/T25，不在 T28 展开。
7. **用户浏览器通道为固定快照替身**（`live_run=NOT_RUN`），未连真实 BSK；三通道中 search/http/js 均为真实控制站 HTTP 调用，user_browser 仅结构验证。

## 6. NOT_RUN / PARTIAL 汇总

- `NOT_RUN` 真实 chat 模型链路（无可用模型凭据；替身只能测结构）。
- `NOT_RUN` BSK 真机/真实登录态（`BSK_LIVE` 未开启）。
- `NOT_RUN`「旧版本晚到」竞态（T13/T26 的版本可见性域，T28 消费已发布版本）。
- `NOT_RUN` Redis 故障注入（T28 不依赖 Redis；`REDIS_USED=False`，DB 15 仅占槽不写）。
- `PARTIAL` 批准后 DB/Redis 故障：DB 侧已有提交/回滚/唯一性证据，Redis 侧不适用。
- `PARTIAL` 预算竞争：覆盖耗尽（tight 模式）与结算，未构造「最后预算并发抢占」。
- `PARTIAL` 富化失败：Job 只到 pending，富化执行与 ACK 属 T08/T25。

## 7. 对 T31 / T32 的输入建议

1. **Run 终态与「未入库」分离**：建议 T31 契约明确 `succeeded + published=0` 的语义（研究报告成功、业务事实未变），避免下游把「成功」误读为「已入库」。
2. **审批事实与发布事实必须同源**：`intent_key` 由确定性 UUID + 同一 candidate body 复算；T31 做审批 API 时应把 candidate 版本号纳入 intent_key，防「审批 A 版本、发布 B 版本」。
3. **外部写幂等键**建议固定为 `{prefix}:{run_id}:{candidate_id}`，并把控制站返回的 `attempts/applied` 作为验收指标（重复恢复必须 `attempts>1` 但 `applied=1`）。
4. **可追溯性是可查询对象**：`verify_traceability` 应作为服务 API 暴露给 T32 的 PoC 汇总与前端展示，而不是仅测试辅助。
5. **预算竞争需要显式规格**：并行分支争抢同一预算时谁先失败、是否允许部分成功，需 T31 在契约中定义；当前只测「耗尽即停」。
6. **checkpoint schema 归属**：建议 T31/T32 统一使用只读共享 checkpoint schema，禁止在业务 schema 内建表，否则清理/迁移会互相牵连。

## 8. 复现

```bash
# 1) schema 会自动重建：测试 session fixture 先 reset p0_t28 再 apply（幂等）
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
  tests/phase0/test_research_e2e.py -q -o addopts= -p no:cacheprovider

# 2) 代码质量
uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
  experiments/phase0/research_probe.py experiments/phase0/research_flow.py tests/phase0/test_research_e2e.py

# 3) 清理确认（应为空）
#    SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'p0_t28';
```

> 清理实测：会话结束后 `p0_t28` 不存在；仓库内仅剩共享槽位 `p0_t20`（预算）、`p0_t23`（调度）、`p0_t27`（并行任务）。本任务未执行任何 git 写操作（无 add/commit/push/切分支）。
