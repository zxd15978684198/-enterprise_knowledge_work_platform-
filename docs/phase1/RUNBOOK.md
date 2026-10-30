# Phase 1 运维手册（RUNBOOK）

> 适用：Phase 1（P0 MVP）本地开发与单机 Compose 部署。所有命令在仓库根 `E:\project_month\pyknora` 执行。
> 本手册只描述**已验证**的行为；未验证项在 `docs/phase1/DEBT.md` 登记，不在此处冒充结论。

## 1. 前置

| 项 | 要求 | 校验命令 |
| --- | --- | --- |
| Python | ≥ 3.12 | `uv run python -V` |
| uv | 0.12.x | `uv --version` |
| Docker | Docker Desktop（含 compose v2） | `docker compose version` |
| 数据库 | PostgreSQL 17 + pgvector（Compose 内为 `pgvector/pgvector:pg17`） | `docker compose ps db` |

## 2. 本地开发启动（进程内模式）

```powershell
uv sync                                              # 装运行依赖（dev 组默认也装）
uv run python -m pyknora.migrate                     # 应用 db/migrations 下未执行的迁移（幂等）
uv run uvicorn pyknora.main:app --reload --port 8000
```

- 入口：API 文档 `http://127.0.0.1:8000/docs`，前端 `http://127.0.0.1:8000/app/`。
- 默认 `PYKNORA_STORAGE_BACKEND=local`、`PYKNORA_INGEST_MODE=inline`：上传在请求内同步入库，无需 Redis/MinIO。

## 3. Compose 一键启动（部署形态）

```powershell
docker compose up -d --build          # 五服务：api / worker / db / redis / minio
docker compose ps -a                  # 必须看到 api(healthy)、worker(Up)、db/redis/minio(healthy)
docker compose logs -f api            # 启动日志；应出现「存储就绪 backend=s3 ingest_mode=queue」
```

- 等价入口：`docker compose -f deploy/compose/docker-compose.yml up -d`（该文件只 `include` 根 `compose.yaml`）。
- api 容器启动时先跑 `python -m pyknora.migrate`，再起 uvicorn；worker 起 `celery -A pyknora.queue worker`。
- 端口：api `8000`、db `5544`、redis `6381`、minio `9000/9001`。
- 停止并清理：`docker compose down`（加 `-v` 会删数据卷，**确认不要保留数据时再用**）。

## 4. 关键配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `PYKNORA_DATABASE_URL` | 见 `compose.yaml` | 应用用 asyncpg，迁移用同步连接串 |
| `PYKNORA_JWT_SECRET` | `dev-insecure-change-me` | **部署必须覆盖** |
| `PYKNORA_STORAGE_BACKEND` | `local` | `local` / `s3`（s3 = MinIO 或兼容 S3） |
| `PYKNORA_INGEST_MODE` | `inline` | `inline` 同步入库 / `queue` 投递 Celery（接口返回 `status=queued`） |
| `PYKNORA_S3_ENDPOINT_URL` 等 | 开发默认值 | 仅 `storage_backend=s3` 时生效；`ensure_bucket` 会自动建桶 |
| `PYKNORA_METRICS_TOKEN` | 空 | 非空时 `/api/v1/metrics` 需 `x-metrics-token` 头 |
| `PYKNORA_CORS_ORIGINS` | `["http://localhost:5173"]` | JSON 数组；Compose 已放宽到 `*` 之外的具体来源 |

## 5. 迁移与回滚口径

- 迁移文件 `db/migrations/NNNN_*.sql`，由 `python -m pyknora.migrate` 顺序执行，已执行记录在 `app.schema_migrations`。
- **迁移只前滚**：不提供自动回滚脚本。回滚 = 用备份恢复到上一版本 + 部署上一版镜像。
- 迁移校验和：runner 会比对已应用迁移的校验和；修改已提交的迁移文件会报「checksum 漂移」。处置：
  1. 若只是本地库脏（例如早期实验库），删库重建后重放；
  2. 若已上部署环境，**不要改历史迁移**，新增 `NNNN_*.sql` 修正。
- 新增迁移后必须重跑：`uv run pytest tests`（`conftest.py` 会在 `pyknora_test` 上重放全部迁移）。

## 6. 健康检查判读

`GET /health`（或 `GET /api/v1/health`）返回：

```json
{
  "status": "ok",
  "checks": {
    "database": {"status": "ok"},
    "storage": {"status": "ok", "backend": "s3"},
    "queue": {"status": "ok", "mode": "queue", "broker": "redis"}
  }
}
```

| 字段 | 判读 |
| --- | --- |
| `status` | `ok` = 数据库探针通过；`degraded` = 数据库不可用（其余检查仍会返回） |
| `checks.storage.backend` | 判断「当前进程到底用的是哪种存储」——本地冒烟时若期望 s3 却看到 `local`，说明打到的不是容器 |
| `checks.queue.mode` | `queue` 表示入库会投递 Celery；`inline` 表示请求内完成 |

> 常见误判：本机残留 uvicorn 占着 8000 时，`/health` 会返回 `backend=local`/`mode=inline`，此时 E2E 打的是本地进程而不是容器。先看这两个字段再读其它结论。

## 7. 指标判读

`GET /api/v1/metrics`（受 `PYKNORA_METRICS_TOKEN` 保护时需带 `x-metrics-token`）：

| 字段 | 含义 |
| --- | --- |
| `requests.by_route[]` | `method` + 路由模板 + 状态码 + 次数；uuid/数字已折成 `{id}`/`{n}` |
| `latency_ms[route]` | 该路由最近 512 次采样的 `count/avg/p50/p95/max` |
| `counters` | 业务自定义计数（当前未接业务埋点，仅后端可用） |
| `scope` | 固定 `process`：**指标是进程内的**，api 与 worker 各自独立，不聚合、不落盘、重启清零 |

不统计的路径：`/app/*`（静态）、`/docs`、`/redoc`、`/openapi.json`；未匹配到路由的请求记为 `unmatched`。

## 8. 日志与追踪

- 每条访问日志含 `request_id`（来自 `x-request-id` 或自动生成），响应头回填同名头；排查时用它串起一次调用。
- 日志级别由 `PYKNORA_LOG_LEVEL` 控制（默认 `INFO`）。
- 入库任务：api 侧记 `已投递入库任务 document_id=... task_id=...`，worker 侧记 `入库完成 document_id=... chunks=... pages=...`，可用 `document_id`/`task_id` 互查。
- **不要**把 token、口令、S3 凭据写进日志或指标；当前实现只记录身份标识与操作元数据。

## 9. 常见故障

| 现象 | 判据 | 处置 |
| --- | --- | --- |
| api 启动即退出 | `docker compose ps -a` 里 api 非 Up；`logs api` 有 traceback | 多为缺依赖或迁移失败；补依赖/修迁移后重建镜像 |
| `/health` 报 `degraded` | `checks.database.status` 非 `ok` | 查 db 容器与 `PYKNORA_DATABASE_URL`；`pg_isready -h 127.0.0.1 -p 5544` |
| 上传后一直 `status=queued` | worker 未消费 | `docker compose ps worker`；`logs worker` 看是否连上 redis；确认 `task_default_queue="pyknora"` 一致 |
| 上传报存储错误 | api 启动日志「对象存储不可用」 | MinIO 未起或凭据/桶错；`ensure_bucket` 需有建桶权限 |
| 端口被占 | `docker compose up` 报 bind 失败 | `Get-NetTCPConnection -LocalPort 8000` 找占用进程；本地 uvicorn 与 Compose 不要同时开 |
| 迁移报 checksum 漂移 | `python -m pyknora.migrate` 退出非 0 | 本地库脏则重建；部署环境改为新增迁移（见 §5） |
| URL 导入 403 `blocked_target` | 目标解析到私网/回环/元数据地址 | 属预期拦截；本地联调用 `PYKNORA_URL_ALLOW_PRIVATE_NETWORK=true`（**仅开发**） |

## 10. 备份与恢复

**备份**（Compose 形态）：

```powershell
docker compose exec db pg_dump -U pyknora -d pyknora -Fc -f /tmp/pyknora.dump
docker compose cp db:/tmp/pyknora.dump .\backup\pyknora-$(Get-Date -Format yyyyMMdd-HHmm).dump
```

**恢复**（恢复到干净库）：

```powershell
docker compose exec -T db pg_restore -U pyknora -d pyknora --clean --if-exists < .\backup\pyknora-YYYYMMDD-HHmm.dump
```

**对象存储**：`storage_backend=local` 时备份 `upload_dir`（默认 `data/uploads`）；`s3` 时备份 MinIO 卷或用 `mc mirror` 同步到外部。

**恢复演练口径**：恢复到干净库 → 重放迁移（应报 `applied 0`）→ `GET /health` → 登录 → 检索/问答冒烟。演练结果要写进当轮记录，**不宣称「已通过」除非真的跑过**。

## 11. 交接清单

1. `git log --oneline` 确认 `dev` 顶点与远端一致（`git ls-remote origin refs/heads/dev`）；
2. `docker compose ps -a` 五服务状态；
3. `GET /health` 的 `storage.backend` 与 `queue.mode` 是否符合部署预期；
4. `uv run pytest tests` 全绿；`ruff check` / `ruff format --check` 通过；
5. 本轮波次记录 `docs/phase1/wave-wN-record.md` §4 已回填真实 SHA；
6. 已知限域与债务见 `docs/phase1/DEBT.md`，交接时逐条确认是否仍然成立。
