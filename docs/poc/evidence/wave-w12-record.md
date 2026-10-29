# W12 波次记录：ADR 立项批（P0-T32 §6.1 的 8 条建议）

- 波次编号：W12
- 分支：`feature/w12-adr`（base `dev@557948b`）
- 并行度 N：**1**
- 日期：2026-10-29

## 1. 声明块

| 项 | 内容 |
| --- | --- |
| 任务 | 把 `docs/poc/evidence/Phase0_Full_Gate_Review.md` §6.1 的 8 条 ADR 建议正式立项为编号 ADR（0007–0014），更新索引与状态文件 |
| 为什么 N=1 | 8 份 ADR 需共享同一套编号、格式与证据口径，且互相引用（0009 补 0006、0012 依赖 0010/0014、0013 影响 0012 的浏览器项）；拆给多个执行者会出现交叉引用与措辞漂移，收益低于协调成本 |
| 执行者 | Lead 自执行（无子 agent） |
| 写权（白名单） | `docs/adr/0007..0014_*.md`、`docs/adr/README.md`、`docs/poc/evidence/wave-w12-record.md`、`docs/IMPLEMENTATION_STATUS.md` |
| 只读依赖 | `docs/poc/03_Phase0_Task_Backlog.md`、`docs/poc/05_Phase0_Acceptance.md`、`docs/poc/07_Contract_Closure.md`、`docs/poc/evidence/Phase0_Full_Gate_Review.md`、`docs/poc/evidence/P0_Core_Gate_Review.md`、`experiments/phase0/compose.yaml`、`experiments/phase0/uv.lock`、`docs/poc/evidence/environment.md` |
| 资源槽 | 无（纯文档，不起容器、不连库、不占端口） |
| 验收标准 | 8 份 ADR 均含「日期/状态/依据/背景/决策提议/替代方案/后果与验证条件/实现契约」；状态一律「提议」；每条决策可追溯到 §6.1 的触发事实或具体证据行号；索引 14 行齐全；状态文件前沿与 W12 段更新；引用路径全部可解析 |

## 2. 派发记录

| 顺序 | 动作 | 结果 |
| --- | --- | --- |
| 1 | 读 §6.1 建议表、既有 ADR 0006 与索引格式 | 确认编号续接 0007、格式沿用「决策提议」而非「决策」（项目内无已接受 ADR） |
| 2 | 采集事实基线（镜像 digest、依赖版本、阈值原文、C-07 状态） | `compose.yaml` 两处 digest、`psycopg` 3.3.6 / `psycopg-pool` 3.3.3、`redis` 8.1.0、`05:73-75` 阈值原文、`07:21` C-07 `BLOCKED` |
| 3 | 写 0007–0014 共 8 份 | 完成，状态全部「提议」；许可类与身份类明确标注裁决方，不代替裁决 |
| 4 | 更新 `docs/adr/README.md` | 索引 14 行 + 按裁决方分组的待评审清单 |
| 5 | 更新状态文件前沿与 W12 段 | 完成 |
| 6 | 门禁 | 见 §3 |

## 3. 门禁

| 项 | 方法 | 结果 |
| --- | --- | --- |
| 结构完整性 | 逐份检查「日期/状态/**提议**/背景/决策提议/替代方案/后果与验证条件/实现契约」 | **8/8 通过**，`problems=0`；8 份合计 266 行（19–23 行/份） |
| 决策可追溯 | 逐条比对 §6.1 触发事实 | 8 条全部可追溯到 `Phase0_Full_Gate_Review.md` §6.1 对应行 + 具体证据文件/行号 |
| 链接解析 | 对 `docs/adr/*.md` 全部相对链接做 `os.path.exists` | 首轮 `total=46 missing=3`（`../tdd/05_Security_Controls.md` ×2、`../tdd/06_Retrieval_And_RAG.md` ×1，均为臆测文件名）；修正为 `07_Data_API_Security.md` / `05_Browser_Adapter.md` / `06_Knowledge_Ingestion.md` 后 **15 文件 59 链接 missing=0** |
| 状态一致性 | 检查 14 份 ADR 是否全部为「提议」 | **14/14 为提议**，索引与正文一致；无任何 ADR 被静默升级为「已接受」 |
| 秘密扫描 | `password=`/`dsn=`/`postgres://`/`sk-`/`ghp_`/`AKIA`/`BEGIN PRIVATE KEY` | 8 份 ADR + 索引 + 本记录 **0 命中** |
| 代码门禁 | 本波次零 `.py` 改动 | ruff **N/A**；不跑全量回归（无代码变更），改为「链接/结构/秘密」三项文档门禁 |
| 状态文件 | 前沿行 + W12 段 | `docs/IMPLEMENTATION_STATUS.md` 416 行；10 处 `docs/adr/*.md` 引用全部可解析；前沿已含 W12 与「ADR 14 份」 |
| 越界检查 | `git status --porcelain` | 11 个文件（8 新增 ADR + README + 状态文件 + 本记录），全部在 §1 写权内；`docs/plans/README.md` 为既有未跟踪，未触碰 |

**偏差记录**

- E1 首版 ADR 链接里凭印象写了三个不存在的 TDD 文件名（`05_Security_Controls.md`、`06_Retrieval_And_RAG.md`），链接门禁直接抓出 3 条 missing；按 `docs/tdd/` 实际清单改为 `07_Data_API_Security.md`、`05_Browser_Adapter.md`、`06_Knowledge_Ingestion.md` 后归零。这条偏差说明「链接必须可解析」作为门禁项确实有效。
- E2 门禁脚本首版把「前沿行替换」和「链接检查」写在一起，重跑时因断言失败而中断；改为结构检查与链接检查分离、可重复执行。
- E3 本波次无代码改动故未跑全量回归（1006 passed 的基线来自 W11）；这与「每波次跑全量」的常规不同，理由是写权范围内零 `.py`，回归不会带来新信息，且已用链接/结构/秘密三项覆盖本次风险面。

## 4. 提交决策

- **待提交（11 个文件）**：`docs/adr/0007..0014_*.md`（8 个新增）、`docs/adr/README.md`（改）、`docs/IMPLEMENTATION_STATUS.md`（改）、`docs/poc/evidence/wave-w12-record.md`（新增）。
- **不提交**：`docs/plans/README.md`（既有未跟踪，非本波次产物）。
- **核心结论**：8 条建议已全部立项为编号 ADR，**状态一律提议**；本波次不改变 Phase 0 的任何判定（G-P0 0/5、G-FULL 9 项 PARTIAL 不变），只把「该谁裁决什么」从评审附录变成可引用的正式记录。
- **用户裁决**：选 1 —— 提交 + 推送 + `--no-ff` 合入 `dev`。

### 4.1 落地结果（回填）

| 环节 | 结果 |
| --- | --- |
| 提交 | `dbfce54`（`docs(adr): 立项 8 份 ADR（0007-0014）`，11 个文件）；`git diff --cached --check` exit 0 |
| 推送分支 | `feature/w12-adr` → `origin`，本地/远端同为 `dbfce54` |
| 合入 dev | `git merge --no-ff feature/w12-adr`（ort）→ `b1759b9`，无冲突 |
| 推送 dev | `557948b..b1759b9  dev -> dev`，exit 0 |
| 一致性 | `docs/adr/*.md` 15 文件 59 链接 `missing=0`；14 份 ADR 状态全为「提议」 |
