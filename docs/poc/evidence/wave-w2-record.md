# Wave W2 记录（P0-T03 / P0-T04）

> 本文件按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立，波次结束时填写门禁。

## 1. 波次声明（启动时填写）

```yaml
wave: W2
branch: feature/w2-t03-t04
base: dev@6ff7b72   # W1 合并提交；T02 PASS / T14 PASS(限域) / T09 PARTIAL
started_at: 2026-10-27T18:05+08:00
parallel_tasks: [P0-T03, P0-T04]
N: 2
serial_tasks: []
blocked_tasks: []
per_task:
  - task: P0-T03
    agent: w2-t03-scope
    write_scope:
      - experiments/phase0/contracts.py
      - experiments/phase0/db/migrations/001_scope_core.sql
      - experiments/phase0/scope.py
      - tests/phase0/test_scope.py
      - docs/poc/evidence/poc01-scope.md
    resource_slot: {pg_schema: p0_t03, redis_db: 1}
  - task: P0-T04
    agent: w2-t04-controlsite
    write_scope:
      - experiments/phase0/control_site/
      - experiments/phase0/faults.py
      - tests/phase0/conftest.py
      - experiments/phase0/fixtures/manifest.json
      - docs/poc/evidence/control-site.md
    resource_slot: {pg_schema: p0_t04, redis_db: 2, http_port: 5500}
```

## 1.1 本波次的已知约束

| 约束 | 内容 |
| --- | --- |
| C-07（BLOCKED） | 「P0 来源审阅 / 身份域」需产品+安全+架构三项决策。T03 只做**合成 Principal / Scope / execution binding**，不得自创来源审阅域模型；涉及部分标 `BLOCKED`，不因未决而编造契约。 |
| C-06（DEFINED） | 「无 Run Job 身份传播」：无 Run 的作业恢复时必须重新读取当前 Principal，不得携带旧身份快照。T03 需覆盖。 |
| C-10（NEEDS_POC） | `asset_bindings` 唯一归属 / 延迟 FK：T03 建最小事实表时要为后续迁移留位，不提前建 `asset_bindings` 完整实现。 |
| 迁移所有权 | `001_scope_core.sql` 归 T03 独占；T04 不得新增迁移编号，任何 schema 需求写进证据文件的「后续迁移建议」。 |
| 依赖所有权 | `experiments/phase0/pyproject.toml` 归 T02，T03/T04 **不得修改**；T04 控制站必须用标准库（`http.server`）实现，不得新增 web 框架依赖。 |
| 端口 | core 容器：`127.0.0.1:5433`(PG)、`127.0.0.1:6380`(Redis)；控制站：`127.0.0.1:5500`。SQL 一律用 `SET LOCAL` 只在本事务生效，不污染连接池。 |
| 执行通道 | `pwsh` 工具不可用（`0xC0000142`）；须用 `terminal_open`/`terminal_send` 的原生 PowerShell 7.6.0 持久会话，命令单行，脚本先写 `%TEMP%` 再调用。 |
| Git | 波次内**禁止**任何 `git add/commit/push/branch/switch` 写操作，由 Lead 在门禁统一处理。 |

## 2. 状态表（执行中更新）

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T03 | 完成（PASS） | w2-t03-scope | 合同 + 迁移 001 + RLS；16 用例；C-07 子域 BLOCKED、C-10 留位 NOT_RUN |
| P0-T04 | 完成（PASS，限域） | w2-t04-controlsite | 控制站 + faults + conftest；74 用例；F1–F18 属后续任务 NOT_RUN，F15/F16 能力缺口 PARTIAL |

## 3. 门禁检查（波次末填写）

- [x] 各任务四项报告交齐（T03、T04 均按提示词 06 的四段结构返回）
- [x] 越界写入核对：`git status --porcelain` 新增未跟踪项与声明写权逐项吻合，无越界；`experiments/phase0/pyproject.toml`、`uv.lock`、`compose.yaml`、`.env.example` 均未改动
- [x] 测试 + ruff + 秘密扫描（实测见 §3.1）
- [x] `git status` / `git diff` 审阅：`git diff --numstat` 合计 `added=0 deleted=0 files=0`（开工时那 51 个 ` M` 是 CRLF 工作树噪声，现已全部归零，无实质改动）
- [x] 追加 `docs/IMPLEMENTATION_STATUS.md` 小节
- [ ] 询问用户是否提交分支

### 3.1 门禁实跑记录（`%TEMP%\w2_gate.log`，2026-10-27T19:22:47 → 19:24:05）

| 步骤 | 命令 | 真实结果 |
| --- | --- | --- |
| Git 基线 | `git rev-parse --abbrev-ref HEAD` / `--short HEAD` / `rev-list --count dev..HEAD` | `branch=feature/w2-t03-t04`、`HEAD=6ff7b72`、`ahead_of_dev=0` |
| 未跟踪项 | `git status --porcelain` | 14 条 `??`（含 T03/T04 全部交付与 3 份证据），零 ` M`、零 staged |
| 工作树实质差异 | `git diff --numstat` / `--ignore-cr-at-eol --stat` | `added=0 deleted=0 files=0`；CR 忽略后 `ignore_cr_stat_lines=0` |
| ruff | `uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0` | `All checks passed!`、`ruff_exit=0` |
| pytest（全量） | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0 -v` | `102 passed in 60.45s`、`pytest_exit=0`、`passed_lines=103 failed_lines=0` |
| core 容器 | `docker compose ... --profile core ps` | postgres `Up 2 hours (healthy) 127.0.0.1:5433->5432/tcp`；redis `Up 2 hours (healthy) 127.0.0.1:6380->6379/tcp` |
| 端口回收 | `Get-NetTCPConnection -LocalPort 5500,5501 -State Listen` | `listeners=0` |
| 仓库根临时目录 | 目录枚举 | `strays=`（T04 的 `.t04_tmp/`、`tmp8cko_qyw`、`tmp_g2z249b` 已清理） |
| 秘密扫描 | 正则扫本轮新增文件（AKIA/ghp_/AIza/sk-/PRIVATE KEY/password= 等） | `secret_hit_count=0` |

### 3.2 交付哈希（sha256，`Get-FileHash`）

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/contracts.py` | 800 | `e6389abbf7bfb709b258fe3dfab6f18b25a80aef9f2e90eec77faf035ef72644` |
| `experiments/phase0/scope.py` | 647 | `78844a4543cf4b275073eb43d62daafa294afcde3ec94c17c1f655e9c89633f0` |
| `experiments/phase0/db/migrations/001_scope_core.sql` | 485 | `1dd1f59673ffbfc9ee034c10d0caf56fca5923fa5b355ac98dfe6f689fa54aaa` |
| `tests/phase0/test_scope.py` | 1144 | `e68847834c0f8a39acb8cb1a6fca4cc13d1b03942d9142358629d8a3514b07ba` |
| `docs/poc/evidence/poc01-scope.md` | 389 | `3189c2af15e59082ca348b22d195cce171534d10416b172318e3dc492a4c2633` |
| `experiments/phase0/faults.py` | 317 | `9b30a2cbc3a65928fd8428f64b53dbb2acdb3d973bc353b2cd7da92dca495792` |
| `experiments/phase0/fixtures/manifest.json` | 858 | `ed3f4fc4e061e13ef15c0e940af29734262b704a96776c25388813d6e649ddaa` |
| `tests/phase0/conftest.py` | 206 | `5a817efa43ccf4d77c191df84d214050eae6148b5f790a025b419937e87bb16e` |
| `tests/phase0/test_control_site.py` | 376 | `9cec7d1563308113d0ddadfe1acc4712f90e238665589dfd9c8fdc4db447e5d7` |
| `tests/phase0/test_faults.py` | 329 | `6b8e3def954a33a19e8d27a0901c9faaaf480ff7ea0366901f7e117aa115afbd` |
| `docs/poc/evidence/control-site.md` | 405 | `8f83ddb56cdd5ea3b02fe721e0e1f9ac6e795bffc0498b04520523be2cd50363` |
| `experiments/phase0/control_site/__init__.py` | 36 | `9a079f3006aaf70e12301da6ee5721256d48aa8450c4a814cad57a1e29e02e95` |
| `experiments/phase0/control_site/__main__.py` | 151 | `04f2d02878b72cb12d5fc188006b51d0d21d27f7470add6d0bd4e5a487139ca3` |
| `experiments/phase0/control_site/client.py` | 186 | `e50c5a18e4008cd9cd8d95c5f9bee9bed0e79de6b8ba0f3957a1d414f8abc138` |
| `experiments/phase0/control_site/handlers.py` | 677 | `983b0f97450041d609bd7b07f13eed890eb10b3b4cae9bbc7990cfd0877cefc5` |
| `experiments/phase0/control_site/payloads.py` | 176 | `76e288fb05c2115e6ac61c145462d2402fbeec6f7c65481bfc2ee5e9aaa64d55` |
| `experiments/phase0/control_site/server.py` | 242 | `ef4ed40712c45623c127c6909524052b9c3304e0a7f5757cb7bbba14dd1181e4` |
| `experiments/phase0/control_site/store.py` | 352 | `055a7353774d308159ed14586641bd8408570dcc1d8c358a2000538e483f1b23` |

### 3.3 逐任务判定

| 任务 | 判定 | 依据 | 未完成项 |
| --- | --- | --- | --- |
| P0-T03 | **PASS** | `test_scope.py` 16 例全绿；复合 FK 跨工作区 100 轮拒绝；RLS 默认拒绝（无 scope 非超管零行）；`test_pooled_connection_does_not_retain_scope`、`test_scoped_connection_rejects_autocommit` 证明 `SET LOCAL` 不泄漏；`test_authorize_job_without_run_reads_current_principal` 覆盖 C-06；迁移记录 sha256 可核 | C-07「来源审阅 / 身份域」子域按约束 **BLOCKED**（不自创域模型）；C-10 `asset_bindings` 只留位 **NOT_RUN** |
| P0-T04 | **PASS（限域）** | 控制站 21 向量 + 12 负向 = 33 条 manifest；内容 sha256 7 条真字节校验；`applied_calls`/`X-Idempotent-Replayed` 幂等可验；fault 双门禁（默认 404 / 缺 env exit 2 / flag+env 200 / production exit 2）；10 轮起停 `ALL_PORT_FREE=True LISTEN_AFTER_TOTAL=0`；`FaultBarrier` 无固定 sleep | F1–F18 故障矩阵场景本身属 T05–T30，**NOT_RUN**；F15 私网 IPv6 / WebSocket / DNS rebinding 与 F16 提示注入页为**能力缺口 PARTIAL**（未造假通过） |

## 4. 提交决策

- 用户答复：待询问（门禁已通过，等用户确认是否提交）
- 提交内容：待定（预计 2 个提交：`feat(phase0): W2 T03 scope/identity contracts`、`feat(phase0): W2 T04 control site & fault barrier` + 1 个 `docs(phase0): record W2 gate result`）
