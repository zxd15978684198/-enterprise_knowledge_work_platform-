# Wave W6 记录（P0-T13 / P0-T24）

> 按 `docs/plans/2026-10-27-phase0-execution-plan.md` §4.5.5 建立。**本波次起门禁新增一步：询问并执行提交后必须回填本文件 §4**。

## 1. 波次声明（启动时填写）

```yaml
wave: W6
branch: feature/w6-t13-t24
base: dev@cc4a475            # W1–W5 已全部合入 dev，本波次首次直接从 dev 起分支
started_at: 2026-10-28T09:10+08:00
parallel_tasks: [P0-T13, P0-T24]
N: 2
serial_tasks: []
blocked_tasks: [P0-T18]      # 依赖 T17（真实 BrowserSkill 设备/协议），T17 仍 BLOCKED
per_task:
  - task: P0-T13
    agent: w3-t07-outbox
    write_scope:
      - experiments/phase0/index_publication.py
      - experiments/phase0/db/migrations/008_publication.sql
      - tests/phase0/test_publication_faults.py
      - docs/poc/evidence/poc03-publication.md
    resource_slot: {pg_schema: p0_t13, redis_db: 9}
  - task: P0-T24
    agent: w1-t02-env
    write_scope:
      - experiments/phase0/evaluator_replanner.py
      - tests/phase0/test_replanning.py
    resource_slot: {pg_schema: p0_t24, redis_db: 10}
```

### 1.1 N 的计算依据（§4.5.2 三层规则）

| 层 | 结论 |
| --- | --- |
| 依赖层 | T13 前置 T12 **PASS**；T24 前置 T23/T20 均 **PASS（限域）**。W6 计划挂 T13/T18/T24，**T18 移出并行集**：T18 需要 T17 的真实 daemon/extension 与真设备，T17 为 BLOCKED（无 pinned upstream 产物、构建脚本无 windows 目标、本机无真设备） |
| 写入权层 | 两份写权不相交；`004_version_index.sql`（T12）、`006_tasks.sql`（T23）、`contracts.py`、`scope.py`、`conftest.py`、`pyproject.toml`、`uv.lock`、`compose.yaml`、`.env.example` 全部只读 |
| 资源层 | PG 独立 schema `p0_t13`/`p0_t24`；Redis DB 9/10（已用 0–8）；无端口需求；core 容器（5433/6380）已在运行，禁止 `up/down/recreate` |

→ **N = 2**。

### 1.2 迁移编号裁定（Lead）

- 已占用：`001` scope（T03）、`002` approvals（T06）、`003` jobs/outbox（T07）、`004` version/index staging（T12）、`005` budget（T20）、`006` tasks（T23）；`007` 预留给 T27（来源审阅）。
- 本波次将 **`008` 分配给 T13**（发布指针与可见性切换所需状态表）。T24 为纯逻辑/评估器，**不新建编号迁移**，只在自己 schema 内建临时表或复用 T23 的 `006` 结构。

## 1.3 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| C-01 | 发布事务内 Job 的唯一写入者归发布路径（T03/T12/T13 共享契约）：T13 不得在事务外写 Job、不得二次投递 |
| C-02 | `TaskOutcome` 与等待事实的转移入口由 T23 定义，T24 只消费、不重定义 |
| C-09 | 同配置重处理/终态重试语义：T13 的失败重试必须与 T12 staging 口径一致（`STALE_ATTEMPT` 不得被静默吞掉） |
| C-10 | `asset_bindings` 唯一归属与延迟 FK：T13 只读该契约，不改 001 的 FK/触发器 |
| T13 验收重点 | 新索引**未激活前不可见**（`search_visible_chunks()` 为空）、激活为单事务原子切换、旧版本不得被旧 worker 覆盖、失败回滚后可见版本不变、`F1–F18` 中与发布相关场景可注入 |
| T24 验收重点 | 最多 **2 次**重规划、超限进入终态并留证据、评估输入为事实而非自述、重规划不得复制已完成子任务、与 T23 的 TaskOutcome 去重一致 |
| 依赖纪律 | 需要新包一律 `uv run --with <pkg>`（但**依赖准入需 Lead 批准**，不得私自写 `pyproject.toml`）；pytest 必须 `-c experiments/phase0/pyproject.toml` |
| 质量纪律 | 禁 `skip`/`xfail` 冒充通过；未执行项记 `NOT_RUN`/`BLOCKED`；证据内的身份/连接串脱敏 |
| Git 纪律 | 波次内禁止 `git add/commit/push/branch/switch`，由 Lead 在门禁统一处理 |
| 终端纪律 | `pwsh` 工具可用（首选）；超大输出写日志文件再读，不要整文件回灌 |

## 2. 状态表（执行中更新）

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T13 | 进行中 | w3-t07-outbox | 原子发布、可见性切换、回滚与旧 worker 防护 |
| P0-T24 | **完成（PASS）** | w1-t02-env | 22 passed / ruff 通过；`evaluator_replanner.py` 1622 行 sha256 `8c866c52…`、`test_replanning.py` 665 行 sha256 `e3cd5673…`、`poc02-replanning.md` 130 行 sha256 `6f6e29a7…`；唯一 PARTIAL 为 T20 真实 BudgetLedger 台账未接线（用 `BudgetPort` + `StaticBudget`，`release_calls==0`）；kill 打断/旧 revision 晚到回放/HTTP 层记 NOT_RUN。Lead 已独立复验：`22 passed`、`ruff All checks passed` |
| P0-T18 | BLOCKED | — | 依赖 T17 真协议/真设备，未派 |

## 3. 门禁检查（2026-10-28 09:30 完成）

- [x] 各任务四项报告交齐（T13 / T24 均按提示词 06 四段交付）
- [x] 越界写入核对：T13 只写 `index_publication.py`/`008_publication.sql`/`test_publication_faults.py`/`poc03-publication.md`；T24 只写 `evaluator_replanner.py`/`test_replanning.py` + 允许的 `poc02-replanning.md`；其余 `git status` 项为既有未跟踪文件（`.idea/`、`CLAUDE.md`、`docs/plans/README.md`、`docs/teacher/`）与本波次 Lead 记录文件
- [x] 迁移编号：新增仅 `008_publication.sql`（title `008_publication`），`001`–`007` 未改；T24 无编号迁移（`p0_t24` 内联 DDL）
- [x] pytest + ruff + 秘密扫描（见 §3.1 实跑）
- [x] `git status` / `git diff` 审阅：受跟踪文件仅 `docs/plans/2026-10-27-phase0-execution-plan.md`（+92/−91，补状态列与 W6 前沿）；无 tracked 意外改动
- [ ] 追加 `docs/IMPLEMENTATION_STATUS.md` 小节并同步计划前沿到 W7（下一步）
- [ ] 询问用户是否提交本波次
- [ ] 回填本文件 §4

### 3.1 门禁实跑（日志 `%TEMP%\w6_gate.log`、`%TEMP%\w6_gate_junit.log`、junit `experiments/phase0/.pytest_cache/w6_gate.xml`）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| ruff（首跑） | `uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0` | **1 error**：`experiments/phase0/index_publication.py:45` `I001` import 块未排序（T13 自测用的是显式 `--config`，与根级扫描口径不同） |
| ruff（修复后） | `ruff check --fix experiments/phase0/index_publication.py` → 全量再扫 | `1 fixed, 0 remaining` → **All checks passed!**，exit 0 |
| T13 用例（修复后复验） | `pytest ... tests/phase0/test_publication_faults.py -q` | `18 passed`，exit 0 |
| T24 用例（Lead 独立复验） | `pytest ... tests/phase0/test_replanning.py -q` | `22 passed`，exit 0 |
| 全量 pytest | `pytest -c experiments/phase0/pyproject.toml tests/phase0 -q --junitxml=...` | **tests=778 failures=0 errors=0 skipped=0 time=209.289s**，exit 0 |
| 秘密扫描 | 正则在 8 份交付上匹配 `password/secret/api_key/token=`、私钥头、`ghp_`/`sk-` | **0 命中** |
| 端口 | 5433/6380 各 1 监听（core 容器）；5500/5501/5600/5610 均 0 | 无残留监听 |
| 临时目录 / 容器 | 仓库根无 `.t*`/`tmp*`；`pyknora-phase0-postgres-1`、`-redis-1` 均 healthy | 干净 |

### 3.2 交付哈希（sha256，门禁后口径）

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/index_publication.py` | 1130 | `0a340d6c110f463a4c69565e6a35463b323a58c8255a6994d5badac35b5e538b`（`ruff --fix` 后） |
| `experiments/phase0/evaluator_replanner.py` | 1622 | `8c866c5203dbdf4af0031499eb6a1315ba200abfafb364a2a15652bd6381857e` |
| `experiments/phase0/db/migrations/008_publication.sql` | 271 | `52d2739f378bf9bcec61e30cd61346addd4c7271caa2f1fe97d558ce36a36f67` |
| `tests/phase0/test_publication_faults.py` | 1062 | `36020792facb17e7c45f4e5a2921ea2a9415d4d14c2619f465a5451b7d8c4bcf` |
| `tests/phase0/test_replanning.py` | 665 | `e3cd56737e5f1a82696b245494f10c62c4627615324fb187d9748753c8831998` |
| `docs/poc/evidence/poc03-publication.md` | 208 | `77e502134623f61dbbbca160a7f2445ddb179cc2d6e74cd3fcf64b1defb9a0a9` |
| `docs/poc/evidence/poc02-replanning.md` | 130 | `6f6e29a791d5d0cf76988d34c2d43e02569d93dfe68bfcf3f4723b6af12175f0` |

### 3.3 逐任务判定

| 任务 | 判定 | 依据 | 未覆盖（NOT_RUN / PARTIAL） |
| --- | --- | --- | --- |
| **P0-T13** | **PASS（限域）** | 10 000 次并发读零混版本零跨空间；单事务三件事切换、`row_version` 恰 +1；8 类故障注入后可见版本不变；幂等复放不二次投递 Job；回滚 5 轮 | B4 真实重试器 PARTIAL（归 T08/T25）；B5 删除/取消/重建、B6 对象丢失 NOT_RUN（假对象存储）；B7 DoD 对象引用未实现 |
| **P0-T24** | **PASS（限域）** | 22 例覆盖成功/部分/失败/阻塞/超限（第 3 次 `limit_exceeded`+`failed`）/幂等/并发单胜者/只替换未完成/环在登记前拒绝/指纹不匹配不重跑 | T20 真实 `BudgetLedger` 台账未接线（`BudgetPort`+`StaticBudget`，`release_calls==0`）；kill 打断、旧 revision 晚到、HTTP 层 NOT_RUN |

**波次结论**：两项均达成本任务验收（含限域标注），全量 778 passed、ruff 通过、零越界、零秘密命中；**待用户裁决是否提交**。

## 4. 提交决策与结果

- 用户答复：待询问
- 实际提交：待定
- 推送：待定
- 未提交清单：待定
