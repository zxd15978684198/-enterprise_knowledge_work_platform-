# P0-T26 证据：LangGraph RAG、可核验引用与终态一次事务提交

- 任务：`P0-T26`（W8 波次唯一任务，N=1）
- 分支：`feature/w8-t26`，基线 `dev@9d3252a`；HEAD 未移动（本任务未提交）
- 执行时间：2026-10-29（Asia/Shanghai）
- PG schema：`p0_t26`（会话结束 DROP，验证无残留）；Redis DB 14 本任务未使用
- 环境：`uv 0.12.24`，实验 venv Python 3.12.13，langgraph 1.2.18，psycopg 3.3.6，pytest 9.1.1
- **限域声明**：没有真实 chat 模型，使用可注入的 `ModelPort` 替身（`DeterministicModel`/
  `CrashAfterTokensModel`）。因此本报告最高判定为 **PASS（限域）**；「真实模型链路」为 NOT_RUN。

## 1. 交付物

| 文件 | 行数 | SHA-256 |
| --- | --- | --- |
| `experiments/phase0/rag_probe.py` | 1797 | `7a141be76dd42a0280e34f166c311ac688633cdfb5c373b68e761a1d0c4a334c` |
| `experiments/phase0/events_probe.py` | 1953 | `d01a0ef0ecc6ba6bae886d804d5451e4018cda3a1f067b7c5e382bd15af4ac0a` |
| `tests/phase0/test_rag_events.py` | 910 | `cb07668bd627a5cd68973b611558cc61a9fa514d46eb3b7f4b95c5d4abc6779e` |
| `docs/poc/evidence/rag-citations.json` | 数据文件 | 100 例实测明细（见 §4） |

`events_probe.py` 的 `MIGRATION_SQL`（schema `p0_t26`，16 张表 + 4 个触发器）SHA-256：
`bcc8e76ef5796e110cf9262a30657c3851e2b098315fcfdb8d319306128c801e`，
`migration_id = t26_rag_events_inline`，台账与 DDL 哈希一致。

所有文件均为新增未跟踪（`??`），未修改任何受保护文件（`git status --porcelain` 仅本任务
4 项 + 他人未跟踪项；`M docs/plans/2026-10-27-phase0-execution-plan.md` 非本任务修改）。

## 2. 命令与真实结果

```bash
# 1) 全量验收
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
  tests/phase0/test_rag_events.py -q -o addopts= -p no:cacheprovider
# 真实结果：21 passed in 162.22s  (exit 0)

# 2) 静态检查（line-length=100，select E,F,I,UP,B,SIM）
uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
  experiments/phase0/rag_probe.py experiments/phase0/events_probe.py tests/phase0/test_rag_events.py
# 真实结果：All checks passed!  (exit 0)

# 3) 端到端冒烟（Graph → 终态）
uv run --project experiments/phase0 python %TEMP%\t26_smoke2.py
# status: completed  answer_kind: grounded  citations: 1  tokens: 4  terminal: completed
```

迭代记录（真实失败，均已修复；失败均在测试/替身模型侧，未放宽任何生产断言）：

| 轮次 | 结果 | 原因与修法 |
| --- | --- | --- |
| 1 | 收集失败 | `ImportError: cannot import name 'TransactionStatus' from 'psycopg'` → 改 `from psycopg.pq import TransactionStatus` |
| 2 | 12 passed / 9 failed | f-string `'{}'::jsonb` 语法错；`uuid.UUID(UUID)` 重复转换；`cancel_run` 返回 `FinalizeResult`；游标用例拿旧 token 复读；复放缺少引用 |
| 3 | 20 passed / 1 failed | 质量指标：`cross_space` 问题与 A 空间文档共享通用词（“工作日”），替身模型据此引用 → 改语料与引用门控（见 §4 限制） |
| 4 | **21 passed** | 最终全绿；`ruff` 全绿 |

## 3. 验收条件逐项核对

| # | 验收条件（提示词/任务卡） | 判定 | 证据 |
| --- | --- | --- | --- |
| A1 | 授权 → 检索/改写 → 生成 → 引用校验 → **Message+Run+terminal 一次事务提交** | PASS | 回滚最终事务后 Run 仍 `running`、草稿 `partial`、无 terminal；提交后三者都在（`test_finalize_is_single_transaction_until_commit`、`test_finalize_is_committed_atomically`） |
| A2 | token 先持久；generation reset | PASS | token 事件数 = `persisted_tokens`、offset 递增、内容拼接一致；`bump_generation` 写 `generation_reset`(from/to)，旧草稿 `incomplete`（`test_completed_commits...`、`test_stale_generation_write_is_rejected`） |
| A3 | 伪引用 / 越权引用 = 0 | PASS | `fabricated_citations=0`、`unauthorized_citations=0`、`wrong_citations=0`；伪 handle `[[99]]` 产生 `not_in_evidence` 审计且不入引用表（`test_bad_handle_is_rejected_and_audited`、§4） |
| A4 | 不足证据拒答、不编造引用 | PASS | 空 KB/火星问题 → `answer_kind=refused`、引用 0、`completed` 终态；`cross_space`/`stale_version` 各 20 例全部拒答 |
| A5 | 50 次重复 finalize 只产生一个 terminal | PASS | 50 次复放 `replayed=True`、同一 `terminal.event_id`；`count_terminal=1`、`completed` 事件=1；DB 部分唯一索引再拦一次（`test_fifty_duplicate_finalize...`、`test_finalize_conflict_and_database_terminal_uniqueness`） |
| A6 | 部分 token 后 DB 失败不出现伪 completed | PASS | `FailingTokenSink(fail_after=2)` → 2 个 token 已落库、`run.status=running`、terminal=0、`completed`=0、草稿 `partial` |
| A7 | 跨 workspace 引用拒绝 | PASS | 引用 B 空间 chunk → `cross_workspace` 拒绝，引用集合空 |
| A8 | 跨版本（非活动版本）引用拒绝 | PASS | 旧版本 chunk 即使进入 evidence 也被拒（`stale_version`/`unknown_chunk`），引用集合空 |
| A9 | 游标过期 / 越权 / 篡改拒绝 | PASS | 过期 → `EVENT_CURSOR_EXPIRED` + `run_status` + `result_url`；换主体 → `CursorScopeMismatch`；签名篡改 → `EventValidationError` |
| A10 | P1 公共续传不伪装 | PASS | `resume_events()` 恒抛 `CAPABILITY_UNSUPPORTED` |
| A11 | 模型进程 kill → 新 generation，旧 token 不入新答 | PASS | 崩溃一次后 `generation=2`、`generation_reset` 事件 1 条、旧草稿 `incomplete`、最终答案不含旧半截内容；连续两次崩溃 → `failed` 终态且 `completed=0` |
| A12 | 取消后不再改终态 | PASS | `cancelled` terminal；迟到 finalize/token 均 `RUN_ALREADY_TERMINAL`，`completed=0` |
| A13 | Rerank 故障降级可诊断 | PASS | `FailingReranker` → `diagnostics` 含 `rerank_degraded`，流程仍 `completed` |
| A14 | 100 例质量与延迟实测 | PASS | 见 §4/§6：`behaviour_match_rate=1.0`、`citation_accuracy=1.0`（40/40） |
| A15 | 引用人工核验 | PARTIAL | 引用为「本 run evidence handle + 可见性口径」的结构化校验；无真实模型输出可人工对照 |
| A16 | 真实 chat 模型链路 | NOT_RUN | 环境无真实模型；替身 `ModelPort` 端口已就绪（`run_rag(model=...)` 可注入） |
| A17 | 删除/取消/重建/源撤销 | NOT_RUN | T12 无删除/重建 API；assets 归属 D10（只读），本任务不越界 |
| A18 | Redis DB 14 投递 | NOT_RUN | 本任务事件与终态走 PG 表（C-02 事实源），不经 broker；DB 14 未使用 |
| A19 | 真实 MinIO / pgvector ANN | PASS（限域）/ NOT_RUN | 拆分看：**pgvector ANN 已由 W13 补跑**（`ann-bench.json`：HNSW recall 0.998–1.000、IVFFlat 0.971–1.000，索引名与 `EXPLAIN` 已落盘），故「ANN 不可用」不再是阻塞项；**真实 MinIO** 仍 NOT_RUN；本任务的**语义召回质量**仍 NOT_RUN（检索用 FTS + 假替身向量，非真实 embedding） |

## 4. 引用准确率实测（100 例，实测非阈值推断）

语料：A 空间（handbook/finance/empty 三 KB，owner/member/outsider；handbook 含 v2 活动版与 v1 旧版，
旧版另有一条不可见的 v2 归档文本）、B 空间（跨空间同话题）、C 空空间。

| 指标 | 实测值 |
| --- | --- |
| 样本数 | 100（answerable 40 / no_evidence 20 / cross_space 20 / stale_version 20） |
| `behaviour_match_rate` | 1.0（100/100） |
| `citation_accuracy` | 1.0（40/40 条引用命中 gold；gold 由「与问题共享内容词最多的活动 chunk」定义） |
| `citations_total` | 40（每条回答仅引用 1 条最匹配证据；不相关则拒答） |
| `fabricated_citations`（伪引用） | 0 |
| `unauthorized_citations`（越权引用） | 0 |
| `forbidden_citations`（命中旧版/他空间） | 0 |
| `wrong_citations` | 0 |
| `completed_runs` | 100 |
| `refused_runs` | 60（全部为 no_evidence/cross_space/stale_version） |

**测量限制**：替身模型的引用选择基于词面重合（`>=3` 共享 token 且 `>=2` 个「内容词」，
并拒绝「同等匹配」的歧义情形），因此准确率衡量的是「检索+可见性+引用校验链路的一致性」，
不是语义正确性。0.95 只是任务卡候选阈值（未评审），本报告不把 1.0 当成人工验收通过。
真实模型接入后需用同一 100 例与人工 gold 复核（见 §9）。

## 5. 故障注入矩阵（全部真实执行）

| 注入 | 手段 | 期望 | 结果 |
| --- | --- | --- | --- |
| 最终事务回滚 | BEGIN → `ep.finalize` → `rollback` | 三者都不在 | PASS：Run `running`、草稿 `partial`、terminal 0 |
| 部分 token 后 DB 失败 | `FailingTokenSink(fail_after=2)`（真实 `SELECT 1/0`） | 无伪 completed | PASS：2 token 落库、无 terminal |
| 模型进程 kill | `CrashAfterTokensModel` → `bump_generation` | 新 generation、旧 token 作废 | PASS：gen=2、旧草稿 `incomplete` |
| 模型连续 kill | `_AlwaysCrashModel` | `failed` 终态，无 completed | PASS |
| 用户取消 | `ep.cancel_run` | `cancelled` 终态不可覆盖 | PASS |
| 旧 generation 写入 | 用 gen=1 向 gen=2 写入 | `STALE_GENERATION` | PASS（0 行更新） |
| 重复 finalize | 50 次同内容 | 仅 1 terminal，其余复放 | PASS |
| 伪引用 | 模型输出 `[[99]]` | 拒绝 + 审计，不入引用表 | PASS |
| 跨空间/旧版本引用 | evidence 注入他空间/旧版 chunk | 拒绝 | PASS |
| Rerank 故障 | `FailingReranker` | 降级 + diagnostics | PASS |
| 游标过期 | `now = expires_at + 1s` | `EVENT_CURSOR_EXPIRED` + 最终结果指引 | PASS |
| 游标越权/篡改 | 换 principal / 改签名 | `CursorScopeMismatch` / `EventValidationError` | PASS |
| 直接写终态表 | INSERT 第二条 terminal | DB 部分唯一索引 `UniqueViolation` | PASS |
| 篡改审计 | UPDATE/DELETE `phase0_run_events` | 追加写触发器 `CheckViolation` | PASS |

## 6. 质量与延迟（实测）

| 指标 | 值 |
| --- | --- |
| 样本 | 100 例（同一进程、同一 PG） |
| 端到端延迟 p50 | 937.4 ms |
| p95 | 1495.2 ms |
| max | 2194.9 ms |
| 失败/异常轮次 | 0（100 次 `completed`） |

说明：延迟含探针框架的「每 token 独立连接 + 短事务 + 终态连接 + 事件读取」开销，
是实验装置的上下界，不是生产路径指标；生产应复用连接池（TDD07 §6 的 SSE 提交点不变）。

## 7. 设计要点（与契约对应）

1. **一次事务提交终态**：`finalize()` 在同一事务内写 `phase0_message_citations`、把草稿
   `partial→final`、Run `completed`、唯一 `completed` 事件；调用方提交失败则四者都不存在。
2. **terminal 唯一由 DB 兜底**：`phase0_run_events` 上的部分唯一索引
   `phase0_t26_run_terminal_unique`（`WHERE event_type IN ('completed','failed','cancelled')`）。
3. **token 先持久**：每个流片段独立短事务落库（`TokenSink`），终态事务只做状态迁移，
   因此「部分 token 后 DB 失败」不会出现伪 completed。
4. **generation 隔离**：`persist_tokens`/`finalize` 都以 generation 作为 CAS 条件；
   `bump_generation` 把旧草稿标 `incomplete` 并写 `generation_reset`，旧 token 永不拼入新答。
5. **引用校验用同一可见性口径**：`chunk_visibility()` 复用授权+活动指针+active 版本+
   winning attempt 的判定，与检索用的 filter-in-query 一致；被拒引用逐条落
   `phase0_citation_rejections`（reason ∈ unknown_chunk / not_in_evidence / cross_workspace /
   stale_version / revoked / duplicate）。
6. **事件追加写**：`phase0_run_events`、`phase0_message_citations`、`phase0_citation_rejections`
   上有 BEFORE UPDATE OR DELETE 触发器；游标只存 `after_seq`，P1 续传显式
   `CAPABILITY_UNSUPPORTED`。
7. **LangGraph 真实执行**：`StateGraph(RagState)` 9 节点、`get_stream_writer()` 发 custom
   token 事件、`FaultBarrier` 兼容的 `before_finalize_commit` 同步点；节点第二参数必须是
   `RunnableConfig`（否则 LangGraph 不注入 config）。
8. **检索**：FTS(bigram 词面) + 向量(RRF, `fake.lexical.v1`, dim=64) 两路融合；
   可见性在 SQL 内过滤（filter-in-query），不做「先检索后过滤」。

## 8. 契约偏离（需 Lead/评审确认）

1. **未复用 T25 的 `p0_t25` schema**：W8 要求独占 `p0_t26`，而 `retrieval_probe.SCHEMA` 为
   模块常量。做法：在 `p0_t26` 自建最小语料与检索，仅复用 T25 的纯函数（`tokenize`/
   `embed`/`cosine`）与可见性口径。T25 的三路检索未受影响；其 ANN 空结论已由 W13 补跑（见 §12）。
2. **`phase0_publication` 类 Job 事实**：本任务的 Job 只是 `phase0_publication_jobs` 之外的
   最小事实（T26 未新建 Job 表）；发布/执行语义归 T13/T25，本任务只证明「终态不含 Job 伪写」。
3. **gold 定义**：合成语料的「答案 chunk」由词面最匹配定义（见 §4 限制），不是人工标注。
4. **事件 TTL/最小保留**：TDD07 §6 建议 7 天 + `min_retained_seq`；本任务只验证游标过期语义，
   长期保留策略未实现（NOT_RUN）。
5. **取消后的审计**：取消保留终态；草稿标 `incomplete`，不删除既有 token 事件（与 C-02 一致）。

## 9. NOT_RUN / PARTIAL 汇总

- NOT_RUN：真实 chat 模型；真实 MinIO/S3；Redis DB 14 事件投递；
  删除/重建/源撤销场景；长时 soak 与并发压测；SSE 网络断开重连（P1）。
  （原列的「pgvector ANN 检索」已于 W13 补跑并从本清单移除，见 §12。）
- PARTIAL：引用人工核验（结构化校验已过，语义需真实模型 + 人工 gold）；RLS 跨空间
  （本任务用 SQL 过滤 + `chunk_visibility`，T03 的 RLS 在 `p0_t03` 未复用）；延迟指标
  （探针装置开销非生产值）。

## 10. 对 T28 / T29 的输入建议

1. **T28（前端/接口）**：`EventEnvelope` 的 12 种 event_type、`token{message_id,generation,
   offset(码点),text}`、`completed{message_id,result_hash,answer_kind,citations}` 已在
   `RunEvent.envelope()` 落地；前端按 `event_id` 去重、`generation+offset` 合并、`generation_reset`
   清空当前气泡即可，不需要理解后端事务。
2. **T28**：P0 的事件读取是「一次性 + 短游标」，不要实现 `Last-Event-ID` 自动续传；
   收到 `CAPABILITY_UNSUPPORTED`/`EVENT_CURSOR_EXPIRED` 时展示「查询最终结果」入口。
3. **T29（可观测/评测）**：`rag-citations.json` 的 100 例与 `by_kind` 可直接作为评测基线；
   指标口径（behaviour/accuracy/fabricated/unauthorized/latency）与 `evaluate_quality()` 一致，
   接真实模型后只替换 `ModelPort` 即可复跑。
4. **T29**：`citation_rejections` 表按 `(reason, run)` 可聚合出「伪引用/越权引用」告警；
   `generation_reset` 事件可作为模型崩溃率来源。
5. **T25 交接**：本任务未改动 `retrieval_probe.py`；ANN 的「索引不可用」阻塞已由 W13 补跑解除
   （`ann-bench.json`），但**语义召回质量**仍未验证，DG-4 不解除。
   若 T25 后续把 schema 参数化，可把 `p0_t26` 检索切到 T25 三路实现。
6. **索引/性能**：100 例 p95≈1.5s（装置开销为主）。真实模型接入后建议把 `TokenSink` 改为
   「同连接批量持久 + 心跳」，并保留「提交前不可见草稿」语义。

## 复现

```bash
# 前置：PG 5433 可连（凭据只经 scope.read_dsn() / .env.example 读取）
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml \
  tests/phase0/test_rag_events.py -q -o addopts= -p no:cacheprovider
uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml \
  experiments/phase0/rag_probe.py experiments/phase0/events_probe.py tests/phase0/test_rag_events.py
```

会话结束时 `p0_t26` schema 已被 DROP（测试 teardown），不污染其他波次槽位。

## 12. 变更记录（W13，2026-10-29 追加）

本报告 §3 的 A19、§8-1、§9、§10-5 已随 W13 更新，原因：ADR-0010 执行把它阻塞的 pgvector ANN 补跑了。

- **新证据**：`docs/poc/evidence/ann-bench.json`（实验代码 `experiments/phase0/ann_bench.py`，
  回归测试 `tests/phase0/test_ann_bench.py`，5 条断言全过，1.8s）。
- **实测口径**：2 万行 × 128 维合成聚簇向量（100 个随机单位中心 + 按维度归一的高斯抖动，
  确定性 seed），100 条查询落在同一批簇内，k=10，recall 对「精确全扫描」逐查询求交并取均值。
- **结果**：HNSW `ef_search=40/64/128` → recall **0.998 / 0.999 / 1.000**（p50 4.8–13.2ms，
  server 0.4–0.6ms）；IVFFlat `probes=1/8/32` → **0.971 / 1.000 / 1.000**；
  精确全扫描 p50≈15ms。索引真的被用上（`index_used` 记录索引名，`EXPLAIN` 一并落盘）。
- **新增限制（实现必读）**：带 `WHERE workspace_id = ...` 的 post-filter 变体在 50% 过滤率下
  **只返回 5/10 行**（pgvector 0.8 未开 `hnsw.iterative_scan`），recall 恒 0.5 且调大 `ef_search`
  无效。检索实现必须用「按工作区分区/子表的前置过滤」或显式开启迭代扫描。
- **不解除的判定**：本基准只证明索引近似误差与延迟，向量是合成的不是真实 embedding，
  故本报告的语义召回质量仍为 `NOT_RUN`，DG-4 仍有效。
