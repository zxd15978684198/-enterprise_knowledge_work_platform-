# Wave W9 记录（P0-T28 Research 全链 + P0-T29 P0 安全回归）

> 本文件按执行计划 §4.5 规程填写：启动时写声明块（§1），门禁后回填 §3，提交确认后回填 §4。
> 计划入口：[docs/plans/2026-10-27-phase0-execution-plan.md](../../plans/2026-10-27-phase0-execution-plan.md) §4.5。

## 1. 波次声明（启动时填写，2026-10-29 12:55）

| 字段 | 值 |
| --- | --- |
| `wave` | W9 |
| `branch` | `feature/w9-t28-t29` |
| `base` | `feature/w8-t26@f7b9676`（W8 顶点；`dev` 仍 `9d3252a`，尚未合并 W8） |
| `parallel_tasks` | `[P0-T28, P0-T29]` |
| `N` | `2`（两项写入权与资源槽互不重叠，依赖全部已满足） |
| `blocked_tasks` | `[]`（T28 的 BSK 段在 T17/T18 真机可用后不再阻塞；无真实 chat 模型 → 该段 `NOT_RUN` 而非 BLOCKED） |
| `write_scope`（含独占） | **T28**：`experiments/phase0/research_probe.py`、`experiments/phase0/research_*.py`（如需拆分）、`tests/phase0/test_research_e2e.py`、`docs/poc/evidence/poc08-report.md`、`docs/poc/evidence/research-*.json`<br>**T29**：`experiments/phase0/security_probe.py`、`tests/phase0/test_security_gate.py`、`docs/poc/evidence/security-matrix.md`、`docs/poc/evidence/security-*.json` |
| 禁止写入（两人共同） | 根 `pyproject.toml`、`experiments/phase0/pyproject.toml`/`uv.lock`/`compose.yaml`/`.env.example`、`contracts.py`、`scope.py`、迁移 `001`–`008`、`tests/phase0/conftest.py`/`fixtures/manifest.json`、**彼此的交付物**、其它波次记录、`docs/poc/00`–`07`、`docs/IMPLEMENTATION_STATUS.md`（Lead 收口改） |
| 资源槽（互斥） | **T28**：PG schema `p0_t28`、Redis DB `15`、端口 `5500`/`5501`（控制站）<br>**T29**：PG schema `p0_t29`、Redis DB `16`、端口 `5600`<br>库名/端口以 `experiments/phase0/.env.example` 为准（当前库 `pyknora`、PG `127.0.0.1:5433`、Redis `6380`）；任务结束前必 DROP 自己的 schema |
| 依赖前置 | T28←T27/T18/T26/T13/T21 全部 PASS（T18 真机已修、T26 限域）；T29←T06/T08/T13/T21/T26 全部 PASS（限域） |

## 1.1 已知约束与限域口径（开工前必须遵守）

| 约束 | 处理口径 |
| --- | --- |
| 无真实 chat 模型（无厂商 key/额度） | 允许注入 `ModelPort` 替身跑全链结构；**必须**标「真实模型链路 `NOT_RUN`」，判定最高 `PASS（限域）` |
| T17/T18 真机可用但需环境变量 | BSK 真机用例须用 `BSK_LIVE=1`（CPython 3.13 + 已装扩展）门控；未开时写 `NOT_RUN` 理由，**禁止 skip 冒充通过** |
| T26 的 `citation_accuracy=1.0` 是结构测量 | T29 不得把它当「安全已验收」依据；T28 的引用段须沿用 T26 的校验口径 |
| pgvector 0.8.7 已可用 | 需要向量的用例自建 schema 内 `CREATE EXTENSION IF NOT EXISTS vector`；不得依赖 `public` 里已装 |
| 0.95 引用阈值 / RPO/RTO 等阈值未批准 | 只写实测值与样本数，不把候选阈值当 PASS 依据 |
| 幂等与恢复 | 研究任务重跑、审批恢复、取消后迟到结果，均要有负例（对齐 C-01/C-02/C-07） |
| Git 纪律 | 不 `git add`/`commit`/`push`/切分支；不跨写对方文件；不删改既有测试 |
| 资源清理 | 每个测试类结束 DROP 自己的 schema；不留临时目录；端口释放 |

## 2. 任务状态

| 任务 | 状态 | 交付 | 说明 |
| --- | --- | --- | --- |
| P0-T28 | **PASS（限域）** | `research_probe.py`（1464 行 `faa61045…a055`）、`research_flow.py`（1273 行 `0dc917b4…488a`）、`tests/phase0/test_research_e2e.py`（738 行 `ef1da8da…2d0f`）、`docs/poc/evidence/poc08-report.md`（131 行）、`docs/poc/evidence/research-batch.json`（799 行） | 派 `w3-t07-outbox`；LangGraph `plan→schedule→fetch/analyze→approve(原生 interrupt)→publish→finalize`；批量 30 任务 `succeeded 12 / approval_rejected 6 / budget_exceeded 4 / browser_down 4 / plan_invalid 4 / unexpected 0` |
| P0-T29 | **PASS（限域）** | `security_probe.py`（3734 行 `48cbfc37…0c00`）、`tests/phase0/test_security_gate.py`（415 行 `e1e44cb3…890e`）、`docs/poc/evidence/security-probes.json`（5264 行）、`docs/poc/evidence/security-matrix.md`（610 行） | 派 `w2-t03-scope`；549 探测 `refused 536 / allowed 13（全正对照）/ breaches 0` |

## 3. 门禁结果（Lead 独立复跑，2026-10-29 13:35–14:0x）

| 项 | 命令 | 真实结果 |
| --- | --- | --- |
| T28 任务级 | `pytest tests/phase0/test_research_e2e.py -q -o addopts=` | **20 passed in 70.72s**（agent 自报 67.53s，一致） |
| T29 任务级 | `pytest tests/phase0/test_security_gate.py -q -o addopts=` | **14 passed in 4.36s**（agent 自报 4.04s，一致） |
| ruff（本波 5 个 .py） | `ruff check --config experiments/phase0/pyproject.toml …` | `All checks passed!`（两组分别 exit 0） |
| 文件指纹 | `Get-FileHash -Algorithm SHA256` | 9 份交付 sha256 与 agent 自报逐一相同 |
| 全量 pytest | `pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -o addopts=` | **1006 passed / 0 failed / 0 errors / 8 skipped in 388.27s**（无 FAILED/ERRORS 段；stderr 仅 1 行 `Traceback (most recent call last):`，来自子进程故障注入用例的预期噪声，非失败） |
| 秘密扫描 | `sk-*`/`ghp_*`/`AKIA*`/`BEGIN …`/`password=` | 真实凭据形状 0 命中；T29 的 5 个 canary 是**合成哨兵**（`sk-canary-t29-…`、`example.invalid`、`+86-100-0000-0029`），用于验证脱敏 sink，非真实秘密 |
| 越界核对 | `git diff --stat` / `git ls-files --others` | 受跟踪改动仅 `wave-w8-record.md`（Lead 自己修的时间戳）；未跟踪 14 项，本波交付 9 项，其余为 `.idea/`、`CLAUDE.md`、`docs/teacher/`、`docs/plans/README.md` 等既有无关项 |
| 资源残留 | schema/role/DB/端口 | `p0_t28`=0、`p0_t29`=0、`p0_t29%` 角色=0、Redis DB15=0、DB0 仍为他人 `t02:probe`=1（未触碰）、5500/5501/5600/5610 全部 free |
| 容器健康 | `docker ps` | postgres / redis 均 healthy（全量测试期间） |

### 3.1 全量计数对照（防「数字对不上」）

- W8 基线：**972 passed + 8 skipped**（收集 980）。
- 本波新增：T28 收集 20 + T29 收集 14 = **34**，全量应为 **1006 passed**；实测见 §3 行内值，与预期一致方判通过。

### 3.2 偏差与纠错（必须留存）

| 编号 | 偏差 | 处理 |
| --- | --- | --- |
| D1 | 派发时给 T29 分配 **Redis DB16**，但容器 `CONFIG GET databases`=16 仅支持索引 **0–15**，实测 `ERR DB index is out of range` | agent 改用空闲 **DB15**，并在证据 `redis.resource_note` 与矩阵 §4 `RESOURCE_DEVIATION` 显式记录；Lead 复核 DB15 前后均 0 键。**后续波次固定 DB≤15** |
| D2 | T29 汇报正文写「6 项 `NOT_RUN`」 | 实为 **5 项 `NOT_RUN` + 1 项 `RESOURCE_DEVIATION`**（证据 JSON `not_run` 长度=5，矩阵 §4 第 6 条是资源偏差）。以证据文件为准，报告口径已按此纠正 |
| D3 | T28 报告称「Redis DB15 未使用（`REDIS_USED=False`）」，与 T29 声明占用 DB15 冲突 | 复核确认 T28 实际未连 Redis（`REDIS_USED=False`），DB15 仅 T29 短时使用且已清空；两者无并发冲突，但**槽位声明应更保守**（下一波起把「可能不用」的槽位标为 `optional`） |
| D4 | T28 的 `poc08-report.md`/`research-batch.json` 未被派发 prompt 显式列入写权，但属 T28 验收产物 | 判定为声明缺口而非越界：两者均在 `docs/poc/evidence/` 且只被 T28 引用；已在 §1 写权表中补登 |

### 3.3 结论

两项均 **PASS（限域）**：全量 1006 passed / 0 failed，任务级与全量计数自洽，资源零残留，无越界写入，无真实秘密。限域原因：无真实 chat 模型（`ModelPort` 确定性替身）、BSK 真机未开、真实 IdP/MinIO/DNS/pgvector ANN 不可用——生产安全与真模型链路均不得据此宣称已验收。

## 4. 提交决策

> 待用户确认后回填真实 SHA / 推送态 / 未提交清单。
