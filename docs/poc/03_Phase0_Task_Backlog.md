# Phase 0 单会话开发任务 Backlog

日期：2026-10-27。以下 **32项均为 NOT_STARTED**；是后续任务授权范围建议，本轮没有创建任何预计代码/依赖/测试文件。`P0-Txx` 是规划Task ID，不是产品UUID `task_id`。依赖唯一清单见 [04](04_Phase0_Dependency_DAG.md)，执行规则见 [06](06_Phase0_Execution_Guide.md)。

## 共同输入、粒度和 DoD

- D0（每任务都必须满足）：按AGENTS先检查Git、保护PRD修改；仅做该任务；按依赖取已验产物；类型/权限/幂等契约一致；检查相应测试与ruff；保存命令/版本/hash/真实结果和NOT_RUN/BLOCKED；证据脱敏；更新实施状态/PoC结果但不自动Accepted ADR；review diff；未获请求不提交/推送。不因一个任务完成就称整个PoC通过。
- 无代码任务的D0用链接/一致性检查替代pytest/ruff，并明确N/A，不安装环境。后续实验代码只在 `experiments/phase0/`，测试在 `tests/phase0/`；代码被验证后能否迁入生产是另一任务。
- 复杂度为相对点：1=窄范围资料/纯逻辑；2=一类组件或一个集成窗口；3=多进程/外部真服务。均以一会话一个可复验产物为目标，无工期承诺；开工发现>3点即拆为子任务并同步DAG，不把多天工作塞进一会话。
- 每任务只有一个逻辑状态负责人；测试代码可注入故障但不绕过状态拥有者。`共享契约/DB/公共port：是` 表示需要集成审查和一次合入顺序，不能不同会话各自改同一权威schema。所有新文档/证据路径先定位既有文件，不能覆盖用户工作。

## P0-T01：闭合实验契约与设计缺口清单（首项，只写文档）

- 关联：PoC-01/02/03/04/05/06/07/08/09；PRD F-01/03/04/06/09/10/11、AC-02/05/06/AC-E09；TDD [01 §4–6](../tdd/01_System_Architecture.md)、[07 §3–6](../tdd/07_Data_API_Security.md)。
- 目标：将评审C-01–C-10变为明确的最小实验契约/待生产确认项，避免实现时暗改状态或字段。
- 依赖：无。
- 输入：[00评审](00_ADR_Readiness_Review.md)、七份本轮规划、现有TDD/ADR、未提交PRD差异。
- 文件：仅预计新增 `docs/poc/07_Contract_Closure.md`，最小更新本轮规划受影响项及IMPLEMENTATION_STATUS；不改原TDD/ADR/PRD/代码/依赖。
- 步骤：逐项分类→写执行binding/幂等/owner/共享UoW/Outbox枚举/重试命令/URL审阅/API草案/asset插入顺序/epoch映射→列最小TDD修订diff建议与评审条件；C-03列可证伪假设不伪造结果。
- 测试：文档链接、字段/状态/错误与现有契约逐项对照，两个合成workspace的命令流桌面推演。
- 量化验收：10/10缺口有owner、精确字段/命令、影响章节、验证任务、生产阻断判断；每事实只有一逻辑writer；PoC可执行schema没有二义字段；仍U事项有门禁。
- 注入：纸面推演无run Job撤权、发布回滚、审批消费后未知结果，标为分析而非运行故障注入。
- 估计：2点；不需运行服务。
- 风险/备选：产品身份/API范围未定→明确provisional synthetic实验范围和生产Blocked条件，仍完成全部修订建议，不擅自接受例外。
- 状态负责人：架构契约负责人；无业务状态写入。
- 集成影响：共享契约/DB/公共port：是（仅提议）；T03/T07/T12/T15/T23必须读此产物，原TDD变更另行评审。
- DoD：D0文档版+十项闭合表、最小修订建议和可复制下一任务入口；无安装/PoC。

## P0-T02：隔离实验环境与核心依赖清单

- 关联：PoC-01/02/03/04/05/06/07/08/09；PRD F-01/04/16、AC-06；TDD [01 §7](../tdd/01_System_Architecture.md)、[08 §2/7](../tdd/08_Test_Strategy.md)。
- 目标：准备可复现核心实验环境，不启动九个业务闭环。
- 依赖：P0-T01。
- 输入：实验契约、批准的包/镜像来源、Python/uv/Docker可用性与实验费用限额。
- 文件：未来 `experiments/phase0/pyproject.toml`、独立 `uv.lock`、`compose.yaml`、`.env.example`（无秘密）、`docs/poc/evidence/environment.md`；不改根依赖。
- 步骤：核官方配套版本/维护/CVE/许可→锁包与镜像digest→定义core/queue/parser/browser profiles→health/preflight→只验核心服务连通。
- 测试：环境preflight、PG/Redis简单健康、依赖解析和ruff/pytest可调用；不预装BSK猜版本。
- 量化验收：核心所有包/镜像100%有精确版本/hash/license/source；从干净隔离目录可按命令重建；发现CVE/许可未知均有阻断/替代记录；零真实秘密入Git。
- 注入：缺环境变量/镜像不可达/端口占用应明确失败，不退回latest。
- 估计：2点；若平台/native安装过大分离profile，不扩成本任务。
- 风险/备选：本机只有3.14/daemon不可用→Linux测试机/容器，记录BLOCKED而非更改Python兼容声明。
- 状态负责人：实验环境负责人（仅环境清单）；不拥有Run/Job。
- 集成影响：公共port/DB：否；共享锁文件：是，后续服务profile变更须串行合入。
- DoD：D0+E1/E2最小连通证据及未就绪profile清单；不声称PoC业务PASS。

## P0-T03：合成身份、Scope 与最小PG契约

- 关联：PoC-01/02/03/06/07/09；PRD F-01、AC-02/06；TDD [01 §4/5](../tdd/01_System_Architecture.md)、[07 §3/4](../tdd/07_Data_API_Security.md)。
- 目标：单一Scope和共享最小表迁移、execution binding，支持无run作业再授权。
- 依赖：P0-T02。
- 输入：T01闭合字段、E1、双workspace/Principal合成fixture；不实现生产登录。
- 文件：`experiments/phase0/contracts.py`、`db/migrations/001_scope_core.sql`、`scope.py`；`tests/phase0/test_scope.py`。
- 步骤：实现可信构造/deny默认→复合FK/最小事实表→非owner RLS实验→池化SET LOCAL清理；后续新增列增量迁移不得复制基表。
- 测试：100组跨空间资源ID/Job binding篡改、membership撤销、连接复用；迁移up/空库重建。
- 量化验收：越权命中/可写0；100%组合FK拒绝跨空间；无Run Job恢复也读当前Principal；RLS role属性可查且无BYPASSRLS。
- 注入：将A scope连接交给B请求、伪造Job payload role、停用主体。
- 估计：2点。
- 风险/备选：身份域例外未评审→仅合成主体，不开放生产auth；RLS缺失可作为PARTIAL，不声称双层隔离通过。
- 状态负责人：授权服务负责Scope/会员与execution binding，业务Run/Job列只由对应服务使用。
- 集成影响：DB/公共port：是；shared migration合入后其他任务引用，禁止平行覆盖。
- DoD：D0+双空间拒绝证据、已确认schema snapshot和RLS限制。

## P0-T04：可控下游、控制站与故障断点fixture

- 关联：PoC-01/02/03/04/05/06/07/08/09；PRD AC-05/06/AC-E08/09；TDD [08 §2/5](../tdd/08_Test_Strategy.md)。
- 目标：提供真实本地HTTP计数/查单、静态/JS/重定向/文件站和进程同步屏障，不混入业务实现。
- 依赖：P0-T02。
- 输入：E1、fixture许可/数据生成策略、安全隔离网络。
- 文件：`experiments/phase0/control_site/`、`faults.py`、`tests/phase0/conftest.py`、`fixtures/manifest.json`。
- 步骤：建立支持/不支持key两个写endpoint→只读查单→数据/跳转/下载/手动验证页→测试专用断点arm/release→健康超时清理。
- 测试：fixture自身计数与hash、fault barrier可确定重现/自动解除；不得依赖固定sleep碰时序。
- 量化验收：≥20控制向量/安全页面、有正负样本；重复key只计1，非幂等写每调用可数；10次kill重建隔离无遗留端口/数据。
- 注入：延迟、超时、5xx、断流、返回成功后主动断连接、人工登录页。
- 估计：2点。
- 风险/备选：无法网络故障隔离→使用测试代理/子进程屏障，保留哪些只是替身的证据边界。
- 状态负责人：fixture服务管理自己的测试计数，绝不写业务Run/Job。
- 集成影响：公共测试port：是；各任务只能按fixture API使用，不各造计数器。
- DoD：D0+全部fixture可复验/许可manifest、生产默认不可启用故障入口。

## P0-T05：原生Checkpoint 单写者与并发恢复

- 关联：PoC-01；PRD F-09、AC-E09；TDD [01 §6.4](../tdd/01_System_Architecture.md)、[04 §7/8](../tdd/04_LangGraph_Workflows.md)。
- 目标：验证独立锁连接与saver连接断开时的writer行为，不先假设lease可fence。
- 依赖：P0-T03、P0-T04。
- 输入：E1、原生Postgres saver配套版本、T03 Scope/run/thread。
- 文件：`experiments/phase0/checkpoint_probe.py`、`tests/phase0/test_checkpoint_owner.py`、`evidence/poc01-owner.md`。
- 步骤：固定Graph→真实持久化/interrupt→进程退出恢复→两个host争owner→独立断锁/saver→记录原生API状态与业务epoch。
- 测试：正常跨进程恢复、50轮每个失锁/重连窗口、未知graph/schema版本拒绝。
- 量化验收：run/thread关联100%一致；至多一有效writer；旧invocation失锁后不能再写checkpoint；否则明确FAIL或单活PARTIAL，不隐藏失败。
- 注入：pg连接terminate、网络阻断、owner lease过期但进程未死、saver独立仍活、重复resume。
- 估计：3点。
- 风险/备选：原生saver自动重连无法安全停止→单活Runner/阻止接管，提出ADR-0001修订，不自研snapshot。
- 状态负责人：LangGraph唯一写checkpoint；RunService管理Run owner/epoch。
- 集成影响：运行port/DB：是，host控制封装供T06/T21复用。
- DoD：D0+连接级时序/原始故障证据和是否支持多实例的明确结论。

## P0-T06：审批绑定、调用账本与未知写核查

- 关联：PoC-01；PRD F-09/13、AC-06/AC-E09；TDD [02 §4/5](../tdd/02_Agent_Harness.md)、[04 §7](../tdd/04_LangGraph_Workflows.md)。
- 目标：批准/重复恢复/崩溃下不产生未经确认重复写。
- 依赖：P0-T05。
- 输入：T04两个计数下游、T05host、T01稳定intent key和审批边界。
- 文件：`experiments/phase0/approval_probe.py`、`tool_ledger.py`、`db/migrations/002_approvals.sql`、`tests/phase0/test_approval_effects.py`。
- 步骤：登记model-response ref/ordinal的intent→ensure/wait原生interrupt→薄测试API批准→重验/consume+executing→调用→receipt→恢复查账本。
- 测试：50轮每崩溃窗口；50重复resume；改参数/工具version/scope/expiry、撤requester或approver。
- 量化验收：明确未发送时下游0；已确认执行最终1；无key未知结果零自动重发；无效审批/主体网络写0；账本有receipt或unknown，不伪称失败未执行。
- 注入：executing提交后、下游成功后receipt前、receipt后checkpoint前kill；审批后撤权/secret canary。
- 估计：3点。
- 风险/备选：key查询语义不足→关闭写connector/人工核查；不新生成key掩盖重复。
- 状态负责人：ApprovalManager只管Approval；ToolExecutor只管ToolCall；RunService消费可信事实。
- 集成影响：DB/工具公共port：是，与T20预算字段兼容但不提前实现预算平台。
- DoD：D0+支持/不支持外部幂等两条路线独立结论、脱敏调用次数证据。

## P0-T07：Outbox 生产事务与派发lease

- 关联：PoC-02；PRD F-03/04、AC-05；TDD [01 §6](../tdd/01_System_Architecture.md)、[07 §3](../tdd/07_Data_API_Security.md)。
- 目标：实现隔离Job/Outbox生产与发送窗口实验。
- 依赖：P0-T03、P0-T04。
- 输入：T01 Outbox枚举/共享UoW、E2/Redis、唯一dedup。
- 文件：`experiments/phase0/outbox_probe.py`、`db/migrations/003_jobs_outbox.sql`、`tests/phase0/test_outbox_transaction.py`。
- 步骤：业务+Job+Outbox一次提交→SKIP LOCKED claim→事务外publish→epoch CAS dispatched/retry_wait；只读dispatcher不改Job执行状态。
- 测试：50轮commit/rollback+Redis断开，两个dispatcher争同Outbox，发送后未标记kill。
- 量化验收：回滚消息可执行数0；已提交Job全部有Outbox；重复发送保持outbox_id/job_id相同；无长事务持网络I/O。
- 注入：DBrollback、broker拒绝、已发未标记、租约过期/迟到旧dispatcher。
- 估计：2点。
- 风险/备选：并发claim/lease模型不成立→先单dispatcher验证并保留PARTIAL，多实例门禁不得跳过。
- 状态负责人：OutboxService；JobService负责创建Job，事务由应用用例编排。
- 集成影响：DB/消息公共schema：是；Celery transport_id不能占用业务task_id。
- DoD：D0+事务裂缝证据、完整状态转移和scan query plan。

## P0-T08：消费幂等、Job 对账与死信redrive

- 关联：PoC-02；PRD AC-05/AC-E06；TDD [01 §5/6](../tdd/01_System_Architecture.md)、[06 §5/6](../tdd/06_Knowledge_Ingestion.md)。
- 目标：broker重投/清空/Worker退出都能形成确定Job结果。
- 依赖：P0-T07。
- 输入：E2、T07消息、T03无Run execution binding、T01failed/cancelled的显式重试命令。
- 文件：`experiments/phase0/job_probe.py`、`job_reconciler.py`、`tests/phase0/test_delivery_recovery.py`。
- 步骤：Job CAS领取attempt/心跳→结果先提交再ACK→DB对账补唤醒→死信隔离/授权redrive；固定无外部写consumer，实验 `job_probe.py` 暴露 `celery_app` 入口供指南启动。
- 测试：100Job×10重复、两worker、Redis临时断/数据丢失、重试耗尽/poison与人工恢复。
- 量化验收：依赖恢复后60秒测试deadline内100Job到明确终态、每Job一业务结果；无RunJob撤权0执行；成功Job重复redrive不复活；死信行和操作者记录不丢。
- 注入：claim后kill、结果提交后ACK前kill、旧epoch返回、超available_at/lease、取消后重投。
- 估计：3点。
- 风险/备选：broker参数无法满足→保留DB扫描恢复/评估队列替换；不宣称acks_late端到端exactly-once。
- 状态负责人：JobService管Job/attempt；OutboxService管redrive派发，不互相直接改表。
- 集成影响：DB/Worker port：是，供T12/T21/T23使用。
- DoD：D0+ACK/retry/visibility设置及真实观察、死信runbook与恢复时序。

## P0-T09：DocReader proto、镜像和许可预检（静态）

- 关联：PoC-04；PRD F-04、AC-01；TDD [06 §3](../tdd/06_Knowledge_Ingestion.md)、[01 §7](../tdd/01_System_Architecture.md)。
- 目标：固定真实proto/实现及部署/许可输入，不运行解析。
- 依赖：P0-T01。
- 输入：只读WeKnora固定SHA、proto/main/registry/auth/lock/NOTICE，镜像来源资料。
- 文件：仅 `docs/poc/evidence/docreader_manifest.md`、`docreader_fixture_plan.md`；需stub复制时留到T10隔离目录。
- 步骤：hash proto→四RPC字段/错误/流契约→依赖/native/OCR权重/镜像清单→格式与定位golden设计。
- 测试：静态路径/行号/hash和license交叉核验，明确A/D/U证据。
- 量化验收：4RPC100%有请求/响应/错误/限制记录；5格式均有路线；镜像/权重未知项逐项列阻断与替代，无“MIT覆盖所有”声明。
- 注入：纸面缺meta/缺图/unknown engine/默认无token路径分析，不冒充运行。
- 估计：1点。
- 风险/备选：镜像/许可不全→BLOCKED runtime准备，仍交付完整manifest；成熟parser替代评审。
- 状态负责人：解析契约负责人；不写业务状态。
- 集成影响：公共Parse port/proto：是（证据提议），无代码/DB改动。
- DoD：D0文档版+真实源码/proto定位与可运行前置清单。

## P0-T10：DocReader Read/ReadStream 真实互通

- 关联：PoC-04；PRD F-04、AC-01/03；TDD [06 §3](../tdd/06_Knowledge_Ingestion.md)。
- 目标：验证真实服务/配套client、auth/deadline/流，而非mock兼容。
- 依赖：P0-T02、P0-T04、P0-T09。
- 输入：T09许可就绪镜像/proto、E3、许可样本PDF/DOCX；schema缺口不得擅改reference。
- 文件：`experiments/phase0/docreader_probe/`（stub/proto镜像副本含版权）、`tests/phase0/test_docreader_wire.py`。
- 步骤：固定stub→以opaque资产文件名/最小metadata发送bytes Read/Stream→检查meta.error/EOF/顺序→超时/auth/服务端日志canary→记录实际engine；flow异常shim单独标记。
- 测试：真实Read/ReadStream/两格式、ListEngines不冒充格式通过；UNIMPLEMENTED降级条件、bad token、断流。
- 量化验收：真实PDF/DOCX各≥2个正例/图表成功；错误与超时映射100%正确；没有meta或中断图流不产生可发布结果；所有产物版本/hash可追踪。
- 注入：首meta后断连接、deadline、server退出、错序shim、超大帧拒绝。
- 估计：2点。
- 风险/备选：缺真实server→BLOCKED不mock PASS；明确格式替代引擎试验需新任务/范围。
- 状态负责人：ParserAdapter只产解析结果，IngestionService尚不发布。
- 集成影响：公共Parse DTO：是；新增依赖仅实验锁文件串行合入。
- DoD：D0+真实wire证据与shim结果分开、部署/认证限制记录。

## P0-T11：五格式golden、图片资产与引用映射

- 关联：PoC-04；PRD F-04/07/08、AC-01/03/04/06；TDD [06 §2–4](../tdd/06_Knowledge_Ingestion.md)。
- 目标：五格式实际支持及变换定位，P0受控HTTP输入先policy后抓bytes。
- 依赖：P0-T10、P0-T03。
- 输入：E3/T04控制站、50份许可样本、T10结果/真实对象服务。
- 文件：`experiments/phase0/parsing_assets.py`、`locator_map.py`、`simple_reader.py`、`tests/phase0/test_parser_golden.py`、fixture manifest。
- 步骤：TXT/MD窄适配→HTML受控抓取/抽取→图片不可变对象/内部ref→区间映射→gold比较；不用任意parser URL/远程image key。
- 测试：50标准文档与拒绝集；中文emoji/CRLF/换图/表格；无locator降级、object断写/越权。
- 量化验收：≥49/50且每格式≥9/10成功；精确定位声明100%金标准匹配；缺图/截断可发布0；每资产关联空间/来源，签名/凭据不进state/log。
- 注入：对象写失败、bad storage_key/SSRF、未知编码、部分stream、缺source block/PII。
- 估计：3点；只含窄golden适配，若某格式需大修拆子任务，不重写parser。
- 风险/备选：格式或定位不达→分格式PARTIAL/替代profile；不能伪造bbox、宣称五格式PASS。
- 状态负责人：ParserAdapter产结果，AssetService管对象绑定，定位manifest不可变；不写active index。
- 集成影响：Parse/Asset/Locator公共schema：是，供T12/T25/T27复用。
- DoD：D0+逐格式支持/定位精度/性能原始表与许可数据清单。

## P0-T12：版本staging、Chunk/Embedding与引用模型

- 关联：PoC-03；PRD F-03/04/05、AC-01/05；TDD [06 §5/6](../tdd/06_Knowledge_Ingestion.md)、[07 §3](../tdd/07_Data_API_Security.md)。
- 目标：构建不可见版本分区与manifest，旧attempt不能混入winning数据。
- 依赖：P0-T03、P0-T08、P0-T11。
- 输入：T11标准产物、T01FK/UoW契约、真实PG/pgvector/对象；初期确定性embedding仅测一致性。
- 文件：`experiments/phase0/index_staging.py`、`db/migrations/004_version_index.sql`、`tests/phase0/test_index_staging.py`。
- 步骤：desired version/generation→Job attempt fencing→chunk ordinal唯一→向量/FTS staging→对象checksum/manifest→index_ready；暂不自动publish。
- 测试：重复chunk批次、同version新attempt、混维度拒绝、引用source/offset一致、FK环的合法插入/rollback。
- 量化验收：staging未发布新检索命中0；重复批次行数不增长；旧attempt写当前分区0；manifest缺任一必需项不能ready。
- 注入：embedding数不符、半批失败、object缺失、错误workspace/source FK、取消generation变化。
- 估计：2点。
- 风险/备选：共享schema不闭合→回T01提最小修订，不能每PoC复制表；向量空间失败先阻断该profile。
- 状态负责人：IngestionService管摄取，IndexPublicationService管build状态，JobService管attempt；同UoW协作。
- 集成影响：DB/索引/引用schema：是；T13/T25强依赖，不平行改迁移。
- DoD：D0+不可见/版本隔离证据和可被T13激活的完整manifest。

## P0-T13：原子发布与版本竞态故障注入

- 关联：PoC-03；PRD AC-04/05/AC-E06/07；TDD [06 §5–7](../tdd/06_Knowledge_Ingestion.md)。
- 目标：一次PG事务切活动指针，任何新查询只见已发布授权版本。
- 依赖：P0-T12。
- 输入：两版本/两attempt staging、T08Job、T03当前授权、T04确定断点。
- 文件：`experiments/phase0/index_publication.py`、`tests/phase0/test_publication_faults.py`、`evidence/poc03-publication.md`。
- 步骤：锁document→验desired/generation/attempt/manifest→各owner服务同UoW转换active/Job/event/enrichment Outbox→提交；reader单snapshot join。
- 测试：100轮竞态每轮≥100次读取；核心成功/富化失败，历史引用/GC；同配置failed Job受控重试不改内容版本。
- 量化验收：混版本/半成品/跨空间命中0；v1晚到不覆盖v2；删除提交后新查询0；取消/重建不复活；成功核心不被富化改failed。
- 注入：T3前/中/后kill、ACK丢、旧epoch、delete/rebuild/源撤销、对象丢失、enrichment失败。
- 估计：3点。
- 风险/备选：原子性不成立→停新发布/每文档串行，重新评ADR-0002，不隐去失败采样。
- 状态负责人：IndexPublicationService管活动指针；通过JobService提交Job终态，不直写jobs。
- 集成影响：DB/读取契约：是；必须检验全部查询入口。
- DoD：D0+完整时间线/读采样元组/最终指针/对象引用与残余风险。

## P0-T14：BrowserSkill 真源码/协议/许可审查（静态）

- 关联：PoC-05；PRD F-11、AC-E02/08/09；TDD [05 §2–5](../tdd/05_Browser_Adapter.md)。
- 目标：先取得真实pinned协议证据，再推荐最小兼容层。
- 依赖：P0-T01。
- 输入：WeKnora构建pin、可授权获取的upstream bundle/源码及daemon/extension产物；没有则记录缺失。
- 文件：`docs/poc/evidence/browserskill_protocol_manifest.md`；upstream若获取放独立实验资料路径，不改references/WeKnora或gitlink。
- 步骤：核SHA/产物hash/许可→实际wire/handshake/capabilities/method/epoch/auth→桥接vsPython直连最小范围/不支持项→设备/OS矩阵。
- 测试：静态符号/路径/序列核对；不执行daemon或浏览器。
- 量化验收：每暴露动作100%有真实协议依据或明确unsupported；三组件部署和授权分开；无材料时所有相关能力U，不编造兼容API。
- 注入：纸面版本错配/owner断线/缺全请求网络控制路径分析，非实测。
- 估计：1点；获取材料耗时单列阻塞，不变成隐式全量审计。
- 风险/备选：源码/许可不可得→T17/T18 BLOCKED，Playwright继续；保留ADR-0004 Proposed。
- 状态负责人：浏览器协议负责人；不写BrowserSession状态。
- 集成影响：Browser port：是（设计输入），不自动选定桥接/改ADR。
- DoD：D0文档版+逐动作证据表/缺口/可逆路线推荐。

## P0-T15：Browser port、能力协商与Scope契约

- 关联：PoC-05；PRD F-11、AC-E02/08；TDD [05 §3–6](../tdd/05_Browser_Adapter.md)。
- 目标：只统一业务语义，拒绝未实现能力，不重写两套浏览器。
- 依赖：P0-T03、P0-T04。
- 输入：T01状态/epoch映射、TDD05 port；BSK wire未知时仅定义declared unsupported。
- 文件：`experiments/phase0/browser_contracts.py`、`tests/phase0/test_browser_contracts.py`。
- 步骤：DTO/typed action/结果→session grant/epoch→capability交集/unsupported→动作effect/审批前置→脱敏artifact引用。
- 测试：schema拒未知参数/任意JS/伪session/旧locator，mock仅测port、非wire互通。
- 量化验收：≥20正负契约fixture全判定正确；unsupported不降级偷偷执行；写action无approval不能dispatch；secret字段拒绝。
- 注入：篡改scope、未知version/feature、过期epoch/locator、timeout sent真假分支。
- 估计：1点。
- 风险/备选：抽象不能表达后端差异→缩公共集合，后端专有capability显式分支，不强行等价。
- 状态负责人：BrowserSessionService管理会话；Adapter只回传事实。
- 集成影响：公共port/错误schema：是；T16/T17共用，不各改字面码。
- DoD：D0+公共/专有/unsupported矩阵与contract测试。

## P0-T16：Playwright 真实隔离读取与SSRF

- 关联：PoC-05；PRD F-11、AC-06/AC-E02/08；TDD [05 §6](../tdd/05_Browser_Adapter.md)。
- 目标：真实JS读取和网络出口控制，不以context代替沙箱。
- 依赖：P0-T15。
- 输入：E4授权控制站、匹配SDK/浏览器镜像/强制代理；不注入真实用户profile。
- 文件：`experiments/phase0/playwright_probe.py`、`network_policy/`、`tests/phase0/test_playwright_security.py`。
- 步骤：每run独立context/容器→domain/IP/redirect/子资源/WS/下载代理→采集snapshot/metadata→cancel/cleanup。
- 测试：动态页正例、≥20SSRF/网络向量、两空间cookie/storage隔离、人工介入支持或unsupported。
- 量化验收：适用禁止目的地到达0，正常JS文本/截图可复现；互访session内容0；超bytes/deadline后无继续下载；凭据/PII canary0。
- 注入：DNS rebinding、IPv6/私网redirect/iframe/WS、browser进程退出、下载超限、导航超时/取消。
- 估计：3点。
- 风险/备选：出口可旁路→FAIL、关闭JS浏览器，保留受控HTTP，不仅加route hook当修复。
- 状态负责人：BrowserSessionService管会话；PolicyEngine决定网络/动作策略；adapter不改Run。
- 集成影响：公共port新增能力需契约审查；网络部署profile：是。
- DoD：D0+网络真实到达日志/权限/产物版本证据，不只SDK成功返回。

## P0-T17：BrowserSkill 真扩展配对与读取

- 关联：PoC-05；PRD F-11、AC-E02/08；TDD [05 §2–4](../tdd/05_Browser_Adapter.md)。
- 目标：窄范围真实handshake/设备授权/read，先不宣称任务恢复。
- 依赖：P0-T14、P0-T15、P0-T06。
- 输入：T14准许路线/许可、成套0.3.1 SHA产物、测试Chrome/Edge与用户显式配对。
- 文件：`experiments/phase0/browserskill_probe/`（最小client或bridge，保留通知）、`tests/phase0/test_bsk_live.py`。
- 步骤：据真协议实现最窄transport→pair/renew/auth→negotiate版本→授权标签read→snapshot/通道；不复制整个WeKnora。
- 测试：真实扩展+daemon正常read、bad/旧token、版本错配、错space/tab/Origin；mock仅辅助。
- 量化验收：配对/续期/读取各一条真实证据；无授权读取0；旧token重放拒绝；每动作能力/协议/产物hash齐全；没有精确实现的方法unsupported。
- 注入：wrong daemon/extension版本、token过期/重放、scope伪造、未许可标签。
- 估计：3点；bridge抽取若超窄范围须拆任务，不全量移植Go。
- 风险/备选：缺真设备/配套产物→BLOCKED；无法隔离用户网络的能力禁用，Playwright路线不冒充BSK通过。
- 状态负责人：BrowserSessionService/device service负责配对会话；网关只传受认证事实。
- 集成影响：内部bridge API/授权schema：是；不得改reference或原ADR状态。
- DoD：D0+成套真实wire证据、平台支持范围与未支持清单。

## P0-T18：BrowserSkill owner、撤销与人工接管

- 关联：PoC-05；PRD AC-E08/09/10；TDD [05 §4–7](../tdd/05_Browser_Adapter.md)。
- 目标：断线恢复仅恢复连接，未知写不重放，用户决定继续。
- 依赖：P0-T17、P0-T06。
- 输入：T17真实session、两个gateway实例/owner、T04计数动作、T06ledger。
- 文件：`tests/phase0/test_bsk_recovery.py`、`experiments/phase0/browserskill_probe/recovery.py`、`evidence/poc05-recovery.md`。
- 步骤：pause/revoke/renew→Handoff原生interrupt→owner epoch/路由→发送动作后断线→unknown核查→重连paused/显式resume。
- 测试：50轮owner/未知窗口，测试账号人工完成登录/验证码演示；canary profile/trace检查。
- 量化验收：未确认重复写0；撤销后新动作0；旧epoch结果不改当前session；用户未继续不能恢复任务；不支持全链网络策略场景全部拒绝。
- 注入：owner kill/WS断、lease过期、旧回复晚到、Handoff期间取消、撤销后重连。
- 估计：3点。
- 风险/备选：上游不支持所需fence→单owner/只读禁写；仍PARTIAL并重评，不自行伪造wire epoch支持。
- 状态负责人：BrowserSessionService只写session；ToolExecutor只写动作账本；RunService独立应用事实。
- 集成影响：恢复/授权port：是；前端只需最薄人工测试入口，无产品UI扩展。
- DoD：D0+真设备恢复/无重放证据，明确人机接管和网络策略限制。

## P0-T19：单Provider ModelGateway 与窄工具Registry

- 关联：PoC-06；PRD F-02/09/13、AC-E10；TDD [02 §2/3](../tdd/02_Agent_Harness.md)。
- 目标：一个模型provider和三个受控工具的typed调用契约，不构建完整Harness。
- 依赖：P0-T03、P0-T04。
- 输入：E5授权模型/预算、T03 Scope、T04只读/写/错误工具；未知模型可替身先验契约。
- 文件：`experiments/phase0/model_gateway.py`、`tool_registry.py`、`tests/phase0/test_gateway_registry.py`。
- 步骤：成熟SDK薄适配→capability/usage→工具schema/name-version唯一→未完整工具参数拒绝→安全错误。
- 测试：一次真实结构化工具/usage调用，≥20协议/429/5xx/length/unknown-tool fixture；工具执行不在本任务暗写loop。
- 量化验收：重名注册拒绝、未授权/截断参数执行0；provider秘密不入任何DTO/log；真实能力与mock结果分别标记。
- 注入：模型错误/输出截断/工具schema漂移、错误体含canary。
- 估计：2点。
- 风险/备选：真实模型不可用→该项BLOCKED，不以替身宣称兼容；只验证单一provider。
- 状态负责人：ModelGateway记录请求结果/用量事实，不写Graph/Run状态；Registry只管工具定义。
- 集成影响：模型/工具公共port：是；供T20/T22/T25复用。
- DoD：D0+provider实证和契约矩阵、没有第二套推理状态机。

## P0-T20：Context 工具配对与预算预留账本

- 关联：PoC-06；PRD F-02/09、AC-E08；TDD [02 §6/7](../tdd/02_Agent_Harness.md)。
- 目标：上下文裁剪不破坏工具对，执行预算并发原子归集。
- 依赖：P0-T19。
- 输入：T19消息/usage类型、T03 PG、定价快照、有限fixture；不实现跨会话记忆。
- 文件：`experiments/phase0/context_budget.py`、`budget_ledger.py`、`db/migrations/005_budget.sql`、`tests/phase0/test_budget_context.py`。
- 步骤：有限窗口/保留引用/tool pair→reserve锁序→settle/release唯一键→未知usage冻结/超额记账→父run一次归因。
- 测试：≥20消息fixture，100轮最后额度双竞争；50重复结算；摘要失败/费用未知/取消。
- 量化验收：孤立tool消息0；已发未知费用自动释放0；并发账本重复扣费/绕过上限0；每retry费用可归集，超实际预留记录真实值并冻结后续。
- 注入：settle后连接断/重复settle、deadline、取消发生在发出前/后，价格快照缺失。
- 估计：2点；首版只必要维度/单币种，不提前通用计费平台。
- 风险/备选：无法得usage→按保守maximum保留待核查、供应商硬限额；不宣称绝对不会超供应商计价。
- 状态负责人：BudgetManager唯一改预算；ContextManager仅产安全窗口/summary引用。
- 集成影响：DB/CostVector公共port：是；T24与T28不得另造预算池。
- DoD：D0+配对/预算原始并发记录与未知费用runbook。

## P0-T21：Harness 节点集成、取消与安全trace

- 关联：PoC-06；PRD F-09/16、AC-E08/09/10；TDD [02 §3/5/8](../tdd/02_Agent_Harness.md)、[04 §4/7](../tdd/04_LangGraph_Workflows.md)。
- 目标：模型/工具/策略/审批/预算嵌入原生Graph并复用Job唤醒。
- 依赖：P0-T05、P0-T06、P0-T08、P0-T20。
- 输入：各窄port、OTel本地安全sink、只读/写/错误三工具。
- 文件：`experiments/phase0/harness_graph.py`、`safe_trace.py`、`tests/phase0/test_harness_integration.py`。
- 步骤：固定Graph组合→只读并行/写屏障→重入读ledger→Job完成唤醒/丢通知对账→cancel/deadline→安全span。
- 测试：4只读并发前后夹写、模型retry不重放工具、50次重复唤醒、trace故障与canary扫描。
- 量化验收：写屏障前悬空只读0；未知写自动重放0；cancel后新动作0；state/log/trace/模型请求secret/PII canary0；checkpoint/interrupt仅框架原生。
- 注入：trace exporter断、审计提交失败、Job通知丢失、tool成功checkpoint失败、超时/取消。
- 估计：2点，依赖实验已有结果，只做集成不重写各组件。
- 风险/备选：模块越界→删Runtime循环/收窄门面；敏感审计不可写fail closed。
- 状态负责人：Graph原生执行；各事实由对应服务，AgentRuntime不是新状态owner。
- 集成影响：节点/执行上下文公共port：是；回归T05/T06/T08/T20受影响部分。
- DoD：D0+最小调用图/安全trace与恢复/取消证据，无大型框架新增。

## P0-T22：Planner结构化输出与DAG 校验

- 关联：PoC-07；PRD F-10、AC-E01；TDD [03 §2/3/4](../tdd/03_Task_Orchestration.md)。
- 目标：将计划变成有限可验证DAG，模型不能授予权限或指定任意资源。
- 依赖：P0-T19。
- 输入：T19真实模型一次可用、TDD九节点例子、32节点/64边/深度8测试上限。
- 文件：`experiments/phase0/dag_contracts.py`、`planner_probe.py`、`tests/phase0/test_dag_validation.py`。
- 步骤：Pydantic schema→模型标签到服务端UUID映射→graphlib无环/依赖/规模/Scope检查→固定合法/非法fixture。
- 测试：一次真实plan，其余确定性fixture；≥20非法环/自依赖/缺节点/越界输入。
- 量化验收：非法计划100%调度前拒绝；合法九节点拓扑可复现；普通RAG路径Planner调用0。
- 注入：模型JSON截断、未知kind、恶意workspace/URL权限扩张、超任务数。
- 估计：1点。
- 风险/备选：自然语言Planner不稳定→固定有限模板PARTIAL，不跳过schema校验。
- 状态负责人：Planner产草案，OrchestrationService验证后登记revision，不由模型写Task状态。
- 集成影响：TaskGraph公共schema：是，T23/T24需版本一致。
- DoD：D0+合法/非法计划证据和不走Planner的RAG边界测试。

## P0-T23：DependencyScheduler 与Task/Graph/Job 对账

- 关联：PoC-07；PRD F-10、AC-E01/09；TDD [03 §3/5](../tdd/03_Task_Orchestration.md)、[04 §5](../tdd/04_LangGraph_Workflows.md)。
- 目标：只执行ready任务，Task事实和Graph引用可恢复一致。
- 依赖：P0-T22、P0-T05、P0-T08、P0-T20。
- 输入：T01TaskOutcome去重契约、T22九节点、真实PG/Celery/原生checkpoint。
- 文件：`experiments/phase0/task_scheduler.py`、`task_executor.py`、`db/migrations/006_tasks.sql`、`tests/phase0/test_task_coordination.py`。
- 步骤：登记TaskGraph/node/edge→依赖检查/claim+reserve→Graph推理子流程或固定Celery Job→结果先事实→state refs；标明每kind执行后端。
- 测试：100轮双scheduler、兄弟并行/失败传播/optional、Job重复/丢通知、Graph回退、cancel。
- 量化验收：未满足依赖提前执行0；每逻辑节点同epoch双领取0；旧事实不会被checkpoint覆盖；所有fixed作业都走Job port。
- 注入：结果入库后checkpoint失败、running lease过期、任务取消后Job迟到。
- 估计：2点。
- 风险/备选：分布式调度不成立→串行scheduler，不把Celery做第二套Agent状态机。
- 状态负责人：OrchestrationService唯一写Task；JobService唯一写Job，LangGraph唯一写checkpoint。
- 集成影响：DB/TaskExecutor port：是；引用现有迁移，不重复建Job/预算表。
- DoD：D0+ready/等待/结果对账时序与失败传播证据。

## P0-T24：Evaluator、重规划revision与产物复用

- 关联：PoC-07；PRD F-10、AC-E01/04/08/09；TDD [03 §4/6](../tdd/03_Task_Orchestration.md)。
- 目标：质量不足时有限重规划，复用有效成功产物且不重复副作用/预算。
- 依赖：P0-T23、P0-T20。
- 输入：T23可恢复DAG、coverage/evidence fixture、当前授权与剩余预算。
- 文件：`experiments/phase0/evaluator_replanner.py`、`tests/phase0/test_replanning.py`。
- 步骤：确定性硬规则+结构化评价→expected_revision CAS→旧执行节点安全收尾→保留兼容成功引用/新节点→次数/预算停止。
- 测试：100轮双Replanner/最后额度争抢，缺生效日期新增节点、source撤销/产物失效。
- 量化验收：revision只有一活动指针；已确认副作用重做0；未知用量预留不释放；最多2次重规划后停止；不匹配input/profile/policy不复用。
- 注入：切revision前后kill、旧revision结果晚到、评价错误/缺证据、预算不足。
- 估计：2点。
- 风险/备选：评价标准未标注→PARTIAL+人工评审；无法安全重规划则固定计划/取消，不能无限循环。
- 状态负责人：OrchestrationService管理revision/Task；BudgetManager管同一父run预算。
- 集成影响：TaskGraph/evaluation artifact schema：是；不得更改已完成历史节点。
- DoD：D0+复用/拒复用清单、预算与revision竞争证据。

## P0-T25：授权检索、真实Embedding与Hybrid对照

- 关联：PoC-09；PRD F-05、AC-02；TDD [04 §3](../tdd/04_LangGraph_Workflows.md)、[06 §6](../tdd/06_Knowledge_Ingestion.md)。
- 目标：同发布版本上验证pgvector/FTS/RRF和Rerank，避免另造摄取捷径。
- 依赖：P0-T13、P0-T19。
- 输入：T13发布索引、E5真实Embedding/Rerank或显式缺失、100QA标注语料/中文tokenizer配置。
- 文件：`experiments/phase0/retrieval_probe.py`、`tests/phase0/test_retrieval_permissions.py`、`evidence/retrieval-baseline.json`。
- 步骤：固定model/dimension→空间/ACL/active过滤→三模式TopK→RRF→可选Rerank/降级diagnostics→质量对照。
- 测试：100QA，跨space同内容/旧版本/混embedding拒绝；向量ANN过滤后召回。
- 量化验收：未授权/未发布命中0；100%结果有正确source/version；Recall@5/MRR/NDCG真实记录，候选0.85不擅升正式门槛。
- 注入：Rerank不可用、embedding数量/维度变化、源撤销、index切换读竞争。
- 估计：2点。
- 风险/备选：中文FTS差→评审tokenizer/BM25实验，不把两者混同；真实embedding不可用BLOCKED质量项。
- 状态负责人：RetrievalService只读授权活动索引，不写摄取/Run状态。
- 集成影响：SearchResult/citation公共schema：是；分数语义固定不相加异构原始分数。
- DoD：D0+三路对照/模型空间/硬件配置和限制，不只接口200。

## P0-T26：RAG Graph、引用与最终事件一致性

- 关联：PoC-09；PRD F-06、AC-03/06；TDD [04 §3/7](../tdd/04_LangGraph_Workflows.md)、[07 §5/6](../tdd/07_Data_API_Security.md)。
- 目标：真实LangGraph RAG输出可核验引用，partial stream不伪完成。
- 依赖：P0-T25、P0-T05、P0-T21。
- 输入：T25证据集、真实chat模型、T21预算/trace、100QA与无证据问答。
- 文件：`experiments/phase0/rag_probe.py`、`events_probe.py`、`tests/phase0/test_rag_events.py`、`evidence/poc09-report.md`。
- 步骤：授权→有限改写/检索/不足拒答→生成→引用校验→Message+Run+terminal一次提交；token先持久、generation reset。
- 测试：100QA、消息保存失败/重复finalize/取消；P0在线流与最终查询，内部事件过期边界与P1续传能力分开。
- 量化验收：伪引用/越权/无提交completed为0；50重复finalize仅一terminal；每答案有证据或不确定说明；引用准确率实际测量（0.95候选待评审）。
- 注入：部分token后DB失败、模型进程kill、新generation、游标过期/越权、Rerank故障。
- 估计：2点。
- 风险/备选：质量未达/阈值未批准→PARTIAL；不能丢弃失败题只报均分，不能自动重新执行Graph补SSE。
- 状态负责人：RunService/MessageService/EventStore在共享UoW，Graph只返回执行结果。
- 集成影响：事件/终态DB约束/API：是；不交付P1公共断线续传产品功能。
- DoD：D0+引用人工核验、最终事务故障和质量/延迟真实表。

## P0-T27：来源质量、去重与人工Review契约

- 关联：PoC-08；PRD F-08/10、AC-E03/04/05；TDD [06 §2/7](../tdd/06_Knowledge_Ingestion.md)、[07 §3/5](../tdd/07_Data_API_Security.md)。
- 目标：从受控快照到可审核候选，硬拒绝不被分数覆盖。
- 依赖：P0-T11、P0-T16、P0-T24、P0-T03。
- 输入：许可控制站、fixture变更/冲突/转载/PII、T24结构化评价、T01来源审核命令。
- 文件：`experiments/phase0/candidate_review.py`、`db/migrations/007_candidates.sql`、`tests/phase0/test_candidate_review.py`。
- 步骤：采集metadata→质量/许可硬规则→hash去重/近重提示→逐事实evidence→来源审核；不直接写向量。
- 测试：30任务候选样本、两空间审阅/批次幂等、冲突保留/许可不明转审/无证据拒绝。
- 量化验收：禁采/无证据自动批准0；已批准每关键事实≥1有效证据；重复正文保留各source；ReviewDecision不能作工具Approval。
- 注入：伪reviewer、source批准后撤销、同key改KB、注入文本要求扩大scope。
- 估计：2点。
- 风险/备选：近重/质量未校准→仅提示+人工审阅，不自动合并/发布。
- 状态负责人：SourceService管来源状态，ReviewService追加ReviewDecision；两者共享用例事务但不代写ToolCall。
- 集成影响：候选/evidence/review公共schema：是，和T01批准字段提议一致。
- DoD：D0+候选/审核/证据完整性清单，发布入口必须只接有效ReviewDecision。

## P0-T28：Research 全链控制站集成

- 关联：PoC-08；PRD F-10/12、AC-E01–AC-E10；TDD [04 §5](../tdd/04_LangGraph_Workflows.md)、[06 §2/7](../tdd/06_Knowledge_Ingestion.md)。
- 目标：目标→DAG→搜索/HTTP/JS/用户浏览器→质量/去重/审阅→Job→发布→RAG引用；不新写各子系统。
- 依赖：P0-T27、P0-T18、P0-T26、P0-T13、P0-T21。
- 输入：所有前置强制验收证据、30研究任务、测试账号、费用/域名授权；缺真实BSK不能全链PASS。
- 文件：`experiments/phase0/research_probe.py`、`tests/phase0/test_research_e2e.py`、`evidence/poc08-report.md`。
- 步骤：组装已有port→人工interrupt审核→统一摄取→Job回传→RAG证据→第二轮来源不变/变更与报告；搜索用本地固定适配注明限制。
- 测试：30任务覆盖正负路径和三读取通道；预算/取消/撤权/重复通知/富化失败，最终引用回源验证。
- 量化验收：已发布候选100%有快照/时间/通道/许可/证据/审核；禁止/越权/无证据发布0；精确重复不重索引；两轮变化/未变化状态正确；任务成功率分类实报。
- 注入：审核后撤权、批准后DB/Redis故障、浏览器断线、旧版本晚到、最后预算竞争/秘密canary。
- 估计：2点，仅集成；若重写子系统立即拆返相应任务。
- 风险/备选：任关键前置FAIL/BLOCKED→本任务BLOCKED，不绕过；仅公开HTTP/JS通过记PARTIAL，禁假称BSK成功。
- 状态负责人：Research Graph管控制流；Source/Review/Task/Job/Index/Run各归既有服务。
- 集成影响：跨port集成：是；无新通用框架/生产周期调度/ Wiki。
- DoD：D0+三通道/审核/入库/引用全链证据、残余风险及P1范围建议。

## P0-T29：P0 全链授权与内容安全回归

- 关联：PoC-01/02/03/04/06/09；PRD AC-02/06、F-08/16；TDD [07 §4/8](../tdd/07_Data_API_Security.md)、[08 §4–6](../tdd/08_Test_Strategy.md)。
- 目标：将已建组件的空间/secret/PII/网络负例汇成P0门禁，不实现生产安全平台。
- 依赖：P0-T06、P0-T08、P0-T13、P0-T21、P0-T26。
- 输入：双空间/无RunJob、受控HTTP、对象/引用/SSE、日志/trace/state/模型请求可审计sink。
- 文件：`tests/phase0/test_security_gate.py`、`evidence/security-matrix.md`；仅已有实验组件的必要修复。
- 步骤：按资源/动作矩阵逐级越权→撤权恢复→签名代理/游标→SSRF控制站→prompt注入不能扩权→canary全sink扫描。
- 测试：≥100空间/动作探测、≥20HTTP网络向量、secret和PII两类canary，所有P0组件错误路径覆盖。
- 量化验收：测试集越权成功/禁止出站到达/秘密PII进入log-trace-state-model为0；人工审核不能越权；生产身份未验明确不宣称上线完成。
- 注入：伪payload/header、角色撤销、RLS连接复用、恶意引用/object key、SDK异常回显。
- 估计：2点，复用各任务已有负例，缺口必须列新任务不临时大改。
- 风险/备选：安全失败→FAIL停线并回归修复；关闭不安全通道，不以性能理由豁免。
- 状态负责人：安全测试不直接写业务状态；命令经各owner服务。
- 集成影响：既有公共port不可暗改；发现契约问题回T01建议流程。
- DoD：D0+逐项通过/失败/未测矩阵、canary扫描范围与安全残余风险。

## P0-T30：备份恢复、迁移和版本回滚演练

- 关联：PoC-01/02/03/06/09；PRD G5、AC-05/AC-E09；TDD [04 §8](../tdd/04_LangGraph_Workflows.md)、[08 §7](../tdd/08_Test_Strategy.md)。
- 目标：验证业务/checkpoint/对象关联恢复与失配时安全停止；不承诺未定RPO/RTO。
- 依赖：P0-T05、P0-T08、P0-T13、P0-T21。
- 输入：真实PG/对象快照、active版本/未完成审批/已写外部receipt、旧Graph/schema fixture。
- 文件：`experiments/phase0/restore_probe/`、`tests/phase0/test_restore_upgrade.py`、`evidence/restore-runbook.md`。
- 步骤：冻结出站/调度→备份catalog/checksum→隔离恢复→stable ID与引用核验→旧schema拒绝或旧host路由→索引显式CAS回退。
- 测试：空环境恢复、对象缺失、业务/CP时间点不匹配、外部写发生但账本回退、旧schema不兼容。
- 量化验收：恢复前外部写0；对象/ID必需关联100%校验；未知写自动重放0；不兼容checkpoint不静默重开；真实恢复时长/数据损失窗口有原始记录。
- 注入：恢复不完整对象、迁移中断、旧saver版本、账本落后于外部计数。
- 估计：2点，首次最小备份/恢复；完整PITR容量/SLA超出则单列后续任务，不虚报通过。
- 风险/备选：无法安全自动恢复→人工runbook/保留旧host并限制写，重新评ADR-0001/03与TDD升级策略。
- 状态负责人：运维只执行受控恢复，恢复后仍由原服务写事实；不得用SQL随意“修好”审批成功状态。
- 集成影响：migration/graph_version/对象manifest：是；恢复环境硬隔离。
- DoD：D0+可执行runbook、真实恢复证据和未验证SLA清单。

## P0-T31：P0 核心出口证据评审（文档）

- 关联：PoC-01/02/03/04/06/09；PRD AC-01–AC-06；TDD [08 §8](../tdd/08_Test_Strategy.md)。
- 目标：评估P0验证是否就绪，不把六项PoC的partial当作完整Phase0完成。
- 依赖：P0-T26、P0-T29、P0-T30。
- 输入：核心所有强制测试证据、依赖锁/许可、C-01–C-10修订建议/处置、质量阈值评审记录。
- 文件：`docs/poc/evidence/P0_Core_Gate_Review.md`、IMPLEMENTATION_STATUS；不改ADR状态。
- 步骤：核每scenario命令/hash→核失败/阻塞/残余风险→分别评价G-P0每门→列TDD/ADR变更建议/生产前待办。
- 测试：证据链接/版本/计数一致性、未执行/跳过未被算通过；无需新运行代码。
- 量化验收：G-P0所有项100%有结论和证据；0未验证项伪标PASS；未校准质量阈值不能质量PASS；安全失败零豁免。
- 注入：评审故意缺一条证据/更换lock hash，必须拒绝门禁而非静默沿用。
- 估计：1点。
- 风险/备选：核心门禁不满足→明确PARTIAL/FAIL/BLOCKED与最小补测任务；不能宣布P0实现/上线。
- 状态负责人：技术负责人只写门禁结果，ADR接受由后续正式决策，不在本任务自动升级。
- 集成影响：文档门禁：是；无公共接口/DB改动。
- DoD：D0文档版+可审查P0准入建议和剩余产品/身份/许可门禁。

## P0-T32：全量 Phase 0 / P1 前置证据评审（文档）

- 关联：PoC-01/02/03/04/05/06/07/08/09；PRD F-09–F-16、AC-E01–AC-E10；TDD [08 §8](../tdd/08_Test_Strategy.md)。
- 目标：核九项PoC全量退出，尤其真实双浏览器和研究闭环，不提前交付P1产品。
- 依赖：P0-T28、P0-T18、P0-T24、P0-T31。
- 输入：T31核心结论、BSK/Playwright真证据、DAG/Research、安全/预算/许可和性能未决。
- 文件：`docs/poc/evidence/Phase0_Full_Gate_Review.md`、IMPLEMENTATION_STATUS；仅列ADR状态建议。
- 步骤：复核九结果与子路线→P1安全矩阵/数据许可→ADR重审/降级清单→明确完整/受限准入区别。
- 测试：证据与Gate一致性，缺真实扩展的状态不得PASS；无需重启PoC。
- 量化验收：九PoC每项5态唯一结果且理由/证据齐全；G-FULL未满足不标完成；受限范围必须写禁用功能、接受者和后续任务。
- 注入：资料评审替换BSK mock结果为real声明，应被证据检查拒绝。
- 估计：1点。
- 风险/备选：设备/协议/许可仍阻塞→保持相关BLOCKED/PARTIAL、限定公开只读范围提议，不自动改PRD/ADR。
- 状态负责人：技术负责人写门禁评审；各PoC负责人对证据负责。
- 集成影响：范围/文档门禁：是；无代码/DB改变。
- DoD：D0文档版+全量出口结论、ADR重审建议、剩余风险和明确下一任务。

## 本轮结果声明

上述文件均为预计修改范围，不是在本轮生成的代码。任务复杂度/门槛是规划建议，执行前需锁环境与证据；实际状态始终以运行记录为准。单会话失败就记录失败，禁止跨任务绕过依赖“顺手做完整框架”。
