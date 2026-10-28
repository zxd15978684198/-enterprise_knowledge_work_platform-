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

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T25 | 进行中 | w2-t03-scope | 授权检索、三路对照、跨空间零命中 |
| P0-T27 | 进行中 | w1-t14-browserskill | 来源质量、去重、人工 Review 契约 |
| P0-T30 | 进行中 | w3-t05-checkpoint | 单实例备份/还原/迁移/回滚演练（限域） |
| P0-T18 | BLOCKED | — | 依赖 T17 真协议/真设备，未派 |

## 3. 门禁检查（波次末填写）

- [ ] 各任务四项报告交齐（摘要/测试命令与结果/验收逐项/风险与后续）
- [ ] 越界写入核对（只允许 §1 声明的写权路径）
- [ ] 迁移编号核对（`007` 归 T27；其余编号未改）
- [ ] pytest（逐文件 + 全量）+ ruff + 秘密扫描
- [ ] `git status` / `git diff` 审阅；确认无 tracked 意外改动
- [ ] 追加 `docs/IMPLEMENTATION_STATUS.md` 小节并同步计划前沿到 W8
- [ ] 询问用户是否提交本波次
- [ ] **回填本文件 §4（真实 SHA / 推送态 / 未提交清单）**

## 4. 提交决策与结果

- 用户答复：待询问
- 实际提交：待定
- 推送：待定
- 未提交清单：待定
