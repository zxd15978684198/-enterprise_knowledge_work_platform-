# Wave W7 记录（P0-T25 / P0-T27 / P0-T30）

> 按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立。门禁最后一步固定为「回填本文件 §4」。

## 1. 波次声明（启动时填写）

```yaml
wave: W7
branch: feature/w7-t25-t27-t30
base: 8f82061                 # W6 顶点（W6 已提交推送但未合入 dev）
started_at: 2026-10-28T09:40+08:00
parallel_tasks: [P0-T25, P0-T27, P0-T30]
N: 3
serial_tasks: []
blocked_tasks: [P0-T18]
per_task:
  - task: P0-T25
    agent: w2-t03-scope
    write_scope:
      - experiments/phase0/retrieval_probe.py
      - tests/phase0/test_retrieval_permissions.py
      - docs/poc/evidence/retrieval-baseline.json
    resource_slot: {pg_schema: p0_t25, redis_db: 11}
  - task: P0-T27
    agent: w1-t14-browserskill
    write_scope:
      - experiments/phase0/candidate_review.py
      - experiments/phase0/db/migrations/007_candidates.sql
      - tests/phase0/test_candidate_review.py
    resource_slot: {pg_schema: p0_t27, redis_db: 12}
  - task: P0-T30
    agent: w3-t05-checkpoint
    write_scope:
      - experiments/phase0/restore_probe/
      - tests/phase0/test_restore_upgrade.py
      - docs/poc/evidence/restore-runbook.md
    resource_slot: {pg_schema: p0_t30, redis_db: 13}
```

### 1.1 N 的计算依据（§4.5.2 三层规则）

| 层 | 结论 |
| --- | --- |
| 依赖层 | T25←T13、T19 均 PASS（限域）；T27←T11、T16、T24、T03 均 PASS（限域）；T30←T05（**PARTIAL**）、T08、T13、T21 均 PASS（限域）。T05 的 PARTIAL 意味着「单活 runner」，因此 T30 只能验证单实例语义，多实例切换/leader election 必须记 NOT_RUN，不得按全通过写 |
| 写入权层 | 三份写权互不相交；`007` 迁移归 T27（`001`–`006`、`008` 已占用）；`004_version_index.sql`、`008_publication.sql`、`006_tasks.sql`、`contracts.py`、`scope.py`、`conftest.py`、`pyproject.toml`、`uv.lock`、`compose.yaml`、`.env.example` 全部只读 |
| 资源层 | PG 独立 schema `p0_t25`/`p0_t27`/`p0_t30`；Redis DB 11/12/13（0–10 已用）；无固定端口需求；**禁止对 core 卷（pgdata/redisdata）做 restore/drop**，T30 的备份演练使用独立临时 database 与 `%TEMP%` 目录并在结束时清理 |

→ **N = 3**。

### 1.2 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| C-01 / C-09 | 发布事实与 Job 唯一写入者归 T13 路径；T25 只读可见性视图，不得直写 `phase0_publications` 或 Job 表 |
| T25 口径 | 必须 `REPEATABLE READ` 单快照同时取「active 指针 + chunk」，禁止多步查询拼接；100 QA 三路对照（向量/FTS/BM25），跨空间命中必须为 0 |
| T27 口径 | 候选/去重/审阅追加历史；撤销后不可入库；缺来源/证据不得发布；`007_candidates.sql` 只能新增，不改 `001`–`006`/`008` |
| T30 限域 | 只能做单实例备份/还原/迁移/回滚；`T05` 的 PARTIAL 必须写进证据的「残余风险」；不得声称 RPO/RTO（未校准，标 NOT_RUN） |
| 依赖纪律 | 新包一律 `uv run --with`，**依赖准入需 Lead 批准**，不得私改 `pyproject.toml`；pytest 必须 `-c experiments/phase0/pyproject.toml` |
| 质量纪律 | 禁 `skip`/`xfail` 冒充；未执行项记 `NOT_RUN`/`BLOCKED`；证据内 DSN/口令脱敏（只经 `scope.read_dsn()`） |
| Git 纪律 | 波次内禁止 `git add/commit/push/branch/switch` |
| 终端纪律 | `pwsh` 工具可用（首选）；大输出写日志再读 |

## 2. 状态表（执行中更新）

| 任务 | 状态 | 负责人 | 自报测试 | Lead 复验 | 备注 |
| --- | --- | --- | --- | --- | --- |
| P0-T25 | **完成（PASS 限域）** | w2-t03-scope | 12 passed | 12 passed / ruff 通过 | 授权检索 + 三路对照 100 QA；跨空间命中 0；ANN/召回 **NOT_RUN**（本机 PG 无 pgvector，向量列用 `float8[]`） |
| P0-T27 | **完成（PASS 限域）** | w1-t14-browserskill | 66 passed | 66 passed / ruff 通过 | 来源质量/去重/审阅追加历史；`007_candidates.sql` 709 行；撤销后不可入库 |
| P0-T30 | **完成（PASS 限域）** | w3-t05-checkpoint | 21 passed | 21 passed / ruff 通过 | 单实例备份/还原/迁移/回滚；RPO/RTO **NOT_RUN**；T05 的 PARTIAL 已写入残余风险 |
| P0-T18 | BLOCKED | — | — | — | 依赖 T17 真协议/真设备；T17 已在 `fix/w5-t17` 单独收口，不属本波次 |

## 3. 门禁检查（波次末填写）

- [x] 各任务四项报告交齐（T25/T27/T30 均按提示词 06 四项结构交付）
- [x] 越界写入核对：仅 §1 声明路径新增（14 个文件，见 §3.1）；`contracts.py`、`scope.py`、`conftest.py`、`pyproject.toml`、`uv.lock`、`compose.yaml`、`.env.example`、迁移 `001`–`006`/`008` 均未改
- [x] 迁移编号核对：`007_candidates.sql` 归 T27；目录内 `001`–`008` 连续，无重号/跳号
- [x] pytest（逐文件）+ ruff + 秘密扫描（数字见 §3.2）
- [x] `git status` / `git diff` 审阅：本分支只新增未跟踪文件，无既有 tracked 改动
- [ ] 追加 `docs/IMPLEMENTATION_STATUS.md` 小节并同步计划前沿到 W8 —— **合入 dev 后执行**（避免与 W6/W7R 的 status 段落冲突）
- [x] 用户已确认：本波次交付需提交并合入 dev（2026-10-28「补一个提交到 W7 分支再合进来」）
- [x] **回填本文件 §4**

### 3.1 交付哈希（sha256，提交前实测）

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/retrieval_probe.py` | 1792 | `bbc43ecad21ea45e093387560f483b5f8a60ea147f6b6775feeab633d82c134b` |
| `experiments/phase0/candidate_review.py` | 2106 | `afd3155736d7dc7d9e229ac660ab704defb0f8d85a374dcc2761278236360a8b` |
| `experiments/phase0/db/migrations/007_candidates.sql` | 709 | `280f77673f3abd86ef50212ac2e7faa113c3232f654f26b4b0586b3f065c4aef` |
| `experiments/phase0/restore_probe/__init__.py` | 50 | `7b4812e4232eb8e9aa72c5851132e8af98e909a4e8a9bba594f0ad4d3d979add` |
| `experiments/phase0/restore_probe/__main__.py` | 10 | `4467f23ca9591de23146ee344d8da3c24f122e19fbef0609766ce1490bab6051` |
| `experiments/phase0/restore_probe/pgctl.py` | 844 | `76bc9208b781407d708a0529e77b11a3d7d27c2b6fe40b664edb5286d9ffe7a4` |
| `experiments/phase0/restore_probe/migrations.py` | 274 | `f10831a3c3eab7c3102564978e1a07c3e92a95a7832ab767afa767b62c49c99a` |
| `experiments/phase0/restore_probe/inventory.py` | 251 | `576141de414c451f43057cf1a87e067fa60d18928cf32a91811b2e2383d449a0` |
| `experiments/phase0/restore_probe/drill.py` | 547 | `087ce82e7fa10f9bb6f6d4a77cd0ef68bcbe3ce4d046c3990874d436aaa1f6fc` |
| `experiments/phase0/restore_probe/seed.py` | 461 | `d68b99f5c5c9f665a4d5a3d046013536dec8371ab1466fc1d43381cb9e5599c4` |
| `tests/phase0/test_retrieval_permissions.py` | 641 | `87fd9ef72ffa04846ad10d87c7063972af1dda371176dcc521a2a908291ef009` |
| `tests/phase0/test_candidate_review.py` | 1041 | `21c33335efe563b367a9791fc78d6b57ae3f49614d29e130014ccf103ba1c9d0` |
| `tests/phase0/test_restore_upgrade.py` | 371 | `c1f718db240a1f0a1cad50c9717d8f73b2e4d33241dbc0c0fbdc858c3c92cdf9` |
| `docs/poc/evidence/retrieval-baseline.json` | 399 | `3c05716cd37f493eafaffd373d3db728a32cca99837fecc12ea2c76876efba9e` |
| `docs/poc/evidence/restore-runbook.md` | 317 | `762995974549fce38e6726cc443140c439f7db3552f9714fbefaf41fe775c30b` |

### 3.2 门禁实跑（2026-10-28T13:09–13:12 +08:00，Lead 独立复跑）

| 检查 | 命令 | 真实结果 |
| --- | --- | --- |
| ruff | `uv run --project experiments/phase0 ruff check <六个目标>` | `All checks passed!` exit 0 |
| T25 | `pytest -c experiments/phase0/pyproject.toml tests/phase0/test_retrieval_permissions.py -q` | `12 passed` exit 0 |
| T27 | `... tests/phase0/test_candidate_review.py -q` | `66 passed` exit 0 |
| T30 | `... tests/phase0/test_restore_upgrade.py -q` | `21 passed` exit 0 |
| 合计 | 三文件同跑 | `99 passed` exit 0 |

## 4. 提交决策与结果

- 用户答复：2026-10-28「补一个提交到 W7 分支再合进来（这样才算 W7 真正收口）」→ 交付落库后合入 `dev`
- 实际提交：本次分支提交（feat = 探针与测试；docs = 本记录 §2–§4 回填），SHA 见 `git log`
- 推送：`origin/feature/w7-t25-t27-t30`（先于合入）
- 合入：`dev` 以 `--no-ff` 合并本分支（保留波次边界）
- 未提交清单：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`（历史噪声，不入库）
