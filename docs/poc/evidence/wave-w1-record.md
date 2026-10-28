# W1 波次记录（T02 / T09 / T14）

## 1. 派发声明（波次启动时写入，派发即生效）

```text
wave: W1
branch: feature/w1-t02-t09-t14
base: dev@59d53ac
parallel_tasks: [T02, T09, T14]
planned_parallel_tasks: [T02, T09, T14]
N = 3   # 17:00:11 子模块初始化成功后由 1 上调为 3（见 §1.2）
serial_tasks: []
blocked_tasks: []   # T09/T14 已解封
per_task:
  T02 | experiments/phase0/{pyproject.toml,uv.lock,compose.yaml,.env.example}, tests/phase0/test_environment.py, docs/poc/evidence/environment.md | PG schema p0_t02 / Redis DB 0 / 端口 5400 起 | w1-t02-env | 前置 T01（已完成）
  T09 | docs/poc/evidence/docreader_manifest.md, docs/poc/evidence/docreader_fixture_plan.md | 纯静态预检，不占容器 | w1-t09-docreader | 前置 T01 + references/WeKnora 已初始化
  T14 | docs/poc/evidence/browserskill_protocol_manifest.md | 纯静态预检，不占容器 | w1-t14-browserskill | 前置 T01 + references/WeKnora 已初始化
```

- 波次前置动作：`git submodule update --init references/WeKnora`，期望钉在 `662abaf9f944e37689d04c42160034b2cdec6829`。
- 命名口径：用户原规范 `feature/{波次名}:{任务名列表}` 含冒号被 Git 拒绝，经用户确认改用连字符。

## 1.1 素材阻塞记录（2026-10-27）

- 命令：`git submodule update --init references/WeKnora`（原生 PowerShell，`feature/w1-t02-t09-t14`）
- 真实输出：
  - `Cloning into 'E:/project_month/pyknora/references/WeKnora'...`
  - `fatal: unable to access 'https://github.com/Tencent/WeKnora/': Failed to connect to github.com port 443 after 21064 ms: Could not connect to server`
  - `fatal: clone of 'https://github.com/Tencent/WeKnora' into submodule path '...' failed`
  - `Failed to clone 'references/WeKnora' a second time, aborting`
  - `git submodule status` 仍为 `-662abaf9f944e37689d04c42160034b2cdec6829 references/WeKnora`（前缀 `-` = 未初始化）
- 处置：T09/T14 移入 `blocked_tasks`，`N` 由 3 下调为 1，本轮只执行 T02；不使用镜像站或改基线 commit（会破坏固定 SHA 前提）。
- 解除条件：github.com:443 可达后重跑同一命令，核对 `git submodule status` 无 `-` 前缀且 `rev-parse HEAD` = `662abaf9…`。

### 1.2 阻塞解除记录（2026-10-27 17:00:11）

- 前置诊断（16:55，`curl -w`）：`https://github.com/Tencent/WeKnora` → `200`（1.06 s）；`api.github.com/repos/Tencent/WeKnora` → `200`（0.70 s）；`git ls-remote https://github.com/Tencent/WeKnora` → `exit 0`，HEAD `256a3f2966866337f7dc3c5fcc10037f2ae18d52`。同期 `codeload.github.com` 与 `raw.githubusercontent.com` 仍 `000 / exit 28`（超时），故放弃 tarball 通道。
- 备选被否：`https://gitee.com/mirrors/WeKnora` 可 ls-remote（`exit 0`，HEAD `eecfe4b3c8c34c94e9b559e5dd02187b4ae447cd`）但落后且非固定基线，仅作兜底、不改 `.gitmodules`。
- 处置：清理半成品目录后重跑 `git submodule update --init references/WeKnora`（脚本 `%TEMP%\submodule_init.ps1`，日志 `%TEMP%\submodule_init.log`）。
- 真实输出：
  - `Cloning into 'E:/project_month/pyknora/references/WeKnora'...`
  - `Submodule path 'references/WeKnora': checked out '662abaf9f944e37689d04c42160034b2cdec6829'`
  - `submodule_exit=0`
  - `git submodule status` → ` 662abaf9f944e37689d04c42160034b2cdec6829 references/WeKnora (v0.2.2-3267-g662abaf9f)`（无 `-` 前缀）
  - `git -C references/WeKnora rev-parse HEAD` → `662abaf9f944e37689d04c42160034b2cdec6829`；`HEAD_matches_pin=True`
  - 工作树文件数 5676，顶层 `.github,cli,client,cmd,config,dataset,deploy,docker`
- 结论：T09/T14 解封，`N` 由 1 上调为 3；派发 `w1-t09-docreader`、`w1-t14-browserskill`。

## 2. 执行结果（门禁时填写）

| 任务 | 状态 | 关键证据 | 备注 |
| --- | --- | --- | --- |
| T02 | **PASS** | `docs/poc/evidence/environment.md`（§13 为门禁复跑）：lock sha256 `bf0b2dc0…`、镜像 digest `postgres:17-alpine@266d3c25…`/`redis:7.4-alpine@6ba19c50…`、E1 `PostgreSQL 17.11` 5433 连通（0.303 s）+ DDL/DML、E2 `PING=true`/`7.4.12` 6380 连通（0.141 s）；pytest `12 passed`、ruff `All checks passed!`、compose config exit 0 + 空 env 对照 exit 1（`PYKNORA_T02_MISSING_*`）、pip-audit `No known vulnerabilities found` | owner `w1-t02-env`（执行）+ `lead`（门禁硬化）；R5 采纳（asyncpg→psycopg）、R6 改为显式失败；残余见 environment.md §9/§10 |
| T09 | **PARTIAL** | `docs/poc/evidence/docreader_manifest.md`（315 行，38 032 B，sha256 `5770cb9e…`）+ `docreader_fixture_plan.md`（190 行，19 814 B，sha256 `03515acc…`）：docreader `0.1.0`（`docreader/pyproject.toml:3`）、WeKnora `0.8.3`、go 1.26.0；proto 锚 `docreader/proto/docreader.proto` sha256 `33d4135c…`（4689 B）；4 个 RPC = `Read`/`ReadStream`/`ListEngines`/`PreprocessImage`（闭包挂载，`main.py:178-182`）；**「解析失败」不是 gRPC 错误**（`main.py:233-236,257-261`），客户端必须同时判 `error` 字段 + meta 唯一 + EOF + `image_count`；默认无 token 且无 TLS（`auth.py:180-181`、`docker-compose.yml:451,458`）；TXT 不在 DocReader 注册表、DOCX 默认走进程内 anydoc（`Dockerfile.app:78` + `engines.go:44-64`）、PDF 是唯一有 `source_blocks`/`parse_stream` 增量的格式 | PARTIAL 依据 §9：镜像 digest、22 个非 OCR 依赖许可、apt 包许可、OCR 权重清单 = `UNKNOWN`（需 T10 前补，见 BLK-01~06）；证据分级用 A/B/C 而非卡片字面的 A/D/U（语义等价，待 Lead 裁定）；T11 运行层 BLOCKED（无镜像 digest） |
| T14 | **PASS（限域）** | `docs/poc/evidence/browserskill_protocol_manifest.md`（535 行）：31 个暴露动作 100% 有 `manager.go:816-848` 白名单 + `manager.go:986` `"tool."+method` 依据；三组件/授权分跳（token 43 字符、库内存 SHA-256、配对 5 min、设备 90 d、租约 45 s）；版本 `0.3.1`，extension/daemon 同 commit `da6bf4eed2dd7256567e152df8c903c87f6598c3`（不在库内，构建期克隆）；构建白名单无 windows（`build_browserskill.sh:8-11`）、`/tmp` 硬编码（`daemon.go:130`） | 自评 PASS 9 / PARTIAL 3（产物 hash、capabilities 语义、真机 OS 矩阵）/ NOT_RUN 2；核心结论：参考实现内 SSRF/域名/速率**无任何代码**，网络策略必须网关前置 |

## 3. 门禁检查（波次末填写，2026-10-27T17:40:34 复跑）

- [x] **各任务四项报告交齐**：T02（`environment.md` §13 门禁硬化复跑 + 本节）、T09（`w1-t09-docreader` 四项报告已收，含末轮 4 处自查纠错）、T14（`w1-t14-browserskill` 四项报告已收，535 行证据）
- [x] **越界写入核对**：T09 只写 `docreader_manifest.md` + `docreader_fixture_plan.md`；T14 只写 `browserskill_protocol_manifest.md`；T02 只写 `experiments/phase0/*` + `tests/phase0/test_environment.py` + `environment.md`；Lead 另改根 `.gitignore`、`docs/IMPLEMENTATION_STATUS.md`、本记录。**无相互覆盖，无越界**。
- [x] **测试 + ruff + 秘密扫描**（`%TEMP%\w1_gate2.log`，17:40:34）：
  - `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -q` → `............ [100%]`，`pytest_exit=0`（12 passed）
  - `uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0` → `All checks passed!`，`ruff_exit=0`
  - 秘密扫描（12 个仓库自有文件，排除 `.venv`）→ `no_high_risk_secret_hits`；仅 `.env.example` 占位值 `phase0-local-only` 与文档里的 `<token>` 占位符
  - `docker compose ... --profile core ps` → postgres / redis 均 `Up 42 minutes (healthy)`，端口 `127.0.0.1:5433`、`127.0.0.1:6380`
- [x] **`git status` / `git diff` 审阅**：`HEAD=59d53ac`、`branch=feature/w1-t02-t09-t14`、`ahead_of_dev=0`；已跟踪文件仅 ` M .gitignore`（+11/−1）、` M docs/IMPLEMENTATION_STATUS.md`（+30）；未跟踪 `.idea/ CLAUDE.md docs/plans/ docs/poc/evidence/ docs/teacher/ experiments/ tests/`；**暂存区为空**
- [x] **追加 `docs/IMPLEMENTATION_STATUS.md` 小节**：已加「本轮：W1 波次执行（2026-10-27）」
- [x] **询问用户是否提交分支**：见 §4（用户 2026-10-27 17:42 答「提交到当前分支，不推送」；20:39 随后推送 `origin/feature/w1-t02-t09-t14`）

### 3.1 交付哈希（门禁时实测）

| 文件 | 字节 | 行 | sha256 |
| --- | --- | --- | --- |
| `docs/poc/evidence/environment.md` | 26 025 | 411 | `b043900f58eff1d48cc7466d011151686313a6d780f048d278a74d8fb2939c61` |
| `docs/poc/evidence/docreader_manifest.md` | 38 032 | 315 | `5770cb9e2cf22cbabc9dc05bbedaf0535a4d0d4f62d453fb60f5bf5f876adb96` |
| `docs/poc/evidence/docreader_fixture_plan.md` | 19 814 | 190 | `03515accb66e9ecf628a5d72304ed27fbb67f4af1cac60849591b79dd852612d` |
| `docs/poc/evidence/browserskill_protocol_manifest.md` | 43 605 | 535 | `7c83929402d43108fa27888267137630b179299e5a984854361fc1d59db4e205` |
| `experiments/phase0/pyproject.toml` | 655 | 33 | `c0885b0fec0219ba0196ebc4f2a728fbf2cda830b749e05f43a9690beb025d3d` |
| `experiments/phase0/uv.lock` | 30 256 | 262 | `bf0b2dc0746b5ebdbdec80310ff2adcb13d0fe8079438b927f6e4067f1fdba63` |
| `experiments/phase0/compose.yaml` | 3 943 | 96 | `6bcfc61f190e6ebe670a864381dbeb50f8ee41727cdf4716bc8596c3145d79c0` |
| `experiments/phase0/.env.example` | 1 530 | 32 | `c58b4355890bd9dba174dc9a39a509e7b2eb0253c4cc967974466911a8a76f2d` |
| `tests/phase0/test_environment.py` | 14 749 | 320 | `12f2288cc7d7cc2c3afc8fce04bba64fec2e93d85768746ddae6fffb769b79c2` |

### 3.2 波次判定

| 任务 | 判定 | 缺口 / 待办 |
| --- | --- | --- |
| T02 | **PASS** | 端口占用负向演练 NOT_RUN；Redis server 许可（RSALv2/SSPLv1）与 psycopg LGPL-3.0 需 ADR |
| T09 | **PARTIAL** | 镜像 digest、22 个非 OCR 依赖许可、apt 包许可、OCR 权重清单 = `UNKNOWN`，须在 T10 前补 `docreader_sbom.md`；证据分级用 A/B/C（卡片字面为 A/D/U），待裁定 |
| T14 | **PASS（限域）** | 产物 hash、capabilities 语义、真机 OS 矩阵 3 项 PARTIAL；上游 daemon/extension 不在库内且本机不可构建（无 windows 目标、`/tmp` 硬编码） |

## 4. 提交决策与结果

- 用户答复（2026-10-27 17:42）：**「提交到当前分支，不推送」**。
- 实际提交：`08c3c67`（feat，`feature/w1-t02-t09-t14`）+ `6c4f5b6`（chore: 作者元数据，W2 启动时单独提交）。
- 推送结果（2026-10-27 20:39 补记）：**已推送 `origin/feature/w1-t02-t09-t14`，顶点 `6c4f5b6`**；`git ls-remote --heads origin` 实测可见；**未合并 `dev`**。
  - 13 文件 / +2823 −1：`.gitignore`、`docs/IMPLEMENTATION_STATUS.md`、`docs/plans/2026-10-27-phase0-execution-plan.md`、`docs/poc/evidence/{environment,wave-w1-record,docreader_manifest,docreader_fixture_plan,browserskill_protocol_manifest}.md`、`experiments/phase0/{pyproject.toml,uv.lock,compose.yaml,.env.example}`、`tests/phase0/test_environment.py`。
  - 提交前校验：`git diff --cached --check` exit 0；暂存区无 `.env` / `.venv` / `.idea` / `references`；`user.name=zxd15978684198`、`user.email=zxd15978684198@163.com`（沿用本机既有配置）。
  - 首次 commit `e78a498` 漏了 `.env.example`：新加的 `.gitignore` 规则 `.env.*` 把它一并忽略（`add` 报 `paths are ignored`，add_exit=1）。修正方式 = 在 `.gitignore` 加 `!.env.example` 例外后 `git commit --amend`，得 `08c3c67`；实测 `git check-ignore -v` 显示 `.env.example` 命中 `!.env.example`（不被忽略）、`.env` 命中 `.env`（被忽略）。
  - 提交后工作树仅剩开工前既有未跟踪项：`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`。
- 提交信息：`feat(phase0): close W1 - isolated env (T02), docreader & browserskill static prechecks (T09/T14)`
- **未提交**：`.idea/`、`CLAUDE.md`、`docs/teacher/`、`experiments/phase0/.venv/`；`references/WeKnora` 为已初始化的 gitlink，保持钉住 `662abaf9f944e37689d04c42160034b2cdec6829`，本轮未改动、未提交。
- 另有门禁后记账提交：`docs(phase0): record W1 gate result and W2 frontier`（本文件 §4/§3 与计划前沿标注的更新）。
