# Phase 1（P0 MVP）执行计划

- 状态：进行中
- 建立：2026-10-30（W18）
- 依据：`docs/PRD_PyKnora.md:608`（Phase 1 / P0 MVP：Auth/KB/文档/混合检索/RAG/SSE/单 URL 导入；交付=可部署 Compose、API、基础前端、端到端测试）
- 范围裁定：用户于 m06739 明确「把 Phase 1 做完即可，Phase 2 及以后不需要」。本计划只覆盖 P0 MVP，不实现 Agent/Research/Wiki/采集任务（P1 项）。

## 1. 与 Phase 0 的关系

Phase 0 的 `experiments/phase0/` 是验证代码，**不迁移**到 `src/`；Phase 1 是重写。可复用的只有四类资产：

| 资产 | 位置 | 复用方式 |
| --- | --- | --- |
| 领域契约 DTO / Scope | `experiments/phase0/contracts.py` | 按产品命名重写为 `src/pyknora/schemas.py`，语义保持 |
| 建表 SQL 经验 | `experiments/phase0/db/migrations/001…008` | 参考表结构、约束与索引；产品侧新建 `db/migrations/`，不带 `p0_` 前缀与 PoC 专用列 |
| 架构决策 | `docs/adr/0001…0014` | 直接约束实现（尤其 0007 检索、0010 pgvector、0011 预算、0012 单活 worker） |
| 检索/索引口径 | `docs/poc/evidence/retrieval-baseline.json`、`ann-bench.json` | 作为默认参数（HNSW m=16/ef_construction=64、ef_search 64、RRF k=60） |

## 2. 技术选型（W18 定稿）

| 关注点 | 选择 | 理由 |
| --- | --- | --- |
| Web 框架 | FastAPI + uvicorn | AGENTS.md 指定；自带 OpenAPI 与依赖注入 |
| 数据访问 | SQLAlchemy 2.0 async + **asyncpg** | Windows 下 uvicorn 固定 `ProactorEventLoop`，`psycopg` 异步驱动在其上不可用（W18 实测）；asyncpg 可用，`psycopg` 仅留给迁移与 Phase 0 同步脚本 |
| 配置 | pydantic-settings | 类型安全、支持 `.env` |
| 认证 | JWT（PyJWT）+ argon2 口令散列 | 无状态、P0 足够；不引入完整的 OAuth2 授权服务器 |
| 迁移 | 自建幂等 SQL runner（`src/pyknora/migrate.py`） | 迁移是纯 SQL，runner 约 100 行；不引入 Alembic 的 autogenerate 复杂度 |
| 队列 | Redis + Celery（W22 引入） | PRD `:508` 指定；W18 不引入 |
| 对象存储 | MinIO / S3（W22 引入） | PRD `:510`；W19 用 `LocalObjectStorage` 实现同一协议 |
| 测试 | pytest + httpx ASGITransport | 无需起真实端口，CI 友好 |

## 3. 波次拆分

| 波次 | 范围 | 验收 |
| --- | --- | --- |
| **W18** | 骨架：配置/日志/DB/迁移 runner；身份（users/workspaces/memberships）；`/health`、`/auth/*`、`/workspaces`；pytest 基建；Dockerfile + compose | `uv run uvicorn` 可起；三组 API 的测试全绿；`docker compose config` 通过 |
| **W19** | 知识库与文档：`/knowledge-bases*`、文档上传（PDF/DOCX/TXT/MD/HTML）、解析与 chunks 落库、`/documents/{id}`、`/documents/{id}/chunks`、`/reprocess`；**同步内联入库**（Celery/Redis 推迟到 W22），对象存储用本地实现（MinIO 推迟到 W22） | 上传→解析→chunk 全链路；同区内容 sha256 幂等；reprocess 版本 +1 且旧分块被替换 |
| **W20** | 混合检索 + RAG + SSE：`/retrieval/search`（向量 + FTS + RRF）、`/conversations*`、`/conversations/{id}/events`；引用结构与「证据不足」终止 | 检索指标与 Phase 0 基线同量级；SSE 事件序列符合 `PRD:503` |
| **W21** | 单 URL 受控入库：`/knowledge-bases/{kb_id}/imports/url`、SSRF 每跳校验、抓取→解析→入库 | 内网/回环/重定向到内网被拒；正常网页可入库 |
| **W22** | 部署与前端：`docker compose up` 一键起（api/worker/postgres/redis/minio）+ **Celery worker/Redis 队列与 MinIO 对象存储接入**；最小前端（登录、KB 列表、上传、问答 + 引用）；端到端脚本 | 空机器 `docker compose up -d` 后健康；E2E 脚本通过 |
| **W23** | 收口：README/运维手册、指标与日志、安全回归、债务清单 | 全量测试绿；文档可交接 |

波次规程沿用 Phase 0：每波次先落声明块（`docs/phase1/wave-wN-record.md`：N、写权白名单、资源槽、验收标准），完成后回填 §2/§3/§4，门禁通过后才提交。

**进度**：W18 已完成（骨架 + 身份/工作区 + 迁移 + 容器化，`uv run pytest` 14 passed，见 `docs/phase1/wave-w18-record.md`）；**W19 已完成**（知识库与文档入库：5 格式解析 + 分块 + 落库 + 幂等去重，`uv run pytest` 23 passed，见 `docs/phase1/wave-w19-record.md`）。下一波次 W20（混合检索 + RAG + SSE）。

## 4. 验收标准（Phase 1 出口）

1. `docker compose -f deploy/compose/docker-compose.yml up -d` 在干净环境可起，`/health` 返回 `{"status":"ok"}` 且依赖项（DB/Redis/MinIO）状态可读。
2. `PRD:476-490` 的 P0 接口全部可用（`/auth/login`、`/workspaces`、`/models`、`/knowledge-bases`、文档上传/查询/分块/重处理、`/retrieval/search`、`/conversations`、SSE）。
3. 跨工作区越权访问全部被拒（对应 `PRD:201` 验收）。
4. 问答返回的引用含 `document_id`、`chunk_id`、标题、定位；无证据时显式「不足以确认」（`PRD:224-225`）。
5. 全量 pytest 绿；`ruff check` / `ruff format --check` 通过。
6. 基础前端可完成「登录→建库→上传→提问→看引用」闭环。

## 5. 已知债务与不做项（明确记录，不隐藏）

- RLS：W18–W21 采用查询层过滤（应用层强制 `workspace_id`），数据库 RLS 作为后续加固项（Phase 0 已在 `p0_` schema 验证可行性）。
- 模型管理 `/models` 在 W20 实现最小版本（增删改查 + 连接测试），不含多 Provider 适配器插件体系。
- 断线恢复、Rerank、查询重写属 P1，不实现。
- 认证只做邮箱口令 + JWT，不含 OAuth/SSO/邀请流。
- 审计日志只记录最小字段（操作者、动作、资源、时间、结果），不含敏感载荷。
