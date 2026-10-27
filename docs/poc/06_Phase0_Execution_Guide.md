# Phase 0 逐任务执行指南

日期：2026-10-27。仓库根目录：`D:/2605/14.项目实战/1.企业智能知识库/pyknora`。本轮只完成计划；下列环境安装/命令/故障测试均为**未来草案，未执行**。不应直接将整页命令一次性运行；每次只执行Backlog一个已授权任务。

当前进展：P0-T01 已完成 [契约闭合文档](07_Contract_Closure.md)，其余31任务与九项PoC均未执行。建议下一项 P0-T02；以下首项说明及第7节 T01 提示词保留为原执行模板，不表示需要重做 T01。下一会话应按 Backlog 的 T02 范围单独授权执行。

## 1. 从哪里开始

推荐第一项 **P0-T01**，定义位于 [03_Backlog的P0-T01](03_Phase0_Task_Backlog.md)。它只补文档，明确C-01–C-10和各状态拥有者，避免先写DB/Worker后才发现主体或事务契约缺失。前置仅现有文档/Git可读，不依赖Docker、模型、BrowserSkill或用户立即回答所有产品问题。

完成T01后可并行资料预检T09/T14与环境T02；其后按 [依赖表与波次](04_Phase0_Dependency_DAG.md) 每次选择ready任务。PoC准备工作能并行，运行代码合入受共享锁文件/迁移所有权约束。不能以某后端缺设备为由把所有其他实验标Blocked，也不能绕过被阻塞路线宣布整体PASS。

## 2. 每次Codex会话的执行契约

1. `git status --short`、当前HEAD、已有文件/差异；保护本轮开始时已存在的PRD F-14修改及其他用户工作。不默认提交、不改参考源码/基线。
2. 读AGENTS、PRD、实施状态、本任务Backlog、对应TDD/ADR/验收条目及前置证据；确认依赖满足和所用版本。依赖PARTIAL仅允许明确限域子实验，不能自动视作PASS。
3. 先写该任务的最小验收清单/命令和预计文件范围，再只实现该任务；公共schema或恢复语义改动必须标集成影响，提出最小TDD修订建议；没有授权不改已确认ADR状态。
4. 分别记录unit/contract/mock/真实服务/真扩展结果。依赖缺失记明确未执行或实际尝试后的BLOCKED，不用pytest skip/接口200冒充验收。
5. 跑相应测试/ruff、diff与秘密扫描；按D0保存可复现脱敏证据、更新PoC子结果和实施状态、未解决风险、下一ready任务；完成后停在任务边界。

实验代码默认 `experiments/phase0/`；全路径代码文件在Backlog给出，简写 `db/migrations/...` 是该实验目录下路径，`evidence/...` 是 `docs/poc/evidence/`，测试路径始终仓库根 `tests/phase0/`。不要把预计路径当已存在文件；创建前先查同名用户文件。

## 3. 环境需求与前置核验

本轮只确认PATH可定位python/uv/docker/node，不运行Docker daemon/容器健康测试、不安装任何包。现有根pyproject只有Python>=3.12和空业务依赖，不推导FastAPI/LangGraph等已存在。主验证Python建议3.12，确切patch/uv版本在T02锁定；本机python为3.14路径不构成兼容证据。

| 依赖 | 最小用途 | 未来核验条件/许可 |
| --- | --- | --- |
| Python / uv / pytest / ruff | 隔离虚拟环境、测试/静态检查 | 核受支持平台/版本、锁file hash；不修改根项目依赖来方便实验 |
| FastAPI / Pydantic | 薄测试API、strict schema | 只做测试入口，合成身份；production auth另评审 |
| LangGraph / Postgres checkpointer | 原生State/interrupt/resume | 配套发行版、连接失效/持久化语义真实验证；禁止自建快照引擎 |
| PG / pgvector / psycopg / 迁移工具 | 事实、向量/FTS、锁与CAS | 版本和扩展兼容、RLS角色、向量维度、空库迁移；数据库无生产凭据 |
| Redis / Celery | broker+Worker | 实际发行物许可/CVE、ACK/retry/visibility参数；不假定跨系统恰好一次 |
| MinIO或S3兼容测试服务 | 原件/图片/snapshot/manifest | 明确实际发行物/服务授权、私有bucket/签名测试、不可变key，不泛称S3全等价 |
| DocReader grpcio/protobuf | Read/ReadStream等 | 固定proto hash/参考SHA/镜像digest，OCR/native/字体/权重许可逐项归档 |
| Playwright / browser image / egress proxy | JS控制站/网络隔离 | SDK浏览器成套；context不是OS/网络沙箱；下载策略与子资源出站验证 |
| BrowserSkill daemon/extension/bridge | 授权用户浏览器 | T14真源码先行、成套SHA+产物hash+license/notice+真设备授权；无真设备则不PASS |
| 本地控制站/写计数器/OTel sink | 故障、查单、安全追踪 | 自有可采集数据/测试账号；默认关闭内容采集，不导出生产PII |
| Chat/Embedding/Rerank provider | 真实协议、质量/费用 | 授权endpoint/数据处理规则/费用硬上限；假模型仅测控制流不能测真实质量 |

T02维护core/queue服务清单，T09准入parser镜像，T14准入BSK产物。每个实验记录SBOM/许可证/NOTICE/源归档要求、模型权重/字体/词典许可、已知漏洞与补救；不能凭WeKnora自有MIT覆盖复用物。不确定分发限制由产品/发布负责人处置，技术实验不等于许可批准。

## 4. 未来最小命令草案（本轮全部未运行）

以下仅在相应任务已创建这些文件、确认隔离实验环境且获准执行时使用。服务名是T02预计Compose服务名，未来若改名须同步此指南；`experiments/phase0`当前尚不存在。

```powershell
# 只读预检：未来T02记录版本，不代表环境已运行
git status --short
git rev-parse HEAD
uv --version
python --version
docker version

# T02未来隔离依赖，不向根项目加包；lock必须已生成/审核
uv sync --project experiments/phase0 --frozen
docker compose -f experiments/phase0/compose.yaml --profile core up -d postgres redis control-site

# T03等测试按文件执行；路径为Backlog预计产物
uv run --project experiments/phase0 pytest tests/phase0/test_scope.py -q
uv run --project experiments/phase0 ruff check experiments/phase0 tests/phase0

# T10/T11需要的profile，不在核心预检时默认启动
docker compose -f experiments/phase0/compose.yaml --profile parser up -d object-store docreader
uv run --project experiments/phase0 pytest tests/phase0/test_docreader_wire.py -q

# T08未来Worker入口必须由任务真实创建，不能猜生产包名
Push-Location experiments/phase0
try {
    uv run celery -A job_probe:celery_app worker -Q phase0 -c 2 --loglevel=INFO
} finally {
    Pop-Location
}
```

最后一组按T08预计 `job_probe.py:celery_app` 入口在实验目录启动；如实际任务调整入口，必须同步指南并记录已测命令，当前仍为草案而非已运行结果。隔离环境依赖T02必须把项目测试路径/ruff配置显式定好，不能让命令无测试而绿。BSK不提供猜测命令，T14据真实协议/构建材料产出后再在T17记录真实启动步骤；Playwright浏览器安装仅后续T16授权环境任务，当前不执行。

## 5. 故障、数据与清理规则

- 建立可命名barrier：after_business_commit、after_publish_before_mark、after_job_claim、after_effect_before_receipt、after_receipt_before_checkpoint、before_activation_commit。名字为实验fixture协议，不是现有生产hook。
- 真实故障优先子进程退出/容器kill、单独DB连接terminate、可控代理断路；关键单writer实验要独立切lock/saver连接。故障管理权限仅隔离测试网，所有动作有scenario ID与超时清理；不碰生产/宿主其他用户进程。
- 自有控制站需为SSRF负例记录最终到达端点；普通mock“返回blocked”不能证明网络未到达。外部写仅测试计数服务，真实写授权/approval不绕过。
- Golden/QA/研究任务清单含许可、hash、标注和版本；损坏/加密/禁采集单列拒绝集，不能混分母制造成功率。签名URL/token/cookie/PII不得进普通证据或模型。
- 清理只删本场景隔离资源，先断运行/租约再确认路径/命名空间/无引用，保留失败证据；不运行泛化递归删除脚本，不从本文直接拼危险命令。

## 6. 自动化与CI集成策略

后续仅在任务需要时创建 `.github/workflows/phase0-*.yml`（当前不创建）。L1标准库/DTO/DAG单元+ruff每变更；L2真实PG/Redis/对象/DocReader针对相关变更或受控手动触发；L3真实模型/Playwright/故障进程独立有预算/超时；L4真BSK设备/Handoff人工参与留证据，CI无设备显示未执行，不能整体PASS。

执行前检查CI provider/远端仓库是否实际采用GitHub；不是则写等价本地runner计划，不强行添加平台。矩阵Linux/Python主版本为首验，Windows用户浏览器单列，不承诺原生Windows daemon/Celery生产运行。每组输出JUnit/JSON+脱敏日志/hash，制品保留期需审批配置；secret使用CI密钥存储、受限fork权限，禁日志headers/prompt自动采样。测试失败不自动push、改ADR或开启无限成本重跑。

## 7. 可以直接复制的下一轮提示词

以下引用真实存在的Backlog/评审文件及实际任务ID；输出新文档路径由该任务预先指定，不是已存在产物。

```text
请在 D:/2605/14.项目实战/1.企业智能知识库/pyknora 执行唯一任务 P0-T01。
先检查 Git 状态、当前 HEAD 和已有文件，保护 docs/PRD_PyKnora.md 及其他未提交修改。
阅读 AGENTS.md、docs/PRD_PyKnora.md、docs/IMPLEMENTATION_STATUS.md，
以及 docs/poc/00_ADR_Readiness_Review.md、docs/poc/03_Phase0_Task_Backlog.md 中的 P0-T01、
docs/poc/04_Phase0_Dependency_DAG.md、docs/poc/05_Phase0_Acceptance.md、
对应 TDD 章节和 ADR 提议。
严格按 P0-T01 的范围，只补文档：逐项闭合 C-01–C-10，明确实验字段、状态拥有者、
执行主体/策略绑定、共享事务、幂等键、失败恢复和生产阻断项；创建任务指定的
docs/poc/07_Contract_Closure.md，列出对既有 TDD 的最小修订建议与集成影响，
必要时最小同步本轮规划及 docs/IMPLEMENTATION_STATUS.md。
不要修改既有 PRD、TDD、ADR 状态或 WeKnora 参考源码；不安装依赖、不启动容器、
不运行 PoC、不写业务/实验代码、不自动提交或推送。
对无法确认的问题给出 provisional 实验边界和明确验证/决策门禁，
不要把未知项写成已验证，也不要只交提纲。
验收要求：10/10 缺口均有负责人、具体字段或命令、影响章节、验证任务、
生产阻断判断；每类事实只有一个逻辑写入者；检查文档链接、字段和依赖一致性。
交付后报告真实检查结果、未决事项和下一项 ready 任务，完成 P0-T01 后停止，不跨任务实施。
```

## 8. 本轮限制

本轮已有资料/源码只读复核与文档静态验证不属于运行PoC。若使用上轮已存在的Mermaid渲染器，只用于文档图检查，不下载新包，不启动业务服务。所有预计命令需在后续对应会话实际执行并写结果，当前不得宣称已经运行或通过。
