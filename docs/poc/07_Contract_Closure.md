# P0-T01 实验契约与设计缺口闭合

日期：2026-10-27（Asia/Shanghai）。起始分支 `master`，HEAD `5e7e3998ff553edb7a75123f4bea8539b9453f6a`，工作区干净。本任务仅定义文档契约；没有安装依赖、启动服务、实现代码或执行 PoC。历史评审中的未提交 PRD 描述属于历史上下文，Wiki P1 已在当前基线中，不再作为待提交事项。

## 1. 适用范围与状态

输入：[AGENTS](../../AGENTS.md)、[PRD](../PRD_PyKnora.md)、[实施状态](../IMPLEMENTATION_STATUS.md)、[就绪度评审](00_ADR_Readiness_Review.md)、[风险登记](01_Technical_Risk_Register.md)、[PoC 计划](02_Phase0_PoC_Plan.md)、[Backlog](03_Phase0_Task_Backlog.md)、[依赖 DAG](04_Phase0_Dependency_DAG.md)、[验收与故障矩阵](05_Phase0_Acceptance.md)。下文全部是本项目设计约束或待验证提议，未重新审计参考源码、未确认第三方当前版本/协议能力。

决策状态仅使用：`DEFINED`（实验接口契约已定义，未证明运行成立）、`NEEDS_POC`（实现假设必须实验）、`BLOCKED`（必要外部条件或设计决策缺失）、`DEFERRED`（明确不在本阶段）。本轮没有 DEFERRED 的缺口。一个条目的状态突出其主要剩余门禁；DEFINED 仍必须完成对应测试、TDD 评审及生产准入。它们不替代 PoC 五态，九项 PoC 仍全部 NOT_STARTED。

“闭合”指十项都有可执行的实验边界、唯一逻辑写入者、验证/决策去向，绝不表示所有风险已解决。接口名为应用自有 port 草案，不是第三方 API；原 PRD/TDD/ADR 内容与状态保持不变。后续公共实验 schema 使用本文件的明确补充，标注 provisional；若与原设计冲突必须留评审记录，不能直接迁入生产。

| 缺口 | 处理结果 | 决策状态 | 验证任务 | 生产阻断范围 |
| --- | --- | --- | --- | --- |
| C-01 | 共享 UoW 中由各域服务更新，发布不直写 Job | DEFINED | P0-T03/T12/T13 | 是，P0/P1 发布一致性 |
| C-02 | Task 事实与 Outcome 去重、等待/恢复命令明确 | DEFINED | P0-T23/T24 | 是，P1 DAG；普通 RAG 不依赖 |
| C-03 | epoch 映射与接管禁令明确，独立 saver fencing 未证实 | NEEDS_POC | P0-T05/T06/T30 | 是，Graph 安全恢复/多实例 |
| C-04 | 持久模型响应定位工具意图，审批/receipt 提交顺序明确 | NEEDS_POC | P0-T06/T19/T21 | 是，敏感工具与副作用恢复 |
| C-05 | 自有能力契约与 upstream 协议分开，未知能力拒绝 | NEEDS_POC | P0-T14/T15/T17/T18 | 是，P1 BrowserSkill；独立 P0 不受其专有通道阻塞 |
| C-06 | 无 Run Job 的不可变 execution binding 与重新授权明确 | DEFINED | P0-T03/T08/T29 | 是，所有异步授权 |
| C-07 | P0 来源审阅草案可实验，身份/API/字段范围待决定 | BLOCKED | P0-T03/T11/T13/T27/T29 | 是，生产身份与 P0 URL 审阅入口 |
| C-08 | Outbox 五态、死信、同消息重派发明确 | DEFINED | P0-T07/T08 | 是，可靠后台作业 |
| C-09 | 自动 attempt 与终态新 Job 重试区分，取消不复活 | DEFINED | P0-T08/T12/T13 | 是，摄取重试/重处理 |
| C-10 | asset_bindings 为唯一归属关系，延迟 FK 与插入顺序待验 | NEEDS_POC | P0-T03/T11/T12/T13 | 是，资产绑定与版本发布 |

## 2. 公共事务、身份和状态所有权

UoW 指同一 PG 业务连接/事务，由应用用例提交或回滚；参与服务只执行命令，不私自 commit，不各开独立事务。网络、模型、对象存储、Redis、LangGraph checkpointer 均不加入该事务。每条更新使用期望状态/row_version，涉及执行者还检查 epoch、generation、当前资源权限；失败整笔回滚。应用用例协调多个域不取得这些域的写入权。

| 事实/状态 | 唯一逻辑写入者 | 可发起命令者与限制 |
| --- | --- | --- |
| 当前授权、membership、execution binding | 授权服务（AuthorizationService） | API/Worker 请求授权；模型、队列 payload、checkpoint 不构造可信 Scope |
| Run、owner_epoch、业务终态 | RunService | 宿主/reconciler 提交 fact；EventStore 同 UoW 追加唯一事件 |
| checkpoint、interrupt、pending writes | LangGraph 原生 runtime/checkpointer | 宿主仅调用原生接口；Harness 不实现第二套恢复引擎 |
| Task revision/node/Outcome | OrchestrationService | Executor/scheduler 提交事实，不能从旧 state 覆盖事实表 |
| Job、attempt、lease_epoch | JobService | Worker/发布用例通过命令写入 |
| Outbox 状态、派发租约 | OutboxService | dispatcher/reconciler/redrive 走 CAS 命令 |
| Approval / ToolCall | ApprovalManager / ToolExecutor（分别唯一） | 单 UoW 消费审批和预登记执行；批准 API 不调用外部工具 |
| 已规范化模型响应及模型步骤绑定 | ModelGateway | prepare_calls 读取已提交响应；不从 checkpoint 丢失推导模型需重跑 |
| BrowserSession、设备绑定与 owner | BrowserSessionService | adapter 上报事实；ToolCall 保存动作结果 |
| SourceVersion 生命周期、ReviewDecision | SourceService | 审阅用例授权后调用；审核记录追加，不原地改历史 |
| DocumentVersion 摄取状态 / 索引活动指针 / 富化状态 | IngestionService / IndexPublicationService / EnrichmentService（各域唯一） | 发布用例共享 UoW 调用三域及 JobService，不跨域直接 SQL |
| 资产对象元数据与 asset_bindings | AssetService | Source/Ingestion 用例在同 UoW 请求绑定；版本服务仅维护自身版本引用 |

API 幂等通则沿 TDD07：`UNIQUE(workspace_id,principal_id,operation,idempotency_key)` 绑定 request_hash；相同 key 改参 `IDEMPOTENCY_CONFLICT`，重放已登记响应前仍校验当前权限。业务唯一键长期保护，不依赖 API 键建议保留期。本文新增字段/命令均为最小修订建议，尚无 DDL 或 OpenAPI 实现。

### C-01：发布事务中的 Job 唯一写入者

1. **问题/模块**：TDD01 指 JobService 管 Job，TDD06 T3 又需同时提交发布与 Job 成功；缺少共享事务端口。涉及 ingestion/index/jobs/outbox。
2. **关联**：[TDD01 §5/6](../tdd/01_System_Architecture.md)、[TDD06 §5/6](../tdd/06_Knowledge_Ingestion.md)、[TDD07 §3](../tdd/07_Data_API_Security.md)、[ADR-0002](../adr/0002_Outbox_Version_Publication.md)。
3. **唯一写入者**：Job/attempt 仅 JobService；摄取仅 IngestionService；build/活动指针仅 IndexPublicationService；富化/Outbox 各自服务。PublicationUseCase 只是事务协调者。
4. **接口/字段**：`publish(ctx, manifest)` 用例调用 `JobService.assert_current(uow, job_id, attempt_id, lease_epoch)`、`IndexPublicationService.activate(uow, expected_generation, expected_desired_version, manifest)`、`IngestionService.mark_succeeded(uow, document_version_id)`、`JobService.complete(uow, result_refs)`；各命令实际均携可信 ctx。字段含 workspace、document/version/index_version、current_attempt_id、winning_attempt_id、manifest checksum、row_version。富化登记和 indexed 事实也在该 UoW。
5. **权限传播**：按 C-06 binding 重建 Scope，再检查来源审核、KB 写、版本/对象归属；客户端 workspace 不是授权依据。
6. **提交点**：staging 构建完成不可见；T3 一次提交 active version+winning attempt、核心 succeeded、Job/attempt succeeded、富化 Job/Outbox。旧 active→retired 也在 T3；提交前任何失败全部回滚。对象预先写入并验证，不在锁内做远端 I/O；完整性和对象保留假设由 T12/T13 验证。
7. **幂等/约束**：build 键 `(workspace,document_id,index_version)`；chunk 键含 attempt_id；发布事实键 `(workspace,document_id,index_version,attempt_id,'activate')`。同 winner 重入读原结果，其他 attempt 不得将其转成自己的成功。
8. **失败/恢复**：T3 回滚保留旧 active；提交后 ACK/checkpoint 丢失读已提交结果；旧 epoch→STALE_ATTEMPT，旧 desired→VERSION_SUPERSEDED。JobService 与摄取服务按有效的新事务记录失败/失效事实，不能让失败事务局部成功。富化失败不反转核心成功。
9. **验证/门禁**：P0-T03/T12/T13；F2/F6/F7/F8/F14。并发读取必须同时 join active 指针、winning_attempt、ACL/tombstone/来源有效性；查询不能只凭 index_ready。**阻塞生产：是。状态：DEFINED**，原子发布运行假设仍未验证。

### C-02：TaskOutcome 与等待状态事实

1. **问题/模块**：TaskOutcome 的逻辑去重未落实到完整字段，等待审批/Job 的写入口不明；模块 orchestration/Research。
2. **关联**：[TDD03 §2/3/5/6](../tdd/03_Task_Orchestration.md)、[TDD04 §5/7](../tdd/04_LangGraph_Workflows.md)、[TDD07 §3](../tdd/07_Data_API_Security.md)、[ADR-0001](../adr/0001_Runtime_Boundaries.md)。
3. **唯一写入者**：OrchestrationService 写 task_nodes/task_outcomes/revisions；Run 状态仍经 RunService；Job/Approval 各自 owner。
4. **接口/字段**：`record_wait(ctx, task_key, expected_row_version, job_id|approval_id, fact_key)`、`resume_wait(ctx, task_key, wake_fact_key)`、`accept_outcome(ctx, lease, outcome)`。task_key 固定 `(workspace_id,run_id,task_graph_revision,task_id)`。拟补 `task_outcomes`：outcome_id、task_key、input_hash、lease_epoch、status、typed artifact_refs、job_id?、approval_id?、error_code?、usage_ref、outcome_hash、committed_at。waiting_job 必有 job_id、waiting_approval 必有 approval_id，两者不可混用；等待引用也存 task_nodes。
5. **权限传播**：重读可信 Run/binding、current_revision、主体和产物/工具权限；checkpoint 中 status 仅关联。复用旧产物也重新授权。
6. **提交点**：claim+预算 reserve+必要 Job/Outbox 一事务；等待事实与对应新审批/Job 登记通过各 owner 同事务。恢复读取已提交审批/Job，CAS 等待→running；有效终局 Outcome+node 终态+业务 wake Outbox 同事务，checkpoint 随后独立保存。
7. **幂等/约束**：终局 Outcome `UNIQUE(workspace,run,revision,task_id,input_hash)`；等待/唤醒事实另以 `(task_key,fact_key)` 唯一，不占终局槽位。相同键不同 outcome_hash→REVISION_CONFLICT；失败尝试另记 attempt_count/诊断，不抢占最终 Outcome。租约过期的新执行必须新 epoch。
8. **失败/恢复**：丢 checkpoint 重读表计算 ready；旧 revision/epoch 不覆盖当前节点；重复 Job 完成只唤醒一次逻辑执行；取消保留事实，未知副作用先核查，禁止重规划绕过。失败依赖 blocked，不伪造 succeeded。
9. **验证/门禁**：P0-T23/T24，F7/F17。**阻塞生产：P1 DAG 是，普通 RAG 否。状态：DEFINED**，并发调度与重规划仍待实验。

### C-03：Checkpoint 单写者与 Fencing

1. **问题/模块**：advisory lock 连接释放不能自动 fence 独立 saver 连接；模块 WorkflowHost/checkpointer。
2. **关联**：[TDD01 §6.4](../tdd/01_System_Architecture.md)、[TDD04 §7/8](../tdd/04_LangGraph_Workflows.md)、[ADR-0001](../adr/0001_Runtime_Boundaries.md)。
3. **唯一写入者**：RunService 写 Run owner；checkpoint 只能 LangGraph 原生 writer。CheckpointManager 只配置/关联/生命周期适配。
4. **接口/字段**：`claim_owner(scope,run_id,expected_owner_epoch)`、`release_owner(ctx)`、`WorkflowHost.resume(scope,run_id,wake_fact_key,approval_id?,completed_job_id?)`。Run 使用 `owner_epoch`，宿主 ctx 的 run_owner_epoch 映射该值；Job/Task/Browser 各自 `lease_epoch`，不能彼此比较或复制。绑定含 thread_id、graph_version、state_schema_version、invocation_id、lock_connection_ref、saver_binding_ref（内部非凭据句柄）。
5. **权限传播**：每次恢复从可信 Run/binding 授权；节点和工具重新检查当前权限；State workspace/principal 只用于一致性校验。
6. **提交点**：取得 run/thread 对应 session lock 后，RunService 短事务 CAS owner_epoch 及租约，再调用原生 runtime；业务事实先提交，state delta/checkpoint 独立。lease 过期不构成安全接管证明。
7. **幂等/约束**：`UNIQUE(workspace,thread_id)` 且 run↔thread 1:1；wake fact 去重；业务提交检查 owner_epoch。该 epoch **不宣称已能约束原生 saver 的写入**，不自改 checkpoint 内部表。
8. **失败/恢复**：失锁必须终止旧 invocation，禁止重连续写；新 owner 必须确认旧 writer 已停止才可执行。单纯 lock 释放/超时不能确认独立 saver 停止。无法证明时禁止自动接管/并行恢复，提出单活 runner 限域评审；仍不能把多实例假设标通过。不兼容 graph/schema→CHECKPOINT_INCOMPATIBLE，保留原 thread。
9. **验证/门禁**：P0-T05 独立切断 lock/saver、旧进程重连、双 host；T06 重复 Resume/原生 interrupt 跨进程；T30 还原/升级。F5/F12/F18。**阻塞生产：是，恢复及多实例能力。状态：NEEDS_POC**。实际停止机制和 saver 配套版本由 T02/T05 取得证据后评审，不凭本地 lease 承诺 fencing。

### C-04：稳定工具意图、审批与外部提交

1. **问题/模块**：模型响应/序号未持久绑定，prepare 重入可能生成新 ToolCall；Approval 消费与外部成功之间存在未知窗口。模块 ModelGateway/Harness/Approval。
2. **关联**：[TDD02 §3–5](../tdd/02_Agent_Harness.md)、[TDD04 §4/7](../tdd/04_LangGraph_Workflows.md)、[TDD07 §3/8](../tdd/07_Data_API_Security.md)、[ADR-0003](../adr/0003_Approval_Side_Effects.md)。
3. **唯一写入者**：ModelGateway 登记脱敏完整响应与稳定步骤绑定；ToolExecutor 的意图登记端口写 tool_calls；ApprovalManager 写 approvals。RunService 独占业务 Run 状态。
4. **接口/字段**：`persist_response(ctx, model_step_key, response_artifact_ref, response_digest)`、`prepare_calls(ctx,response_artifact_ref)`。模型步骤键由已持久输入/逻辑步骤分配，不从临时内存计数或 trace 生成。响应 artifact 含 schema_version、调用顺序和规范化安全参数引用；增加响应绑定 `UNIQUE(workspace,run,model_step_key)`。ToolCall 补 response_artifact_id、call_ordinal（0-based）、canonicalization_version。`intent_key=SHA256(canonical(schema_version,run_id,response_artifact_id,call_ordinal,name,tool_version,arguments_hash,scope_hash))`；稳定外部 key 由持久 tool_call_id 派生。scope_hash 覆盖主体、具体资源、操作和授权范围；审批另绑定 policy/version/expiry。
5. **权限传播**：prepare/execute 都授权；消费前重验 requester 和 approver 当前资格、资源、工具/schema/operation_version、参数 hash、policy、expiry、revocation。改变绑定生成新意图/审批，不修改旧 tool_call_id 或挪用旧批准。
6. **提交点**：完整且脱敏的响应先提交，重入读 model_step_key 原绑定，再幂等登记意图/待批；approval_wait 仅原生 interrupt。批准+resume Outbox 同事务；执行前 Approval approved→consumed 与 ToolCall prepared→executing 同事务。远端调用在事务外；receipt+结果+预算结算经各 owner 同事务，随后返回 Graph/checkpoint。最终消息另遵守 Message+Run+terminal 同提交。
7. **幂等/约束**：tool_calls 既有 `(workspace,run,intent_key)` 唯一，另 `(workspace,run,response_artifact_id,call_ordinal)` 唯一并核对 hash，禁止换参绕过意图唯一键；同 ordinal 参数漂移→APPROVAL_MISMATCH。模型调用已返回但响应未持久化时不得产生工具执行；响应已提交而 checkpoint 未落时不得重新生成另一批意图。
8. **失败/恢复**：succeeded 复用 receipt；executing 过期且不能证明未发出→unknown_outcome/Run needs_reconciliation，冻结未知预算；不支持真实去重/查单的下游禁止自动重放。人工 resolve 必须有证据、权限及审计；补偿为新调用/审批。拒绝/过期/撤销不得发新请求。已经发出的请求无法靠本地事务追回，必须保留这一限制。
9. **验证/门禁**：P0-T06/T19/T21，F3/F4/F5/F16；含模型响应已提交/checkpoint 未提交、消费后请求前、外部成功/receipt 前各窗口。**阻塞生产：是，敏感操作。状态：NEEDS_POC**；下游 key 保留期、查单语义和原生 Resume 行为均未确认。

### C-05：BrowserSkill 真实协议与能力差异

1. **问题/模块**：没有 pinned upstream 真协议证据，不能假定支持 epoch 注入、每跳子资源控制或人工交接。模块 Browser Adapter/gateway/daemon/extension。
2. **关联**：[TDD05 §2–7](../tdd/05_Browser_Adapter.md)、[ADR-0004](../adr/0004_Parser_Browser_Reuse.md)、[ADR-0003](../adr/0003_Approval_Side_Effects.md)。
3. **唯一写入者**：BrowserSessionService 管配对、会话/owner；动作账本归 ToolExecutor；adapter 不独立决定业务恢复。
4. **接口/字段**：沿用 negotiate/create/execute/control，自有 envelope 含 browser_session_id、tool_call_id、expected_epoch、action、page_generation/locator_ref、grant_ref、deadline/max_bytes；响应含 action_sent、observed_epoch、artifact_refs/error_code。Capabilities 含 adapter_api_version/backend/daemon/extension/wire_protocol_version、features/limits/unsupported_actions/platform 和 evidence_ref；这些字段不是已确认 upstream wire 字段。T14 必须产逐方法映射、版本/hash/license、网络控制与 owner 拒绝能力证据，才能选择 bridge/直连。
5. **权限传播**：可信 ctx→同空间同 principal 的 device/session/tab grant；每次动作校验当前权限/域名/每跳网络策略/审批，不凭“已登录”扩权。
6. **提交点**：配对消费、续期/撤销、owner epoch CAS 为短 PG 事务；动作发送前 C-04 executing 提交，发送后 receipt 提交；外部 WS/浏览器不加入业务事务。
7. **幂等/约束**：token_hash 唯一、单 session 串行，tool_call_id 为动作去重身份；旧 epoch 迟到结果不写当前 session。内部 nonce 防重放不替代下游副作用幂等。
8. **失败/恢复**：未知版本→BROWSER_INCOMPATIBLE；未知能力→CAPABILITY_UNSUPPORTED；不能管控的高风险网络场景拒绝，不能自动换后端绕过。断连→disconnected，重连→paused；显式用户 resume 且重新授权。已发写未知按 C-04 核查。真材料/设备无法取得时后续实际尝试任务记 BLOCKED，不能以 mock 消除；本轮未尝试 PoC，仍 NOT_STARTED。
9. **验证/门禁**：P0-T14 静态协议/许可预检，T15 自有 port，T17/T18 真配对/撤销/Handoff/epoch/未知写，F9/F15。**阻塞生产：P1 BrowserSkill 是，独立 P0 否。状态：NEEDS_POC**。bridge/直连路线、原生 Windows 支持由真实证据及架构/发布负责人决定。

### C-06：无 Run Job 的身份传播

1. **问题/模块**：jobs.run_id 可空，fetch/ingestion 无法总从 Run 恢复执行主体。模块 auth/jobs/Worker。
2. **关联**：[TDD01 §4–6](../tdd/01_System_Architecture.md)、[TDD07 §3/4](../tdd/07_Data_API_Security.md)、[ADR-0005](../adr/0005_Workspace_Events_Privacy.md)。
3. **唯一写入者**：AuthorizationService 管 execution_bindings 的不可变授权来源，JobService 管 Job 的绑定 FK；Worker 无权替换绑定主体。
4. **接口/字段**：`create_execution_binding(scope, operation, resource_refs, parent_context) -> execution_binding_id`、`authorize_job(job_id) -> AttemptContext`。新增 binding：workspace_id、execution_binding_id、principal_id、policy_snapshot_id、operation、typed resource_refs（source/kb/document/version 按 kind 严格 schema）、resource_scope_hash、parent_run_id?、parent_task_id?、task_graph_revision?、created_at、expires_at?、binding_hash。jobs.execution_binding_id 必填；Job 所有者仍至少 source/document/run 之一。Job payload 仅 job_id/可核对 workspace 引用；role/权限 snapshot 不可作为授权。
5. **权限传播**：受信 Worker 用 job_id 查询 Job+复合 FK binding，读取当前 principal/membership/policy/资源状态构造 Scope；有 Run 时校验父主体/范围一致。领取、每个敏感节点、恢复、发布前重新授权；policy_snapshot_id 只作审计，不授予历史权限。后台服务身份不自动替代原请求主体；需要委托时另做绑定和评审。
6. **提交点**：binding+业务 owner+Job+Outbox 同一 UoW；已有 binding 仅同范围复用。授权失败由 JobService 原子 failed（FORBIDDEN 等安全码）/取消适用 Job，不发外部请求，不因 broker 重投重新授权给系统管理员。
7. **幂等/约束**：binding 主键及所有 FK 含 workspace；parent_task 有值则 parent_run/revision 必有值且复合 FK 一致。Job 唯一 `(workspace,kind,dedup_key)`，冲突必须核对 binding_hash/请求语义，不返回其他主体的私有结果；API 重试键另按公共通则。
8. **失败/恢复**：binding 缺失、跨空间、主体停用、资源撤销均 fail closed；旧 payload 篡改不会改变 DB binding；Reconciler 重领也同样授权。新受控重试按 C-09 创建新的当前 binding，不复用过期授权。
9. **验证/门禁**：P0-T03/T08/T29，F13/F16；至少双空间、run_id=NULL、篡改 payload、撤权/过期、池连接复用。**阻塞生产：是。状态：DEFINED**，跨进程传播与权限检查实现待实验；生产身份域例外仍见 C-07。

### C-07：P0 来源审阅、结果查询与身份决策

1. **问题/模块**：URL fetch 后可能待审，而已有 review API 依赖 P1 candidate；PRD 全局 User 与空间 Principal、入库 approval_id 与 review_decision_id 语义未获正式确认。模块 API/auth/source/review。
2. **关联**：[PRD §9.9/11/12](../PRD_PyKnora.md)、[TDD01 §4/5](../tdd/01_System_Architecture.md)、[TDD06 §2](../tdd/06_Knowledge_Ingestion.md)、[TDD07 §3/5](../tdd/07_Data_API_Security.md)、[ADR-0005](../adr/0005_Workspace_Events_Privacy.md)。
3. **唯一写入者**：SourceService 管 SourceVersion 与追加 ReviewDecision；IngestionService 版本、JobService 作业、AuthorizationService 身份；ReviewUseCase 只协调。
4. **接口/字段（实验草案）**：`POST /api/v1/sources/{source_id}/versions/{source_version_id}/reviews`，body 为 kb_id、decision（approve/reject/revoke）、reason_code、expected_content_hash，headers 为 If-Match/Idempotency-Key；source/version/KB 由后端校验关系。返回 review_decision_id、source_version_id、kb_id、status、job_id?、document_version_id?、request_id。`GET /api/v1/jobs/{job_id}` 的 result_refs 严格类型化为 source_version/review_decision/后续 job/document_version；附 review_required 与 review_url，不要求 candidate_id。`GET /api/v1/sources/{source_id}/versions/{source_version_id}` 提供已授权的快照状态/审阅结果，不暴露裸 object key。
5. **权限传播**：实验用合成双空间 Principal，仅在 KB write + 内容审阅权限下批准；上传者确认也必须绑定实际快照 hash/KB。生产由产品/安全决定审阅角色、自审是否允许、全局 User 例外和认证方式；不得通过测试合成主体宣称已解决。
6. **提交点**：初次 URL 创建 Source+fetch Job+Outbox；fetch 完成登记不可变快照/证据与 Job 结果，保持 captured/quarantined，不提前可检索。批准通过当前许可/证据检查后，一事务写 ReviewDecision、Source 状态、DocumentVersion、ingestion Job/Outbox；reject 不入库；revoke 原子阻断当前来源使用并登记清理。fetch Job 成功仅指快照阶段完成，不指入库成功。
7. **幂等/约束**：ReviewDecision 补 operation_version、content_hash、policy_snapshot_id、resource_scope_hash，追加历史；API 键绑定 source version+KB+decision+hash。Ingestion 的 `(workspace,document,source_version,processing_profile_hash)` 去重；批准复用已登记结果，不重复建版本/Job。多 KB 批准按来源状态和每 KB 有效审阅记录共同判断，不能从全局 approved 推导全部 KB 授权。
8. **失败/恢复**：快照变化必须新版本新审阅；旧审核过期/撤销或权限失效拒绝 submit/publish；回滚不产生有效 Job。工具审批 approval_id 不能冒充来源 review_decision_id；生产不得同时接受两种同义字段。发布引用保留 source_version/evidence/review_decision 链。
9. **验证/门禁**：P0-T03/T11/T13 实验最小来源/授权/发布；T27 扩展 P1 质量审阅；T29 安全回归，F4/F7/F13/F16。**阻塞生产：是。状态：BLOCKED**，产品+安全+架构负责人须决定身份域例外、P0 新端点/角色及 PRD 字段映射；本轮未请求或假定这些决策已批准。只允许 provisional 合成实验，不改 PRD/TDD/ADR 状态。

### C-08：Outbox 枚举、死信与派发恢复

1. **问题/模块**：缺少完整枚举与死信/重放协议；模块 Outbox/dispatcher/Celery。
2. **关联**：[TDD01 §6](../tdd/01_System_Architecture.md)、[TDD07 §3](../tdd/07_Data_API_Security.md)、[PoC-02](02_Phase0_PoC_Plan.md)、[ADR-0002](../adr/0002_Outbox_Version_Publication.md)。
3. **唯一写入者**：OutboxService 管投递事实；JobService 独占消费执行状态。broker ACK 不写业务终态。
4. **接口/字段**：`append(uow,event)`、`claim(batch_limit,now)`、`mark_dispatched(outbox_id,expected_epoch)`、`retry(outbox_id,expected_epoch,error_code)`、`redrive(scope,outbox_id,expected_row_version,reason,idempotency_key)`。补 delivery_attempt_count、lease_expires_at、last_error_code、dispatched_at、redrive_count、payload_schema_version、row_version。枚举固定 pending/dispatching/retry_wait/dispatched/dead_letter；payload_refs 仅可信资源引用。
5. **权限传播**：生产事件由授权业务用例登记，dispatcher 是受限系统执行身份只负责唤醒；consumer 重新走 C-06。redrive 需运维重派发动作授权和业务范围验证，不默认允许读内容/执行原操作。
6. **提交点**：业务+Job+Outbox 同事务；claim pending/retry_wait→dispatching 的短事务递增 epoch；提交后事务外发 broker，再 CAS→dispatched。失败或过期 lease→retry_wait；确定 poison/耗尽→dead_letter，保留原行/原因。回滚的事件不可被读取/消费。
7. **幂等/约束**：`UNIQUE(workspace,operation,dedup_key)`，消息业务键 job_id（transport_id 仅传输关联）。redrive 旧 epoch 作废、沿用 outbox_id/job_id/dedup_key，不建新业务结果；相同命令键幂等。
8. **失败/恢复**：发送成功未标记会重发；dispatched 只说明已发送，不说明 Job 已完成。DB Reconciler 扫 pending/retry_wait/过期 running Job，授权后调用 `rewake`：以稳定 `(job_id,current_attempt_id或初始标记,recovery_generation)` 登记唯一新唤醒事件，JobService 串行分配 recovery_generation；不修改原业务 Job/结果，不把终态重开。死信人工 redrive→pending，有审计；终态 Job 消费 no-op，欲重做走 C-09。ACK 失败不回滚已提交业务。
9. **验证/门禁**：P0-T07/T08，F1/F2/F13；rollback 50轮、100 Job×10消息、双 dispatcher/worker、Redis 丢失、ACK 丢失及死信恢复均按既有验收。**阻塞生产：是。状态：DEFINED**，至少一次唤醒/去重与恢复期限待真实实验。

### C-09：同配置重处理与终态作业重试

1. **问题/模块**：复用相同 DocumentVersion 不能永久返回失败 Job，也不能用投递重试复活 cancelled Job；模块 ingestion/jobs。
2. **关联**：[TDD01 §4/5](../tdd/01_System_Architecture.md)、[TDD06 §2/5/6](../tdd/06_Knowledge_Ingestion.md)、[TDD07 §3/5](../tdd/07_Data_API_Security.md)、[ADR-0002](../adr/0002_Outbox_Version_Publication.md)。
3. **唯一写入者**：JobService 写新旧 Job/attempt/retry lineage；IngestionService 写版本执行状态；IndexPublicationService 写 build/活动指针。
4. **接口/字段**：普通 `reprocess(scope,document_id,processing_profile_revision,key)` 相同配置查原版本/当前 Job。新增受控应用命令 `retry_terminal(scope,prior_job_id,expected_row_version,reason,key)`（生产 REST 路由待独立 TDD 评审，不冒充已有 API）。jobs 补 retry_of_job_id?、retry_sequence、execution_generation；document_version 补 current_job_id、execution_generation；build attempt FK 同时校验 Job/version 关系。
5. **权限传播**：重新校验当前主体、KB write、来源/审阅许可、预算及 profile；新 Job 使用新当前 execution binding。过期/撤销审阅、工具批准不复用；未知外部副作用先核查，不能以新 Job 换 key 逃避账本。
6. **提交点**：临时错误且未终态由 JobService running→retry_wait、新 attempt（同 job_id）执行。终态 failed/cancelled 的人工 retry 则锁 document/version/旧 Job，在同事务校验仍为 desired、未删未撤销、无活跃新 Job，创建新 job_id/new binding/Outbox，切 current_job_id、execution_generation++；同版本仅重置执行状态和 current attempt，不改来源/配置/已提交结果。被取消版本重新授权执行需明确该命令，绝不自动恢复。新 profile revision 创建新 DocumentVersion/index_version，不能改变旧内容。
7. **幂等/约束**：旧 Job 终态永久保持；`UNIQUE(workspace,retry_of_job_id,retry_sequence)`，新 dedup_key 包括 document_version_id+execution_generation+kind；并发 retry 由版本 CAS/活跃 Job 约束只创建一个。相同 API key 返回原失败结果也是正常重放；用户明确 retry 必须新命令键，不伪装原请求。语义版本唯一约束继续复用同来源同配置版本。
8. **失败/恢复**：pending/running/retry_wait 返回当前 Job，不创建并行 Job；succeeded 返回已发布结果，强制重建须显式新 profile revision；superseded/删除/撤销拒绝重试。旧 worker 的 current_job_id/generation/epoch 任一失配→STALE_ATTEMPT，无权改新 Job/build。取消后晚到结果不能发布。部分 staging 用新 attempt 分区，不能清当前 winner。
9. **验证/门禁**：P0-T08/T12/T13，F2/F6/F7；并发人工 retry、failed/cancelled 重投、相同 key、过期批准、旧 Worker 晚到。**阻塞生产：是。状态：DEFINED**，实际唯一约束/CAS 与状态转换待 PG 实验。

### C-10：资产归属与 FK 创建环

1. **问题/模块**：assets owner FK 与版本 snapshot_asset_id/manifest_asset_id 互指，存在插入环和多套归属事实。模块 assets/source/ingestion/storage。
2. **关联**：[TDD06 §4/6](../tdd/06_Knowledge_Ingestion.md)、[TDD07 §3/8](../tdd/07_Data_API_Security.md)、[ADR-0002](../adr/0002_Outbox_Version_Publication.md)、[ADR-0005](../adr/0005_Workspace_Events_Privacy.md)。
3. **唯一写入者**：AssetService 独占 assets/asset_bindings；Source/Ingestion/IndexPublication 只维护本域引用字段，经共享 UoW 调 AssetService。
4. **接口/字段**：`register_asset(uow,scope,object_metadata)`、`bind(uow,scope,asset_id,typed_owner)`、`assert_manifest_bindings(uow,manifest)`。实验提议 assets 仅存 workspace/asset_id/object_key/checksum/mime/size/state；去掉其重复 owner FK，以 asset_bindings 为唯一归属来源。binding 包含 workspace_id/binding_id/asset_id、source_version_id?、document_version_id?、wiki_revision_id?，CHECK 恰一 owner（Wiki 此阶段不实现）。版本的 snapshot_asset_id/manifest_asset_id 是用途引用，不另立所有权。
5. **权限传播**：按 binding→版本→来源/KB 当前授权，多个绑定分别检查，任意 UUID/object_key 不授予权限；普通用户不能访问无绑定隔离对象。
6. **提交点/顺序**：事务外上传不可变隔离对象并校验；服务端预分配 asset/version UUID；同 UoW 依次登记 assets 元数据→SourceVersion（引用 snapshot）/DocumentVersion→asset_bindings→build/manifest 用途引用。循环复合 FK 仅对必要边采用 DEFERRABLE INITIALLY DEFERRED；提交时校验用途引用绑定到相同 owner/version，持久 asset 至少一个有效 binding。该“至少一个”跨表规则由 AssetService 及拟验证的 deferred constraint trigger 共同约束；迁移 owner 禁止旁路写业务。具体 DDL 和约束名由 T03 产出验证，不假装已存在。
7. **幂等/约束**：assets `UNIQUE(workspace,object_key)` 且 checksum 相同；binding 按每种 owner 建部分 UNIQUE(workspace,asset_id,owner_id)，不可依赖含 NULL 的普通复合 UNIQUE 实现恰一去重。所有 FK 含 workspace，snapshot/manifest 校验 asset-binding-version 一致。
8. **失败/恢复**：commit 前少 binding/跨空间/用途不匹配必须回滚；对象成功 DB 回滚只留不可见孤儿，GC 经无引用/无活跃上传或尝试/保留期检查才删。DB 成功但对象缺失禁止发布；已发布损坏暂停相关服务并显式修复/回退，不能静默返回错误引用。GC 不凭对象年龄删有引用资产。
9. **验证/门禁**：P0-T03 真 PG 验插入/提交拒绝/FK，T11 对象绑定，T12/T13 manifest/发布/GC 故障，F14/F6。**阻塞生产：是。状态：NEEDS_POC**；延迟约束可实现性、并发 GC 与引用完整性未验证，失败需修改提议并重评。

## 3. 必须保持的不变量与后续测试

以下为设计硬约束，均未执行自动化验证。测试场景 ID `CT-INV-xx` 是本文件的追踪标签，纳入对应现有任务，不新增 DAG 节点或变更依赖；故障 F 编号沿用验收文档。

| ID | 不变量/可判定断言 | 后续自动化或故障注入任务 |
| --- | --- | --- |
| INV-01 | 异步 Job、Graph 节点、工具不能仅凭传入 workspace_id 授权；恢复必须重校有效主体、工作区、资源与工具权限，历史 policy/state 不授予权限 | CT-INV-01：P0-T03/T08/T29，F13；run_id=NULL、payload 改空间/role、撤主体/工具授权后执行数为0 |
| INV-02 | 每项业务事实仅一个逻辑写入者；checkpoint 不能代替业务 Task/Job/Run 状态，旧 state 不回写已提交事实 | CT-INV-02：P0-T12/T21/T23，F5/F17；调用边界检查+checkpoint 丢失重入后事实/结果不回退 |
| INV-03 | 重复 Resume、重复消息和 Worker 重试不得造成未经确认重复副作用；未知结果不能假称未执行 | CT-INV-03：P0-T06/T08/T21，F2/F3/F5；同 key 下游计数与 receipt 对账，无 key 未知写零自动重放 |
| INV-04 | 未完成或未发布版本不可见；所有用户检索只命中当前授权 active build 的 winning attempt，禁止半成品/混版本 | CT-INV-04：P0-T12/T13/T25，F6/F7/F14；并发构建/发布回滚/对象缺失/新旧反序时新查询无未发布 chunk |
| INV-05 | 业务回滚不产生可消费有效事件；重复派发不重复创建业务结果 | CT-INV-05：P0-T07/T08，F1/F2；事务回滚执行0，100 Job×10消息每 Job 单份业务结果 |
| INV-06 | 审批绑定具体 requester/approver、资源、参数、操作/工具版本；过期或撤销批准不得执行 | CT-INV-06：P0-T06/T18/T29，F4；逐字段变更、撤权/过期/重复消费后无新网络写，已发请求保留核查 |
| INV-07 | 恢复不回退正式业务提交；旧 Worker 不得覆盖新版本/attempt，取消删除不复活；恢复账本也不能推导外部写未发生 | CT-INV-07：P0-T05/T13/T23/T30，F6/F7/F12/F18；v2 active 不被 v1 覆盖、旧 epoch 拒提交、恢复前禁写并查单 |
| INV-08 | 正式发布采集知识必须关联已批准且仍有效的 SourceVersion、ReviewDecision、证据/快照/locator/quote_hash；引用固定版本且访问时再授权 | CT-INV-08：P0-T11/T13/T27/T28，F7/F14/F16；缺来源/审核/证据或许可撤销均不发布，篡改引用版本拒绝 |

P0-T11/T13 使用 P0 URL/上传的最小快照证据链与合成审阅记录，不依赖 P1 T27；T27/T28 扩展事实级候选/研究证据。这样不产生 T13→T27→T28 的反向前置。INV-08 不允许为了无 candidate_id 就跳过 P0 来源审核。

## 4. 技术假设、外部决策与解除条件

| 主题 | 当前可确定的设计约束 | 尚待验证的实现假设/任务 | 需负责人决策的条件 |
| --- | --- | --- | --- |
| Checkpoint 单写者/fencing | 仅原生 writer，失锁不续写，lease 不足以证明安全 | T05 独立 lock/saver 断开、旧 writer 重连仍安全；T30 版本恢复 | 架构负责人据结果批准多实例或受限单活部署；不自研 checkpoint |
| Interrupt/Resume | 等待用原生 interrupt，业务先提交，重复恢复查事实 | T05/T06/T21 配套版本跨进程恢复、prepare 重入、重复 wake | 无安全恢复时停相应能力并重评 ADR-0001/0003 |
| Human Approval/外部副作用 | 批准不是执行成功，consume+executing 同提交，未知不盲重放 | T06/T18/T21 下游真实 key/查单/保留期与请求窗口 | 安全/产品指定审批者及人工核查责任；无能力的写连接器禁用 |
| Outbox/Celery | 事务登记、至少一次、consumer 幂等、死信保留 | T07/T08 ACK 丢失/重投/Redis 清空/对账重唤醒 | 架构/运维据失败决定替代派发方案，不删门禁 |
| DocReader 真实协议 | 优先评估复用；bytes 输入、隔离认证、流不完整不发布、locator 不虚构 | T09 真 proto/镜像/hash/许可资料，T10/T11 真 Read/ReadStream/图片/EOF/meta/error/Unicode 定位 | 解析/发布负责人批准 engine/OCR/HTML profile 和许可；缺精确 locator 降级不能冒充精确定位通过 |
| BrowserSkill 真实协议 | 自有 port 与 wire 分离，不支持即拒绝，重连暂停 | T14 真 upstream/成套产物/授权资料；T17/T18 真扩展及 owner/network/Handoff | 架构/安全决定 bridge/直连/禁用能力，发布负责人审许可 |
| 索引原子发布 | PG 同事务域，active+winner+核心 Job 共同提交 | T12/T13 真约束/并发/对象完整性/取消/旧 Worker | 外部搜索引擎或跨事务存储须重评 ADR-0002，不能沿用原子承诺 |
| 无 Run Job 身份 | 必填不可变 binding，执行时读当前权限 | T03/T08/T29 跨进程/重领/撤权/篡改拒绝 | 产品/安全/架构确认 C-07 身份域与生产委托规则 |

C-07 的三个明确决策交付物：①身份域例外及认证边界评审结论；②P0 来源审阅 endpoint/角色/是否允许自审的确认；③PRD `target_kb_id/approval_id` 与实验 `kb_id/review_decision_id` 的正式映射及兼容策略。由产品+安全+架构共同评审，在后续 TDD/ADR 修订任务留记录；本任务不擅自接受。审计/幂等/对象 TTL、费用与数据权利、RPO/RTO 和质量阈值保留原计划待决项，不阻塞纯合成文档/资料预检，不据此宣称生产可用。

## 5. 最小 TDD 修订建议包（本轮不改原件）

| 缺口 | 后续需修订的章节/最小内容 | 集成与接受条件 |
| --- | --- | --- |
| C-01 | TDD01 §5/6、06 §6、07 §3：UoW 端口与发布事实键 | T03 公共 schema；T12/T13 各 owner 同事务及回滚证据 |
| C-02 | TDD03 §2/3/5、07 §3：task_outcomes、等待引用、fact 去重与命令 | T23/T24 证明旧 revision/epoch 不回写，状态不由 Graph 代管 |
| C-03 | TDD01 §6.4、04 §7/8：epoch 命名映射、旧 writer 停止/接管条件 | T05 连接级证据；ADR-0001 部署范围由架构评审 |
| C-04 | TDD02 §2/4/5、04 §7、07 §3：模型步骤/响应绑定、ordinal/hash、操作版本字段 | T19 复用持久响应；T06/T21 未知窗口及审批重放证据；ADR-0003 单独评审 |
| C-05 | TDD05 §3–7：真实协议映射/能力证据/unsupported、网络边界 | T14 资料、T17/T18 真设备；ADR-0004 路线不由 mock 决定 |
| C-06 | TDD01 §4/6、07 §3/4：execution_bindings、jobs FK、AttemptContext 无 Run 支持 | T03 单一 contracts/迁移，T08/T29 撤权证据；不把可空 run_id 传进必须有 Run 的 ExecutionContext |
| C-07 | TDD01 §4/5、06 §2、07 §3/5：P0 review/query 及 ReviewDecision 绑定 | 三项产品/安全/架构决策先形成评审记录；保持生产 BLOCKED |
| C-08 | TDD01 §6、07 §3：Outbox 五态、lease/死信/redrive/rewake 字段 | T07/T08 真队列对账，不把 dispatched 当业务完成 |
| C-09 | TDD06 §2/5、07 §3/5：retry_terminal、current_job/generation、终态不复活 | T08/T13 并发新 Job/旧 Worker，生产路由与状态变更正式评审 |
| C-10 | TDD07 §3、06 §4/6：唯一 binding、部分唯一/延迟约束及插入顺序 | T03/T11/T12 真 PG/对象完整性；不得保留两套权威 owner |

T03 是公共实验 contracts/最小迁移集成点；T07/T12/T15/T23 必须读取本文件，并以增量契约迁移引用它，不能分别造 Scope/Job/epoch 方言。TDD 修订和 ADR 接受属于后续独立评审，不新增未授权实现任务，不更改现有 32 任务/72 依赖边。

## 6. 双空间纸面推演（分析，不是运行证据）

合成空间 A/B 各有 principal、KB、source；所有 ID 均为符号，无真实账号或秘密。

| 场景 | 命令流/故障窗口 | 契约推导的预期结果与后续验证 |
| --- | --- | --- |
| 无 Run Job 撤权/换空间 | A 创建 fetch Job（run_id=NULL）及 binding→消息伪装 B/管理员→A 主体被撤销→Worker/reconciler 领取 | 以 DB A binding 读当前权限，拒绝执行；不得用 B payload 或 Worker 服务身份绕过。T03/T08/T29 实测，当前未运行 |
| 发布整体回滚 | A 版本 ready→T3 调各 owner→Job 完成命令失败→整笔 rollback；B 请求 A 文档 | A 保留旧 active，Job/新版本/富化 Outbox 均未部分成功；B 无读取权限。T12/T13/T29 实测，当前未运行 |
| 审批消费后结果未知 | A 意图登记/批准→consume+executing commit→下游可能成功→receipt 前崩溃→重复 Resume；B 提交 A approval | A 标未知并核查，禁止自动重复写；B 不能审批/恢复 A，checkpoint 不替代 receipt。T06/T21/T29 实测，当前未运行 |
| 新 Job 与旧 Worker | A 旧 Job cancelled→显式 retry_terminal 创建新 Job/generation→旧 Worker 返回→新 Job 完成发布 | 旧 Job 仍 cancelled，旧 generation 拒提交；仅新 winner 可见；B 相同 hash 不复用 A 资产。T08/T13 实测，当前未运行 |

## 7. 本任务验收与停止边界

静态验收检查十项必填信息、唯一 owner、状态枚举、八项不变量及现有任务映射、Backlog/依赖图表一致和 Markdown 本地链接；真实命令与结果记录于实施状态。本文件不含运行通过声明，pytest/ruff/DB/API/故障注入为未运行。没有新增架构/状态图，因此无需引入 Mermaid 图或渲染依赖。

P0-T01 文档任务完成后，P0-T02（环境清单与隔离环境）为建议下一任务；P0-T09、P0-T14 也仅从依赖上满足静态预检前置。是否取得外部资料仍需相应任务实际检查。本轮到此停止，不执行任何下一任务，不提交或推送。
