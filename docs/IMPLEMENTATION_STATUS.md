# PyKnora 实施状态

更新日期：2026-10-27（Asia/Shanghai）。当前为 **Phase 0 / 八份 TDD 与五份 ADR 提议已交付，架构评审及 PoC 计划/运行验证待开展**。本轮只做技术设计及文档验证，不实现业务功能，不代表 ADR 已接受、真实协议兼容或 Phase 0 架构验证完成。

## 当前轮：技术设计（文档交付）

- 起始主仓库 `master` / `7b2a02ff828eb5694d90d5b88947d6694918fdab`，工作树干净；参考 HEAD `662abaf9f944e37689d04c42160034b2cdec6829`，与研究基线一致，前后工作树均干净，没有更新/切换/修改参考源码。
- 已读 AGENTS、PRD、实施状态、全部现有 ADR/TDD/PoC 索引及四份研究输入；仅针对解析 proto/TXT、resume、BrowserSkill daemon/构建、上传 Enqueue、核心/富化边界定向复读源码，不声称重做全量审计。
- 实际新增八份 TDD：[01 架构与统一契约](tdd/01_System_Architecture.md)、[02 Harness](tdd/02_Agent_Harness.md)、[03 Task DAG](tdd/03_Task_Orchestration.md)、[04 Graph](tdd/04_LangGraph_Workflows.md)、[05 Browser](tdd/05_Browser_Adapter.md)、[06 Ingestion](tdd/06_Knowledge_Ingestion.md)、[07 Data/API/Security](tdd/07_Data_API_Security.md)、[08 测试策略](tdd/08_Test_Strategy.md)，更新 TDD 索引。
- 新增 ADR-0001–0005 并更新 [ADR 索引](adr/README.md)：运行时边界、Outbox/版本发布、审批/副作用、解析/浏览器复用、空间/事件/隐私；**均为提议**，没有自动标为已接受。
- 统一 20 项必需标识、run/thread 1:1、状态拥有者、typed port/schema、工具 receipt、预算预留/结算、DAG revision、Job fencing、不可变索引及原子活动指针、最终消息/事件事务；08 逐项规定 F1–F11 的执行顺序、提交点、CAS/唯一约束、恢复触发者和最终断言。
- 官方资料核对：LangGraph interrupt/persistence、Celery Tasks（当时页面为 5.6）、Playwright context、PG 锁/RLS、pgvector、FastAPI lifespan、Pydantic strict、S3 presigned URL、OTel 敏感数据。链接位于对应 TDD，页面核验不等于已安装版本或实测结果；精确依赖锁、CVE/SBOM/许可证核验在后续任务，未改依赖。
- **期间出现的外部工作树编辑**：PRD §6 表格被非本轮工具编辑，F-14 从 P0 改为 P1，另有表格排版修改；已保留。初读冲突历史保留，TDD 按当前工作树 P1 对齐，正式需求基线待评审/提交。PRD diff 不属于本轮作者交付，不回滚、不暂存、不宣称本轮修改。该版本 PRD SHA256：`183B7BF06720BE2C650DD7A2B83B5F87F5AD9ECBEEE843DF1A6F0853449EDD77`。
- 跨文档复核已修正：URL 初次抓取先 Source/fetch Job，批准后才建文档版本；reprocess 以显式处理配置 revision 新建、同配置幂等复用；审批唯一约束仅限有效 pending/approved，过期可新建；asset bytes 与 artifact 逻辑 manifest 区分；PRD §9.9 来源审批字段细化为 review_decision_id 属显式设计提议，不与工具 Approval 混同。

## 本轮验证及限制

- 使用临时目录 PowerShell/Python 检查脚本检查八份 TDD、五份 ADR、两个索引及本状态，共 **16 文件、65 个本地链接、17 个 Mermaid 块、7 个 Python 草案 AST、2 个 JSON 示例、20 必需 ID、11 类故障覆盖，零错误**；新增文件行尾空白检查通过，7 个定向复核源码路径存在。人工复核状态、接口、错误码与 PRD 映射，修正项见上文；字面覆盖检查不等于证明设计正确。脚本与 npm 验证包仅位于系统 TEMP，没有加入项目依赖或源码。
- 实际使用 Mermaid **11.17.2**、Mermaid CLI 临时依赖及系统 Edge headless 验证 17 个 Mermaid 块：**17/17 parse 与 SVG render 成功**；抽看总体组件图和 ER 图截图，未见节点裁切。GitHub 自身渲染器未运行，无法保证其版本/主题与本地完全一致。
- 已执行 `git diff --check`（通过）、`git diff --name-only`、`git ls-files --others --exclude-standard`、`git diff --exit-code -- src pyproject.toml uv.lock references/WeKnora`（退出 0）、参考 `rev-parse HEAD`/`status --porcelain=v1`（指定 SHA、干净）。仅本轮 16 个文档文件及前述外部 PRD 编辑；未修改业务源码、依赖、锁文件或参考源码，未暂存/提交。
- 实际文档验证命令：`python "$tddCheckDir/check_docs.py"`、`node "$tddCheckDir/render.mjs"`、`python "$tddCheckDir/final_check.py"`；临时目录为 `C:/Users/unihou/AppData/Local/Temp/pyknora-tdd-check-a62beb7f2085408f97570c019a6e0091`。渲染使用临时安装的 mermaid/Mermaid CLI、Puppeteer 与系统 Edge，不是业务 PoC。SVG 和两张抽检截图位于该临时目录，不作为项目生产资产或长期证据存储。
- 单元、集成、端到端、安全、性能、备份恢复均**未运行**。TDD 内命令是未来建议，百分比/时延/并发/TTL 是 PRD 目标或待验证建议，不是通过结果。

## 本地提交记录（2026-10-27）

- 用户明确要求将本轮技术设计提交到本地；提交范围为八份 TDD、五份 ADR、两个索引及本状态文件，共 16 个文档文件。
- 提交前检查工作树、暂存区、差异及空白；原暂存区为空。PRD 当前仍有外部未提交的 F-14 P0→P1 修改，明确排除在此次提交之外，保留工作区内容。
- 参考源码 SHA 仍为 `662abaf9f944e37689d04c42160034b2cdec6829` 且工作树干净；没有业务代码或依赖改动。本次仅本地提交，不推送远端；提交 SHA 通过 `git log -1 --oneline` 查询，避免提交内自引用。

## 本轮未决与下一阶段

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
3. 规划六项 Phase 0 PoC 与 Backlog、验收门槛和环境；优先评估 Python DocReader 复用及 Checkpoint/审批恢复。
4. 按任务依赖逐项实现并记录真实测试证据，之后再进入 P0 业务闭环。

## 阻塞、风险与待澄清

- **本轮无硬阻塞**：源码已在本地，未发生网络获取失败；远端最新状态和全新 submodule 下载未测试。离线恢复方式见源码基线文档。
- **基线差异**：PRD 以 v0.8.3 作参考，实际 clone 为其后 202 个提交；现有报告的 tree SHA 不是本轮 commit，不可默认沿用全部结论。
- **需求冲突历史及当前变化**：初读 PRD §6 F-14 为 P0、其他章节为 P1；本轮期间检测到外部未提交编辑已将 §6 改成 P1，保留该工作并按 P1 设计，正式基线待确认。
- **许可证**：MIT 不覆盖全部第三方；DocReader/OCR、BrowserSkill 协议及分发约束未完成审计。PyKnora 自身许可待确定。
- **集成与容量**：数据库、Redis/Celery、对象存储、模型、浏览器设备、观测栈可用性尚未验证；性能数值与安全验收仍是目标，不是已达成结果。
