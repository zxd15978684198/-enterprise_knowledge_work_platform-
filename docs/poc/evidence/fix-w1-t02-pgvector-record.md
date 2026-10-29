# 修复记录：W1/T02 的 PostgreSQL 镜像换成自带 pgvector 的版本

> 修复分支 `fix/w1-t02-pgvector`（从 `dev@5809d80` 拉出）。本文件是 W1 波次 `docs/poc/evidence/environment.md` 的**追加式变更记录**，不改写那份历史证据。
> 触发来源：W5 缺口清单 ③「本机 PG 无 `vector` 扩展 → ANN/召回只能记 NOT_RUN」，以及 P0-T26（检索/引用/事件）需要真实向量能力。

## 1. 声明块

| 字段 | 值 |
| --- | --- |
| 波次 / 任务 | W1 遗留修复 · P0-T02（隔离实验环境） |
| 分支 | `fix/w1-t02-pgvector` |
| 基线 | `dev@5809d80` |
| 并行度 | `N=1`（单任务修复，无并行） |
| 写入范围 | `experiments/phase0/compose.yaml`、`tests/phase0/test_environment.py`、`docs/poc/evidence/environment.md`（§14 追加）、本文件 |
| 禁止事项 | 不改根 `pyproject.toml`/根 `.venv`、不删既有数据卷、不改其它波次记录、`git add/commit/push` 仅在门禁后由用户确认 |

## 2. 问题

`experiments/phase0/compose.yaml` 的 postgres 原为 `postgres:17-alpine@sha256:266d3c25…`——纯官方镜像，**不含 `vector` 扩展**。后果：

- W5（T12 staging）只能用 `float8[] + embedding_dim` 存向量，ANN/召回记 `NOT_RUN`；
- T25 检索对照、T26 RAG/引用只能做「结构 + 权限」验证，无法做真实向量检索；
- 若后续任务各自想办法装扩展，会出现「运行时 `CREATE EXTENSION` 失败」这类环境漂移，违反 T02「镜像 100% 精确版本/hash」的验收口径。

## 3. 候选方案取舍

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| **A. 换官方 pgvector 镜像** | `pgvector/pgvector:pg17`（PG 17 + pgvector 0.8.7，同 major 可直接复用数据卷） | ✅ 采用 |
| B. 保持 postgres:17-alpine + 运行时装扩展 | 容器里 `apk add`/编译 pgvector | ❌ 镜像不可复现，且违背 digest 固定 |
| C. 用 `pgvector/pgvector:pg16` 等其它 major | 需重建数据卷、迁移所有已有 schema | ❌ 破坏既有证据与数据卷复用 |

镜像来源与可信度：`pgvector/pgvector` 是 pgvector 官方维护的镜像仓库（同一 upstream 组织），`pg17` tag 对应 PostgreSQL 17。

## 4. 变更清单

| 文件 | 变更 |
| --- | --- |
| `experiments/phase0/compose.yaml` | `postgres.image` → `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`；加两行注释说明理由与证据路径 |
| `tests/phase0/test_environment.py` | 新增 `test_postgres_image_ships_pgvector_extension`：断言镜像仓库必须是 `pgvector/pgvector`、tag major 必须是 `pg17`、且仍满足 `tag@sha256:<64hex>` 固定 |
| `docs/poc/evidence/environment.md` | 追加 §14「后续变更」，把旧镜像/旧版本串明确标注为历史值 |
| `docs/poc/evidence/fix-w1-t02-pgvector-record.md` | 本文件 |

数据卷未改动：`compose.yaml` 的 `volumes.phase0-pgdata` 名不变，PostgreSQL major 未变（17），容器重建即原地复用。

## 5. 实测证据（2026-10-29）

### 5.1 镜像 digest

```text
$ docker image inspect pgvector/pgvector:pg17 --format '{{json .RepoDigests}}'
["pgvector/pgvector@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"]
```

### 5.2 compose 校验与重建

```text
$ docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core config -q
config_exit=0

$ docker compose ... --profile core up -d postgres
 Container pyknora-phase0-postgres-1 Recreate → Recreated → Starting → Started
up_exit=0
healthy after 10s
image_now=pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
pyknora-phase0-postgres-1 | pgvector/pgvector:pg17@sha256:ac08538c… | Up 14 seconds (healthy)
pyknora-phase0-redis-1     | redis:7.4-alpine@sha256:6ba19c50…        | Up 50 minutes (healthy)
```

### 5.3 服务端版本与扩展可用性

```text
PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit
available: vector default=0.8.7
数据卷复用核对 → 4 schemas: p0_t02_probe,p0_t05,p0_t27,public（既有 schema 仍在）
```

### 5.4 `CREATE EXTENSION vector` + HNSW 实跑

```text
create extension if not exists vector → CREATE EXTENSION
select extversion → 0.8.7
create table _pgv_probe (id int primary key, embedding vector(128));
insert 500 行随机向量 → INSERT 0 500
create index _pgv_probe_hnsw on _pgv_probe using hnsw (embedding vector_cosine_ops) → CREATE INDEX
order by embedding <=> ... limit 3 → top3=2,3,1
drop table _pgv_probe → DROP TABLE（探针表已清理，未留残留）
```

> 说明：500 行的 `EXPLAIN` 走 Seq Scan 属正常（数据量太小，规划器不选 HNSW）；W5 的 `pgvector-probe.md` 已在 5000×128 上实测 HNSW 走 Index Scan（建索引 1289.7 ms、查询 0.94–1.48 ms、top-5 与精确扫描 5/5 一致）。

### 5.5 测试

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q --timeout=180
.............  [100%]
env_test_exit=0   (13 passed，含新增 pgvector 静态断言)
```

全量回归（`tests/phase0`）结果见 §7。

## 6. 影响与残余风险

| 项 | 影响 | 处置 |
| --- | --- | --- |
| 旧镜像证据作废 | `environment.md` §5/§13 的 `postgres:17-alpine@sha256:266d3c25…` 不再是当前事实 | 已用 §14 显式标注「历史值」，不删旧记录 |
| PG 发行版变化 | Alpine → Debian（musl → glibc），`version()` 字符串变化 | 证据里保留两种字符串，避免下游按字符串硬匹配 |
| 客户端兼容 | 镜像内 `pg_dump`/`psql` 版本随 major 不变（17）；T30 备份探针走 Client 容器 | 全量回归覆盖（§7） |
| 镜像体积 | Debian 镜像比 alpine 大的 | 首次拉取耗时记入 §5.2 实测 |
| 镜像可达性 | 本机从镜像源取回后已本地存在 | 若换机器不可达，按 T02 口径记 `BLOCKED`，不退回 `latest` |

## 7. 门禁结果（2026-10-29 10:56–11:01 实跑）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 环境测试 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q` | **13 passed**，exit 0 |
| 全量回归 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q --timeout=180 --junitxml=experiments/phase0/.pytest_cache/pgvec_gate.xml` | **959 passed / 0 failed / 0 errors / 8 skipped**，`time=269.236s`（junit 实测） |
| 静态检查 | `uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0` | `All checks passed!`，exit 0 |
| compose 校验 | `docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core config -q` | exit 0 |
| 容器健康 | `docker compose … --profile core ps` | `pyknora-phase0-postgres-1 Up (healthy)`、`pyknora-phase0-redis-1 Up (healthy)` |

8 个 `skipped` 全部是 BrowserSkill 真机用例（`BSK_LIVE=1` + `CPython>=3.13` 前置不满足），`skipped.message` 均写明 `NOT_RUN: …` 理由，不是用 skip 冒充通过。

一处门禁观察（记入残余风险，不阻塞本修复）：全量回归会**重写两个被跟踪的证据 JSON**——`docs/poc/evidence/poc01_owner_process_level.json`（`generated_at`/`popen_pid`/`reported_pid`）与 `docs/poc/evidence/retrieval-baseline.json`（多为 timing 字段），diff 各 14/13 行且与本次镜像变更无关。处置：本次随修复提交一并纳入（内容为同一套件在新环境下的重生成值），后续应把这类证据写入 `experiments/phase0/.pytest_cache/`（同 `wire_evidence.json` 的做法），避免每次跑测都污染工作树。

## 8. 结论与后续

- **T02 环境从「无 pgvector」升级为「PG 17.11 + pgvector 0.8.7」**，数据卷原地复用，未破坏既有证据链。
- 直接解锁：T26（检索/引用/事件）可做真实向量检索；W5 记录中的缺口 ③ 由此关闭；T25 的 ANN 部分可在后续回归中从 `NOT_RUN` 升级（需重跑该任务才有新证据，不能凭本记录改判）。
- 仍需注意：`vector` 扩展要在每个新建数据库里 `CREATE EXTENSION`（本记录只在 `pyknora` 库执行过探针并已清理），后续任务的迁移/夹具需要自带这一句，否则依旧会 `type "vector" does not exist`。

## 9. 提交与合入（2026-10-29，用户确认「提交 + 推送 + 合入 dev」后执行）

| 步骤 | 真实结果 |
| --- | --- |
| env 改动提交 | `c44ce5d` `fix(phase0): switch postgres image to pgvector/pgvector:pg17`（5 文件 +69 −29） |
| docs 记录提交 | `7c8a585` `docs(phase0): record pgvector env fix and refresh frontier to W8/T26`（3 文件 +159 −9，含本文件） |
| 推送分支 | `git push -u origin fix/w1-t02-pgvector` → `* [new branch]`，exit 0，已设上游 |
| 合入 dev | `git merge --no-ff --no-edit fix/w1-t02-pgvector` → 合并提交 `cc45265`（8 文件 +228 −38），exit 0 |
| 推送 dev | `git push origin dev` → `5809d80..cc45265`，exit 0；`origin/dev` = `cc45265` |

提交前均跑 `git diff --cached --check`（exit 0），只暂存本修复相关文件；`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/` 仍未入库。

---

**owner**：lead　分支 `fix/w1-t02-pgvector`　**status**：门禁通过（见 §7），已提交并合入 `dev`（见 §9）
