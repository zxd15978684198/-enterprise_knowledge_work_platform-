# P0-T02 环境证据：隔离实验环境与核心依赖清单

- **Task ID**：P0-T02（Phase 0 / Wave W1）
- **owner**：w1-t02-env
- **branch / base**：`feature/w1-t02-t09-t14` @ `59d53ac9897f8b0e5d3eeb096332737f7ed6e1ce`
- **记录时间**：2026-10-27T16:39–17:14 +08:00（Asia/Shanghai；§13 为 Lead 门禁硬化后的复跑记录）
- **本机**：Windows 10.0.19045 / amd64；uv 0.12.24；Docker 29.7.2（compose v5.4.0）；Python 3.12.13（uv 托管解释器）
- **状态**：**PASS**（D0 + E1/E2 + 未就绪清单达成；CVE 扫描已完成且零已知漏洞；缺环境变量改为显式失败并补负向用例。残余：端口占用负向演练 NOT_RUN、Redis server 许可与 psycopg LGPL 需 ADR 定稿 —— 见 §9、§10、§13）

> 说明：本文件是 T02 的唯一证据文件；任何数字均来自本次真实命令输出，未运行的项在 §9 显式列出，未做推测。

## 1. 基线与可追溯性

| 项 | 值 |
| --- | --- |
| Git 分支 | `feature/w1-t02-t09-t14` |
| Git HEAD | `59d53ac9897f8b0e5d3eeb096332737f7ed6e1ce` |
| 工作区脏差异（`git diff HEAD`） | sha256 `20af57d332a4560edee417884e57d5e9d978839163ca149af3862ebf704669f0`（33 行，仅 `docs/IMPLEMENTATION_STATUS.md`，Lead 的改动，非本任务） |
| 未跟踪目录 | `.idea/`、`CLAUDE.md`、`docs/plans/`、`docs/poc/evidence/`、`docs/teacher/`、`experiments/`、`tests/`（均为本轮尚未提交的产物） |
| 依赖锁文件（dependency_lock_hash） | `experiments/phase0/uv.lock` sha256 `bf0b2dc0746b5ebdbdec80310ff2adcb13d0fe8079438b927f6e4067f1fdba63`（30 256 B；CVE 修复后重锁，见 §13） |
| 解释器 | CPython 3.12.13（`>=3.12,<3.13`，uv 托管 `cpython-3.12-windows-x86_64-none`），实验 venv 位于 `experiments/phase0/.venv` |
| 根项目隔离 | 根 `.venv`（3.13.7）、根 `pyproject.toml`（dependencies=[]）、`.python-version`(3.12) 均未改动 |

## 2. 结论摘要

| 维度 | 结果 |
| --- | --- |
| 隔离实验项目可重建（D0） | ✅ `uv sync --project experiments/phase0 --frozen` → `Checked 16 packages in 9ms`，exit 0 |
| E1 PostgreSQL 最小连通 | ✅ `PostgreSQL 17.11`（容器内 172.22.0.3），psycopg 真连接 + DDL/DML 往返 1 行，0.092 s |
| E2 Redis 最小连通 | ✅ `PING=true`、`GET=ok`、server `7.4.12`，0.049 s |
| 容器健康 + loopback 绑定 | ✅ 两服务 `Up (healthy)`，`127.0.0.1:5433->5432/tcp`、`127.0.0.1:6380->6379/tcp` |
| 镜像 digest 固定 | ✅ 见 §6，均为 `tag@sha256:<64hex>`，无 `latest` |
| 零秘密入 Git | ✅ `.env.example` 仅占位值（`phase0-local-only`），测试有秘密模式断言 |
| 测试 | ✅ `12 passed`（EXIT=0，1.57 s，真实用例、无 skip/xfail；含 2 个负向用例，见 §13） |
| 依赖 CVE | ✅ `pip-audit` → `No known vulnerabilities found`，exit 0（升级 pytest 9.1.1 后，见 §13） |
| 缺变量显式失败 | ✅ 空 env 对照 → `docker compose config -q` exit 1，stderr 含 `PYKNORA_T02_MISSING_PHASE0_PG_PASSWORD` |
| lint | ✅ `All checks passed!` |
| 未就绪 profile | ⏸ `queue`/`parser`/`browser`/`control-site` = `not_ready`（见 §8；素材阻塞已于 17:00 解除，镜像仍属 T09/T14） |
| 状态判定 | **PASS**：DoD（D0 + E1/E2 + 未就绪清单）达成；CVE 零漏洞；缺变量显式失败已落地并有用例覆盖。残余项见 §9（端口占用演练 NOT_RUN）与 §10（许可需 ADR） |

## 3. 交付物与哈希

| 文件 | 作用 | sha256 |
| --- | --- | --- |
| `experiments/phase0/pyproject.toml` | 独立实验项目元数据、ruff/pytest 显式配置、无 `package` 构建 | `c0885b0fec0219ba0196ebc4f2a728fbf2cda830b749e05f43a9690beb025d3d`（655 B） |
| `experiments/phase0/uv.lock` | `uv lock` 真实解析结果（16 个包，精确版本 + sha256） | `bf0b2dc0746b5ebdbdec80310ff2adcb13d0fe8079438b927f6e4067f1fdba63`（30 256 B） |
| `experiments/phase0/compose.yaml` | core profile（postgres+redis，digest 固定、loopback 端口、healthcheck、必填变量显式失败），其余 profile 骨架注释 | `6bcfc61f190e6ebe670a864381dbeb50f8ee41727cdf4716bc8596c3145d79c0`（3 943 B） |
| `experiments/phase0/.env.example` | 无秘密环境变量模板（PG/Redis 连接、端口、profile、schema 前缀） | `c58b4355890bd9dba174dc9a39a509e7b2eb0253c4cc967974466911a8a76f2d`（1 530 B） |
| `tests/phase0/test_environment.py` | 12 个真实断言用例（10 正向 + 2 负向，禁 skip/xfail） | `12f2288cc7d7cc2c3afc8fce04bba64fec2e93d85768746ddae6fffb769b79c2`（14 749 B） |
| `docs/poc/evidence/environment.md` | 本证据文件 | 不作为锁对象（随审阅更新，hash 在波次门禁复核） |

`experiments/phase0/pyproject.toml` 关键内容（节选）：

```toml
[project]
name = "pyknora-phase0"
requires-python = ">=3.12,<3.13"          # 强制 3.12 线，避免 3.13 漂移
dependencies = [
  "psycopg[binary]>=3.1",                 # TDD01 §7 指定 psycopg
  "redis>=5",
  "sqlalchemy[asyncio]>=2.0",
]

[dependency-groups]
dev = ["pyyaml>=6,<7", "pytest>=8,<9", "pytest-asyncio>=0.24,<1", "ruff>=0.9,<0.13"]

[tool.uv]
package = false                           # 实验目录不是可发布包

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.pytest.ini_options]
testpaths = ["../../tests/phase0"]        # 不带路径的 pytest 不会 0 用例空跑绿
addopts = "-q"
asyncio_mode = "auto"
```

## 4. 依赖清单（精确版本 / 来源 / 许可）

来源均为 `https://pypi.org/simple`（`uv.lock` 中 `source = { registry = ... }`），许可列取自各包 `*.dist-info/METADATA` 的 `License-Expression` / `License` / `Classifier`（本次实测读取，非记忆）。

| 包 | 版本 | 用途 | 许可（METADATA 原文） |
| --- | --- | --- | --- |
| sqlalchemy | 2.1.6 | ORM / Core（sync + async） | MIT |
| psycopg | 3.3.6 | PG 驱动（sync/async 双语系） | LGPL-3.0-only |
| psycopg-binary | 3.3.6 | psycopg 预编译二进制 | LGPL-3.0-only |
| redis | 8.1.0 | Redis 客户端 | MIT |
| greenlet | 3.5.6 | SQLAlchemy asyncio 依赖 | MIT AND PSF-2.0 |
| typing-extensions | 4.16.0 | 类型回填 | PSF-2.0 |
| tzdata | 2026.5 | 时区数据 | Apache-2.0 |
| pyyaml | 6.0.3 | compose/证据解析（dev） | MIT |
| pytest | 8.4.2 | 测试（dev） | MIT |
| pytest-asyncio | 0.26.0 | async 测试（dev） | Apache-2.0 |
| iniconfig | 2.3.1 | pytest 依赖 | MIT |
| packaging | 26.3 | pytest 依赖 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.7.0 | pytest 依赖 | MIT |
| pygments | 2.21.0 | pytest 依赖 | BSD-2-Clause |
| colorama | 0.4.6 | pytest 依赖（Windows） | BSD |
| ruff | 0.12.12 | lint/format（dev） | MIT |

`uv.lock` 中 sha256 抽样（完整清单见 lock 文件）：

| 包 | 工件 | sha256 / size |
| --- | --- | --- |
| psycopg-binary 3.3.6 | `win_amd64` wheel | `e23a66a763fbe83fcc210bc77c27e5a5ea380ebf091c06f34d8561b695e5a40f` / 3 658 864 B |
| packaging 26.3 | sdist | `94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79` |
| pyyaml 6.0.3 | sdist | `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f` / 130 960 B |
| redis 8.1.0 | sdist | `6e1a19beef9225c83efd689c7e6b7da2d5215b1f42cd13b7fc3714d0a09c7b25` / 5 254 356 B |
| ruff 0.12.12 | sdist | `b86cd3415dbe31b3b46a71c598f4c4b2f550346d1ccf6326b347cc0c8fd063d6` / 5 359 915 B |

## 5. 命令与真实结果

所有命令在 `E:\project_month\pyknora`（原生 PowerShell 7.6.0 持久会话，非一次性 `pwsh`）执行。

### 5.1 依赖解析与锁定

```text
# uv add（改口径：asyncpg → psycopg，见 §10 R5）
$ uv add --project experiments/phase0 "sqlalchemy[asyncio]>=2.0" "psycopg[binary]>=3.1" "redis>=5"
Resolved 17 packages in 4.14s
Downloading sqlalchemy (2.3MiB) / psycopg-binary (3.5MiB)
Prepared 3 packages in 5m 03s
Installed 7 packages in 3.84s
EXIT=0 SECONDS=311.7
```

```text
# uv remove（移除 Lead 底稿里的 asyncpg/redis/sqlalchemy 直依赖，改由 add 重建）
$ uv remove --project experiments/phase0 --group dev asyncpg sqlalchemy redis
Resolved 10 packages in 50ms
Uninstalled 6 packages (asyncpg 0.30.0, greenlet, pyjwt, redis 5.3.1, sqlalchemy 2.0.55, typing-extensions)
EXIT=0 SECONDS=0.3
```

`uv.lock` 首次生成由 Lead 实测（`C:\Users\Administrator\AppData\Local\Temp\t02_run.log`）：`Using CPython 3.12.13`、`Resolved 16 packages in 2.05s`、EXIT 0；我本次口径变更后为 `Resolved 17 packages`。`uv.lock` 全程由 uv 生成，未手工编辑。

### 5.2 冻结同步（D0 可重建）

```text
$ uv sync --project experiments/phase0 --frozen
Checked 16 packages in 9ms
EXIT=0 SECONDS=0.1
```

### 5.3 pytest（带 `-c` 的正式跑法）

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q
```

- **首次运行**：`8 passed, 2 failed`，EXIT=1。两项失败均为真实缺陷，已修复：
  1. `test_uv_lock_pins_exact_versions_and_artifact_hashes`：断言正则 `^\d+\.\d+\.\d+` 误判 `packaging 26.3`（PEP 440 两段版本），改为 `^\d+(\.\d+)+`；
  2. `test_workspace_layout_and_evidence_contract`：`AssertionError: 缺少 docs/poc/evidence/environment.md 证据文件`（本文件当时尚未写入，属预期失败）。
- **最终运行**（本证据文件落盘后复跑）：

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q
..........                                                               [100%]
10 passed
pytest_exit=0 SECONDS=7.43
```

日志：`C:\Users\Administrator\AppData\Local\Temp\pyknora-t02\pytest_final.log`。

> ⚠️ 已知坑（Lead 实测，本任务复现）：`uv run --project experiments/phase0 pytest` **不带 `-c`** 时 `configfile` 会落到仓库根 `pyproject.toml`（rootdir=仓库根、`asyncio_mode=STRICT`），与实验项目配置不一致；正式验证必须显式 `-c experiments/phase0/pyproject.toml`。

### 5.4 ruff

```text
$ uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0
All checks passed!
EXIT=0
```

### 5.5 compose 静态校验

```text
$ docker compose -f experiments/phase0/compose.yaml --profile core config -q
config_exit=0

$ docker compose -f experiments/phase0/compose.yaml --profile core config --images
redis:7.4-alpine@sha256:6ba19c50b56777f5c11c58eff8ddb69d0d5f27ba131cffb63f180ccfd43c66af
postgres:17-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b
images_exit=0
```

### 5.6 容器启动与健康检查

```text
$ docker compose -f experiments/phase0/compose.yaml --profile core up -d postgres redis
 Volume pyknora-phase0_phase0-pgdata Created
 Volume pyknora-phase0_phase0-redisdata Created
 Container pyknora-phase0-postgres-1 Recreate → Recreated → Starting → Started
 Container pyknora-phase0-redis-1   Recreate → Recreated → Starting → Started
up_exit=0 SECONDS=6.7
```

```text
$ docker compose -f experiments/phase0/compose.yaml --profile core ps
pyknora-phase0-postgres-1 | postgres:17-alpine@sha256:266d3c25… | Up 17 seconds (healthy) | 127.0.0.1:5433->5432/tcp
pyknora-phase0-redis-1    | redis:7.4-alpine@sha256:6ba19c50…    | Up 17 seconds (healthy) | 127.0.0.1:6380->6379/tcp
```

> 首次 `up` 前这两个容器是 Lead 用 `postgres:16-alpine` / `redis:7-alpine` 起的旧实例；本次 `Recreate` 后已全部切换到 digest 固定的 17.11 / 7.4.12，旧镜像 `postgres:16-alpine` 不再被引用（未删除，避免影响他人镜像）。

### 5.7 E1/E2 最小连通（实验 venv 真连接）

临时探针 `C:\Users\Administrator\AppData\Local\Temp\pyknora-t02\probe_conn.py`（不在仓库内），用实验 venv 执行：

```text
$ uv run --project experiments/phase0 python "%TEMP%\pyknora-t02\probe_conn.py"
{
  "pg_version": "PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit",
  "pg_identity": ["pyknora", "pyknora", "172.22.0.3/32"],
  "pg_rowcount": 1,
  "pg_seconds": 0.092,
  "redis_ping": true,
  "redis_get": "ok",
  "redis_version": "7.4.12",
  "redis_seconds": 0.049
}
probe_exit=0
```

探针实际执行：`select version()` → `create schema if not exists p0_t02_probe` → 建表 → `insert ... on conflict do update` → `select count(*)`；Redis 侧 `PING` / `SET p0_t02:e2 ok EX 60` / `GET` / `INFO server`。即 E1、E2 均为主机侧真实 TCP + 协议往返，非容器内自测。

venv 内实际安装版本（`uv run ... python -c "..."`）：`3.12.13 AMD64`、`psycopg 3.3.6`、`sqlalchemy 2.1.6`、`redis 8.1.0`、`pyyaml 6.0.3`——与 `uv.lock` 完全一致（测试 `test_installed_versions_match_uv_lock` 断言）。

## 6. 镜像 digest（image_digests）

| 镜像 | digest | 拉取耗时 / 大小 | 容器 ID |
| --- | --- | --- | --- |
| `postgres:17-alpine` | `sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b` | 216.1 s / 117 214 490 B | `8b9309b4cd0c7b0a6d2a742c0e6952b8d081c211cb8a743a3c67b6ec74b94ebe` |
| `redis:7.4-alpine` | `sha256:6ba19c50b56777f5c11c58eff8ddb69d0d5f27ba131cffb63f180ccfd43c66af` | 178.7 s / 16 261 881 B | `64251cf2b524d838324eece75667b24cbbc32e86094ff4daa23cfdf74f774da3` |

- 拉取记录：`C:\Users\Administrator\AppData\Local\Temp\pyknora-t02\pull.log`（`EXIT=0`，DONE 16:49:01）。
- 校验：`docker inspect` 的 `.Image` 与 digest 完全一致；`redis:7-alpine` 与 `redis:7.4-alpine` 指向同一 digest，因此固定 digest 后 tag 漂移不再有影响。
- **未使用 `latest`**；镜像均由 digest 固定（`tag@sha256:<64hex>`），`test_compose_core_services_are_digest_pinned_and_loopback_only` 强制断言。

## 7. 预期 vs 实际

| # | 预期（任务卡/计划） | 实际 | 判定 |
| --- | --- | --- | --- |
| 1 | 独立实验项目，解释器 3.12，根 `.venv` 不受影响 | `experiments/phase0/.venv` 为 3.12.13；根 `.venv` 未变 | 一致 |
| 2 | `uv lock` 真实生成，含精确版本 + sha256 | 17 包，`source=pypi.org/simple`，全部带 sha256 | 一致 |
| 3 | `uv sync --frozen` 成功 | `Checked 16 packages in 9ms`，exit 0 | 一致 |
| 4 | pytest >0 用例且全绿 | `10 passed`，EXIT=0（§5.3） | 一致 |
| 5 | ruff check 通过 | `All checks passed!` | 一致 |
| 6 | compose config 通过 | `config -q` exit 0，`--images` 两项 digest | 一致 |
| 7 | core 两服务健康 + 可连通 | 两容器 healthy；E1/E2 探针 exit 0 | 一致 |
| 8 | 镜像 digest 固定，端口 5433/6380 | `127.0.0.1:5433->5432`、`127.0.0.1:6380->6379` | 一致（且仅 loopback） |
| 9 | `.env.example` 无秘密、字段齐全 | 仅占位值；PG/Redis/端口/profile 齐全 | 一致 |
| 10 | `control-site` 不实现、记 `not_ready` | compose 中显式 `not_ready` | 一致 |

## 8. profile / 服务就绪矩阵

| profile | 服务 | 状态 | 说明 / 准入任务 |
| --- | --- | --- | --- |
| `core` | postgres 17.11、redis 7.4.12 | ✅ ready | 本任务 E1/E2 证据 |
| `queue` | celery worker/beat | ⏸ `not_ready` | T08（Celery/Redis 作业） |
| `parser` | DocReader gRPC | ⏸ `not_ready` | T09，当前 **BLOCKED**：`references/WeKnora` 子模块克隆失败（github.com:443 超时 21 064 ms），无法确认镜像与协议 |
| `browser` | BrowserSkill / Playwright | ⏸ `not_ready` | T14，同样受素材 BLOCKED；按约束不预装 BrowserSkill 依赖 |
| — | `control-site` | ⏸ `not_ready` | T04 实现；本任务只做占位说明，无镜像、无构建 |

骨架写法：compose 顶部以注释列 `# profile: queue|parser|browser … not_ready`，`control-site … not_ready`，不声明任何未经验证的 `image:`，避免捏造。

## 9. NOT_RUN / BLOCKED

| 项 | 状态 | 原因 / 缺口 |
| --- | --- | --- |
| CVE / 漏洞扫描（pip-audit） | **DONE（零漏洞）** | 首轮 `Found 2 known vulnerabilities`（pytest 8.4.2 / PYSEC-2026-1845，fix 9.0.3）→ 升级 pytest 9.1.1 + pytest-asyncio 1.4.1 后复扫 `No known vulnerabilities found`（exit 0）；详见 §13。镜像层 trivy 扫描仍 **NOT_RUN**（未安装 trivy） |
| 端口占用负向演练（5433/6380 已被占用时的显式失败） | **NOT_RUN** | 需要占用端口再启动的可控实验窗口；compose 已用固定映射，预期报 `port is already allocated`（未实测） |
| `parser` / `browser` profile 容器化 | **未开始（非阻塞）** | 素材阻塞已于 2026-10-27T17:00:11 解除（子模块 `662abaf9…` 已就位）；镜像与服务定义属 T09/T14，T02 按设计不填 |
| DocReader / BrowserSkill 镜像 digest | **未开始（非阻塞）** | 同上，无镜像可固定，未猜 tag |
| Redis server 侧许可合规 | **未决（需 ADR）** | 客户端 `redis` 包 MIT，但 **server 镜像 7.4 为 RSALv2/SSPLv1（非 OSI）**；见 §10 R2 |
| pgvector / 向量扩展连通 | **NOT_RUN** | core 用官方 postgres 镜像，无 pgvector；向量能力属 T03 |
| Redis server 侧许可合规 | **未决（需 ADR）** | 客户端 `redis` 包 MIT，但 **server 镜像 7.4 为 RSALv2/SSPLv1（非 OSI）**；见 §10 R2 |
| pgvector / 向量扩展连通 | **NOT_RUN** | core 用官方 postgres 镜像，无 pgvector；向量能力属 T03 |

## 10. 残余风险

- **R1 网络极慢**：`Prepared 8 packages in 2m 26s`（Lead）/ `Prepared 3 packages in 5m 03s`（本次 2.3 MiB + 3.5 MiB）→ 干净机器重建可能超时。建议固定 `UV_LINK_MODE=copy`（已出现 `Failed to hardlink files; falling back to full copy`，cache 与目标不在同一文件系统）并考虑本地 wheel 缓存/内网镜像。
- **R2 Redis server 许可**：`redis:7.4-alpine` 服务端为 RSALv2/SSPLv1，不是 OSI 许可，与"优先成熟开源"和可能的 SaaS 分发存在张力。可选替代：Valkey（BSD-3，Linux Foundation fork）或 Redis 8（AGPLv3）。**需在 ADR 中定稿**，当前仅实验环境使用，未进入产品依赖。
- **R3 psycopg LGPL-3.0**：动态链接使用通常可接受，但分发边界需法务确认；`psycopg-binary` 同许可。
- **R4 SQLAlchemy 2.1.6**：TDD01 §7 写的是 2.0 线语义，2.1 为同 major 内升级（2.x 兼容），待后续任务在真实 schema/迁移上验证；若不兼容需回退并重锁。
- **R5 依赖口径变更（已采纳）**：移除 Lead 底稿的 `asyncpg`，改 `psycopg[binary]`（TDD01 §7 指定 psycopg；psycopg3 单驱动同时覆盖 sync Alembic 与 async SQLAlchemy，减少双驱动漂移）。门禁裁定采纳，见 §13。
- **R6 缺变量回退（已修复）**：原 `${VAR:-默认值}` 会静默回退，违反「注入：缺环境变量必须明确失败」。已改为 `${PHASE0_PG_USER:?PYKNORA_T02_MISSING_PHASE0_PG_USER}` 同构写法，端口默认值保留并在注释说明是刻意设计；代价是日常命令必须显式 `--env-file experiments/phase0/.env.example`（见 §13）。
- **R6b 端口固定**：5433/6380 若被占用，`up` 会失败（显式失败符合要求，但未演练，见 §9）；且 `restart: "no"`，宿主重启后需手动 `up -d`。
- **R7 运行时产物**：`experiments/phase0/.venv`、`__pycache__`、`.pytest_cache` 属本地产物；根 `.gitignore` 已覆盖 `.venv`，提交前仍需 `git status` 复核，勿 `git add .`。
- **R8 未做资源限额**：仅 `shm_size: 128mb`、`security_opt: no-new-privileges`、`stop_grace_period: 30s`；无 CPU/内存上限，实验期可接受。

## 11. 干净环境复现步骤

```powershell
# 0) 前置：uv ≥ 0.12、Docker Desktop 已启动
uv --version            # 0.12.24
docker info --format '{{.ServerVersion}}'   # 29.7.2

# 1) 依赖（锁文件驱动，不解析新版本）
uv sync --project experiments/phase0 --frozen

# 2) 静态检查与测试（-c 不可省，见 §5.3 的坑）
uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_environment.py -q

# 2b) 依赖漏洞扫描（应输出 No known vulnerabilities found）
uv run --project experiments/phase0 --with pip-audit pip-audit

# 3) 容器（core）
#    注意：PG 三变量为必填（缺失即 config 报错，见 §13.3），日常命令必须带 --env-file；
#    也可先 cp experiments/phase0/.env.example experiments/phase0/.env（.env 已被 .gitignore 忽略）。
docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core config -q
docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core up -d postgres redis
docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core ps

# 4) 连通性（E1/E2）
#    psql: postgresql://pyknora:phase0-local-only@127.0.0.1:5433/pyknora
#    redis: redis://127.0.0.1:6380/0
```

## 12. 证据溯源

| 内容 | 位置 |
| --- | --- |
| Lead 首次 lock/sync/pytest/ruff/compose 记录 | `C:\Users\Administrator\AppData\Local\Temp\t02_run.log`（55 行，16:43:15 起） |
| 镜像拉取耗时与大小 | `C:\Users\Administrator\AppData\Local\Temp\pyknora-t02\pull.log` |
| E1/E2 探针脚本与输出 | `C:\Users\Administrator\AppData\Local\Temp\pyknora-t02\probe_conn.py`（临时，不入库） |
| 镜像 digest / 容器 ID | `docker inspect pyknora-phase0-postgres-1 pyknora-phase0-redis-1`、`docker inspect postgres:17-alpine redis:7.4-alpine` |
| 锁文件与文件哈希 | `Get-FileHash ... -Algorithm SHA256`（§1、§3） |
| 门禁硬化复跑（12 用例 / 依赖版本 / 文件哈希 / 负向断言） | `C:\Users\Administrator\AppData\Local\Temp\t02_gate_final.log`（2026-10-27T17:12:26 起） |
| 显式失败对照与 E1/E2 复跑 | `C:\Users\Administrator\AppData\Local\Temp\t02_gate.log`、`C:\Users\Administrator\AppData\Local\Temp\t02_e12.log`（17:13:32 起） |
| pip-audit 两轮结果 | `C:\Users\Administrator\AppData\Local\Temp\audit_before.log`、`audit_after.log` |

## 13. 门禁硬化（Lead 复跑，2026-10-27T17:12–17:14）

### 13.1 裁定与改动

| 项 | 裁定 | 落地 |
| --- | --- | --- |
| R5 驱动口径 `asyncpg` → `psycopg[binary]` | **采纳** | `pyproject.toml` 依赖为 `psycopg[binary]>=3.1`、`redis>=5`、`sqlalchemy[asyncio]>=2.0`；`uv.lock` 重锁 |
| R6 缺变量静默回退 | **不采纳，改显式失败** | `compose.yaml` 三个 PG 变量改 `${PHASE0_PG_USER:?PYKNORA_T02_MISSING_PHASE0_PG_USER}` / `..._PASSWORD` / `..._DB`；端口保留默认值并注释说明刻意 |
| CVE 扫描 | 执行 | `uv run --project experiments/phase0 --with pip-audit pip-audit` |
| 测试覆盖显式失败 | 补用例 | `tests/phase0/test_environment.py` 增至 12 个用例（+2 负向） |

### 13.2 CVE：从 2 条到 0 条

首轮（`audit_before.log`）：

```text
Found 2 known vulnerabilities in 1 package
Name   Version ID               Fix Versions
pytest 8.4.2   PYSEC-2026-1845  9.0.3
```

首次尝试 `uv add --dev "pytest>=9.0.3"` 失败（真实冲突）：`pytest-asyncio>=0.24.0,<=0.26.0 depends on pytest>=8.2,<9`。
改为一并升级 asyncio 插件：`uv add --project experiments/phase0 --dev "pytest>=9.0.3" "pytest-asyncio>=1.0"` → `add_exit=0`（5.8 s），装成 `pytest 9.1.1` / `pytest-asyncio 1.4.1`。
复扫（`audit_after.log`）：`No known vulnerabilities found`，exit 0。

### 13.3 负向用例真实性（新增 2 条）

```text
$ docker compose -f experiments/phase0/compose.yaml --profile core --env-file <空文件> config -q
error while interpolating services.postgres.environment.POSTGRES_DB: required variable PHASE0_PG_DB is missing a value: PYKNORA_T02_MISSING_PHASE0_PG_DB
error while interpolating services.postgres.environment.POSTGRES_PASSWORD: required variable PHASE0_PG_PASSWORD is missing a value: PYKNORA_T02_MISSING_PHASE0_PG_PASSWORD
error while interpolating services.postgres.environment.POSTGRES_USER: required variable PHASE0_PG_USER is missing a value: PYKNORA_T02_MISSING_PHASE0_PG_USER
config_exit=1  negative_marker_present=True
```

对照：`--env-file experiments/phase0/.env.example` → `config -q` exit 0。测试内用 `subprocess.run(timeout=180)` 断言“非 0 + stderr 含标记”，不是跳过。

### 13.4 复跑结果（`t02_gate_final.log` 原文）

```text
python 3.12.13 | pytest 9.1.1 | pytest-asyncio 1.4.1 | ruff 0.12.12
sqlalchemy 2.1.6 | psycopg 3.3.6 | redis 8.1.0 | pyyaml 6.0.3
uv sync --frozen → Checked 16 packages in 8ms | sync_exit=0
pytest -c experiments/phase0/pyproject.toml → ............ [100%] | pytest_exit=0  (12 passed)
ruff check experiments/phase0 tests/phase0 → All checks passed! | ruff_exit=0
docker compose ... --env-file .env.example config -q → compose_ok_exit=0
```

E1/E2 复跑（`t02_e12.log`，容器 `Up 15 minutes (healthy)`）：

```text
E1 PostgreSQL 17.11 on x86_64-pc-linux-musl sec=0.303
E1 ddl_dml_ok 1
E2 ping True set True get b'ok' sec=0.141
E2 server 7.4.12
probe_exit=0
```

compose ps（同日志）确认镜像均为 digest 形态：`postgres:17-alpine@sha256:266d3c25…`、`redis:7.4-alpine@sha256:6ba19c50…`，端口 `127.0.0.1:5433->5432`、`127.0.0.1:6380->6379`。

### 13.5 仓库卫生

- pytest 9 在仓库根生成的 `pytest-of-Administrator/` 已确认路径后删除（`removed=True`）。
- 根 `.gitignore` 追加 `.env`、`.env.*`、`.pytest_cache/`、`.ruff_cache/`、`pytest-of-*/`（原文件末尾无换行，直接在 `references/**` 后追加），防止实验 `.env` 与临时目录误入库。
- 全程未 `git add` / `commit` / `push`；`git status --porcelain` 为零 staged。

### 13.6 门禁结论

T02 判定由 **PARTIAL → PASS**：DoD 三项（D0 可重建、E1/E2 真连通、未就绪 profile 清单）全部有实测证据；CVE 零已知漏洞；缺变量显式失败有负向用例。仍留 1 项 NOT_RUN（端口占用演练）与 2 项需 ADR 的许可问题，均已在 §9/§10 显式记录。

---

## 14. 后续变更（2026-10-29，修复分支 `fix/w1-t02-pgvector`）

> 本节只追加、不改写上文历史输出。上文 §1/§5/§13 中的 `postgres:17-alpine@sha256:266d3c25…` 与 `PostgreSQL 17.11 ... musl` 记录的是 W1 当时的实测，**现已被替换**。

| 项 | 变更前（W1，2026-10-27） | 变更后（2026-10-29） |
| --- | --- | --- |
| postgres 镜像 | `postgres:17-alpine@sha256:266d3c25…` | `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` |
| 服务端版本串 | `PostgreSQL 17.11 on x86_64-pc-linux-musl`（Alpine） | `PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu` |
| pgvector 可用性 | ❌ 无 `vector` 扩展（W5 记录为缺口 ③，检索 ANN 只能 NOT_RUN） | ✅ `vector` 0.8.7 可用；`CREATE EXTENSION vector` 实测通过 |
| 数据卷 | `phase0-pgdata`（PG17） | 同一卷原地复用（major 未变），既有 schema 保留 |

变更理由、探针输出与判定见 `docs/poc/evidence/fix-w1-t02-pgvector-record.md`；`tests/phase0/test_environment.py` 已新增静态断言 `test_postgres_image_ships_pgvector_extension`，防止镜像被换回无扩展版本。

**换镜像后的回归结论（2026-10-29 实跑）**：`tests/phase0/test_environment.py` 13 passed；全量 `tests/phase0` **959 passed / 0 failed / 0 errors / 8 skipped**（`time=269.236s`，8 个 skip 全是 BrowserSkill 真机用例且 `skipped.message` 写明 `NOT_RUN` 理由）；`ruff check experiments/phase0 tests/phase0` → `All checks passed!`；core 两容器 `Up (healthy)`。即：换镜像未破坏任何既有组件，`vector` 扩展在 `pyknora` 库实测可建、HNSW 索引可用。

**给后续任务的接口约定**：`vector` 扩展是**按库**安装的，新库需自带 `CREATE EXTENSION vector`（本记录只在 `pyknora` 库建过探针并已清理），否则会报 `type "vector" does not exist`。

---

**owner**：w1-t02-env（执行）/ lead（门禁硬化）　Wave W1，T02　**status**：PASS（残余见 §9/§10；镜像与 PG 发行版已于 2026-10-29 升级，见 §14）
