# W21 波次记录：单 URL 受控导入（P0 MVP 任务 P1-T01）

> 规程沿用 Phase 1：开工先落声明块，完成后回填 §2/§3/§4，门禁通过且用户裁决后才提交。

## 1. 声明块

| 项 | 内容 |
| --- | --- |
| 波次 | W21 |
| 分支 | `feature/p1-w21-url-import`，base `dev@3d8ed07` |
| 并行度 N | **1**（单任务，写权集合不可分割：抓取层 + API + 测试 + 文档同进同出） |
| 任务 | P1-T01 单 URL 受控导入（PRD:476-490 的 URL 导入接口；`AGENTS.md` 要求「浏览器/抓取严格执行域名、网络、速率、资源预算策略和每跳 SSRF 校验」） |
| 写权白名单 | `src/pyknora/ingest/url_fetch.py`（新）、`src/pyknora/api/knowledge.py`、`src/pyknora/schemas.py`、`src/pyknora/config.py`、`tests/test_url_import.py`（新）、`docs/phase1/wave-w21-record.md`（新）、`README.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/phase1/PLAN_Phase1_MVP.md` |
| 禁写 | 迁移文件（`documents.source_type/source_url` 在 0002 已存在，无需新迁移）、`src/pyknora/rag/**`、`src/pyknora/retrieval/**`、`tests/conftest.py` |
| 资源槽 | PostgreSQL `pyknora_test`/`pyknora_dev`（5433）、本地回环临时 HTTP 服务（随机端口，仅测试期） |
| 依赖 | W19（文档入库链路 `parse_bytes`/`process_document`）、W20 无依赖 |

### 验收标准

1. `POST /api/v1/knowledge-bases/{kb_id}/documents/from-url` 可用，成功返回 `201` 且 `status=ready`、`chunk_count>0`；
2. 落库行 `source_type='url'`、`source_url` 为重定向后的最终地址、`filename` 由 URL 或内容类型推导；
3. 同知识库内相同内容重复导入返回 `200` + 既有文档（幂等，与上传一致）；
4. 非 http/https scheme、带 userinfo 的 URL 被拒绝（`422`）；
5. 回环/私网/链路本地/保留地址被拒绝（`403`），含 `169.254.169.254` 云元数据地址；
6. **重定向到内网被拒绝**（逐跳复校验，`403`），超过跳数上限 `502`；
7. 响应超限返回 `413`、非白名单内容类型返回 `415`、上游错误返回 `502`；
8. 目标主机 DNS 解析失败返回 `400`（`dns_resolution_failed`）；
9. 跨工作区导入被拒（`404`，沿用 `_load_kb` 的既有语义）；
10. `tests/test_url_import.py` 全绿，`ruff check`/`format --check` 通过，全量 `pytest` 不少于上一波基线（39 passed）；
11. 不新增依赖（httpx 已是 FastAPI 测试栈既有依赖，需在 §3 复核声明）。

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-30 | Lead 自执行 | Lead | 见 §3（本轮未派子 agent：N=1 且写权不可分割） |

## 3. 执行记录

### 3.1 交付物

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/pyknora/ingest/url_fetch.py`（新） | 314 | URL 规范化、scheme/类型白名单、DNS 解析后私网判定、手动逐跳重定向、流式字节上限、`UrlFetchError(code, message, status_code)` |
| `src/pyknora/api/knowledge.py` | 462（+71） | 新增 `POST /api/v1/knowledge-bases/{kb_id}/documents/from-url`（`import_document_from_url`） |
| `src/pyknora/config.py` | 129（+16） | `url_fetch_timeout_seconds=15.0`、`url_max_bytes=5 MiB`、`url_max_redirects=3`、`url_allow_private_network=False` |
| `src/pyknora/schemas.py` | 267（+7） | `UrlImportIn(url, title?)` |
| `tests/test_url_import.py`（新） | 354 | 46 例：规范化正反例、文件名推导、私网/保留段判定、fail-closed、重定向到内网、跳数/字节/类型/DNS、API 403/422/413/415/404、幂等 200、真实回环 HTTP 服务成功路径 |

### 3.2 关键技术取舍

- **不新增依赖**：`httpx>=0.28` 已在 `pyproject.toml` 运行时 `dependencies`（第 28 行），验收 11 成立。
- **手动逐跳重定向**（不用 `follow_redirects`）：每跳都要重新做 `assert_public_target` 校验，`max_redirects=3` 超限返回 502；首跳与终跳都校验，避免「公网 → 内网」跳板。
- **同步内联导入**：Phase 1 无队列（Celery 属 P1），抓取 → 解析（复用 `parse_bytes`）→ 分块 → 落库在请求内完成，与上传同路径；耗时由 `timeout_seconds` + `max_bytes` 双向兜住。
- **幂等键复用内容 sha256**：与上传共用同一去重逻辑，URI 相同内容相同 → 第二次返回 200 + 既有文档；内容变化则视为新文档（内容寻址语义）。
- **类型白名单 html/xhtml/plain/markdown**：PDF/DOCX 经 URL 导入未支持（记为遗留，非本轮验收项）。
- **`url_allow_private_network` 默认 False**：仅本地开发/测试可开；测试用 monkeypatch fixture，真实运行时靠环境变量，生产不得开启。
- **字节上限即时判定**：流式累积到 `max_bytes` 立即 413，不做「先读完再判断」。

### 3.3 遇到的问题与处理

1. `config.py`/`schemas.py` 为 CRLF，首次按 `\n` 拼接的字符串替换 MISS → 改用 `[regex]::Replace` 锚 `(?m)^    @property`，`schemas.py` 用正则追加类。
2. `fetch_url(timeout=...)` 命中 ruff `ASYNC109` → 参数改名 `timeout_seconds`，并改为 `None` 时回落到 `settings.url_fetch_timeout_seconds`。
3. `_resolve_ips` 初版用 `__import__("asyncio")` 丑写法 → 改为正常 `import asyncio`。
4. 测试中多余 `# noqa: N802` 被 ruff 判为无效 noqa → `ruff check --fix` 自动移除。
5. 本机 python 进程无法写 `%TEMP%`（`PermissionError: [Errno 13]`）→ junit 与临时脚本改落 `.pytest_cache/`（已 gitignore），跑完即删。
6. PG/Redis 容器再次 `Exited (255)`（桌面重启后）→ `docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core up -d postgres redis` 恢复，`pg_isready` accepting。
7. 冒烟脚本首版断言口径错误：在 `PYKNORA_URL_ALLOW_PRIVATE_NETWORK=true` 的进程里请求 `169.254.169.254` 得 502（放行后连接失败），**非缺陷**；改用默认配置的新进程复测得 403（见 §3.4 进程 B）。

### 3.4 门禁实测（2026-10-30 11:15–11:16）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check src tests` | All checks passed |
| 格式 | `uv run ruff format --check src tests` | 40 files already formatted。**注意口径**：仓根无参 `ruff format --check` 会额外报 9 个 `docs/**/*.md`（`poc/evidence` 与 `tdd` 文档里的 Python 代码块）需重排，均为存量文档、不在本波次写权内，登记为存量债而非本轮回归 |
| 迁移 | `uv run python -m pyknora.migrate` | database is up to date（无需新迁移） |
| 全量测试 | `uv run pytest tests -q --junitxml=.pytest_cache/w21_junit.xml` | **EXIT=0，tests=85 failures=0 errors=0 skipped=0，16.900s**（W20 基线 39 + 新增 46） |
| 秘密扫描 | 5 个改动文件 × 5 模式 | `secret_hits=1`，唯一命中为 `tests/test_url_import.py:18` 的测试固化口令常量 `correct-horse-battery`（本地测试账号，非秘密） |
| 越界核对 | `git status -- src/pyknora/rag src/pyknora/retrieval tests/conftest.py db/migrations` | 空（未越界） |
| 空白/尾部 | `git diff --check` | exit 0 |

**真实 HTTP 冒烟（真实 uvicorn + dev 数据库，非 TestClient）**

- 进程 A（`PYKNORA_URL_ALLOW_PRIVATE_NETWORK=true`，本地 `http.server:8099`）：register 201 → workspace 201 → kb 201 → `from-url` **201**（`source_type=url`、`source_url=http://127.0.0.1:8099/page.html`、`filename=page.html`、`status=ready`、`chunk_count=1`）；同 URL 二次 **200** 且同 id（幂等）；`/documents/{id}/chunks` total=1。
- 进程 B（默认配置，端口 8017）：`http://169.254.169.254/latest/meta-data/` → **403 `blocked_target`**；`http://127.0.0.1:8099/page.html` → **403 `blocked_target`**；`file:///etc/passwd` → **422 `unsupported_scheme`**。

### 3.5 偏差与遗留

- 验收 1–11 全部满足；无迁移、无越界、无新增依赖。
- 遗留（不影响本轮验收）：
  - **DNS rebinding**：解析与连接之间存在 TOCTOU 窗口，未做「连接后再校验对端 IP」；记录为 Phase 1 加固项。
  - 无按主机/域名的速率与预算控制（`AGENTS.md` 的「速率/资源预算」仅实现了字节与超时上限）。
  - 无 `robots.txt` 遵循、无 ETag/缓存、无重试。
  - URL 导入为同步内联，大页面会占请求时长；队列化留待 P1。
  - PDF/DOCX 经 URL 未支持。

## 4. 提交决策

- 立即可提交状态：3 个改动文件（`+94`）+ 2 个新文件（`url_fetch.py`、`test_url_import.py`）+ 本记录 + README/STATUS/PLAN 文档更新。
- 按 Phase 1 规程：**提交权在用户**。本记录 §4 在用户裁决后回填真实 SHA、推送与合入结果。
- 建议提交拆分：①`feat(p1): W21 单 URL 受控导入`（代码 + 测试）→ ②`docs(p1): W21 波次记录与状态更新`。
### 4.1 提交结果（2026-10-30）

| 项 | 值 |
| --- | --- |
| feat 提交 | `62269a6` feat(p1): W21 单 URL 受控导入（SSRF 逐跳校验 + 类型/字节/跳数上限 + 幂等复用）——5 文件（`+893` 含新文件） |
| docs 提交 | `66320db` docs(p1): W21 波次记录、README API/配置/限制与 PLAN/状态更新——4 文件 |
| 分支 | `feature/p1-w21-url-import` 顶点 `66320db`，已推送 origin（新建分支） |
| 合入 | `--no-ff` 合入 `dev` = **`fb7557d`**（`3d8ed07..fb7557d` 已推送 origin/dev） |
| 回填 | 本条记录 + `IMPLEMENTATION_STATUS.md` 的提交状态由「待裁决」改为真实 SHA |
