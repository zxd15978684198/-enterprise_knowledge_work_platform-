# Wave W8 记录（P0-T26：RAG Graph、引用与最终事件一致性）

> 本文件按执行计划 §4.5 规程填写：启动时写声明块（§1），门禁后回填 §3，提交确认后回填 §4。
> 计划入口：[docs/plans/2026-10-27-phase0-execution-plan.md](../../plans/2026-10-27-phase0-execution-plan.md) §4.5。

## 1. 波次声明（启动时填写，2026-10-29 11:12）

| 字段 | 值 |
| --- | --- |
| `wave` | W8 |
| `branch` | `feature/w8-t26` |
| `base` | `dev@9d3252a`（含 pgvector 环境修复 `cc45265` 与回填 `9d3252a`） |
| `parallel_tasks` | `[P0-T26]` |
| `planned_parallel_tasks` | `[P0-T26]` |
| `serial_tasks` | `[]` |
| `blocked_tasks` | `[]`（T18 已由 `fix/w5-t18` 收口；T28/T29 等 T26 完成后再解锁） |
| `N` | `1`（W8 只有 T26 一个任务可开工：T28/T29 依赖 T26，T31/T32 更后） |
| `write_scope`（唯一写入权） | `experiments/phase0/rag_probe.py`、`experiments/phase0/events_probe.py`、`tests/phase0/test_rag_events.py`、`docs/poc/evidence/poc09-report.md`，以及本任务自产证据 `docs/poc/evidence/rag-*.json`（若需要） |
| `禁止写入` | 根 `pyproject.toml`、`experiments/phase0/pyproject.toml`/`uv.lock`/`compose.yaml`/`.env.example`、`contracts.py`、`scope.py`、迁移 `001`–`008`、`tests/phase0/conftest.py`/`fixtures/manifest.json`、其它波次记录与 `docs/poc/00`–`07`、`docs/IMPLEMENTATION_STATUS.md`（由 Lead 收口时改） |
| 资源槽 | PG schema `p0_t26`（独占，任务结束前清理；实测已建，库名以 `experiments/phase0/.env.example` 为准：当前库 `pyknora`、端口 `127.0.0.1:5433`）；Redis 走 `PHASE0_REDIS_URL`（当前 `redis://127.0.0.1:6380/0`），本任务额外用 DB `14` 时须在证据里写明；不占用 5433/6380 以外的固定端口 |
| 依赖前置 | T25 PASS（限域）、T05 PASS（限域，单活 runner 口径）、T21 PASS（限域）——均已合入 `dev`，产物可直接读 |

## 1.1 已知约束与限域口径（开工前必须遵守）

| 约束 | 处理口径 |
| --- | --- |
| 真实 chat 模型可能不可用（无厂商 key、无额度） | 允许用可注入的 `ModelPort` 替身跑结构/事务/引用校验，但**必须**在证据与本记录中标注「真实模型链路 `NOT_RUN`」，判定最多 `PASS（限域）`，禁止声称「真实模型生成已通过」 |
| `vector` 扩展按库安装 | 测试库/临时 schema 用前先 `CREATE EXTENSION IF NOT EXISTS vector`（pgvector 镜像已就绪，`vector` 0.8.7） |
| T25 检索基线已记 ANN `NOT_RUN` | T26 不自作主张改判 T25；引用校验只依赖 T25 现有证据集与本任务自己的查询 |
| 事件与终态必须同一事务 | 依 `docs/poc/07_Contract_Closure.md` C-02/C-01：Message + Run + terminal 事件一次提交；重复 finalize、过期游标、取消路径都要有负例 |
| 阈值未批准 | 引用准确率目标 0.95 只是候选值（Backlog 原文「0.95候选待评审」），**不得**把未批准阈值当成 PASS 依据；报告要写实际测量值与样本数 |
| 禁止 skip/xfail 冒充通过 | 未执行项写 `NOT_RUN` + 理由；不可用的外部依赖用 `pytest.mark` 门控时必须在报告里列出 |
| Git 纪律 | 不 `git add`/`commit`/`push`/切分支；只写 `write_scope` 内文件 |

## 2. 任务状态

| 任务 | 状态 | 交付 | 说明 |
| --- | --- | --- | --- |
| P0-T26 | **完成（PASS，限域）** | 5 份：`experiments/phase0/events_probe.py`（1953 行）、`experiments/phase0/rag_probe.py`（1797 行）、`tests/phase0/test_rag_events.py`（910 行，21 用例）、`docs/poc/evidence/poc09-report.md`（204 行）、`docs/poc/evidence/rag-citations.json`（353 行，100 例实测明细） | 首派 `w3-t05-checkpoint` 于 2026-10-29 11:16 **异常退出、未留任何文件**（复核：四个交付路径均不存在，工作树仅本记录）；11:20 改派 `w3-t07-outbox`（复用 inactive 成员，`N` 仍为 1）；12:15 交付，Lead 独立复验通过（见 §3） |

### 2.1 派发事故记录

| 时间 | 事件 | 真实证据 | 处置 |
| --- | --- | --- | --- |
| 2026-10-29 11:16 | 子 agent `cb343ea4-…` 报 `failed before it finished. It left no closing message.` | 复核 `rag_probe.py`/`events_probe.py`/`test_rag_events.py`/`poc09-report.md` 均不存在；`git status` 仅本记录为 `??` | 不改 `N`（仍 1）；改派有 DB 事务上下文的 `w3-t07-outbox`，并把「从零开始」写进派单 |

## 3. 门禁结果（Lead 实跑，2026-10-29 12:15–12:50）

### 3.1 实跑命令与结果

| # | 项 | 命令 | 真实结果 | exit |
| --- | --- | --- | --- | --- |
| 1 | 任务级 pytest（Lead 独立复跑） | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_rag_events.py -q -o addopts= -p no:cacheprovider` | **21 passed in 157.18s**（0 failed / 0 skipped） | 0 |
| 2 | 任务级 ruff | `uv run --project experiments/phase0 ruff check --config experiments/phase0/pyproject.toml experiments/phase0/events_probe.py experiments/phase0/rag_probe.py tests/phase0/test_rag_events.py` | `All checks passed!` | 0 |
| 3 | 收集数 | `pytest --collect-only -q` | **980 tests collected in 6.29s**（= W7 后基线 959 + T26 的 21） | 0 |
| 4 | 全量 pytest | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -o addopts= -p no:cacheprovider --junitxml=experiments/phase0/.pytest_cache/w8_gate.xml` | **972 passed, 8 skipped in 691.18s**；junit：`tests=980 failures=0 errors=0 skipped=8 time=691.173` | 0 |
| 5 | 秘密扫描 | 私钥头 / `ghp_` / `sk-` / `xox?-` / 明文 `password=` / `api_key=` 扫本波 6 个文件 | **0 命中** | — |
| 6 | 越界写入 | `git status --porcelain` | 受跟踪文件改动 **0**；新增=声明的 5 份交付 + `wave-w8-record.md`；其余 `??` 为本波前既有（`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`） | — |
| 7 | 资源残留 | `select nspname from pg_namespace where nspname like 'p0_%'` | `p0_t26` **已 teardown 消失**（子 agent 自证 + Lead 复核一致）；余 `p0_t02_probe`/`p0_t05`/`p0_t27` 为 W1/W3/W7 遗留（非本波产物，见 §3.3） | — |
| 8 | 端口占用 | `Get-NetTCPConnection -State Listen` | 5433=1、6380=1（本项目必需）；5500/5501/5600/5610 **均 0** | — |
| 9 | 容器健康 | `docker ps` | `postgres-1` healthy（约 1 小时）、`redis-1` healthy（2 小时） | — |
| 10 | 临时目录 | 仓库根扫 `tmp*` / `.t0*` / `.venv` | **无残留** | — |

### 3.2 交付物 SHA-256（Lead 复核，与子 agent 自报逐字一致）

| 文件 | 行数 | SHA-256 |
| --- | --- | --- |
| `experiments/phase0/events_probe.py` | 1953 | `d01a0ef0ecc6ba6bae886d804d5451e4018cda3a1f067b7c5e382bd15af4ac0a` |
| `experiments/phase0/rag_probe.py` | 1797 | `7a141be76dd42a0280e34f166c311ac688633cdfb5c373b68e761a1d0c4a334c` |
| `tests/phase0/test_rag_events.py` | 910 | `cb07668bd627a5cd68973b611558cc61a9fa514d46eb3b7f4b95c5d4abc6779e` |
| `docs/poc/evidence/poc09-report.md` | 204 | `875d3726e96e0eb005086a33801d2cabe08edbb6cee8ad8ac8ff6c1771f0948c` |
| `docs/poc/evidence/rag-citations.json` | 353 | `3342b405bbb4c904d771f7dfe5933edd863471d44f40b9338ee56e092b624bce` |

> 子 agent 自报的 `events_probe.py` 内联 `MIGRATION_SQL` sha256 `bcc8e76e…` 属代码内常量，随文件哈希一并固定，不再单独复核。

### 3.3 判定与门禁边界

| 项 | 判定 | 依据 / 边界 |
| --- | --- | --- |
| P0-T26 交付 | **PASS（限域）** | 21/21 用例通过、ruff 干净、100 例引用实测 `citation_accuracy=1.0`（40/40）、伪引用/越权/错误引用全 0、50 次 finalize 仅 1 terminal、部分 token 后 DB 失败无 completed、取消后终态不可覆盖 |
| 真实 chat 模型链路 | **NOT_RUN** | 无厂商 key/额度；`ModelPort` 替身按内容词重合选证据，非语义理解 → 引用准确率只证明「检索+可见性+引用校验链路一致性」，**不等于**人工验收 |
| 0.95 引用精度阈值 | **未批准，不作 PASS 依据** | Backlog 原文「0.95 候选待评审」；报告只写实测值（1.0，40/40）与样本量（100 例） |
| RLS 跨空间 | **PARTIAL** | 用 SQL 过滤 + `chunk_visibility`，未复用 T03 的 `p0_t03` RLS 策略 |
| 延迟 p50 937ms / p95 1495ms | **不作为生产指标** | 探针装置多连接开销，非压测口径 |
| pgvector ANN / 真实 MinIO / Redis DB 14 事件投递 / 删除重建 / SSE 断网重连 | **NOT_RUN** | 逐项理由见 `poc09-report.md` §9；未用 skip/xfail 冒充通过（0 skipped 属于 T26 用例） |
| ruff 全仓（`experiments tests docs` + phase0 配置） | **76 条既有告警，非本波引入** | 逐文件核对：分布在 `test_task_coordination.py`(9)、`test_bsk_recovery.py`(9) 等 W1–W7 旧文件；**T26 三个文件 0 错**。与前几波「只扫本波改动文件」的门禁口径一致，作为既有债务记入 `IMPLEMENTATION_STATUS.md` |
| T25 ANN 结论 | **不改判** | T25 在 pgvector 修复前跑，ANN 仍为 NOT_RUN；pgvector 0.8.7 现已可用，属**可补做**项（已记入状态文件待办） |

## 4. 提交决策（2026-10-29 12:52 回填）

用户选择：**提交 + 推送 + 开 W9**（不合入 `dev`）。

| 项 | 真实值 |
| --- | --- |
| feat 提交 | `cf3b498` `feat(phase0): close W8 - RAG graph, verifiable citations and single-transaction terminal events (T26)`（5 文件 +5217） |
| docs 提交 | `047a856` `docs(phase0): record W8 gate result and advance plan frontier to W9`（5 文件 +174/−59，含本记录 §2/§3、计划前沿、状态文件与两个重生成的 tracked 证据 JSON） |
| 推送 | `git push -u origin feature/w8-t26` → exit 0，远端新建 `origin/feature/w8-t26`，顶点 `047a856`；GitHub 提示可开 PR |
| 提交前检查 | 两笔 `git diff --cached --check` 均 exit 0 |
| 未合入 | `dev` 仍为 `9d3252a`（用户未选合并项） |
| 未提交清单 | 仅既有无关项：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`（均长期未入库，非本波产物） |
| 局限 | 两个 tracked 证据 JSON（`poc01_owner_process_level.json`、`retrieval-baseline.json`）被全量回归重写（仅时间戳/PID/branch/dirty_sha256 抖动），随 docs 提交一并纳入；根因（进程级用例写死 tracked 路径）仍待后续改为写 `.pytest_cache/` |
