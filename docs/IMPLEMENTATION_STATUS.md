# PyKnora 实施状态

更新日期：2026-10-28（Asia/Shanghai）。当前为 **Phase 0 实验阶段**：W1–W7 七个波次已执行完成，并连同两条修复分支（`fix/w5-t17`、`fix/w7r-t05-t09`）按序合入 `dev`。判定现状：P0-T05、P0-T09、P0-T13、P0-T17、P0-T24、P0-T25、P0-T27、P0-T30 为 **PASS（限域）**；P0-T18、P0-T26、P0-T28、P0-T29、P0-T31、P0-T32 仍 NOT_STARTED。九项 PoC 无整体 PASS；六份 ADR 仍为 Proposed（`0006` 已登记为提议）；所有结果仅是实验环境内的验证结论，不代表 ADR 已接受或 Phase 0 工程验证通过。

## 阶段收口：W1–W5 全部合入 dev（2026-10-28）

- `dev` 由 `6ff7b72` 推进到 **`d9eb02c`**，已推送 `origin/dev`（`6ff7b72..d9eb02c`）。
- 采用逐波次 `--no-ff` 合并（用户选 B），四个合并提交保留波次边界、可单独回滚：
  - `94e8a62` Merge `feature/w2-t03-t04`（W2：T03 PASS、T04 PASS 限域）
  - `9b4c9ff` Merge `feature/w3-t05-t07-t10-t15-t19`（W3：T05 PARTIAL、T07/T10/T15/T19 PASS 限域）
  - `10c1f5b` Merge `feature/w4-t06-t08-t11-t16-t20-t22`（W4：六项均 PASS 限域）
  - `d9eb02c` Merge `feature/w5-t12-t17-t21-t23`（W5：T12/T21/T23 PASS 限域，T17 BLOCKED）
- 合并前已验证 `git diff --stat dev fb4d64e` 为空：`dev` 合并后的**树与 W5 门禁通过的 `fb4d64e` 完全一致**，因此 W5 的 `738 passed / 0 failed / 0 skipped in 262.78s` 结论对 `dev` 直接成立，无需重跑。
- 未合并入 `dev` 的内容：无（五个波次分支均已并入）。`refs/stash` 内仍留 W4 的 `w4-gate-regenerated-wire-evidence` 暂存记录（`fec4144`），内容已被后续提交覆盖，尚未丢弃。
- 下一步前沿 **W6 = T13（原子发布）+ T24（Evaluator/重规划）**，`N=2`（T18 依赖 T17 真设备/真协议，T17 为 BLOCKED，故不派）。

## 本轮：Claude Code 指引与计划目录（2026-10-27）

- 新增根 `CLAUDE.md`，记录当前仓库事实、文档权威顺序、命令与目标架构边界；未新增业务代码或依赖。
- 新增 `docs/plans/README.md`，确立用户要求：后续所有计划写入 `docs/plans/`，执行证据仍放 `docs/poc/evidence/`。
- 未修改 PRD/TDD/ADR/参考源码；未运行 pytest/ruff（根项目无测试与 lint 配置，N/A）；未暂存、提交或推送。
- 工作区另有用户未跟踪内容 `.idea/`、`docs/teacher/`，本轮未触碰。

## 当前轮：P0-T01 契约闭合（仅文档）

- 起始分支 `master`，HEAD `5e7e3998ff553edb7a75123f4bea8539b9453f6a`，`git status --short` 为空；没有既有未提交改动。原 PRD Wiki P1 调整已在当前基线，保留其内容。
- 已读取 AGENTS、PRD、实施状态、Phase 0 评审/风险/计划/Backlog/DAG/验收/指南、相关八份 TDD 与全部五份 ADR；未重新审计参考源码或核实远端最新协议。
- 新增 [07 契约闭合](poc/07_Contract_Closure.md)：C-01–C-10 全部给出字段/命令、唯一逻辑写入者、可信身份传播、提交点、幂等/唯一约束、失败恢复、生产阻断及验证任务；附最小 TDD 修订建议、INV-01–08 和双空间纸面推演。
- 状态汇总：DEFINED 为 C-01/02/06/08/09；NEEDS_POC 为 C-03/04/05/10；BLOCKED 为 C-07。DEFINED 只表示实验契约已定义；生产身份域例外、P0 来源审核 API/角色及 PRD 字段映射仍待产品/安全/架构决策，未修改 PRD/TDD/ADR 内容或状态。
- Checkpoint 独立 saver fencing、原生 interrupt/resume、审批与外部提交、Outbox/Celery、DocReader/BrowserSkill 真协议、索引发布、无 Run Job 身份传播均未运行验证；C-10 延迟 FK/绑定规则也待真实 PG 验证。每项不变量已映射后续现有任务/故障场景，未新增 DAG 依赖。
- 最小同步 PoC README、Backlog、DAG 与执行指南的任务进展；保留历史规划/验收目标，九项 PoC 均 NOT_STARTED。未安装依赖、运行容器、写业务/实验代码、启动 PoC、暂存、提交或推送。
- 已执行 `python -`（通过 PowerShell here-string 输入，仅用 pathlib/re/subprocess/graphlib 等标准库）：10/10 契约必填要素、8/8 不变量测试映射、全仓 AGENTS/docs 的317个本地 Markdown 链接、59个成对 fenced 代码块检查通过；32任务/72边，Backlog 与权威依赖表及 Mermaid 逐边一致、拓扑无环，T02/T09/T14 前置均为已完成的 T01。零静态检查错误。
- 已执行 `git diff --check`（退出0）；`git diff --exit-code HEAD -- AGENTS.md docs/PRD_PyKnora.md docs/tdd docs/adr src pyproject.toml uv.lock references/WeKnora`（退出0）；`git diff --stat`、`git status --short` 并审阅全部文档差异。改动仅新增07文档及最小更新5份已有文档，共6文件，未暂存。新文件也另检查行尾空白（通过），不依赖 git diff 对未跟踪文件的覆盖。
- pytest/ruff/业务集成/安全/性能测试均未运行（文档任务不适用）；没有验证远端链接可达性或执行 Mermaid 渲染，本轮无新增图。纸面推演不能充当运行证据。

## 上一轮：Phase 0 技术验证规划（历史交付）

- 起始 `master` / `feb7b4a30e41a904a48b42d971d4d0844f1cf6a3`。唯一既有工作区改动为 PRD F-14 P0→P1，完整保留、不暂存；当前 PRD SHA256 为 `7DAADD701063FAB7E5DC61F9FFBDD49463397A35BB73DA2F3F01C65C4EB6C1BD`。参考仍为 `662abaf9f944e37689d04c42160034b2cdec6829`、工作树干净。
- 阅读规范/PRD/状态、三份研究、八份TDD、ADR索引和全部五份提议，及现有PoC索引；定向只读复核真实DocReader proto/main/registry和BrowserSkill接入/daemon/构建pin。没有重新全量源码审计。
- 实际新增七份规划：[00 ADR就绪度/一致性](poc/00_ADR_Readiness_Review.md)、[01 风险登记](poc/01_Technical_Risk_Register.md)、[02 九项PoC计划](poc/02_Phase0_PoC_Plan.md)、[03 任务Backlog](poc/03_Phase0_Task_Backlog.md)、[04 依赖DAG](poc/04_Phase0_Dependency_DAG.md)、[05 验收与Exit Gate](poc/05_Phase0_Acceptance.md)、[06 执行指南/下一轮提示词](poc/06_Phase0_Execution_Guide.md)，最小更新PoC README及本状态。
- ADR五份推荐状态均 Proposed：边界原则依据充分，但复合记录含未验证恢复/协议/事务/身份假设；原ADR/TDD内容及状态不变。没有现成已接受决策被擅自变更。
- 一致性评审提出C-01–C-10局部契约缺口：共享UoW中的Job/发布owner、TaskOutcome去重/转移入口、独立锁与saver连接fencing、稳定模型响应intent绑定、BSK真协议/能力、无RunJob主体策略binding、P0 URL审阅API、Outbox枚举/死信、同版本失败重试命令、asset/FK创建环。仅在规划记录最小修订建议，未扩大修改TDD/ADR。
- 风险R-01–R-13及补充R-14–R-17有评分/阻塞/PoC/任务/备选，优先五项为R-11权限传播、R-01单writer、R-02副作用、R-05版本发布、R-04可靠Outbox；R-16内容安全贯穿停线。
- 九项PoC均 NOT_STARTED；本轮明确PASS/PARTIAL/FAIL/BLOCKED/NOT_STARTED、F1–F18故障与G-DOC/G-P0/G-FULL。P0核心准入不与缺BSK设备的全量Phase0混同；有强制项PARTIAL/BLOCKED不得宣称全量PASS。
- 32个单会话任务每项都有PoC/PRD/TDD、目标/依赖/输入/预计文件/步骤/测试/量化验收/注入/复杂度/风险与备选/唯一状态责任域/集成影响/DoD；规划点数共66（不是工期）。第一项P0-T01只写契约闭合文档，下一轮提示词已给出，不在本轮执行。
- 官方能力边界只读复核LangGraph interrupt、Celery Tasks、PG锁、Playwright context；精确依赖/镜像/许可/CVE在T02/T09/T14锁定。本轮尝试打开pinned BrowserSkill upstream树被工具以restricted URL拒绝，未获真上游源码，不推断仓库不存在/协议不兼容；该材料缺口不冒充PoC已启动。

## 本轮规划检查与执行限制

- 最终静态检查通过：PoC目录8文件+本状态共9文件、111个本地链接、32任务、72条依赖；拓扑无环、Backlog/权威表/Mermaid逐边一致、波次满足前置、任务必需要素/九项NOT_STARTED/13项必需风险评分检查通过。另核49处任务→TDD章节引用、10项设计缺口与五份ADR提议原状态；零检查错误。
- 按权重并列最长链：解析线T01→T02→T04→T10→T11→T12，与可靠作业线T01→T02→T03（或T04）→T07→T08→T12同长；之后共同T13→T25→T26→T29→T31，均23点，全量加T32为24点。BrowserSkill材料/真设备等待可能改变日历关键路径，未承诺工期。
- 复用上轮系统TEMP中已存在的Mermaid 11.17.2/Puppeteer与系统Edge headless，仅渲染文档：完整32节点/72边图1/1 parse+SVG render成功，抽看截图无节点裁切。GitHub自身渲染器未运行；未安装新包/修改项目依赖，不是浏览器PoC。
- 本轮所有pytest/ruff/uv sync/docker/Celery命令都只写为未来草案，**均未运行**；只用现有Python标准库/Node做静态文档验证。当前没有真实模型/DB/队列/解析/浏览器/安全/性能/灾备测试结果。
- 静态检查脚本及SVG/截图位于 `C:/Users/unihou/AppData/Local/Temp/pyknora-phase0-plan-check-855088410cb84f10889dedaaa785b76f`，不是长期生产产物；实际命令 `python .../check_plan.py`、`python .../check_crossrefs.py`、`node .../render_plan.mjs <临时目录>`。已执行`git diff --check`（通过）、`git diff --exit-code -- src pyproject.toml uv.lock AGENTS.md docs/tdd docs/adr references/WeKnora`（退出0）、Git改动/参考SHA/参考工作树及PRD hash检查；除原有PRD修改外，仅七份新规划/PoC README/本状态变化，未暂存或提交。

## 本轮本地提交授权（2026-10-27）

- 用户明确要求提交本轮规划，并包含PRD中Wiki的P1调整；提交范围为七份PoC规划、PoC索引、本状态及PRD，共10个文档文件。
- PRD F-14从P0调整为P1已获用户明确确认，随本次提交纳入本地需求基线；上文关于“未提交/待用户处理”的记录为规划时历史状态，不再是当前待办。
- 提交前检查工作树、暂存区、PRD差异与空白；只提交上述文档，不推送远端、不变更ADR状态、不执行PoC。提交SHA通过`git log -1 --oneline`查询，不在提交中自引用。

## 下一轮（只按任务推进）

1. P0-T01 已完成文档交付。下一建议执行 P0-T02，范围以 Backlog 为准；本轮不执行。
2. T01后可并行资料预检T09/T14与环境T02，再按DAG波次执行；前置只有PARTIAL时后续限域且保留原失败，禁止删依赖制造通过。
3. 产品/安全/架构仍需确定身份域例外、来源审核字段/API、合法站点/数据权利、费用/审批核查人、精确发行物许可、质量阈值/TTL/RPO/RTO。Wiki P1 已随前轮获授权的本地提交纳入当前基线，不再待用户提交。

## 上一轮：技术设计（历史交付）

- 起始主仓库 `master` / `7b2a02ff828eb5694d90d5b88947d6694918fdab`，工作树干净；参考 HEAD `662abaf9f944e37689d04c42160034b2cdec6829`，与研究基线一致，前后工作树均干净，没有更新/切换/修改参考源码。
- 已读 AGENTS、PRD、实施状态、全部现有 ADR/TDD/PoC 索引及四份研究输入；仅针对解析 proto/TXT、resume、BrowserSkill daemon/构建、上传 Enqueue、核心/富化边界定向复读源码，不声称重做全量审计。
- 实际新增八份 TDD：[01 架构与统一契约](tdd/01_System_Architecture.md)、[02 Harness](tdd/02_Agent_Harness.md)、[03 Task DAG](tdd/03_Task_Orchestration.md)、[04 Graph](tdd/04_LangGraph_Workflows.md)、[05 Browser](tdd/05_Browser_Adapter.md)、[06 Ingestion](tdd/06_Knowledge_Ingestion.md)、[07 Data/API/Security](tdd/07_Data_API_Security.md)、[08 测试策略](tdd/08_Test_Strategy.md)，更新 TDD 索引。
- 新增 ADR-0001–0005 并更新 [ADR 索引](adr/README.md)：运行时边界、Outbox/版本发布、审批/副作用、解析/浏览器复用、空间/事件/隐私；**均为提议**，没有自动标为已接受。
- 统一 20 项必需标识、run/thread 1:1、状态拥有者、typed port/schema、工具 receipt、预算预留/结算、DAG revision、Job fencing、不可变索引及原子活动指针、最终消息/事件事务；08 逐项规定 F1–F11 的执行顺序、提交点、CAS/唯一约束、恢复触发者和最终断言。
- 官方资料核对：LangGraph interrupt/persistence、Celery Tasks（当时页面为 5.6）、Playwright context、PG 锁/RLS、pgvector、FastAPI lifespan、Pydantic strict、S3 presigned URL、OTel 敏感数据。链接位于对应 TDD，页面核验不等于已安装版本或实测结果；精确依赖锁、CVE/SBOM/许可证核验在后续任务，未改依赖。
- **期间出现的外部工作树编辑**：PRD §6 表格被非本轮工具编辑，F-14 从 P0 改为 P1，另有表格排版修改；已保留。初读冲突历史保留，TDD 按当前工作树 P1 对齐，正式需求基线待评审/提交。PRD diff 不属于本轮作者交付，不回滚、不暂存、不宣称本轮修改。该版本 PRD SHA256：`183B7BF06720BE2C650DD7A2B83B5F87F5AD9ECBEEE843DF1A6F0853449EDD77`。
- 跨文档复核已修正：URL 初次抓取先 Source/fetch Job，批准后才建文档版本；reprocess 以显式处理配置 revision 新建、同配置幂等复用；审批唯一约束仅限有效 pending/approved，过期可新建；asset bytes 与 artifact 逻辑 manifest 区分；PRD §9.9 来源审批字段细化为 review_decision_id 属显式设计提议，不与工具 Approval 混同。

## 技术设计轮验证及限制（历史）

- 使用临时目录 PowerShell/Python 检查脚本检查八份 TDD、五份 ADR、两个索引及本状态，共 **16 文件、65 个本地链接、17 个 Mermaid 块、7 个 Python 草案 AST、2 个 JSON 示例、20 必需 ID、11 类故障覆盖，零错误**；新增文件行尾空白检查通过，7 个定向复核源码路径存在。人工复核状态、接口、错误码与 PRD 映射，修正项见上文；字面覆盖检查不等于证明设计正确。脚本与 npm 验证包仅位于系统 TEMP，没有加入项目依赖或源码。
- 实际使用 Mermaid **11.17.2**、Mermaid CLI 临时依赖及系统 Edge headless 验证 17 个 Mermaid 块：**17/17 parse 与 SVG render 成功**；抽看总体组件图和 ER 图截图，未见节点裁切。GitHub 自身渲染器未运行，无法保证其版本/主题与本地完全一致。
- 已执行 `git diff --check`（通过）、`git diff --name-only`、`git ls-files --others --exclude-standard`、`git diff --exit-code -- src pyproject.toml uv.lock references/WeKnora`（退出 0）、参考 `rev-parse HEAD`/`status --porcelain=v1`（指定 SHA、干净）。仅本轮 16 个文档文件及前述外部 PRD 编辑；未修改业务源码、依赖、锁文件或参考源码，未暂存/提交。
- 实际文档验证命令：`python "$tddCheckDir/check_docs.py"`、`node "$tddCheckDir/render.mjs"`、`python "$tddCheckDir/final_check.py"`；临时目录为 `C:/Users/unihou/AppData/Local/Temp/pyknora-tdd-check-a62beb7f2085408f97570c019a6e0091`。渲染使用临时安装的 mermaid/Mermaid CLI、Puppeteer 与系统 Edge，不是业务 PoC。SVG 和两张抽检截图位于该临时目录，不作为项目生产资产或长期证据存储。
- 单元、集成、端到端、安全、性能、备份恢复均**未运行**。TDD 内命令是未来建议，百分比/时延/并发/TTL 是 PRD 目标或待验证建议，不是通过结果。

## 本地提交记录（2026-10-27）

- 用户明确要求将本轮技术设计提交到本地；提交范围为八份 TDD、五份 ADR、两个索引及本状态文件，共 16 个文档文件。
- 提交前检查工作树、暂存区、差异及空白；原暂存区为空。PRD 当前仍有外部未提交的 F-14 P0→P1 修改，明确排除在此次提交之外，保留工作区内容。
- 参考源码 SHA 仍为 `662abaf9f944e37689d04c42160034b2cdec6829` 且工作树干净；没有业务代码或依赖改动。本次仅本地提交，不推送远端；提交 SHA 通过 `git log -1 --oneline` 查询，避免提交内自引用。

## 技术设计轮未决与下一阶段（历史，当前安排见上）

1. 先评审 ADR/统一模型：空间内 Principal 与 PRD 全局 User 的身份域例外；来源审核/工具审批字段细化；P1 Wiki 当前工作树变更的正式基线。
2. 下一轮仅先形成 PoC 计划与环境/数据/阈值清单；优先 H1/H2（原生 checkpoint/审批/副作用/单 owner）和 H4/H5（五格式/图/定位、Outbox/重复投递/版本激活），H9 安全贯穿，之后 H6 检索基线。
3. H3 DAG 与 H7 真实 BrowserSkill/Playwright 为 P1 前置，H8 Research 端到端依赖它们；不能用 mock 声称真实扩展协议通过。
4. 明确精确依赖版本/镜像、Redis/MinIO/DocReader/BrowserSkill/模型权重许可、维护/CVE、身份认证、首批合法站点、全文/摘要权限、预算计费、审批人和人工核查责任。
5. 单 owner 与原生 saver 连接失效 fencing 尚未实测；若不满足约束必须单活降级。未知外部写没有去重/查询时只能人工核查；不能承诺 exactly-once。FTS/BM25、中文分词、PII 处理、TTL、RPO/RTO/迁移升级门槛均待真实证据。

## 上一轮：源码审计（历史记录）

- 开始时主仓库工作树干净，HEAD `a6220dd`；参考仓库仍为 `main` / `662abaf9f944e37689d04c42160034b2cdec6829`，工作树干净。
- 新增 [01_Source_Architecture.md](research/01_Source_Architecture.md)：Agent、BrowserSkill、DocReader、摄取、RAG/Agent Chat/SSE 的静态调用链、状态和真实路径/符号/行号，区分 A/D/N/U 证据。
- 新增 [02_Architecture_Mapping.md](research/02_Architecture_Mapping.md)：Python 模块建议及复用/重写/替换/延期依据，明确 Harness、Graph、Task DAG、Celery 边界，尚非已接受 ADR。
- 新增 [03_Compatibility_Risks.md](research/03_Compatibility_Risks.md)：协议、数据、队列、许可与风险矩阵，以及 Phase 0 必验证假设/退出条件。
- 重要修正：上下文摘要 checkpoint 与 RunSnapshot 不同；Python DocReader 不覆盖原 Go simple 路径的全部格式；普通上传未验证为统一 Outbox；attempt guard 不等于 PRD 的不可变版本索引激活；官方 ListEngines 描述与当前调用存在漂移。
- BrowserSkill 配套构建清单固定 0.3.1 / `da6bf4eed2dd7256567e152df8c903c87f6598c3`，只核验了 WeKnora 接入层和构建清单，未获取单独 upstream 实现或运行真实扩展。
- 本轮无报告编写硬阻塞；真实测试前置未落实：当前 shell 无 Go 命令，DocReader 依赖/服务、模型、数据库/队列和浏览器设备未验证可用。未运行单元、集成、安全或性能测试；不得解读成通过。
- 保留原 PRD、旧报告、AGENTS、参考源码和 Python 业务代码；本轮预期改动仅三份研究报告与此状态文件。
- 静态验证通过：PowerShell 断言检查 100 个本地链接、90 处源码行号范围、6 个成对的 Mermaid fenced 代码块；另抽查 30 个关键源码符号、参考 SHA/工作树和受保护文件未变化。新增报告无行尾空白，`git diff --check` 通过。未使用 Mermaid 渲染器验证布局，也未运行参考项目业务测试。

## 仓库现状

- 开始时主仓库分支 `master`，HEAD `c9afb8c`，工作树干净；保留原分支。
- 已有 Python 3.12+ / uv_build 项目元数据、空 README、`src/pyknora/__init__.py` 的 Hello World 入口；依赖列表为空，尚无业务 API、工作流或测试套件。
- 已有 PRD、Codex 开发指南、源码架构调研报告；全部保留，不替换已有结论。
- `references/WeKnora` 已存在官方 origin 和完整 Git 元数据，工作树干净。基线 `main` / `662abaf9f944e37689d04c42160034b2cdec6829`；详见 [源码基线](research/00_Source_Baseline.md)。

## 初始化记录（已完成，提交 a6220dd）

- 阅读 PRD、开发指南和已有研究；检查已有文件、主仓库和参考仓库状态。
- 新增根 `AGENTS.md`，约束职责、安全、幂等/版本、测试与每轮交付。
- 建立 `docs/adr/`、`docs/research/`、`docs/tdd/`、`docs/poc/`，用 README 保证目录可被 Git 跟踪并说明用途。
- 记录参考仓库 commit/branch/tree、版本区别、许可证及第三方通知。
- 补充已有 gitlink 缺失的 `.gitmodules`，保留源码 SHA 与忽略规则；未重新克隆、fetch 或更改原源码。

## 初始化验证记录

- `git status --short`、`git ls-files --stage references/WeKnora` 及参考仓库 `rev-parse` / `branch` / `remote` / `describe`：确认初始工作树干净、来源与指针一致。
- 文档路径、相对链接、Markdown fence 成对、规范必需项与基线一致性：PowerShell 断言检查通过（8 个新增文件），原有业务与文档文件无改动，参考源码工作树仍干净。
- `git diff --cached --check`：提交前检查空白错误。
- 单元/集成/性能/安全测试未运行：本轮只改文档与 Git 元数据，尚无业务实现或测试套件。未验证真实 DocReader、BrowserSkill、LangGraph 或 Celery 集成。
- 本轮变更以本地文档初始化提交交付；通过 `git log -1 --oneline` 查询提交，避免在同一提交内自引用 SHA。

## 待办（不在本轮实施）

1. 已交付固定 commit 的三份静态审计报告；后续补充运行与故障注入证据，并继续核验未覆盖分支及外部 BrowserSkill 实现。
2. 已交付 TDD 与 ADR 提议；后续完成评审、PoC 验证及 ADR 接受流程，不把提议当已通过决策。
3. 九项 Phase 0 PoC、32任务Backlog、验收门槛和环境规划已交付；后续按P0-T01及依赖DAG逐项执行，优先验证Checkpoint/审批恢复、DocReader与可靠发布。
4. 按任务依赖逐项实现并记录真实测试证据，之后再进入 P0 业务闭环。

## 阻塞、风险与待澄清

- **本轮无硬阻塞**：源码已在本地，未发生网络获取失败；远端最新状态和全新 submodule 下载未测试。离线恢复方式见源码基线文档。
- **基线差异**：PRD 以 v0.8.3 作参考，实际 clone 为其后 202 个提交；现有报告的 tree SHA 不是本轮 commit，不可默认沿用全部结论。
- **需求冲突历史及当前变化**：初读 PRD §6 F-14 为 P0、其他章节为 P1；本轮期间检测到外部未提交编辑已将 §6 改成 P1，保留该工作并按 P1 设计，正式基线待确认。
- **许可证**：MIT 不覆盖全部第三方；DocReader/OCR、BrowserSkill 协议及分发约束未完成审计。PyKnora 自身许可待确定。
- **集成与容量**：数据库、Redis/Celery、对象存储、模型、浏览器设备、观测栈可用性尚未验证；性能数值与安全验收仍是目标，不是已达成结果。

## 计划记录（2026-10-27，未执行任务）

- 新增 `docs/plans/2026-10-27-phase0-execution-plan.md`：并行判定三层规则（依赖/写入权/资源）、共享产物写入权互斥表、波次表与 mermaid 图、提示词 06 实例化执行规程、证据与停边界、风险回滚、假设 A1–A6；2026-10-27 追加 §4.5 波次编排（每波次一条分支、子 agent 数 = 可并行任务数并强制声明、波次末放行门禁询问是否提交）。
- 波次分支命名：规范 `feature/{波次名}:{任务名列表}` 因 Git 禁止 `:` 无法落库，改用 `feature/w<N>-<task>-<task>`；11 条分支名已全部通过 `git check-ref-format --branch` 校验。
- 本轮**未执行任何 Backlog 任务**：未创建 `experiments/`、`tests/`、`docs/poc/evidence/`，未解析依赖、未拉取镜像、未运行测试或容器，未创建分支、未暂存、未提交。
- 计划自检（只读脚本 + 原生 PowerShell）：Markdown 围栏成对（8 行 / 4 块）且含 1 个 mermaid 块、19 条相对链接全部可达、31 项任务全覆盖、计划依赖字段与 Backlog 32 卡/72 边逐项一致、序列对 72 边拓扑校验违反 0 条、11 条波次分支名合法、未发现疑似秘密。
- 桌面执行通道变更：`pwsh` 一次性调用在本机持续失败（`0xC0000142`），改用原生持久 PowerShell 会话（PowerShell 7.6.0）；实测 git=`dev@59d53ac`、uv 0.12.24、docker 29.7.2、compose v5.4.0，wsl 通道停用。
- 已知文档差异（2026-10-27 已澄清）：早期记录「`docs/poc/04` Mermaid 少 `P0-T01 --> P0-T02` 一条边」**是误报**——第 48 行写成 `T01["T01 契约闭合"] --> T02["T02 环境"]`，带节点标签使朴素正则漏匹配。P0-T22 用结构化解析复核为 **72/72 边一致**，并加回归测试守护；`04` 文档本身无需修改。
- 计划已标注**执行前沿**：唯一可开工位置为 W1（T02/T09/T14，分支 `feature/w1-t02-t09-t14`），其余 28 项在 §1.1、§3.3、§4.1、§5、§12 中统一标为「未解锁」。用户已于 2026-10-27 确认 A 方案（授权初始化 `references/WeKnora` 子模块、N=3）与连字符分支名格式；但用户要求继续完善计划，**执行尚未授权**，未建分支、未下载子模块、未派发子 agent。

## 本轮：W1 波次执行（2026-10-27，分支 `feature/w1-t02-t09-t14`）

- 分支：`git switch -c feature/w1-t02-t09-t14 dev`（HEAD `59d53ac9897f8b0e5d3eeb096332737f7ed6e1ce`）；未推送、未合并。
- 素材前置：`git submodule update --init references/WeKnora` 于 2026-10-27T17:00:11 成功，钉 `662abaf9f944e37689d04c42160034b2cdec6829`（`git submodule status` 无 `-` 前缀，`git -C references/WeKnora rev-parse HEAD` 一致）。
- **P0-T02 PASS**：`experiments/phase0/` 为独立 uv 项目（Python 3.12.13、`requires-python >=3.12,<3.13`、`uv.lock` 16 包 sha256 `bf0b2dc0…`）；compose core 固定 `postgres:17-alpine@sha256:266d3c25…`（`127.0.0.1:5433`）+ `redis:7.4-alpine@sha256:6ba19c50…`（`127.0.0.1:6380`），两容器 healthy；E1 `PostgreSQL 17.11` + DDL/DML 往返 0.303 s，E2 `PING=true` + `SET/GET` + server `7.4.12` 0.141 s；`pytest` 12 passed、`ruff` All checks passed、`pip-audit` 零已知漏洞；必填变量缺失改为 `:?` 显式失败并有负向用例。证据 `docs/poc/evidence/environment.md`（§13 为门禁硬化复跑）。
- **P0-T14 PASS（限域）**：`docs/poc/evidence/browserskill_protocol_manifest.md`（535 行）。31 个暴露动作全部映射到 `internal/browserskill/manager.go:816-848` 白名单与 `manager.go:986` 的 `"tool."+method`；daemon（Rust `bsk`）与扩展不在参考库内，版本 `0.3.1`、上游 commit `da6bf4eed2dd7256567e152df8c903c87f6598c3`；参考实现内 SSRF/域名/速率**无任何代码**，网络策略必须网关前置。自评 PASS 9 / PARTIAL 3（产物 hash、capabilities 语义、真机 OS 矩阵）/ NOT_RUN 2。
- **P0-T09 PARTIAL**：`docs/poc/evidence/docreader_manifest.md`（315 行）+ `docreader_fixture_plan.md`（190 行）。proto 锚点 `docreader/proto/docreader.proto`（sha256 `33d4135c…`，28.2 KB，v1.0.0）的 4 个 RPC 逐个列出请求/响应/错误/限制；解析失败走 `ParseResponse.error` 字段而 gRPC status 仍 OK（直接影响 T10 断言）；默认部署**无 token 鉴权、无 TLS**；镜像 digest、22 个非 OCR 依赖许可、apt 包许可、OCR 权重清单均 `UNKNOWN`，须在 T10 前补 `docreader_sbom.md`。
- 依赖口径变更：PG 驱动由 Lead 底稿 `asyncpg` 改为 `psycopg[binary]>=3.1`（TDD01 §7），并修 CVE 升级 `pytest 9.1.1` / `pytest-asyncio 1.4.1`；现装 `sqlalchemy 2.1.6`、`redis 8.1.0`、`pyyaml 6.0.3`、`ruff 0.12.12`。
- 根 `.gitignore` 追加 `.env`、`.env.*` + `!.env.example`、`.pytest_cache/`、`.ruff_cache/`、`pytest-of-*/`（既防止真实 `.env` 与 pytest 临时目录误入库，又保留 `.env.example` 可提交）。
- 门禁检查（`%TEMP%\w1_gate2.log`，17:40:34 复跑）：`pytest -c experiments/phase0/pyproject.toml tests/phase0 -q` 12 passed exit 0、`ruff` exit 0、仓库自有 12 文件秘密扫描 `no_high_risk_secret_hits`、compose core 两容器 `Up (healthy)`、已跟踪差异仅 `.gitignore` 与 `.env.example` 相关、**暂存区为空**。
- **已提交（用户 17:42 明确授权）**：`08c3c67` 于 `feature/w1-t02-t09-t14`，13 文件 / +2823 −1；随后 `6c4f5b6`（chore: 作者元数据）。首提交 `e78a498` 因 `.gitignore` 的 `.env.*` 误伤 `.env.example` 而漏文件，加 `!.env.example` 例外后 `--amend` 修正。
- **已推送（2026-10-27 20:39 补记）**：`origin/feature/w1-t02-t09-t14` 顶点 `6c4f5b6`；**未合并 `dev`**。
- 未提交：`.idea/`、`CLAUDE.md`、`docs/teacher/`、`experiments/phase0/.venv/`；`references/WeKnora` gitlink 保持 `662abaf9f944e37689d04c42160034b2cdec6829`。
- 波次收官证据：`docs/poc/evidence/wave-w1-record.md`（声明块、素材阻塞与解除、§3 门禁、§4 提交结果）。**W2 已启动**（见下节）。

## 本轮：W2 波次执行（2026-10-27，分支 `feature/w2-t03-t04`）

- 前置收尾：`chore: update project author metadata` → `6c4f5b6`（`pyproject.toml` 作者改为 ZhangXianDa）；`feature/w1-t02-t09-t14` 以 `--no-ff` 合入 `dev` → `6ff7b72`；`dev`、`master`、`feature/w1-*`、`feature/w2-*` 已推送到 GitHub `origin`（该仓库原为零分支空仓）。
- 分支：`feature/w2-t03-t04` 从 `dev@6ff7b72` 拉出；波次声明写入 `docs/poc/evidence/wave-w2-record.md`（`N=2`、两任务写权互斥、资源槽 `p0_t03`/`p0_t04` + Redis DB 1/2 + 端口 5500）。
- **P0-T03 PASS**：`experiments/phase0/contracts.py`（800 行）+ `scope.py`（647 行）+ `db/migrations/001_scope_core.sql`（485 行）+ `tests/phase0/test_scope.py`（16 例，1144 行）。RLS 默认拒绝、`SET LOCAL` 不随连接池泄漏、复合 FK 跨工作区 100 轮全部拒绝、成员撤销即时生效、无 Run 作业重读当前 Principal（C-06）。证据 `docs/poc/evidence/poc01-scope.md`（389 行）。C-07「来源审阅/身份域」按约束记 **BLOCKED**、C-10 `asset_bindings` 只留位 **NOT_RUN**。
- **P0-T04 PASS（限域）**：`experiments/phase0/control_site/`（7 文件 1820 行，纯标准库 `http.server`）+ `faults.py`（317 行 `FaultBarrier`，无固定 sleep）+ `fixtures/manifest.json`（33 向量：21 正 / 12 负，7 条真实响应 sha256）+ `tests/phase0/conftest.py`(206) + `test_control_site.py`(376) + `test_faults.py`(329)。证据 `docs/poc/evidence/control-site.md`（405 行）。F1–F18 场景本身属 T05–T30 **NOT_RUN**；F15 私网 IPv6/WebSocket/DNS rebinding、F16 提示注入页为**能力缺口 PARTIAL**。
- 修掉两个真缺陷（T04 自查发现）：`ControlStore.reset()` 持非可重入锁再调 `snapshot()` 的自死锁；`server_close()` join 死线程导致的无界等待（改 `wait_for_completed` + `server_close(timeout)`）。
- 门禁（`%TEMP%\w2_gate.log`，19:22:47→19:24:05）：`pytest -c experiments/phase0/pyproject.toml tests/phase0` **102 passed in 60.45s** exit 0；`ruff` All checks passed；`git diff --numstat` 合计 `added=0 deleted=0 files=0`（开工时 51 个 ` M` 系 CRLF 噪声，现已归零）；core 两容器仍 healthy；5500/5501 `listeners=0`；仓库根无残留临时目录；11 份新交付 sha256 已记入波次记录 §3.2；秘密扫描 0 命中。
- **已提交并推送（用户「提交 3 笔」+「推送 W2」）**：`94defe2`（T03）、`d36629a`（T04）、`9b88cc7`（docs）、`f86765e`（style，EOF 空行）→ `origin/feature/w2-t03-t04` 顶点 **`f86765e`**；**未合并 `dev`**。
- 波次记录：`docs/poc/evidence/wave-w2-record.md` §4 已回填真实 SHA 与推送结果；**W3 已启动**（见下节）。

## 本轮：W3 波次执行（2026-10-27，分支 `feature/w3-t05-t07-t10-t15-t19`）

- 分支基线：从 **W2 顶点 `f86765e`** 拉出（用户选择 W2 只推分支、不合 `dev`；T05/T07/T15/T19 需要 T03/T04 产物，故不从 `dev@6ff7b72` 起）。波次声明 `docs/poc/evidence/wave-w3-record.md`（`N=5`、五份写权互斥、`serial_tasks: []`、资源槽 `p0_t05`/`p0_t07` + Redis DB4/DB3 + 端口 5600）。团队名额上限 8，第 4/5 个 `spawn` 返回 `Team member limit 8 reached`，改用 `send_message` 复用 inactive 成员承接 T15/T19，N 未降级（记录于波次记录 §1.1.1）。
- **P0-T05 PARTIAL**：`experiments/phase0/checkpoint_probe.py`（1674 行）+ `tests/phase0/test_checkpoint_owner.py`（262 行，11 例）+ `docs/poc/evidence/poc01-owner.md`（195 行）。fenced 路径 `stale_writes_accepted=0` vs 无 fence 对照 `50`；owner binding `100/100`、重复绑定 `300/300` 全拒；churn 50 轮 `accepted=51 / rejected=50 / final_seq=51`、`restore_mismatches=[]`。**关键发现**：原生 `PostgresSaver` 在 advisory lock 连接被 kill 后 `put` 仍成功 → 原生路径**无法 fence 独立 saver 连接**，结论「**单活 runner，不声称安全横扩**」；`pg_terminate_backend` 异步，接管判据必须是「我持有锁」而不是快照为空。未修改任何 ADR/契约状态。
- **P0-T07 PASS（限域）**：`outbox_probe.py`（1267 行）+ `db/migrations/003_jobs_outbox.sql`（295 行）+ `test_outbox_transaction.py`（968 行，20 例）。DB 层强制：`DEFERRABLE INITIALLY DEFERRED` 约束触发器在 **COMMIT 时**校验「Job 必有 Outbox」；状态触发器保证身份列不可变、epoch 不回退、`row_version` 严格 +1、转移矩阵、claim 必 epoch+1；审计表只追加。实测：4 worker `SKIP LOCKED` 认领并集无重复、事务内 publish 被观察者捕获（`PUBLISH_NOT_AFTER_COMMIT`）、stale epoch `updated=False stale=True`、50 轮 commit/rollback + 断 Redis 后 25 条恢复为 `dispatched`、dispatcher 死在 publish 后可回收重发。**偏离待裁**：迁移落 schema `p0_t07` 而非 001 留位的 `p0_t03` 增量（并行 schema 槽与 001 teardown 冲突），生产合并时须换前缀并补 policy/GRANT。
- **P0-T10 PASS（限域）**：`experiments/phase0/docreader_probe/`（proto 副本 + 生成代码 + 语义 stub server + client + `wire_evidence.json`）+ `tests/phase0/test_docreader_wire.py`（894 行，17 例）。工具链 `grpcio 1.85.0 / grpcio-tools 1.85.0 / protobuf 7.36.2`（`--with` 临时注入，未改实验 `pyproject.toml`）。实测：解析失败在 **OK** 状态返回 `ParseResponse.error`；256KiB 上限下 unary `RESOURCE_EXHAUSTED` 而 ReadStream 8 帧 1.2MB 全通；发送期异常实测 `UNKNOWN`；取消/超时服务端可观察且不清空后续调用。**NOT_RUN**：真实 DocReader 服务与镜像 digest/许可（T09 遗留 UNKNOWN）。
- **P0-T15 PASS（限域）**：`browser_contracts.py`（1622 行）+ `test_browser_contracts.py`（1038 行，117 例）。单入口 `execute(session, *, action, params, expected_epoch, timeout_ms, approval, tool_call_id)`，过闸顺序 scope→动作→参数白名单→密钥/JS→默认→必填→类型→审批→网络前置→epoch→传输；`BrowserAction` 31 动作；`negotiate_capabilities` 落 U1–U9 九位；`AUTOMATIC_RETRY=False`；`BrowserSession` 只能由服务端 `_trust` 构造。自查修掉 4 个缺陷（`wait_ms` evidence 空串、`_is_secret_key` 不分词形、凭据样本 39<40 字符、`press.modifiers` 负例选错字段）。限域：`tested_protocol_versions` 默认空集、能力位为模块常量未与 daemon 对齐（T17）。
- **P0-T19 PASS（限域）**：`model_gateway.py`（926 行）+ `tool_registry.py`（734 行）+ `test_gateway_registry.py`（992 行，98 例）。`ProviderConfig` 全显式；退避 `min(base*2**i, cap)` 且 `Retry-After` 封顶；`redact_secrets`/`Secret.__repr__` 防泄漏；`compute_intent_key` 按 C-04 八字段并 `verify()`；`assert_executable` 保留审批门。**NOT_RUN**：真实厂商 API、`stream()`（实现为同步 `call()`，T21 前须先定 port）。
- 门禁（`%TEMP%\w3_gate.log` / `w3_gate2.log` / `w3_close.log` / `envclean.log`，20:05–20:12）：全量 ruff 首跑 1 个 `I001`（`outbox_probe.py:62`）→ `--fix` 后 `All checks passed!`；**逐文件 pytest 合计 365 passed / 0 failed**（environment 12、control_site 49、faults 25、scope 16、checkpoint_owner 11、outbox 20、docreader_wire 17、browser_contracts 117、gateway 98）；5500/5501/5600 `listeners=0`；`git diff --numstat` = `added=0 deleted=0 files=0`（无 tracked 改动，18 条 `??`）；秘密扫描 0 命中；12 份交付 sha256 见波次记录 §3.4。
- **已知缺口（20:29 已解除两条，用户当轮授权测试依赖准入）**：① 全量单进程 pytest 原判 NOT_RUN（20:05 无输出、CPU 冻结）→ **已定位并复跑通过**：根因是 `--with` overlay 与模块级 side effect 交互，依赖进 venv 后 `tests=365 failures=0 errors=0 skipped=0 time=128.327`（junit，`experiments/phase0/.pytest_cache/junit.xml`，20:29）；② 测试依赖已准入 `experiments/phase0/pyproject.toml` dev 组：`grpcio>=1.85.0`、`grpcio-tools>=1.85.0`、`langgraph-checkpoint-postgres>=3.2.0`、`pytest-timeout>=2.4.0`（+4 行，`uv.lock` 同步重锁），`test_environment.py` 12 例在新 venv 下仍全绿；③ 仍开放：`--with` 不得用于跑全套（纪律写入 `wave-w3-record.md` §1.2），`p0_t07` 迁移前缀偏离待裁，T05 只能单活 runner。
- **已提交并推送（用户「提交 W3 两笔」+「推送并开启 W4」）**：`ed669af`（feat W3，24 文件 / +14336）、`32f0035`（docs: W3 门禁 + 全量复跑）→ `origin/feature/w3-t05-t07-t10-t15-t19` 顶点 **`32f0035`**；**未合并 `dev`**。
- 波次记录：`docs/poc/evidence/wave-w3-record.md` §4 已回填真实 SHA 与推送结果；**W4 已启动**（见下节）。

## 本轮：W4 波次执行（2026-10-27，分支 `feature/w4-t06-t08-t11-t16-t20-t22`）

- 分支基线：从 **W3 顶点 `32f0035`** 拉出（`dev` 是其祖先，merge-base 校验通过）；声明见 `docs/poc/evidence/wave-w4-record.md`（`N=6`、六份写权互斥、资源槽 `p0_t06`/`p0_t08`/`p0_t20` + Redis 6/5 + 端口 5610）。六个任务复用 inactive 成员执行，未新增名额。
- **P0-T06 PASS（限域）**：`approval_probe.py`(2073) + `tool_ledger.py`(551) + `002_approvals.sql`(455) + `test_approval_effects.py`(578)。20 passed；50 轮重放 `downstream_applied=1 / replayed=49`；过期/篡改/跨 workspace/撤销 4/4 拒绝且零下游调用；三崩溃窗口各 50 轮真实 `os._exit`，`violations=[]`（`after_executing_commit` 记 50 条 `unknown_outcome`，不伪称 0 次执行）；账本 append-only 由触发器强制。限域：`002` 落独立 schema `p0_t06`、未压测真并发执行者、`TRUNCATE` 边界待 `REVOKE`。
- **P0-T08 PASS（限域）**：`job_probe.py`(1495) + `job_reconciler.py`(382) + `test_delivery_recovery.py`(903)。13 passed（48s）；100 Job×10 消息 → `applied=100 / duplicate=900`；旧 generation `STALE_EXECUTION_EPOCH`；退避 1/2/4s → `dead_letter`；对账 `repair_count=1` 且并发不双修；断 Redis 显式 `BROKER_UNAVAILABLE` 且 PEL 不丢；五态与 `003` 枚举逐字核对。限域：DDL 写在代码里（写权不能建 004）、真实 Celery `NOT_RUN`、未实现 `XAUTOCLAIM`。
- **P0-T11 PASS（限域）**：`simple_reader.py`(859) + `locator_map.py`(311) + `parsing_assets.py`(925) + `test_parser_golden.py`(570)。70 passed；50/50 golden（每格式 10/10）、offset-free 定位 50/50（page/paragraph/heading/table_cell/code_block）、单字符插入 0 破损、19 例空/损坏/无可见文本显式失败、与 T10 `ReadResponse.error` 跨引用已测。限域：素材全为合成样本，真实样本与真实镜像 `NOT_RUN`。
- **P0-T16 PASS（限域）**：`network_policy/`(8 文件 1182 行) + `playwright_probe.py`(354) + `test_playwright_security.py`(866)。109 passed；26 条 SSRF 向量、每跳重定向/协议/端口/DNS rebinding/体量/时长/重定向次数全覆盖、`navigate_back/forward/reload` 也过策略且 transport 零调用、限流并发可观测、5610 端口已释放。限域：**Playwright 未安装 → 真浏览器 `NOT_RUN`**（显式 `PlaywrightUnavailable`，未 skip）；`allow_loopback_endpoints` 测试逃生门需 T29 启动期断言为空。
- **P0-T20 PASS（限域）**：`context_budget.py`(946) + `budget_ledger.py`(1288) + `005_budget.sql`(316) + `test_budget_context.py`(1100)。27 passed；四区段裁剪确定性（30 次同指纹）；无价格时 `settle_unavailable` 按 maximum 记 `provisional=true / zeroed=false`；100 轮双竞争 `winners=100 / rejected=100 / overdraft=0` 且有 DB CHECK 兜底；崩溃后从 PG 恢复。限域：`005` 落独立 schema、tokenizer 为启发式、真实价格 `NOT_RUN`、**「每 retry 费用可归集」需 T21 显式选择重试与预留的接口**。
- **P0-T22 PASS（限域）**：`dag_contracts.py`(799) + `planner_probe.py`(461) + `test_dag_validation.py`(501)。51 passed；32 节点/72 边/66 点、最长链 12 节点 11 跳 24 点、核心链 23 点与 `poc/04` 一致；28 个非法计划全拒且首错定位到节点/边；RAG 路径 planner 调用 0。**并澄清一处长期误报**：`poc/04` 的 Mermaid 并非缺 `T01-->T02`，是带节点标签导致旧正则漏解析，实际 72/72 一致（已加回归测试）。限域：真实模型 plan `NOT_RUN`；默认上限 32/64/8 与样例图 72 边口径冲突，待评审定 profile。
- 门禁（`%TEMP%\w4_gate_out.txt` + `w4_close_out.txt`，21:29–21:36）：全量 ruff 首跑 1 个 `I001`（`job_reconciler.py:20`）→ 门禁 `--fix` 后全绿；**全量单进程 pytest 655 passed / 0 failed / 0 skipped in 233.77s**（365 既有 + 290 本轮）；5500/5501/5600/5610 无监听；仓库根无临时目录；秘密扫描 0 命中；Windows git 视角仅 `docs/IMPLEMENTATION_STATUS.md` 与计划被改，其余全部新增未跟踪。
- 本轮新增：代码/测试 **15 733 行** + `network_policy/` **1 182 行**；19 份交付 sha256 记入波次记录 §3.3。
- **已提交并推送**（用户 2026-10-27 21:45 选择「提交 + 推送 + 开 W5」）：`0a1ed5d` feat、`a2a7dfc` docs，均推到 `origin/feature/w4-t06-t08-t11-t16-t20-t22`；未合并 `dev`。提交前 `git diff --cached --check` 拦下 `network_policy/engine.py`、`flow.py` 的 EOF 空行并修净。
- 已知缺陷（W5 待修）：`docreader_probe/wire_evidence.json` 会被 T10 的测试重写（gate 时用 `git stash` 暂存，未丢弃），应改为写 `.pytest_cache/`。
- 下一前沿：**W5 = T12/T17/T21/T23**（T12 是解析线与作业线收敛点），已启动。

## 本轮：W5 波次执行（2026-10-27，分支 `feature/w5-t12-t17-t21-t23`）

- 分支基线：从 **W4 顶点 `a2a7dfc`** 拉出。声明见 `docs/poc/evidence/wave-w5-record.md`：**`N=3`**（T12/T21/T23），**T17 记 BLOCKED**（无 pinned upstream 源码/daemon/extension 产物、无真实设备；构建需 cargo+pnpm 且脚本无 windows 目标），按 §4.5.2 从并行集移出、N 由 4 下调为 3。执行通道自 21:56 起改为**原生 `pwsh`**（`0xC0000142` 已复测消失），`wsl` 仅回退。
- **P0-T12 PASS（限域）**：`index_staging.py`(1106) + `004_version_index.sql`(413) + `test_index_staging.py`(642)。16 passed；staging 重放 `inserted=0/skipped=3`；旧 attempt `STALE_ATTEMPT`（API + DB 触发器双挡）；ready 后 `search_visible_chunks()==[]`、模拟 T13 激活才可见；manifest 缺项 `MANIFEST_INCOMPLETE`；`p0_t12` 零残留。限域：对象存储 `NOT_RUN`（假存储明标 `in-memory-fake`）、**pgvector 缺失 → 向量 ANN/召回 `NOT_RUN`**（`float8[] + embedding_dim`）。
- **P0-T21 PASS（限域）**：`harness_graph.py`(845) + `safe_trace.py`(259) + 测试(476) + `harness-integration.md`(139)。14 passed；6 节点图复用 T05/T06/T08/T19/T20 公开 API；checkpoint 全删后 `recover_from_facts()` 得 `receipts==1`、节点返回 `replayed`、`applied_calls` 仍 1；取消/超时零副作用；canary 在 trace/State/checkpoint 三处为空。**T20 冲突已选定方案①**：`ProviderConfig(max_retries=0)` + Harness 逐 attempt `reserve_model_call`（`purpose_key=t21:{step}:attempt:{n}`），代价写入证据。限域：**`langgraph` 仅 namespace 包**（`graph`/`types` 均 `ModuleNotFoundError`）→ 原生 `StateGraph`/`interrupt` 未验，验收第 5 项 PARTIAL。
- **P0-T23 PASS（限域）**：`task_scheduler.py`(1613) + `task_executor.py`(225) + `006_tasks.sql`(230) + 测试(1080)。28 passed；未满足依赖执行 0、required 失败递归 blocked、optional 不阻断；双 scheduler 30 轮 120 条 claim 无重复；旧 epoch 完成/引用均 `STALE_*`；事实先落库、checkpoint 失败只留 `state_ref_pending` 且重入不重跑；真实 T08 Job port 集成通过；迁移重复 apply 显式失败；静态守卫证明无跨域写。限域：单活前提、checkpoint 用端口替身、对账拉取式、30 轮非 100 轮。
- **P0-T17 原记 BLOCKED**：无 upstream 产物/真设备（解除条件写在波次记录 §1）；**已于 2026-10-28 由 `fix/w5-t17` 解除**，详见本文件「本轮：T17 真配对修复」与 `docs/poc/evidence/poc05-browserskill-live.md`。
- 门禁（pwsh，22:12–22:18）：ruff 首跑 1 个 `I001`（`index_staging.py:44`）→ `--fix` 后全绿；首次全量 **713 passed / 0 failed / 0 skipped in 319.70 s**（655 既有 + 58 本轮）；5610/5500/5600 无监听；无临时目录；秘密扫描 0 命中。
- **缺口修复（用户选择「先修缺口再提交」）**：
  - `langgraph` 仅 namespace → `uv add --dev langgraph` 装 **1.2.18**；T21 增加原生主路径 `NativeGraphRun`（`StateGraph` 6 节点 + `interrupt()` + `Command(resume=…)` + 原生 `PostgresSaver`），**19 passed**（14 既有 + 5 原生）；跨连接恢复 exactly-once、框架 `setup()` 建 checkpoint 表、代码零直写。证据 `harness-integration.md` 更新。
  - `intent_key` 两套算法 → `tool_registry.compute_intent_key` 改为委托 `tool_ledger.intent_key`，`SCHEMA_VERSION` 统一为 int；新增 `test_cross_component_keys.py`（20 例），**98 + 20 = 118 passed**；旧算法分叉已复现并锁定。
  - 无 `pgvector` → 独立探针 PASS（带门禁）：`pgvector/pgvector:pg17@sha256:ac08538c…`、PG 17.11 + pgvector 0.8.7，`hnsw`/`ivfflat`、三种距离算子、2000 维硬上限、迁移路径全部实测；**未动 core compose**（换镜像须单独立项，否则作废 T02 证据）。证据 `pgvector-probe.md`（333 行）。
  - 附带修复：`test_docreader_wire.py` 的证据写入改到 `.pytest_cache/`，不再回写 tracked 的 `docreader_probe/wire_evidence.json`。
- **缺口修复后复跑**：ruff `All checks passed!`；**738 passed / 0 failed / 0 skipped in 262.78 s**（junit `tests=738`）。
- 本轮新增代码/测试/迁移约 **8 000+ 行**；关键交付 sha256 记入波次记录 §3.3 与 §3.6。
- **已提交并推送（用户「先修缺口再提交」→ 22:56 提交）**：`f0cf3e1`（feat W5 + 缺口修复）、`8cfd3bc`（docs: W5 门禁/缺口/pgvector 探针）→ `origin/feature/w5-t12-t17-t21-t23` 顶点 **`8cfd3bc`**；**未合并 `dev`**。
- **波次状态回填（2026-10-28）**：W1–W5 的 `wave-*-record.md` §4 与本节各波次小节的「未提交/待询问」旧说法已统一回填真实 SHA 与推送结果；W1 的 §3 门禁复选框补勾。
- 下一前沿：**W7 = T25/T27/T30**（三份交付 `retrieval_probe.py` / `candidate_review.py` + `007_candidates.sql` / `restore_probe/` 及对应测试仍为工作树未跟踪文件，**尚未提交**；`feature/w7-t25-t27-t30` 目前只含波次声明）。T18 仍 BLOCKED（依赖 T17 真设备）；T17 已在 `fix/w5-t17` 收口为 **PASS（限域）** 并合入 `dev`。

## 本轮：W6 波次执行（P0-T13 / P0-T24）

- 分支 `feature/w6-t13-t24`，基线 **`dev@cc4a475`**（W1–W5 全部合入 dev 后的首个波次）；声明见 `docs/poc/evidence/wave-w6-record.md`：`N=2`，**T18 移出并行集**（依赖 T17 真设备/真协议，T17 BLOCKED）。
- 迁移编号裁定：`001`–`006` 已占、`007` 留 T27 → **`008` 归 T13**（`008_publication.sql`）；T24 不新建编号迁移，`p0_t24` 内联 DDL。
- **P0-T13 PASS（限域）**：`experiments/phase0/index_publication.py`（1130 行）+ `db/migrations/008_publication.sql`（271）+ `tests/phase0/test_publication_faults.py`（1062，18 例）+ `docs/poc/evidence/poc03-publication.md`（208）。单事务完成「旧 active→retired / ready→active / documents.active_version CAS」三件事并与发布事实、发布内 Job、审计同提交；锁顺序 `documents FOR UPDATE → 版本行`，与 T12 staging 共用行锁；幂等键 `(workspace, document, idempotency_key)` 唯一，复放不写指针不写 Job；100 轮×100 读 = **10 000 次采样零混版本、零跨空间命中**，每次切换 `row_version` 恰 +1；8 类故障注入后可见版本不变；旧 attempt `STALE_ATTEMPT`、旧版本晚到 `STALE_GENERATION`（沿用 T12 口径）；回滚 5 轮（publications=11、jobs=11）。**NOT_RUN**：删除/取消/重建、对象丢失注入（假对象存储）、真实 MinIO/pgvector/Redis 运行；**PARTIAL**：真实重试器（归 T08/T25）、DoD 对象引用未实现。
- **P0-T24 PASS（限域）**：`experiments/phase0/evaluator_replanner.py`（1622）+ `tests/phase0/test_replanning.py`（665，22 例）+ `docs/poc/evidence/poc02-replanning.md`（130）。评估只认事实与工具回执（无回执的自述 → `partial` + `RECEIPT_MISSING`）；最多 2 次重规划，第 3 次 `limit_exceeded` + 终态 `failed`；只替换未完成节点、已完成节点按指纹复用且已确认副作用重做 0；环在登记前被 T22 `validate_graph` 拒绝；并发同请求单胜者（`created True→False`）、过期 revision 抛 `REPLAN_REVISION_CONFLICT`；超预算 `blocked` 且 `release_calls==0`。**PARTIAL**：T20 真实 `BudgetLedger` 未接线（`BudgetPort` + `StaticBudget`）；**NOT_RUN**：kill 打断、旧 revision 晚到回放、HTTP 接口层。
- 门禁实跑（日志 `%TEMP%\w6_gate.log` / `w6_gate_junit.log`，junit `experiments/phase0/.pytest_cache/w6_gate.xml`）：ruff 首跑命中 1 个 `I001`（`index_publication.py:45`，T13 用显式 `--config` 自测与根级扫描口径不同）→ `--fix` 后 `All checks passed!`；修复后 T13 复验 `18 passed`；T24 `22 passed`；**全量 tests=778 / failures=0 / errors=0 / skipped=0 / time=209.289s**；秘密扫描 0 命中；5500/5501/5600/5610 无监听；仓库根无临时目录；core 容器 healthy。
- 已知契约偏离（写入 `poc03-publication.md` §7，未改 ADR/`poc/00–07`）：schema 落独立 `p0_t13` + 只读 `p0_t12`；跨 schema 无 FK；回滚以新版本行重放（消耗一次 generation）；发布内 Job 仅最小事实；无 DB 级「每文档单 active」唯一约束（靠行锁+状态机+提交前自检）；被拒尝试审计随事务回滚。
- 状态：**已过门禁，未提交**（等用户裁决）；`dev` 未变动。

## 本轮：T17 真配对修复（2026-10-28，独立修复分支 `fix/w5-t17`）

- 分支：`fix/w5-t17`，基线 `dev@cc4a475`；**不并入 W6/W7 的线性链**（用户明确「不用移到 W7」）。
- 交付：`experiments/phase0/browserskill_probe/`（`cli.py` / `adapter.py` / `live_run.py` / `__init__.py`）、
  `tests/phase0/test_bsk_live.py`（35 例 = 34 单测 + 1 真机门控用例）、
  `docs/poc/evidence/poc05-browserskill-live.md`、`docs/poc/evidence/p0-t17-live-run.json`。
- 真机证据：daemon `0.3.1` / protocol `1.3`；Chrome 147 + 扩展 `0.3.1`（unpacked）；
  `session ivff` → `tab 270464479` → `snapshot` / `evaluate` / `screenshot`（56,940 B，`sha256=BE95C974…`）/
  `console` / `tab close` / `session stop` 全链路走通，未使用 mock。
- 门禁实跑（`%TEMP%\fix_t17_gate2.log`）：`ruff check experiments/phase0 tests/phase0` → **All checks passed!**（exit 0）；
  `pytest -c experiments/phase0/pyproject.toml tests/phase0`（排除 worktree 中 W7 未跟踪的三份测试）→
  **774 passed / 1 skipped / 0 failed，355.56s**（junit `experiments/phase0/.pytest_cache/fix_t17.xml`）；
  唯一 skip 为真机用例的 3.12 环境门控，真机结果由 3.13 的 `live_run.py` 提供，不是跳过验证。
- 判定：**P0-T17 PASS（限域）**。限域与 NOT_RUN 清单见证据 §6：动作覆盖 13/31、交互类动作仅单测、
  `owner`/撤销属 T18、网络策略属 T16、多浏览器/跨平台未测。
- 未改动：`dev` 与 W6/W7 分支及其未跟踪产物未触碰；未执行 `git add .`。

## 本轮：W7R 补口波次（P0-T05R / P0-T09R，2026-10-28）

- 波次记录：`docs/poc/evidence/wave-w7r-record.md`；执行计划 `docs/plans/2026-10-28-P0-T05-T09-convergence.md`；N=2 并行、写权不相交；W7（T25/T27/T30）已声明未启动，未被占用。
- 原则（用户 2026-10-28 要求）：**原文零改动**——`poc/00–07`、`evidence/` 既有文件、`tdd/`、`adr/0001–0005` 未改一字，修订一律旁挂。

### P0-T05R（单活正式化 + 进程级证据）→ PASS（限域：单活）

- 缺口：`poc01-owner.md` §6.1-E 只有连接级证据、缺进程级存活；接管判据用 `pg_locks` 快照会被 `pg_terminate_backend` 异步性误导。
- 补口（纯追加，`git diff --numstat` = **+383/−0**）：
  - `tests/phase0/test_checkpoint_owner.py` 新增 `test_process_alive_after_lock_loss_still_writes_natively`：子进程持 distributor + 原生 `PostgresSaver`，主进程 `pg_terminate_backend` 杀掉锁连接，经 IPC 触发子进程写一次。
  - 实跑 `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_checkpoint_owner.py -q` → **12 passed，exit 0**。
  - 证据 `docs/poc/evidence/poc01_owner_process_level.json`：子进程 PID 33180 存活、锁 backend pid 47430 已释放、原生 `put` 返回 `accepted=true`（负面证据）；替身路径同场景 `STALE_OWNER_EPOCH` 拒绝。
- 判定：**P0-T05 PASS（限域：单活）**。多 writer 仍 FAIL（原生 saver 无租约/fencing），PoC-01「多实例」强制项仍 PARTIAL；G-P0 限域准入建议保留，G-FULL 不可宣称。
- 新 ADR：`docs/adr/0006_Single_Active_Checkpoint_Runner.md`（**提议**）——P0 唯一形态单活 runner、禁止自动接管、多实例横扩不在 P0 范围；关联 ADR-0001 但不改其正文，`docs/adr/README.md` 已登记。

### P0-T09R（镜像盘点与许可闭合）→ PASS（限域）

- 镜像：`wechatopenai/weknora-docreader@sha256:9af668ac0ac857b9dfe8430d0fe77c0e5ad2636dc2b5285cd5500a13d5a35266`；`docker pull` 经 `docker.1panel.live` 于 11:08→11:59 完成（有效 ~0.9 MB/s）；本地 `RepoDigests` 与 registry 侧重算逐字一致；label `revision=3e22822c1` = `git rev-list -n1 v0.8.3`；`1ms.run` 401 / `xuanyuan` 403 / `daocloud` 401 已记入证据。
- 盘点（镜像内）：apt 971 包（744 有 `License:` 字段，227 无）；pip 72 distribution（许可 61 可判定 / 11 unknown）；`*.onnx` 4 个（最大 3,163,737 B 为 magika 内容分类模型，sha256 `fe2d2eb4…`），**OCR 权重 0 个**；Playwright WebKit 273 MB 与 `grpc_health_probe` 11,457,876 B 无许可文本。
- 关键修正：① `v0.8.3` **不含 RapidOCR**，原文 §4.5「权重 UNKNOWN」实为空集，§6 PDF 行「OCR 依赖 PreprocessImage + RapidOCR」对镜像不成立；② §4.3 的 26 条依赖是 HEAD 口径，`v0.8.3` 只有 22 条；③ `THIRD_PARTY_NOTICES` 的 numpy 2.3.4 / onnxruntime 1.23.2 是 darwin/py≥3.11 分支，镜像实际 **2.2.6 / 1.20.1**。
- 可复现性：镜像内 `/app/uv.lock` sha256 `57861b67…` 与 `git show v0.8.3:docreader/uv.lock` 逐字节一致，pyproject 同理。
- 判定：**P0-T09 PASS（限域）**——四类 UNKNOWN 中 digest 与 OCR 权重闭合、pip/apt 许可部分闭合；11 项 pip + 227 项 apt + WebKit/`grpc_health_probe`/magika 训练数据/基础镜像 digest 仍 unknown，已逐项写阻断与替代。**不满足「无未知许可进入发布候选」**，故仍属限域通过。
- 新增差异 D-13~D-22，其中 **D-22**：compose 仍写 `${WEKNORA_VERSION:-latest}` 而非固定 digest，属仓内配置缺口（T10 前须 pin）。
- 对后续输入：T10 可直接引用 digest；**T11 不要按 `v0.8.3` 设计 OCR 用例**，若 OCR 为验收项须先换 tag/自建镜像，否则记 `BLOCKED(镜像能力缺口)`。

### 门禁实跑（2026-10-28）

- `git status` / `git diff --numstat`：仅 T05R 测试文件 +383/−0；其余为新增 9 项 + `docs/adr/README.md`、本文件各 1 处修订。
- `uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0` → **All checks passed!**（exit 0）。
- 秘密扫描（DSN/口令模式）7 份新文件 **0 命中**；6 份新证据 sha256 逐一复算一致（inventory `3c0d993a…`、sbom `5c37e79b…`、amendment `8afa909d…`、ps1 `574c7909…`；T05 三份由 teammate 记录）。
- 全量 `pytest tests/phase0` 仍会无输出挂起（W3 起已知），本轮未重跑；主集合沿用 W6 的 778 passed，只跑受影响单文件（12 passed）。

### 未决与剩余风险

- W7R 产物**未提交**（波次内禁 git）；待用户授权后按 `fix:W7R-T05R-…` / `fix:W7R-T09R-…` 各自分支提交。
- 多实例横扩仍 FAIL/PARTIAL：仅当明确要求多实例时才做 `poc01b`（持锁代理 + lease/fencing），成本约 1.5 点。
- 许可 unknown 项不阻塞 T05/T09 的限域判定，但阻塞「发布候选」门槛；要转全量 PASS 需 PyPI 许可元数据或上游声明，本机直连 PyPI 不通。
- 本轮未跑 W7（T25/T27/T30）；状态文件抬头已修正为「W1–W7 合入 dev」。
- 全量 `pytest` 已于本轮在合入后的 `dev` 上重跑并通过（见下节），自 W3 起记录的「全量无输出挂起」不再复现——当时根因是 `--with` overlay 环境，依赖入 venv 后消失。

## 本轮：W7 交付落库 + 五个分支按序合入 dev（2026-10-28）

- **W7 交付落库**：`feature/w7-t25-t27-t30` 由 `047b7a8` 推进到 `cbf7b7f`——`4ebdf46` feat（15 文件 +9813：`retrieval_probe.py` 1792 行、`candidate_review.py` 2106、`db/migrations/007_candidates.sql` 709、`restore_probe/` 7 文件 2437、三份测试 2053、`retrieval-baseline.json` 399、`restore-runbook.md` 317）、`cbf7b7f` docs（`wave-w7-record.md` §2–§4 回填，含 16 份交付 sha256）。已推送 `origin/feature/w7-t25-t27-t30`。
- **W7 门禁（Lead 独立复跑）**：`ruff` → `All checks passed!`；T25 `12 passed`、T27 `66 passed`、T30 `21 passed`、三文件合计 `99 passed`，均 exit 0。限域口径：T25 的 ANN/召回 **NOT_RUN**（本机 PG 无 pgvector）、T30 的 RPO/RTO **NOT_RUN**（未校准）、T18 仍 BLOCKED（依赖 T17 真设备，T17 已在 `fix/w5-t17` 单独收口）。
- **dev 按序合入（全部 `--no-ff`，保留波次边界，不删改任何分支）**：
  - `c037614` Merge `feature/w6-t13-t24`（W6：T13 原子发布 / T24 评估重规划）
  - `b5d8cce` Merge `feature/w7-t25-t27-t30`（W7 声明）
  - `7d40c7b` Merge `fix/w5-t17`（T17 真配对；唯一冲突 `IMPLEMENTATION_STATUS.md`，解法=两侧「本轮」段并存 + 重写前沿行）
  - `027c5b5` Merge `fix/w7r-t05-t09`（W7R：T05R 单活正式化 / T09R 镜像许可）
  - `63cee71` Merge `feature/w7-t25-t27-t30`（W7 交付，16 文件 +9862）
- **修复满套件偶发失败**：`tests/phase0/test_control_site.py::test_arrival_log_gives_final_endpoint_for_outbound_vector` 原断言「最后两条到达记录」，全量并发落盘时会被其它用例请求插入而偶发变红（W2 起潜伏，913 用例规模下暴露）；改为以 path 唯一的 `/js/beacon` 为锚点、回看其之前是否存在 `/static/iframe.html`，证明的仍是「顺序保留」原命题。
- **已知遗留（不阻塞）**：`tests/phase0/test_checkpoint_owner.py` 的进程级用例会重写 tracked 证据 `docs/poc/evidence/poc01_owner_process_level.json`（时间戳/PID 抖动），本轮以 stash `regen-poc01-owner-evidence-T05R-run` 暂存未入库；后续应改为写临时路径。

## 本轮：单开修复分支 `fix/w5-t18` 完成 P0-T18（2026-10-29）

- **为什么单开**：T18 原在 W6 记 `⊘ BLOCKED`（依赖 T17 真设备/真协议），W6/W7 已收口；按用户指示新开 `fix/w5-t18`（分支名用 `/`，git ref 不允许 `:`），从 `dev`（`34337c8`）拉出，不改动已合并波次。
- **交付**：`experiments/phase0/browserskill_probe/recovery.py`（900 行）、`tests/phase0/test_bsk_recovery.py`（738 行）、`docs/poc/evidence/poc05-recovery.md`（266 行），sha256 见 `fix-w5-t18-record.md` §7。
- **门禁（Lead 独立复跑）**：单测 `45 用例 / 7 skipped（BSK_LIVE 门控，带 NOT_RUN 理由）`；真机（`BSK_LIVE=1` + CPython 3.13）`44 passed / 1 skipped`；全量 `958 tests / 0 failed / 0 errors / 8 skipped / 458.707s`；`ruff` `All checks passed!`；秘密扫描 0；越界检查仅计划状态刷新 + 本记录 + 两处回归重生成证据。
- **判定**：**PASS（限域）**——真机 `tab borrow` 需人工确认记 `NOT_RUN`；`LeaseRegistry` 仍是进程内状态（多副本不可用）、网关重启 epoch 归零；`bsk` 自身无 fencing，唯一性由网关单点保证。
- **提交状态**：feat 交付提交 `cfdc99c`（3 文件 +1904）；本记录 + 计划状态刷新 + 两处回归重生成证据随其后一笔 docs 提交；分支 `fix/w5-t18` 已推送（远端顶点 `fd19151`），并以 `--no-ff` 合入 `dev`（合并提交 `f35443c`，父 `34337c8` + `fd19151`），`origin/dev` 已更新到 `f35443c`。
- **下一步**：仅剩 **5 项未开始**（T26、T28、T29、T31、T32）。T26 的直接前置（T25/T05/T21）均已 PASS（限域），是下一个可开工项；但本机 PostgreSQL 无 `vector` 扩展（T25 已记 ANN `NOT_RUN`），开工前需先定限域口径。

## 本轮：修复 `fix/w1-t02-pgvector` —— PG 镜像升级到自带 pgvector（2026-10-29）

- **为什么做**：上一节指出的「本机 PG 无 `vector` 扩展 → T26 只能做结构/权限验证」是环境缺口（W5 缺口 ③）。按用户选择（B = 升级 postgres 镜像）单开 `fix/w1-t02-pgvector`，从 `dev`（`5809d80`）拉出，不动已合并波次。
- **改了什么**：`experiments/phase0/compose.yaml` 的 postgres 镜像 `postgres:17-alpine@sha256:266d3c25…` → `pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`（同 major 17，数据卷原地复用）；`tests/phase0/test_environment.py` 增加静态断言 `test_postgres_image_ships_pgvector_extension`（仓库名/tag major/digest 三重固定，防回退）；`docs/poc/evidence/environment.md` 追加 §14 标注旧值已历史化。
- **实测**：容器重建后 `healthy 10s`；服务端 `PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu`；`CREATE EXTENSION vector` → `extversion 0.8.7`；`vector(128)` 表 + `hnsw (embedding vector_cosine_ops)` 索引 + `<=>` top-3 查询全部成功且已清理；既有 schema（`p0_t02_probe`/`p0_t05`/`p0_t27`/`public`）在复用卷上完好。
- **门禁**：`test_environment.py` **13 passed**；全量 `tests/phase0` **959 passed / 0 failed / 0 errors / 8 skipped**（`time=269.236s`，8 个 skip 均为 BrowserSkill 真机用例并写明 `NOT_RUN` 理由）；`ruff` `All checks passed!`；`compose config -q` exit 0；core 两容器 `Up (healthy)`。
- **判定**：**PASS**（环境修复，非 PoC 判定；不改变 T25 已记的 ANN `NOT_RUN`，如需升级须重跑 T25 才有新证据）。
- **残余风险**：①全量回归会重写两个 tracked 证据 JSON（`poc01_owner_process_level.json`、`retrieval-baseline.json`，时间戳/PID/timing 抖动），本轮随提交纳入，后续应改为写 `.pytest_cache/`；②`vector` 扩展按库安装，新库迁移/夹具需自带 `CREATE EXTENSION vector`。
- **提交状态**：已提交 `c44ce5d`（`compose.yaml` + 环境测试 + `environment.md` §14）与 `7c8a585`（波次记录 + 本状态文件回填），已推送 `origin/fix/w1-t02-pgvector`，并以 `--no-ff` 合入 `dev`（合并 `cc45265`，父 `f35443c`），后续回填提交 `9d3252a` 为 `dev` 顶点。
- **下一步**：T26 的环境前置已解除，W8 已在该基础上完成（见下一节）。

## 本轮：W8 波次执行（P0-T26：LangGraph RAG、可核验引用、终态一次事务，2026-10-29）

- **为什么单任务波次**：T26 是 W8 唯一就绪项（T28/T29 依赖 T26）。分支 `feature/w8-t26` 从 `dev@9d3252a` 拉出，`N=1`；声明见 `docs/poc/evidence/wave-w8-record.md` §1。
- **派发事故**：首派 `w3-t05-checkpoint` 于 11:16 异常退出且**未留任何文件**（复核四个交付路径均不存在）；11:20 改派 `w3-t07-outbox`（复用 inactive 成员，`N` 不变），12:15 交付。
- **交付（5 份，全部新增未跟踪，未改受保护文件）**：
  - `experiments/phase0/events_probe.py` 1953 行 `d01a0ef0…af4ac0a`：schema `p0_t26` 内联 DDL（16 表 / 12 种 event_type / 4 触发器）、run/message 状态机、terminal 部分唯一索引、finalize 单事务、generation CAS、游标 HMAC + 过期 410、P1 续传 `CAPABILITY_UNSUPPORTED`。
  - `experiments/phase0/rag_probe.py` 1797 行 `7a141be7…0c4a334c`：真实 LangGraph `StateGraph` 9 节点 + custom stream、FTS+向量 RRF、filter-in-query 可见性、`ModelPort`/`Reranker`/`TokenSink` 可注入、引用解析与审计、100 例评测。
  - `tests/phase0/test_rag_events.py` 910 行（21 用例）`cb07668b…bc6779e`；`docs/poc/evidence/poc09-report.md` 204 行；`docs/poc/evidence/rag-citations.json` 353 行（100 例明细）。
- **门禁（Lead 独立复跑，全部实跑）**：任务级 pytest **21 passed in 157.18s**（子 agent 自报 162.22s，一致）；收集数 **980**（= W7 后基线 959 + 21）；全量 **972 passed / 0 failed / 0 errors / 8 skipped / 691.173s**（junit `w8_gate.xml`）；本波 3 个 `.py` 的 `ruff` `All checks passed!`；秘密扫描 **0**；受跟踪改动 **0**、新增仅声明内 6 个文件；`p0_t26` 已 teardown（余 `p0_t02_probe`/`p0_t05`/`p0_t27` 为 W1/W3/W7 遗留）；5500/5501/5600/5610 **0 监听**；core 两容器 healthy；仓库根无临时目录。
- **实测指标（100 例：answerable 40 / no_evidence 20 / cross_space 20 / stale_version 20）**：`behaviour_match_rate=1.0`、`citation_accuracy=1.0`（40/40 命中 gold）、`citations_total=40`、`fabricated=0`、`wrong=0`、`forbidden=0`、`unauthorized=0`、`completed=100`、`refused=60`；延迟 `p50=937.4ms / p95=1495.2ms / max=2194.9ms`。关键负例：50 次重复 finalize 仅 1 条 terminal、部分 token 后 DB 失败无 `completed`、pg 层第二条 terminal 触发 `UniqueViolation`、取消后迟到 finalize → `RUN_ALREADY_TERMINAL`。
- **判定**：**PASS（限域）**。`NOT_RUN`：真实 chat 模型链路（无厂商 key，替身 `ModelPort`）、真实 MinIO/S3、pgvector ANN（不改判 T25）、Redis DB 14 事件投递（事件走 PG 事实表）、删除/取消/重建/源撤销、长时 soak/并发压测/SSE 断网重连。`PARTIAL`：引用人工核验（结构化校验已过、语义需真实模型 + 人工 gold）、RLS 跨空间（用 SQL 过滤 + `chunk_visibility`，未复用 T03 的 `p0_t03` RLS）、延迟（探针装置开销，非生产指标）。`0.95` 引用阈值仍是候选未评审，**未**当作 PASS 依据。
- **已知债务**：`ruff` 按 phase0 配置扫 `experiments tests docs` 有 **76 条既有告警**（`test_task_coordination.py` 9、`test_bsk_recovery.py` 9、`test_dag_validation.py` 7 等 W1–W7 旧文件），**T26 三个文件 0 错**；前几波门禁只扫本波改动文件，故未暴露。是否批量清理需单独决策。
- **提交状态**：**待用户确认**（分支 `feature/w8-t26` HEAD 仍 `9d3252a`，交付物均未 `git add`）。
- **下一步**：确认提交后可开 W9 = T28 + T29（`N=2`）。可补做项：pgvector 已就绪，T25 的 ANN 对照可补跑改判；T26 拿到真实模型 key 后可用同一 100 例复跑。
