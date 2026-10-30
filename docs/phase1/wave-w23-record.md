# W23 波次记录：Phase 1 收口（可观测性、运维手册、安全回归、债务清单）

> 规程沿用 Phase 1：开工先落声明块，完成后回填 §2/§3/§4，门禁通过且满足收敛条件后提交（用户 m08528：每轮符合收敛要求即直接进入下一阶段）。

## 1. 声明块

| 项 | 内容 |
| --- | --- |
| 波次 | W23（Phase 1 收口波次） |
| 分支 | `feature/p1-w23-closeout`，base `dev@ddafbf3` |
| 并行度 N | **1**（收口动作互相引用同一批文档与指标口径，拆分只会制造冲突） |
| 任务 | P1-T05 收口：①进程内指标与访问日志口径；②运维手册（启动/迁移/备份/排障/健康判读）；③安全回归批次；④债务清单 |
| 写权白名单 | `src/pyknora/metrics.py`（新）、`src/pyknora/api/metrics_api.py`（新）、`src/pyknora/api/__init__.py`、`src/pyknora/main.py`、`src/pyknora/config.py`、`tests/test_metrics.py`（新）、`tests/test_security_regression.py`（新）、`docs/phase1/RUNBOOK.md`（新）、`docs/phase1/DEBT.md`（新）、`README.md`、`docs/phase1/PLAN_Phase1_MVP.md`、`docs/IMPLEMENTATION_STATUS.md`、本记录 |
| 禁写 | `db/migrations/**`、`src/pyknora/retrieval/**`、`src/pyknora/rag/**`、`src/pyknora/ingest/**`、`src/pyknora/ports/**`、既有 `tests/test_*.py`（W18–W22 用例语义不动）、`compose.yaml`、`Dockerfile` |
| 资源槽 | PostgreSQL `pyknora_dev`/`pyknora_test`（5433）、Redis（6380）；本波次不新增容器、不占端口 |
| 依赖 | W18–W22 全部交付（本波次不加业务能力，只做可交接性与口径收拢） |

### 验收标准

1. 进程内指标可查：`GET /api/v1/metrics` 返回 `uptime_seconds`、按「方法/路由模板/状态码」聚合的请求计数、按路由模板的时延分位（count/avg/p50/p95/max）、以及自定义计数器；**不引入新依赖**（不引 prometheus-client）。
2. 指标端点可加保护：`PYKNORA_METRICS_TOKEN` 非空时缺/错 `x-metrics-token` 返回 403 `metrics_forbidden`；为空时开放（仅计数与时延，不含请求体、URL 查询串、身份信息）。
3. 中间件采集不改变既有响应：`x-request-id` 仍回填，`/health`、`/app/`、API 响应状态码与响应体不变。
4. 指标不落盘、不跨进程：仅当前进程内存；重启清零；文档写明多实例/worker 进程各自独立，不宣称全局聚合。
5. 安全回归批次覆盖五类不变量：未认证访问受保护资源 401、跨工作区资源 404（不泄漏存在性）、SSRF 私网/元数据地址被拒、非法 JWT 401、CORS 仅放行配置来源。
6. 安全回归**只调用现有实现**，不为通过测试放宽任何策略（如不为测试打开 `url_allow_private_network`）。
7. `docs/phase1/RUNBOOK.md` 覆盖：本地开发起法、Compose 起法、迁移与回滚口径、健康检查判读（`status` 与 `checks.*` 字段含义）、常见故障（DB 连不上/队列积压/存储不可用/端口占用）、日志与 request_id 追踪、备份与恢复步骤、指标判读。
8. `docs/phase1/DEBT.md` 列出已知债务：Phase 1 限域项、加固项、误报与口径风险，每条含「现状/影响/建议动作/触发条件」，不把未验证写成已验证。
9. `uv run pytest tests` 全绿且无静默 skip；`ruff check`/`ruff format --check` 通过。
10. 文档不新增未经证据支持的结论：所有数字须能在本记录 §3 或既有波次记录中定位。
11. README 增加「运维与排障」指针到 RUNBOOK，并保持既有三节（一键启动/前端入口/队列与存储开关）。
12. `docs/IMPLEMENTATION_STATUS.md` 更新前沿与「本轮：Phase 1 W23」段，并明确 **Phase 1 出口判定仍不成立的部分**（真实供应商 `NOT_RUN`、检索指标未标定）。

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-30 | Lead 自执行 | Lead | N=1，见 §3（收口波次以文档与口径为主，派发无收益） |
| 2026-10-30 | Lead 自执行 | Lead | 指标/文档/回归批次全部由 Lead 串行完成（N=1） |
| 2026-10-30 | 真实服务冒烟 | Lead | `python -m pyknora.serve --port 8019` + `PYKNORA_METRICS_TOKEN=w23-token`：无令牌 403 `metrics_forbidden`、带令牌 200；`/health` 后再取指标可见 `total=3`、路由模板按 `/api/v1/metrics` 与 `/health` 归类（未出现 uuid 维度爆炸） |

## 2.1 与声明块的偏差（登记）

1. **指标路由用「折低基数的真实路径」而不是 `route.path`**：FastAPI 的嵌套 `include_router` 让 `scope["route"].path` 只保留内层前缀（实测拿到 `/metrics` 而不是 `/api/v1/metrics`），因此改为对真实路径把 uuid/数字段折成 `{id}`/`{n}`；未匹配路由统一记 `unmatched`。已在 `src/pyknora/metrics.py::route_template` 的 docstring 与本记录说明。
2. **未接业务埋点**：`counters` 字段保留但当前无业务调用方（入库/问答可通过请求计数与状态码间接观察）。这是有意的范围收敛，避免在收口波次改业务代码。
3. **`tests/test_security_regression.py` 的错误码断言按真实实现**（`unauthenticated` / `invalid_token` / `not_found`），未为了测试改动错误码。

## 3. 执行与门禁

### 3.1 交付物

| 类别 | 文件 | 要点 |
| --- | --- | --- |
| 指标实现 | `src/pyknora/metrics.py`（新，~150 行） | `observe_request`/`increment`/`snapshot`/`reset` + `route_template`；按「方法/模板/状态码」计数，按模板保留最近 512 次时延采样算 `count/avg/p50/p95/max`；静态与文档路径不统计 |
| 指标端点 | `src/pyknora/api/metrics_api.py`（新）、`api/__init__.py`（注册） | `GET /api/v1/metrics`；`PYKNORA_METRICS_TOKEN` 非空时用 `secrets.compare_digest` 校验 `x-metrics-token`，失败 403 `metrics_forbidden` |
| 装配与配置 | `src/pyknora/main.py`（+6）、`src/pyknora/config.py`（+7） | 中间件在保留 `x-request-id` 与访问日志的前提下追加一次指标观测；新增 `metrics_token` 配置 |
| 测试 | `tests/test_metrics.py`（新，6 例）、`tests/test_security_regression.py`（新，9 例） | 指标结构与分位、静态路径不计、零增量忽略、端点自计数、令牌门禁；安全回归覆盖未认证 401、畸形 JWT 401、篡改签名 401、跨工作区不可见 404、五类私网/元数据地址被 SSRF 拦截、CORS 只放行配置来源 |
| 运维手册 | `docs/phase1/RUNBOOK.md`（新，11 节） | 前置、本地起法、Compose 起法、关键配置、迁移与回滚口径、健康判读、指标判读、日志与 request_id、常见故障表、备份恢复、交接清单 |
| 债务清单 | `docs/phase1/DEBT.md`（新，19 条） | 五类分组（能力限域 4 / 安全加固 6 / 可靠性与运维 5 / 文档流程 4），每条含现状、影响、建议动作、触发条件、来源；状态 `OPEN`/`WATCH` |
| 文档更新 | `README.md`（+「运维与排障」节）、`docs/phase1/PLAN_Phase1_MVP.md`、`docs/IMPLEMENTATION_STATUS.md`、本记录 | 三处指针与完成标记；状态文件写明出口仍受限项 |

### 3.2 门禁实测（2026-10-30 12:4x）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check src tests scripts db` | `All checks passed!`（修 1 条 E501：配置长描述换行） |
| 格式 | `uv run ruff format --check src tests scripts db` | `50 files already formatted` |
| 全量单测 | `uv run pytest tests --junitxml=.pytest_cache/w23_final.xml` | **`tests=120 failures=0 errors=0 skipped=0 time=32.465s`**，`PYTEST_EXIT=0`（W22 基线 105 + 新增 15） |
| 真实服务冒烟 | `uv run python -m pyknora.serve --port 8019` + `PYKNORA_METRICS_TOKEN=w23-token` | 无令牌 `403 metrics_forbidden`；带令牌 `200`；`/health` 后指标 `total=3`，`by_route` 含 `/api/v1/metrics`（200/403）与 `/health`（200），未出现按 uuid 分维度 |

### 3.3 指标口径（供后续判读）

- 只统计非静态路径；`/app/*`、`/docs`、`/redoc`、`/openapi.json` 不进入指标。
- `latency_ms` 是**最近 512 次采样**的分位，不是全量历史；重启清零。
- `scope` 固定为 `process`：**不聚合 api 与 worker**，也不跨实例。
- 指标不含 query string、请求体、头部与身份信息；令牌只用于访问控制。

### 3.4 未覆盖/限域

- 指标只覆盖 HTTP 层；Celery 任务时长、队列积压、入库失败率没有指标（可通过 worker 日志与 `documents.status` 观察）。
- RUNBOOK 的备份恢复命令按单机 Compose 编写，**未在真实数据量上演练**；演练口径写在 §10，实际演练留待有真实数据时执行（债务 D-13）。
- 安全回归只覆盖五类不变量，不是渗透测试；SSRF 的 DNS rebinding、限速、robots 仍为债务 D-05/D-06/D-07。

## 4. 提交记录

- 分支：`feature/p1-w23-closeout`（base `dev@ddafbf3`）。
- 提交：`e2f54e2` feat（7 文件：指标实现 + 端点 + 装配 + 2 份测试）、`504573c` docs（6 文件：RUNBOOK + DEBT + 状态/计划/README/本记录）；分支 `feature/p1-w23-closeout` 顶点 **`504573c`**，已推送 `origin`。
- 合入：`--no-ff` 合入 `dev` = **`78f2e92`**（无冲突）；随后本回填提交再推 `dev`。
- 推送实证：`git push -u origin feature/p1-w23-closeout` exit 0（`* [new branch]`）。
