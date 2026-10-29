# 波次记录 W10 — P0-T31 核心出口证据评审

## 0. 声明块

| 项 | 值 |
| --- | --- |
| `wave` | W10 |
| `parallel_tasks` | `[P0-T31]` |
| `N` | 1 |
| `blocked_tasks` | `[]` |
| `base_branch` | `dev` |
| `base_commit` | `ae0a5ae`（W9 合入后的 dev 顶点） |
| `work_branch` | `feature/w10-t31` |
| `resource_slots` | 无（纯文档评审：不启容器、不占 PG schema / Redis DB / 端口） |
| `write_scope` | `docs/poc/evidence/P0_Core_Gate_Review.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/poc/evidence/wave-w10-record.md` |
| `forbidden_writes` | `docs/adr/**`（不得改 ADR 状态）、`experiments/phase0/**`、`tests/phase0/**`、`docs/poc/0*.md`（PoC 计划与报告只读）、`.env*` |

### 依赖满足证据（实拉 git 输出）

```
$ git log --oneline -1 dev
ae0a5ae merge: W9 (P0-T28/T29) research full chain and security regression gate

$ git merge-base --is-ancestor <T26 提交> dev   → exit 0
$ git merge-base --is-ancestor <T29 提交> dev   → exit 0
```

T31 声明的三项依赖：

| 依赖 | 落点 | 状态 |
| --- | --- | --- |
| P0-T26 | `experiments/phase0/rag_probe.py`、`events_probe.py`、`tests/phase0/test_rag_events.py`、`docs/poc/evidence/poc09-report.md`、`rag-citations.json` | 已在 dev（W8 合并 `61624ea`） |
| P0-T29 | `experiments/phase0/security_probe.py`、`tests/phase0/test_security_gate.py`、`security-matrix.md`、`security-probes.json` | 已在 dev（W9 合并 `ae0a5ae`） |
| P0-T30 | `docs/poc/evidence/sbom.md` / 依赖锁与许可证据 | 已在 dev（W7 交付） |

## 1. 任务与 DoD（摘自 `docs/poc/03_Phase0_Task_Backlog.md` §P0-T31）

- **目标**：评估 P0 验证是否就绪，不把六项 PoC 的 partial 当作完整 Phase 0 完成。
- **输入**：核心所有强制测试证据、依赖锁/许可、C-01–C-10 修订建议与处置、质量阈值评审记录。
- **文件**：`docs/poc/evidence/P0_Core_Gate_Review.md`、`IMPLEMENTATION_STATUS`；**不改 ADR 状态**。
- **步骤**：核每 scenario 命令/hash → 核失败/阻塞/残余风险 → 分别评价 G-P0 每门 → 列 TDD/ADR 变更建议与生产前待办。
- **测试**：证据链接/版本/计数一致性、未执行/跳过未被算通过；无需新运行代码。
- **量化验收**：G-P0 所有项 100% 有结论和证据；0 未验证项伪标 PASS；未校准质量阈值不能质量 PASS；安全失败零豁免。
- **注入**：评审故意缺一条证据或更换 lock hash，必须拒绝门禁而非静默沿用。
- **DoD**：D0 文档版 + 可审查的 P0 准入建议和剩余产品/身份/许可门禁。

## 2. 派发记录

| 时间（本地） | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-29 | 合并 W8 / W9 入 dev 并推送 | Lead | `dev 9d3252a -> ae0a5ae`；`git push origin dev` exit 0 |
| 2026-10-29 | 拉 W10 分支 | Lead | `feature/w10-t31` @ `ae0a5ae` |
| 14:12 | 派发 T31（第一次） | `w1-t09-docreader` | **静默失败**：15 分钟无交付物、无消息、无进程；`list_agents` 显示 inactive |
| 14:36 | 派发 T31（第二次） | `w2-t04-controlsite` | 14:36:38 骨架落盘 → 14:49:21 完成 S1（§1 输入清单 134 行）→ 14:50:26 进 S2 → 约 14:55 **异常退出**（`failed before it finished. It left no closing message.`），遗留仓库根 `.t31_tmp/` 23 个中间文件 |
| 14:55 | **Lead 接管** | Lead | 复用其 §1 与 `t31_digest.txt`，自写 §2–§9；真跑注入测试与全量门禁 |
| 15:05 | 发现环境漂移 | Lead | 全量 pytest 首跑出 15 个 `E`：core `postgres` 容器 `Exited (255)`、5433 拒连；另现陌生容器 `pyknora-pg-p0`（`pgvector/pgvector:pg16`，映射 5434，约 26 分钟前起，非本项目 compose 管理） |
| 15:09 | 修复环境并重跑 | Lead | `docker compose -f experiments/phase0/compose.yaml --env-file experiments/phase0/.env.example --profile core up -d postgres redis` → 两容器 healthy；实测 `PostgreSQL 17.11` / `vector 0.8.7` / schema 基线 `p0_t02_probe,p0_t05,p0_t27` |
| 15:21 | 门禁收口 | Lead | 全量 `1006 passed, 8 skipped in 690.20s`；重生成证据已 stash 隔离 |

**两次派发失败的方法学结论**：本任务属"跨 12 份波次记录做全量对账 + 写 400 行评审"，超出单个 subagent 的单轮承载（两次都在"读完材料、开始写"的临界点断掉）。后续同类任务应拆成"证据提取（脚本化）→ 人工判定 → 文档写作"三段，或直接由 Lead 执行。

## 3. 门禁

| 项 | 命令 / 方式 | 结果 |
| --- | --- | --- |
| 引用一致性 | 正则扫描本波次两份文档的全部 `docs/`、`experiments/`、`tests/` 路径引用 | `exists=12 / missing=4`；4 条均为已登记缺口（D5）或已标明的伪路径，**无新增断链** |
| 全量 pytest | `pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -o addopts=` | **1006 passed / 0 failed / 0 errors / 8 skipped in 690.20s**，与 W9 基线（1006）一致 |
| ruff（本波次改动） | `git status` 无 `.py` 改动 | 本波次 N/A |
| ruff（存量） | `ruff check experiments/phase0 tests/phase0` | 首查 **4 条 `I001`**（`rag_probe.py:18`、`research_flow.py:19`、`research_probe.py:26`、`security_probe.py:26`，W8/W9 引入）→ **本轮已 `--fix` 清理，复检 `All checks passed!` exit 0**（见 §3.1） |
| 秘密扫描 | 正则覆盖：明文密码赋值、带凭据的 DSN、云厂商密钥前缀、GitHub/AWS token 前缀、私钥 PEM 头 | **0 命中**（首次扫描因本条规则字面量自命中 1 处，已改写为描述式表述） |
| 证据 hash 对账 | 34 份 md 中引用的文件路径 + sha256 全量重算 | `MATCH=31 / DRIFT=119 / cited_path_missing=0` |
| 注入测试 | 证据完整性校验器 3 例 + 口径 linter 3 例 | 全部符合预期（详见评审文档 §8） |
| 越界检查 | `git status --porcelain` | 仅 3 个写权内文件；无 `.t31_tmp/`、无 `tmp*/` 残留 |

**偏差与处理（E1–E3）**

- **E1 首跑被环境漂移污染**：全量 pytest 首跑 15 个 `E` 后中止。判定为环境（容器退出）而非代码回归，按规程**不计入门禁失败**；修复环境后原命令重跑通过。此事件本身已作为评审文档 §2.4 的一条证据（"环境可复现性未纳入 G-P0"）。
- **E2 全量 pytest 会重写 6 份 tracked 证据**：`poc01_owner_process_level.json`、`rag-citations.json`、`research-batch.json`、`retrieval-baseline.json`、`security-matrix.md`、`security-probes.json`（时间戳 / PID / 计数抖动）。`git checkout --` 被安全网阻止，改用 `git stash push -m "w10-fullsuite-regen-6-evidence" -- <6 路径>` 隔离，**不入本波次提交**（本轮共两次全量跑，故 `stash@{0}` 与 `stash@{1}` 各存一份，均为同一组 6 份文件的时间戳/PID 抖动，提交前已全部隔离）。这是测试侧债务：证据产物应写入临时目录而非 tracked 路径。
- **E3 仓库根临时目录**：pytest 期间 `tempfile` 回退到 CWD 产生 `tmp8e1p9388/`、`tmpjwti2ho6/`，连同 `.t31_tmp/` 已一并删除。

### 3.1 存量 `I001` 清理与证据 hash 失效

用户裁决「本轮顺手清掉」，执行 `ruff check --select I001 --fix` 于 4 个文件（**仅 import 排序，12 插入 / 8 删除，无逻辑变更**），复检全量 `All checks passed!`（exit 0）。行为不变由定向测试证明：`test_rag_events.py` + `test_research_e2e.py` + `test_security_gate.py` = **55 passed in 308.08s**；随后全量回归 **1006 passed / 0 failed / 0 errors / 8 skipped in 936.41s**（与修复前同计数，确认无回归）。

**副作用：这 4 个文件的 sha256 变化，使 W8/W9 波次记录中登记的 hash 成为历史值**（这正是评审文档 §2.3 量化的 hash 漂移机制在本次评审自身上的复现）。登记对照如下，后续引用应以"新 hash"为准：

| 文件 | W8/W9 记录中的旧 sha256（前 12 位） | 本轮修复后 sha256（前 12 位） |
| --- | --- | --- |
| `experiments/phase0/rag_probe.py` | `7a141be76dd4` | `9c0adfd059c7` |
| `experiments/phase0/research_flow.py` | `0dc917b4ad2e` | `90fddb82abb2` |
| `experiments/phase0/research_probe.py` | `faa61045abaa` | `9ef99b7dafc7` |
| `experiments/phase0/security_probe.py` | `48cbfc372352` | `fa31c7e804f6` |

处理原则：**不修改历史波次记录**（它们如实记录了当时的 hash），而是在本文件中登记 supersede 关系。这也印证了评审文档 §9 的建议——证据里的 hash 只能作"记录时点"凭据，不能作为门禁判据。

## 4. 提交决策

- **已提交（用户裁决「提交 + 推送 + 合入 dev」）**：
  - `44cc5eb` docs(poc)：`P0_Core_Gate_Review.md`（新，424 行）+ 本文件 + `IMPLEMENTATION_STATUS.md`（3 文件 +530/−3）。
  - `5ca0659` style(T26/T28/T29)：4 个文件只做 import 排序（+12/−8），清掉本轮发现的 4 条 `I001` 存量告警，复检 `All checks passed` exit 0。
- **不提交**：`.t31_tmp/`（已删）、`tmp*/`（已删）、6 份重生成证据（已 stash）。`docs/plans/README.md` 为既有未跟踪文件，不属本波次写权。
- **核心结论**：**G-P0 不成立 —— 5 门 0 门 PASS（G1/G2/G3/G4 = PARTIAL，G5 = NOT_RUN）**；任务级 28/30 成立。出口建议见评审文档 §9。
- **推送状态**：`feature/w10-t31` 已推送，顶点 `20744b1`（`git ls-remote` 与本地逐位一致）；随后以 `--no-ff` 合入 `dev` → merge commit `8c97c75`，`dev` 已推送（`ae0a5ae..8c97c75`）。
- **下一步**：W11 = P0-T32（全量评审 / 收口），待用户授权后开工；三件可补做项见 `IMPLEMENTATION_STATUS.md` W10 段。
