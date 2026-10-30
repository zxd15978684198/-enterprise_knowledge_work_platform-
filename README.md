# PyKnora

企业知识工作平台（RAG + Agent 工作流）。本仓库按 `docs/PRD_PyKnora.md` 的五阶段路线推进。

| 阶段 | 范围 | 状态 |
| --- | --- | --- |
| Phase 0 | 架构验证与 PoC（32 项 P0 任务） | 已完成（见 `docs/poc/`） |
| **Phase 1** | **P0 MVP：认证、知识库、文档、混合检索、RAG+SSE、单 URL 导入** | **进行中（W18 已完成）** |
| Phase 2–4 | Agent / Research / 企业知识整理 / P2 扩展 | 未开始（用户已明确不做） |

进度与逐波次证据：`docs/IMPLEMENTATION_STATUS.md`、`docs/phase1/PLAN_Phase1_MVP.md`、`docs/phase1/wave-wN-record.md`。

## 快速开始（Windows / PowerShell）

前置：Docker Desktop、Python 3.12、[uv](https://docs.astral.sh/uv/)。

```powershell
# 1. 基础设施（PostgreSQL 17 + pgvector、Redis）
docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core up -d postgres redis

# 2. 建库（首次）
docker exec pyknora-phase0-postgres-1 psql -U pyknora -d postgres -c "CREATE DATABASE pyknora_dev;"

# 3. 依赖
uv sync --extra dev

# 4. 配置
Copy-Item .env.example .env      # 生产必须改 PYKNORA_JWT_SECRET（≥32 字节）

# 5. 迁移（应用启动时也会自动补迁移）
uv run python -m pyknora.migrate

# 6. 启动
uv run python -m pyknora.serve --reload
#   http://127.0.0.1:8000/docs
```

验证：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing | Select-Object -ExpandProperty Content
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

容器化（W18 起可用，一键起全部服务见 W22）：

```powershell
docker compose -f deploy/compose/docker-compose.yml config
```

## 已实现 API（W18）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活探针（不查依赖） |
| GET | `/api/v1/health` | 依赖状态（database / redis） |
| POST | `/api/v1/auth/register` | 注册并返回 JWT |
| POST | `/api/v1/auth/login` | 登录（OAuth2 表单：`username`/`password`） |
| GET | `/api/v1/auth/me` | 当前用户 |
| GET | `/api/v1/workspaces` | 我参与的工作区 |
| POST | `/api/v1/workspaces` | 新建工作区（创建者自动成为 owner） |
| GET | `/api/v1/workspaces/{workspace_id}` | 工作区详情（非成员返回 404） |

约定：业务接口默认要求 `Authorization: Bearer <token>`；错误统一为 `{"error": {"code", "message", "details"}}`，响应头带 `X-Request-Id`。

尚未实现（计划内）：知识库与文档（W19）、检索与问答 SSE（W20）、URL 导入（W21）、前端与一键部署（W22）。

## 目录结构

```
src/pyknora/           应用代码（FastAPI，Python 3.12）
  api/                 路由：auth / health / workspaces
  config.py            配置（pydantic-settings，前缀 PYKNORA_）
  db.py                异步引擎与会话（SQLAlchemy 2.0 + asyncpg）
  models.py            ORM：users / workspaces / memberships
  schemas.py           Pydantic DTO
  security.py          JWT 与口令散列（argon2）
  deps.py              依赖注入：当前用户、工作区成员校验
  errors.py            统一错误封装与请求 ID 中间件
  migrate.py           幂等 SQL 迁移 runner（db/migrations/*.sql）
  main.py / serve.py   应用装配与本地启动
db/migrations/         0001_identity.sql
deploy/                Dockerfile、compose/docker-compose.yml
tests/                 应用层测试（none 依赖真实 PG）
experiments/phase0/    Phase 0 实验探针（只读研究，不进入产品路径）
docs/                  PRD、ADR、Phase 0 PoC 与 Phase 1 计划
```

## 配置（`.env`，前缀 `PYKNORA_`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `PYKNORA_DATABASE_URL` | `postgresql+asyncpg://…@127.0.0.1:5433/pyknora_dev` | 应用用 asyncpg；迁移脚本自动转同步驱动 |
| `PYKNORA_JWT_SECRET` | `dev-insecure-change-me` | **生产必须覆盖** |
| `PYKNORA_JWT_TTL_MINUTES` | `720` | 令牌有效期 |
| `PYKNORA_REDIS_URL` | `redis://127.0.0.1:6380/0` | 队列与事件流（W19 起使用） |
| `PYKNORA_CORS_ORIGINS` | `["http://localhost:5173"]` | JSON 数组 |

## 已知限制

- **Windows 下 uvicorn 固定使用 ProactorEventLoop**（`uvicorn/loops/asyncio.py`），而 `psycopg` 的异步连接在 Proactor 上不可用，因此应用侧驱动选 **asyncpg**；`+psycopg` 仅用于迁移与 Phase 0 同步脚本。
- 文档入库、分块、向量化尚未实现（W19）。
- 认证仅邮箱口令 + JWT，无 OAuth/SSO；无前端。
- 数据库 RLS 未启用，工作区隔离在应用层强制（后续加固项）。
