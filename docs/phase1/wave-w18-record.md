# W18 波次记录：Phase 1 骨架（身份 + 工作区 + 健康检查）

## 1. 声明块

| 项 | 值 |
| --- | --- |
| 波次 | W18 |
| 阶段 | Phase 1（P0 MVP） |
| 分支 | `feature/p1-w18-skeleton`（base `dev@c95ed6a`） |
| 并行度 N | 1（全部为新建骨架，文件间强耦合，无法拆分并行） |
| 执行者 | Lead 自执行 |
| 写权白名单 | `src/pyknora/**`、`db/migrations/**`、`tests/**`、`deploy/**`、`pyproject.toml`、`uv.lock`、`.env.example`、`docs/phase1/**`、`docs/IMPLEMENTATION_STATUS.md`、`README.md` |
| 资源槽 | PostgreSQL `127.0.0.1:5433`，数据库 `pyknora_dev` / `pyknora_test`（新建，互不干扰）；端口 8000（uvicorn 本地验证） |
| 不碰 | `experiments/**`、`docs/poc/**`、`docs/adr/**`、`references/**` |
| 验收标准 | ①`uv run uvicorn pyknora.main:app` 可起且 `/health` 返回依赖状态；②`/auth/register`、`/auth/login`、`/auth/me`、`GET/POST /workspaces` 全绿；③跨工作区访问被拒；④`ruff check` + `ruff format --check` 通过；⑤`docker compose -f deploy/compose/docker-compose.yml config` 通过 |

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-30 10:0x | 建分支 | Lead | `feature/p1-w18-skeleton` 自 `dev@c95ed6a` 拉出 |
| 2026-10-30 10:0x | 落计划 | Lead | `docs/phase1/PLAN_Phase1_MVP.md`（W18–W23 拆分与技术选型） |

## 3. 执行与门禁

### 3.1 交付物

| 文件 | 行数/大小 | 说明 |
| --- | --- | --- |
| `src/pyknora/config.py` | 2008 B | pydantic-settings 配置；`sync_database_url` 把 `+asyncpg`/`+psycopg` 归一为同步 DSN 供迁移用 |
| `src/pyknora/db.py` | 2048 B | SQLAlchemy 2.0 async 引擎/会话工厂 |
| `src/pyknora/models.py` | 3383 B | `users` / `workspaces` / `memberships` 三张表（含软删除与时间戳） |
| `src/pyknora/schemas.py` | 1260 B | 请求/响应 DTO |
| `src/pyknora/security.py` | 2255 B | argon2 口令散列 + PyJWT 签发/校验 |
| `src/pyknora/deps.py` | 3161 B | 当前用户依赖、工作区成员/属主校验（越权即 404） |
| `src/pyknora/errors.py` | 2397 B | 统一错误体 `{error:{code,message,details}}` + 请求 ID 中间件 |
| `src/pyknora/migrate.py` | 4479 B | 幂等 SQL 迁移 runner：`app.schema_migrations` 记录版本与 sha256 校验和 |
| `src/pyknora/api/health.py` | 1694 B | `/health`（存活）与 `/api/v1/health`（依赖探测） |
| `src/pyknora/api/auth.py` | 3525 B | register / login / me |
| `src/pyknora/api/workspaces.py` | 3599 B | 列表 / 新建 / 详情 |
| `db/migrations/0001_identity.sql` | 2980 B | identity 表结构与索引 |
| `tests/conftest.py`、`tests/test_{health,auth,workspaces}.py` | 2700/836/2896/3435 B | 14 个应用层用例 |
| `deploy/Dockerfile`、`deploy/compose/docker-compose.yml` | 997/1491 B | api + pgvector:pg17 + redis |
| `README.md`、`docs/phase1/PLAN_Phase1_MVP.md` | — | 交接文档与 W18–W23 计划 |

### 3.2 执行中修掉的两个真问题

1. **Windows 上 uvicorn 固定 ProactorEventLoop** → `psycopg` 异步驱动不可用（`Psycopg cannot use the 'ProactorEventLoop'`）。取证实测：`uvicorn/loops/asyncio.py:10` 直接 `return asyncio.ProactorEventLoop`，应用层设 policy 无效。**决策：应用侧驱动改 asyncpg**（`PYKNORA_DATABASE_URL` 默认 `postgresql+asyncpg://`），`psycopg` 保留给迁移脚本与 Phase 0 同步脚本；`config.sync_database_url` 负责剥离 `+asyncpg`/`+psycopg`。
2. **`tests/phase0/conftest.py` 的临时证据目录落在仓库根**：本 venv 下 `tempfile.gettempdir()` 返回仓库根，产生 `pyknora-evidence-*` 残留。改为显式优先 `TEMP`/`TMP`，并清掉本轮 1 个残留目录。

### 3.3 门禁

| 项 | 命令 | 结果 |
| --- | --- | --- |
| ruff check | `uv run ruff check src tests` | `All checks passed!` exit 0 |
| ruff format | `uv run ruff format --check src tests` | `19 files already formatted` exit 0 |
| 应用层测试 | `uv run pytest -q` | **14 passed in 4.47s**，exit 0 |
| compose 校验 | `docker compose -f deploy/compose/docker-compose.yml config --quiet` | exit 0 |
| 启动冒烟 | `python -m pyknora.serve --port 8012` | `/health` 200，`{"database":{"status":"ok"}}`；`POST /api/v1/auth/register` **201** 且返回 JWT；`POST /api/v1/conversations` 404（W20 才实现，符合预期） |
| 秘密扫描 | 正则扫 `src/tests/deploy/.env.example/README/db` | 4 条命中全为 **f-string 模板或本地 dev 默认值**（`{_PG_USER}` 变量、compose 的 `pyknora:pyknora` 本地占位，现已改为 `${PYKNORA_PG_PASSWORD:-pyknora}`），非真实凭据 |
| 残留检查 | `git status --porcelain` + `Get-ChildItem -Filter 'pyknora-evidence-*'` | 无 `pyknora-evidence-*` 残留；未跟踪文件均在写权白名单内（`.idea/`、`CLAUDE.md`、`docs/teacher/` 为会话前已存在，不入提交） |

### 3.4 偏差与遗留

- 计划 §2 原写「数据访问 = SQLAlchemy 2.0 async + psycopg 3」，实测不可行（见 §3.2-1），已改为 asyncpg 并回写计划文档。
- 本轮**未**实现文档/检索/SSE/URL 导入，属 W19–W21 范围；`/api/v1/health` 的 redis 状态在未配置时为 `not_configured`（W19 接入 Celery 后变为真实探测）。
- `git status` 中 `docs/plans/README.md` 为既有未跟踪文件，不在本波次范围。

## 4. 提交决策

- 待提交（同一逻辑变更：Phase 1 骨架 + 文档）：
  - `src/pyknora/**`（15 个 py 文件）、`db/migrations/0001_identity.sql`、`deploy/**`、`tests/conftest.py` + 3 个测试文件；
  - `pyproject.toml`、`uv.lock`、`.env.example`、`README.md`；
  - `docs/phase1/PLAN_Phase1_MVP.md`、`docs/phase1/wave-w18-record.md`、`docs/IMPLEMENTATION_STATUS.md`；
  - `tests/phase0/conftest.py`（临时目录修复）。
- 不提交：`.idea/`、`CLAUDE.md`、`docs/teacher/`、`docs/plans/README.md`（会话前已存在，非本波次产出）。
- 判定：W18 验收标准 ①–⑤ **全部满足**；Phase 1 整体仍为「进行中」，下一波次 W19。
- **等待用户裁决是否提交 + 推送 + 合入 `dev`。**
