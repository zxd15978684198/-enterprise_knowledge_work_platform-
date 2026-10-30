# W22 波次记录：部署与前端（P0 MVP 任务 P1-T02/T03/T04）

> 规程沿用 Phase 1：开工先落声明块，完成后回填 §2/§3/§4，门禁通过且用户裁决后才提交。

## 1. 声明块

| 项 | 内容 |
| --- | --- |
| 波次 | W22 |
| 分支 | `feature/p1-w22-deploy-frontend`，base `dev@efdb5db` |
| 并行度 N | **1**（单任务，写权集合跨「存储抽象 + 队列 + 容器编排 + 前端 + E2E」，拆分只会制造互相等待） |
| 任务 | P1-T02 Celery/Redis 队列与 MinIO 对象存储、P1-T03 一键部署（compose 五服务）、P1-T04 最小前端 + 端到端脚本 |
| 写权白名单 | `src/pyknora/config.py`、`storage.py`、`queue.py`（新）、`ingest/pipeline.py`、`api/knowledge.py`、`main.py`、`web/`（新）、`bootstrap.py`（新）、`deploy/**`、`scripts/e2e_phase1.py`（新）、`tests/test_storage.py`（新）、`tests/test_queue.py`（新）、`tests/test_web.py`（新）、`pyproject.toml`、`uv.lock`、`README.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/phase1/PLAN_Phase1_MVP.md`、本记录 |
| 禁写 | `db/migrations/**`（W22 无 schema 变更）、`src/pyknora/retrieval/**`、`src/pyknora/rag/**`、`src/pyknora/ports/**`、`tests/test_*.py`（W21 及更早用例语义不动） |
| 资源槽 | PostgreSQL `pyknora_dev`/`pyknora_test`（5433）、Redis（6380）、MinIO 容器 `127.0.0.1:9000`（仅 W22 开发期）、本地 `data/uploads`（local 后端） |
| 依赖 | W19 入库链路、W20 检索/RAG、W21 URL 导入 |

### 验收标准

1. `PYKNORA_STORAGE_BACKEND=s3` 时应用走 MinIO/S3 实现，`local` 时行为与 W19–W21 完全一致（既有 85 例全绿）；
2. `get_storage()` 按配置返回两个实现之一，接口仍为 `put/get/delete/exists`（+`size`）；
3. S3 后端启动时能建桶（`ensure_bucket`），缺桶时 `put` 自动补建或报明确错误；
4. `PYKNORA_INGEST_MODE=queue` 时上传/URL 导入/重处理把入库丢给 Celery，接口返回即 `status=queued`；`inline`（默认）不变；
5. Celery 任务 `pyknora.queue.ingest_document` 可被 worker 消费，任务内自建 async 引擎并在结束时释放，不共享 API 进程连接池；
6. 队列模式失败可观测：任务抛错时文档落 `status=failed` + `error` 摘要（沿用 `process_document` 语义）；
7. `deploy/compose/docker-compose.yml` 含 `api`/`worker`/`db`/`redis`/`minio` 五服务，健康检查与依赖顺序正确，`docker compose config -q` 通过；
8. `docker compose up -d` 后 `GET /health` 200，且 `/health` 能报告 storage/queue 后端；
9. 最小前端在 `GET /app/` 可用：登录/注册 → 工作区 → 知识库 → 上传文档 → 提问并渲染引用；无 Node 构建步骤（纯静态）；
10. 前端不绕过 API 权限：所有调用带 `Authorization`，工作区/知识库 ID 由前端持有但不作为信任边界（后端仍校验）；
11. `POST /api/v1/retrieval/search` 与问答在浏览器端可用（CORS 覆盖 compose 访问来源）；
12. `scripts/e2e_phase1.py` 跑通「注册→建工作区→建库→上传→就绪→检索→提问→引用非空」，失败退出码非 0；
13. `uv run pytest tests` 全绿且不含 skip 静默通过（外部依赖缺失必须写明原因）；
14. `ruff check`/`ruff format --check src tests` 通过；秘密扫描无真实凭据（compose 中仅开发默认值并标注）；
15. README 增加「一键启动」「前端入口」「队列/存储开关」三节，`docs/IMPLEMENTATION_STATUS.md` 更新前沿与本轮段。

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-30 | Lead 自执行 | Lead | 见 §3（N=1；历史同类任务派子 agent 多次静默失败，改自执行） |
| 2026-10-30 | Lead 自执行 | Lead | 代码/测试/编排/前端/E2E 全部由 Lead 串行完成（N=1） |
| 2026-10-30 | 依赖变更 | Lead | `uv add "celery[redis]>=5.4" "boto3>=1.35"`（清华镜像，pypi.org 直连 44s 超时）；后补 `httpx>=0.28` 提升为运行依赖 |
| 2026-10-30 | 真实 Compose 冒烟 | Lead | `BUILD_EXIT=0` / `UP_EXIT=0`，五容器 Up（api healthy、worker Up），`/health` = `backend=s3` + `mode=queue` + `broker=redis`，E2E `--queue` 10 步全 PASS（`failures=[]`、`E2E_EXIT=0`） |

## 2.1 与声明块的偏差（登记）

1. **新增 `db/migrations/0005_ingest_queue_status.sql`**：声明块写「禁写 `db/migrations/**`（W22 无 schema 变更）」。实际 `app.documents.status` 的 CHECK 约束只允许 `pending/processing/ready/failed`，队列模式写入 `status=queued` 会被数据库拒绝 → 放宽为 `pending/queued/processing/ready/failed`。属于**必要的约束放宽**，不是结构调整。
2. **Compose 定义落仓库根 `compose.yaml`，`deploy/compose/docker-compose.yml` 改为 `include:` 引用**：构建上下文必须是仓库根（需要 `src/`、`db/`、`pyproject.toml`、`uv.lock`）。声明块写权只列了 `deploy/**`，根 `compose.yaml` 与根 `Dockerfile` 属新增文件，在此登记。
3. **`pyproject.toml` 把 `httpx>=0.28` 从 dev 依赖组提升为运行依赖**：W21 的 `ingest/url_fetch.py` 在运行时 `import httpx`，但此前只在 dev 组 → 镜像内 `ModuleNotFoundError`（见 §3.2）。这是 W21 遗留的依赖分类缺陷，W22 构建时才暴露。
4. **未新增 `.env.example`**（验收 15 的隐含项）：`compose.yaml` 用 `x-app-env` 内联开发默认值并在 README「一键启动」节说明覆盖方式，未单独落 `.env.example`。记为收口波次（W23）可补项。
5. **`scripts/e2e_phase1.py` 与根 `Dockerfile`、根 `compose.yaml` 为新增路径**，声明块写权未逐一列出，在此登记。

## 3. 执行与门禁

### 3.1 交付物

| 类别 | 文件 | 要点 |
| --- | --- | --- |
| 依赖 | `pyproject.toml`、`uv.lock` | `celery[redis]>=5.4`、`boto3>=1.35`、`httpx>=0.28`（运行依赖）；锁文件 68 包 |
| 配置 | `src/pyknora/config.py`（+31） | `storage_backend=local\|s3`、`ingest_mode=inline\|queue`、`s3_endpoint_url/s3_access_key/s3_secret_key/s3_bucket/s3_region/s3_presign_seconds`、`celery_task_time_limit/celery_task_soft_time_limit`、`web_dir`（空=包内 `web/`） |
| 存储 | `src/pyknora/storage.py`（+153/−41） | `ObjectStorage` 协议加 `size()`；`LocalObjectStorage` 行为不变；新增 `S3ObjectStorage`（boto3 path-style、`ensure_bucket`、`S3StorageError`）；`_build_storage()` 按配置选实现；`reset_storage(root)` 供测试强制本地 |
| 队列 | `src/pyknora/queue.py`（新） | `create_celery()` 以 `redis_url` 为 broker/backend、`task_acks_late=True`、`worker_prefetch_multiplier=1`、`task_default_queue="pyknora"`；任务 `pyknora.ingest_document` 内部 `asyncio.run` + `finally dispose_engine()`；`enqueue_ingest()` 返回 task id |
| 引导 | `src/pyknora/bootstrap.py`（新） | `ensure_storage_ready()` 返回 backend/bucket 摘要（不含凭据），S3 不可用即失败 |
| 装配 | `src/pyknora/main.py`（+21） | lifespan 调 `ensure_storage_ready()`（失败 fail-fast）；`/app` 挂载 `StaticFiles`，`GET /` → `/app/` |
| 入库分派 | `src/pyknora/ingest/pipeline.py`（+20）、`api/knowledge.py`（+8/−6） | `dispatch_ingest()`：inline 直接 `process_document`；queue 置 `status=queued` → commit → 延迟导入 `enqueue_ingest`。上传/URL 导入/重处理三处统一走它 |
| 健康 | `src/pyknora/api/health.py`（+7/−3） | checks 增 `storage{status,backend}`、`queue{status,mode,broker}` |
| 迁移 | `db/migrations/0005_ingest_queue_status.sql`（新） | 放宽 `documents_status_check` 至含 `queued` |
| 前端 | `src/pyknora/web/{index.html,app.js,styles.css}`（新，68/229/137 行） | 纯静态无构建：登录/注册 → 工作区 → 知识库（新建/上传/URL 导入）→ 提问 + 引用渲染 + 事件日志；`Authorization: Bearer`，token 存 localStorage；前端持有的 ID 不作为信任边界 |
| 编排 | `compose.yaml`（新，109 行）、根 `Dockerfile`（新，18 行）、`deploy/compose/docker-compose.yml`（改 include） | api/worker/db/redis/minio 五服务；db=`pgvector/pgvector:pg17`、redis=`redis:7.4-alpine`、minio=`minio/minio`；api 先 `python -m pyknora.migrate` 再 uvicorn；worker `celery -A pyknora.queue worker --concurrency=1`；`x-app-env` 统一注入 `PYKNORA_*`（s3 + queue + `PYKNORA_WEB_DIR`） |
| 测试 | `tests/test_storage.py`（134 行，12 例）、`tests/test_queue.py`（90 行，3 例）、`tests/test_web.py`（35 行，5 例） | 存储双后端与越界/建桶/错误包装、Celery 配置与 task 名与 `queued` 语义、`/app/` 静态资源与 `/` 重定向且不遮蔽 `/openapi.json` |
| E2E | `scripts/e2e_phase1.py`（157 行） | `--base-url/--queue/--url`；health→frontend→register→workspace→knowledge-base→upload→ingest→retrieval→conversation→answer（+可选 url-import），失败退出 1 并打印 failures |
| 文档 | `README.md`（+60/−8）、`docs/phase1/PLAN_Phase1_MVP.md`、本记录、`docs/IMPLEMENTATION_STATUS.md` | README 增「一键启动（Compose）」「前端入口」「队列与存储开关」三节 |

### 3.2 过程问题（都是构建/编排层面暴露的真缺陷）

1. **镜像构建失败 ①**：`uv sync` 报 `failed to open file /app/README.md`（`pyproject.toml` 的 `readme` 指向）→ 根 `Dockerfile` 补 `COPY README.md`。
2. **api 容器启动崩溃**：`ModuleNotFoundError: No module named 'httpx'`（`ingest/url_fetch.py:28`）→ 把 `httpx` 从 dev 组提升为运行依赖并 `uv lock`；这是 W21 引入的依赖分类缺陷，本地 dev 环境因装了 dev 组而掩盖。
3. **假阳性冒烟**：第一次 compose 冒烟的 E2E「全 PASS」不可信——`/health` 报 `backend=local`/`mode=inline`，且 `docker compose ps` 只有 db/redis/minio；原因是本机残留 uvicorn（pid 17836）占着 8000，E2E 打的是本地进程。杀掉本地进程后重跑，`/health` 变为 `backend=s3`/`mode=queue` 才是 Compose api。教训：**先证明被调用的是容器，再读结果**（`backend` 字段就是判据）。
4. **只看 `docker compose ps` 会隐藏崩溃容器** → 冒烟脚本改用 `ps -a`。

### 3.3 门禁实测（2026-10-30 12:2x，全部本机实跑）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check src tests scripts db` | `All checks passed!` |
| 格式 | `uv run ruff format --check src tests scripts db` | `46 files already formatted` |
| 单测 | `uv run pytest tests --junitxml=.pytest_cache/w22_final.xml` | **`tests=105 failures=0 errors=0 skipped=0 time=32.478s`**（W21 基线 85 + 20 新增），`PYTEST_EXIT=0` |
| 编排语法 | `docker compose -f compose.yaml config -q` / `deploy/compose/docker-compose.yml` | 均 `EXIT=0` |
| 真实 Compose | `docker compose -f compose.yaml up -d --build` | `BUILD_EXIT=0`、`UP_EXIT=0`；api healthy、worker Up、db/redis/minio healthy；`/health` 200 → `storage.backend=s3`、`queue.mode=queue`、`queue.broker=redis` |
| 队列往返 | worker 日志 | `Task pyknora.ingest_document[...] received` → `入库完成 chunks=1 pages=1` → `succeeded in 8.88s: 'ready'`；API 侧上传返回 `status=queued`，E2E 轮询到 `ready` |
| 端到端 | `uv run python scripts/e2e_phase1.py --base-url http://127.0.0.1:8000 --queue` | `steps=10`、`failures=[]`、`E2E_EXIT=0`；检索 `total=1`/`mode=hybrid-rrf`；问答 `status=completed`/`citations=1`/`model=extractive-local` |

> 说明：本次 `pytest` 的 summary 行被 `addopts=-q` 叠加抑制，计数取自 junit XML（`tests=105 failures=0 errors=0 skipped=0`），不是凭肉眼数点。

### 3.4 未覆盖/限域

- 真实 MinIO 的预签名 URL 与 `size()` 未做浏览器侧验证（仅服务端 put/get/exists 与建桶）；预签名保留在 `s3_presign_seconds` 配置但当前 API 未使用。
- Celery 失败重试与 `soft_time_limit` 软超时路径未做故障注入（只在单测里覆盖 `queued`→`ready` 与配置断言）。
- 前端只覆盖「最小可用」路径：无分页、无删除、无 SSE 流式渲染（走非流式 `POST /messages`）、无多知识库筛选 UI。
- 真实 embedding/chat 供应商仍 `NOT_RUN`（沿用 W20 替身），因此 Phase 1 出口的「同量级检索指标」仍不可判。

## 4. 提交记录

- 分支：`feature/p1-w22-deploy-frontend`（base `dev@efdb5db`）。
- 提交：`07e5a8c` feat（21 文件，+2675/−958：存储/队列/装配/前端/编排/迁移/测试/E2E/依赖）→ `99efc02` docs（4 文件，+179/−11：README 三节、PLAN 完成标记、本记录 §2–§3、状态文件）。`git push -u origin feature/p1-w22-deploy-frontend` 成功（新分支，顶点 `99efc02`）。
- 合入：`git merge --no-ff` → **`51c0e72`**；`dev` 由 `efdb5db` 推进到 `51c0e72` 并推送 `origin/dev`（`efdb5db..51c0e72`）。本段回填提交后再次 `--no-ff` 合入 `dev`（同 W19–W21 规程）。
