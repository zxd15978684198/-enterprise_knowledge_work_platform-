# Phase 0 ADR 就绪度与设计一致性评审

日期：2026-10-27。范围：只读评审与验证规划；未运行 PoC。当前主仓库 `master` / `feb7b4a30e41a904a48b42d971d4d0844f1cf6a3`；工作区原有 PRD F-14→P1 修改保留，未作为本轮改动。参考源码 `662abaf9f944e37689d04c42160034b2cdec6829`，工作树干净。

## 1. 基线、证据和状态规则

已核对 [AGENTS](../../AGENTS.md)、[PRD](../PRD_PyKnora.md)、[实施状态](../IMPLEMENTATION_STATUS.md)、[源码分析](../research/01_Source_Architecture.md)、[架构映射](../research/02_Architecture_Mapping.md)、[兼容风险](../research/03_Compatibility_Risks.md)、八份 [TDD](../tdd/README.md)、全部五份 [ADR](../adr/README.md)。A=本地源码静态事实，D=官方资料能力描述，N=本轮建议，U=尚待验证。没有已有业务运行基线。

本轮只在此处给出**推荐状态**，不编辑原 ADR。原文“提议”规范化读作 `Proposed`。允许状态为 `Accepted / Proposed / Blocked / Superseded`；PoC 结果使用另一组枚举，不能混用。`Blocked` 表示决策所需外部事实无法取得，不因“还没开始实验”将全部 ADR 标为 Blocked。明确责任分离可继续作为用户给定设计基线，但复合 ADR 内未验证的部署/恢复能力不能因此自动 Accepted。

## 2. 设计一致性检查

结论：职责总体一致，足以分解验证任务；发现以下局部契约缺口，尚不能直接作为生产迁移/恢复实现定稿。它们不是已测 bug。本轮不改旧 TDD，以任务 P0-T01 提出最小修订包，由后续单独评审决定。

| 检查项 | 证据与判断 | 所需澄清/验证及阻断点 |
| --- | --- | --- |
| 1 职责重叠 | TDD01 §2/3、02 §1/8：Graph 管执行，Runtime 为门面，CheckpointManager 仅适配；设计原则一致 | P0-T19/T21 做依赖边界检查，不得添加快照引擎/interrupt broker；R-10 |
| 2 同一事实多个写入者 | TDD01 §5 指 JobService 唯一写 jobs；TDD06 §6 发布事务又含 Job succeeded，接口尚未说明如何共同提交 | **C-01**：P0-T01 明确 JobService 提供同一 UnitOfWork 内转换命令，由发布用例编排；Worker/IndexPublication 不直接绕过拥有者写 Job；阻塞 T03/T12/T13 公共契约定稿 |
| 3 DAG/Graph 状态一致 | TDD03 §3/5/6、04 §5：Task 表为事实，state 为引用；方向一致 | **C-02**：TaskOutcome 唯一键已描述，但 TDD07 没有结果/事实去重字段完整 DDL；等待审批/Job 的 Task 状态转移入口需写清。T01 提议补字段/命令，不新增推理状态机；T23/T24 验证 |
| 4 Celery/checkpointer 恢复 | TDD01 §6.4、04 §7/8 已说明独立连接和单 owner 风险 | **C-03 / 高**：session lock 释放不自动 fence 独立 checkpoint 连接；owner_epoch/lease_epoch 命名映射也需明确。T05 必测锁连接断开但 saver 仍活、旧 writer 重连；未通过则单活/阻止接管，不能只靠 lease 宣称安全 |
| 5 审批副作用重放 | TDD02 §4/5 有 ledger/unknown_outcome；机制合理，外部幂等能力 U | **C-04**：tool intent 来源为持久化模型响应/序号，但模型响应产物绑定和 replay key 细节未定；T01 固定 response artifact+ordinal+args hash，T06 测外部成功/receipt 未落窗口；不改 tool_call_id |
| 6 暂存索引暴露 | TDD06 §5/6 以 active index+winning attempt 查询，未见设计反向承诺 | T12/T13 在所有检索入口做并发可见性探针，旧 attempt 不得写 winning partition；PG 同事务域为硬假设，换外部搜索需重评 ADR-0002 |
| 7 浏览器抽象过度 | TDD05 §3/5/6 已有差异/unsupported/epoch；抽象方向正确 | **C-05**：尚无 pinned upstream 实现证据；不能假定其支持每跳子资源拦截/epoch 注入。T14 先审协议，再选桥接；不足则关闭动作/BrowserSkill 通道，T17/T18 才可真实验收 |
| 8 异步权限传播 | TDD07 §4 要求 Job 恢复原主体，但 jobs 表没有执行 principal/policy binding；run_id 可空，fetch/ingestion Job 无法总从 Run 还原 | **C-06 / 高**：T01 提议不可变 execution binding（workspace、principal、policy ref、资源范围、父上下文）与 jobs FK；不把旧 snapshot 当当前权限。T03/T08/T29 验证无 Run 作业、撤权、篡改 payload；阻塞安全摄取 |
| 9 API/模型/状态机 | TDD06 §2 URL 初始 202 先 source/fetch；TDD07 review endpoint 仅 collection candidate P1，而 P0 URL 可 review_required=true | **C-07**：T01 提议明确 P0 来源版本审阅端点/命令和结果查询，不借 P1 candidate ID。身份全局 User→空间内 Principal、review_decision_id 与 PRD §9.9 approval_id 差异已显式列出，但仍需范围决策；阻塞相应生产 API，不阻塞隔离测试主体 |
| 10 幂等/事务/失败 | TDD01/02/06/07 大部分边界清楚；Outbox 只有字段/派发描述，缺完整枚举/死信协议 | **C-08**：本轮 PoC-02 规定拟验证状态和重放顺序；**C-09**：相同 profile reprocess 与 failed/cancelled Job 的再尝试命令需明确：复用版本不能等于永久复用 failed 响应。T01 提议同版本新受控 Job attempt，不重用过期批准；T08/T13 实验 |

额外记录 **C-10**：assets 与 SourceVersion/DocumentVersion 的 owner FK 存在创建环；TDD07 仅对部分文档指针提示 deferred FK。T01 提议一个权威 asset_bindings 关系、可延迟 FK/事务插入顺序，T03/T12 用真实 PG 验证，不在本轮重构模型。所有 C-01–C-10 的解决建议都需回到 TDD 对应章节，不能只在 PoC 代码中暗改接口。

## 3. 状态拥有者复核基准

| 事实 | 唯一逻辑写入者 | 可触发命令但不可直接写者 |
| --- | --- | --- |
| Run / terminal event | RunService（EventStore 在同 UoW 追加事件） | Graph host、API、reconciler |
| Graph checkpoint/interrupt | LangGraph 原生 checkpointer/runtime | Harness、Celery、API 均不得自写 checkpoint |
| Task DAG/node/revision | OrchestrationService | Research Graph 调度入口、TaskExecutor 回传事实 |
| Job/attempt | JobService | Worker、Outbox dispatcher、发布用例在共享 UoW 调服务 |
| Outbox lease/投递状态 | OutboxService | Dispatcher/巡检通过该服务 CAS |
| Approval / ToolCall | ApprovalManager / ToolExecutor 各管本表，在单 UoW 协同 | API 决定命令、Graph 等待/恢复，不代写两表 |
| BrowserSession/owner | BrowserSessionService | adapter 产生连接事实，Graph 只保存 ref |
| 文档摄取 / active index / enrichment | IngestionService / IndexPublicationService / EnrichmentService 各管本域 | Job 完成事实经用例服务协调；富化失败不覆盖核心成功 |

“同一事务写多个域”并不等于多个域争抢同一状态；共享 UoW 允许各拥有者一次提交，需 T01 定稿端口后实验。事务不含远端模型/工具/对象 I/O。

## 4. 五份 ADR 逐一评审

### ADR-0001：运行时边界

- 决策目标：分离推理执行、业务事实、能力和后台可靠运行。
- 当前建议：四 Graph+薄 Harness+按需 DAG+Celery 唤醒；PG 事实/checkpointer 分域；run/thread 1:1。
- 合理替代：常驻 Graph Runner 代替 Celery graph 队列宿主，仍用 LangGraph；不选择自研 checkpoint。
- 选择依据：用户规范、PRD §7/10/13、审计 A14–A18/A23/R01；框架已经提供中断，不应重复。
- 性能：Celery 启动/排队可能增加交互延迟；独立队列能防解析堵塞。复杂度：双域对账；维护性：薄 port、旧 Graph 版本路由较可控。
- 风险与未验证假设：C-03 单 writer、saver 连接失效及宿主取消语义；thread 每 run 隔离下多轮摘要成本；U。
- 是否需 PoC：是，PoC-01/06/07；边界原则本身可建议评审接受，但不拆分修改本 ADR。
- 推荐状态：**Proposed**。通过单 writer/恢复门禁后再评审整体；保持原文状态不变。失败→单活 Graph Runner，若更换部署需修订 TDD01/04。

### ADR-0002：Outbox 与索引版本

- 决策目标：DB 提交不丢作业、重复投递无重复可见发布、旧版本不覆盖新版本。
- 当前建议：事务 Outbox、至少一次派发、Job CAS/lease、不可变版本 staging、同 PG 原子 active pointer+winning attempt；富化分离。
- 替代：CDC 推送已提交 Outbox，减少轮询；外部搜索引擎采用单独版本别名发布协议，不能沿用 PG 原子性承诺。
- 依据：AC-04/05；原 I02 为直接 Enqueue，I07/I10 不能证明新机制。PG 事务可覆盖事实与本地索引，选择面较小。
- 性能：多版本写放大/存储开销、轮询延迟；复杂度：GC、重试、对账、延迟 FK；维护性：独立 producer/consumer port 易替换 broker。
- 风险/假设：C-01/06/08/09/10，broker ACK、锁竞争、死信处理、对象完整性、PG 查询一致 snapshot 均 U。
- 是否需 PoC：必须 PoC-02/03，权限和对象故障贯穿。
- 推荐状态：**Proposed**。失败→先停发布/单文档串行/显式人工重试；若 CDC/替换队列，重新评审而非静默更换。

### ADR-0003：审批与副作用

- 目标：有效批准才能执行，恢复不导致未经确认重复外部写。
- 当前建议：审批 hash/主体/工具版本/有效期绑定；原生 interrupt；执行 ledger+receipt，未知结果人工核查。
- 替代：连接器只读上线，关闭写动作；支持外部幂等 key/查询的连接器可缩小未知窗口，但仍不能保证所有下游恰好一次。
- 依据：PRD §14/AC-E09、审计 A16–A18 的崩溃窗口；恢复节点可能重入。
- 性能：多次本地短事务；复杂度：核查和人工操作；维护性：每个 connector 明确 effect/idempotency/reconcile 能力，优于隐式统一 retry。
- 风险/假设：C-04；外部 key 保留期、查单真实性、批准与发请求间撤权窗口、未知计费；U。
- 是否需 PoC：必须 PoC-01，PoC-06/05 回归。
- 推荐状态：**Proposed**。未验证写 connector 禁用；未知结果不自动重发。HITL 原则可继续设计，不等于写恢复已经可用。

### ADR-0004：解析与浏览器复用

- 目标：复用成熟解析/用户浏览器能力，同时控制跨平台、授权与产物一致性成本。
- 当前建议：DocReader 服务+TXT/MD 简单适配；Browser 双 port，先审 pinned upstream，再验证 Go bridge 与 Python 直连取舍。
- 替代：独立 Python parser 库组合替换缺陷格式；Playwright-only 服务端公开读取，BrowserSkill 在协议/许可就绪后再启用。
- 依据：A/C01/C06、B05/B07/B11；原 Python 不覆盖 simple 全格式，daemon/extension/网关独立。固定构建输入不是互通结果。
- 性能：gRPC/容器和图片存储开销；复杂度：浏览器桥接增加语言/部署面；维护性：独立版本矩阵、窄能力交集优于万能 browser action。
- 风险/假设：DocReader locator/stream、许可/权重、BSK 真源码/协议缺口、Windows daemon 与子资源策略 U；读取与网络治理须分层。
- 是否需 PoC：必须 PoC-04/05；R-13 清单先于真实运行/分发。
- 推荐状态：**Proposed**（真实材料未就绪不等于已尝试失败）；若 T09/T14 无法取得所需材料，该子路线记录 BLOCKED，整体 ADR 再评审是否 Blocked。不得因此阻止 P0 TXT/MD 或 Playwright 的独立验证。

### ADR-0005：空间、事件与隐私

- 目标：检索/引用/对象/恢复全链授权，最终结果与终态事件一致，秘密不外泄。
- 当前建议：显式 workspace、复合 FK、可信 Scope、当前权限再验、RLS 纵深、PG 事件先持久后发，敏感内容代理。
- 替代：tenant database/schema 物理隔离提升隔离强度但显著增加迁移成本；Redis-only SSE 更轻却无法满足相同持久语义，不作为等价方案。
- 依据：AC-02/03/06，SEC-01/OBS-01；SSE/Graph/业务恢复不同。
- 性能：权限与事件写放大；复杂度：身份域例外、RLS 连接清理、TTL；维护性：统一 Scope 与事件 schema 可测试。
- 风险/假设：C-06/07、PRD 全局 User 与空间内 Principal 例外，当前权限读取、PII 脱敏及签名撤销边界 U。
- 是否需 PoC：必须 PoC-01/02/03/06/09 中的安全场景，汇总 T29/T30；身份域是产品/架构确认，不能靠 PoC 决定合法范围。
- 推荐状态：**Proposed**。最小权限原则依据充分，但复合 ADR 的身份与事件机制仍未验证。RLS 不可用时保持应用授权/复合 FK 并显式评审降级，不能假装纵深已验证。

## 5. 待确认项与进入实验条件

| 项目 | 谁负责/何时 | 处理方式 |
| --- | --- | --- |
| C-01–C-10 契约修订包 | 架构负责人，P0-T01 | 只产最小建议/影响/验收，后续获授权后改 TDD；实验可使用有标签的 provisional schema，生产定稿前必须合入评审结论 |
| 主体与全局身份例外、来源审核字段/API | 产品+安全+架构 | 隔离实验用合成双空间主体，不冒充生产身份通过；P0 生产阻断项 |
| Wiki P1 | 产品 | 当前未提交 PRD 已对齐，保留并等待正式基线；本轮不实现 Wiki |
| 精确发行版/镜像/CVE/许可 | P0-T02/T09/T14 | 不安装 latest 猜兼容，不将 WeKnora MIT 代替第三方许可 |
| 数据权利/站点/模型/计费/审批核查角色 | 产品+测试负责人 | 本地自有 fixture 可先行，真实模型/浏览器前必须授权和限额 |

官方资料本轮只读核对：[LangGraph interrupt](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Celery Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)、[PG 锁](https://www.postgresql.org/docs/current/explicit-locking.html)、[Playwright context](https://playwright.dev/python/docs/browser-contexts)。它们支持组件职责分析，不证明本方案跨进程行为。精确版本由 T02 固定后复核。全部 PoC 当前 `NOT_STARTED`，门禁见 [验收](05_Phase0_Acceptance.md)。

本轮尝试只读打开 pinned [BrowserSkill upstream 树](https://github.com/Tencent/BrowserSkill/tree/da6bf4eed2dd7256567e152df8c903c87f6598c3)，工具返回 restricted URL/无法抓取，未获得独立上游源码；不能据此断言仓库不存在或协议不兼容。本地只复核了WeKnora接入层/daemon启动/构建pin，T14仍须取得真实资料。此资料获取限制属于规划证据缺口，不是本轮已经启动PoC-05。
