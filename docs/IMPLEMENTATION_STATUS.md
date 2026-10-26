# PyKnora 实施状态

更新日期：2026-10-27（Asia/Shanghai）。当前为 **Phase 0 / 五条主链源码静态审计已交付，TDD/ADR 与运行验证待开展**。本轮仅研究并写文档，不实现业务功能，不代表穷尽安全审计或 Phase 0 架构验证完成。

## 当前轮：源码审计

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
2. 完成有效 ADR 和 TDD：明确 Harness/LangGraph/Task DAG/Celery、权限作用域、业务/执行状态及可靠投递/索引发布边界。
3. 规划六项 Phase 0 PoC 与 Backlog、验收门槛和环境；优先评估 Python DocReader 复用及 Checkpoint/审批恢复。
4. 按任务依赖逐项实现并记录真实测试证据，之后再进入 P0 业务闭环。

## 阻塞、风险与待澄清

- **本轮无硬阻塞**：源码已在本地，未发生网络获取失败；远端最新状态和全新 submodule 下载未测试。离线恢复方式见源码基线文档。
- **基线差异**：PRD 以 v0.8.3 作参考，实际 clone 为其后 202 个提交；现有报告的 tree SHA 不是本轮 commit，不可默认沿用全部结论。
- **需求冲突待评审**：PRD §6 的 F-14 Wiki 勾选 P0，但摘要、§11/12/17 等定位 P1；本轮不改 PRD，实施前需明确。
- **许可证**：MIT 不覆盖全部第三方；DocReader/OCR、BrowserSkill 协议及分发约束未完成审计。PyKnora 自身许可待确定。
- **集成与容量**：数据库、Redis/Celery、对象存储、模型、浏览器设备、观测栈可用性尚未验证；性能数值与安全验收仍是目标，不是已达成结果。
