# Phase 0 技术风险登记表

日期：2026-10-27。全表为架构风险判断 N/U，非发生频率实测。风险 owner 是处置负责人，业务状态拥有者仍按 [评审 §3](00_ADR_Readiness_Review.md)；不从风险 owner 推导写表权限。

## 1. 评分与排序

可能性 L：1 很少/2 较低/3 有现实窗口/4 多路径暴露/5 预计频繁；影响 I：1 局部可逆/2 单请求/3 单任务/4 多任务或数据损失/5 越权、不可逆副作用或发布一致性破坏。分数=L×I；≥15 高、8–14 中、≤7 低。评分主观，首次实验后更新，不假装有概率统计。

排序综合：安全/一致性硬阻塞、影响面、依赖扇出、验证成本。R-07 虽高但只阻塞特定 P1 通道，可先做低成本源码/许可预检，不占据所有 P0 资源；R-10 分数较低但检查便宜，T01/T19 提前做。关键链为 R-11→R-03/04→R-05 和 R-11→R-01/02；不是按产品页面开发顺序排队。

## 2. 必需风险及处置

| Risk ID / 描述 | 模块 / owner | L | I | 分数 | 当前缓解（设计，未验） | 需 PoC / 阶段阻塞 | 验证方法与任务 | 失败备选 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| R-01 Checkpoint 单写者/并发恢复、锁断开而 saver 活跃 | host/checkpointer；执行负责人 | 4 | 5 | 20 | session lock+owner epoch、禁止失锁 invocation 续写；TDD01 §6.4 | 是 PoC-01；P0/P1 Graph 安全恢复阻塞 | T05/T06：两个独立进程、丢锁/网络分区/旧 writer 重连；检查原生 checkpoint 分支与 receipt，50轮每窗口 | 单活宿主、禁止自动接管；若无法消除双 writer 则停止恢复功能并重评 ADR-0001 |
| R-02 批准消费/外部写后崩溃导致重复副作用 | Approval/ToolCall；安全执行负责人 | 4 | 5 | 20 | 参数/主体/版本绑定、调用账本、外部 key、unknown_outcome 不重放 | 是 PoC-01/06；P0 受控恢复基座、P1 所有敏感写阻塞 | T06/T21：写前/成功后/receipt后 kill，支持与不支持幂等下游各测，改参/撤权/过期 | 只读工具、关闭未知写自动重试，人工核查；无法满足则禁用写连接器 |
| R-03 DB 与 Celery 派发提交裂缝 | API/Job；作业负责人 | 4 | 4 | 16 | 业务+Job+Outbox 同事务，202 只确认登记 | 是 PoC-02；P0/P1 后台作业阻塞 | T07/T08：DB commit/rollback 前后断 Redis；无事务事件不得派发 | PG 待办轮询执行/替换消息层须重评；禁止退回直接 Enqueue |
| R-04 Outbox 投递丢失/重复、过期 lease/死信无恢复 | Outbox/dispatcher；派发负责人 | 4 | 4 | 16 | lease CAS、唯一 dedup、DB 对账、至少一次 | 是 PoC-02；P0/P1 阻塞 | T07/T08：发送成功未标记、两个 dispatcher、Redis数据丢失、重试耗尽、人工 redrive | CDC/更可靠 broker 可评估但仍保留消费幂等；未知 poison 进隔离队列不丢事件 |
| R-05 索引半成品可见/旧版本或旧 attempt 覆盖 | ingestion/index；发布负责人 | 4 | 5 | 20 | 不可变版本、winning_attempt、desired/generation、同PG发布事务 | 是 PoC-03；P0/P1 知识发布阻塞 | T12/T13：100轮逆序/并发读，delete/rebuild、提交失败、丢对象/富化失败；引用版本比对 | 每文档串行+停新发布保留旧 active；若换外部索引另立协议 ADR |
| R-06 DocReader proto/stream/图片与 locator 漂移 | parser/assets/citation；解析负责人 | 4 | 4 | 16 | 固定 proto/镜像，检查 meta.error/EOF，TXT 单独适配，变换映射 | 是 PoC-04；P0 五格式与P1引用阻塞 | T09/T10/T11：真实服务、50份 golden、Read/ReadStream、Unicode/换图/超时/缺图 | 按格式换成熟 parser；无精确 locator 只文档级并标精度，不能通过“精确定位”门禁 |
| R-07 BrowserSkill 真协议、用户授权与 owner 恢复未知 | gateway/daemon/extension；浏览器负责人 | 4 | 5 | 20 | pinned 成套版本、能力差异、pause/revoke/unknown、桥接预案 | 是 PoC-05；P0 不阻塞（不含BSK），P1 AC-E02/用户浏览器阻塞 | T14/T17/T18：先审真实源码/许可，真扩展测试配对/撤销/owner crash/Handoff；不以 mock 通过 | Playwright公开读取、关闭用户浏览器；PoC-05保留PARTIAL/BLOCKED，ADR-0004重评 |
| R-08 Playwright session/进程/出站隔离不足、SSRF | browser/network；网络安全负责人 | 4 | 5 | 20 | 独立context+隔离容器/强制代理、每跳DNS/IP控制 | 是 PoC-05；P0受控HTTP同类策略由T11/T29验证，P1浏览器阻塞 | T15/T16：私网IPv4/IPv6、DNS rebinding、redirect/iframe/WS/download、profile隔离 | 暂停浏览器，仅审核域静态HTTP；不能只凭route hook放行 |
| R-09 DAG revision/Task事实与Graph引用分叉 | orchestration/Research；调度负责人 | 4 | 4 | 16 | 节点CAS、不可变revision、结果引用、当前revision指针 | 是 PoC-07；P0普通RAG不阻塞，P1复杂研究阻塞 | T22/T23/T24：非法DAG、旧结果、丢checkpoint、失败传播、重规划竞争/复用 | 固定有限研究模板/串行计划；不强制RAG经Planner |
| R-10 Harness 重复造 checkpoint/interrupt/推理循环 | runtime/module；架构负责人 | 3 | 4 | 12 | port薄层、依赖方向、由原生Graph控制 | 是 PoC-06（静态+小实验）；P0/P1架构债阻塞对应模块合入 | T01/T19/T21：导入/调用图检查，checkpoint只有原生API，无第二套SnapshotEngine | 删除重复层、收窄Runtime门面，回到原生节点组合 |
| R-11 workspace/principal 在无Run Job、异步恢复/共享路径丢失 | Scope/jobs/API/RLS；授权负责人 | 4 | 5 | 20 | 全链Scope/复合FK/当前权限；C-06需补 execution binding提议 | 是 多PoC贯穿；P0/P1所有外部数据操作硬阻塞 | T03/T06/T08/T13/T29：双空间、伪造payload、撤权、对象/引用/游标、连接池Scope污染 | fail closed、禁共享/禁该作业；先合成主体测试，生产身份例外待评审 |
| R-12 来源质量/许可/证据不足、近重误合并污染知识 | Research/review/ingestion；知识质量负责人 | 4 | 4 | 16 | 分项质量、硬拒绝、人工审核、独立来源/精确版本 | 是 PoC-08/09；P0 URL需最小审核，P1自动研究阻塞 | T11/T27/T28：30任务、矛盾转载/无证据/禁采/PII/注入、变更候选 | 默认待审、仅指针/摘要、禁自动发布；近重只提示不强合并 |
| R-13 组件/镜像/模型权重/桥接分发许可与安全维护不明 | dependencies/DocReader/browser；发布负责人 | 4 | 4 | 16 | 固定SHA/版本、SBOM/NOTICE、无自定项目许可 | 不仅PoC：T02/T09/T14资料审查+真实镜像清单；P0相关组件、P1桥接/权重分发阻塞 | 查具体发行物许可证/传递依赖/CVE/产物hash/授权；缺项禁止该产物发布 | 换可评审组件/托管服务/禁OCR或桥接，保留版权；不能用mock消除许可问题 |

## 3. 补充横切风险

| Risk ID | 描述 / owner | L×I | 缓解及验证 | 阶段/备选 |
| --- | --- | --- | --- | --- |
| R-14 | 并发预算重复预留/错误释放、重规划逃逸；预算负责人 | 4×4=16 | T20/T24/T28，锁序、purpose_key、未知usage保守冻结，PoC-06/07/08 | P0模型成本/P1研究阻塞；供应商硬限额+冻结后续调用，不能虚报绝不超支 |
| R-15 | SSE伪完成/草稿混流/事件游标越权；事件负责人 | 3×4=12 | T26/T29，Message+Run+terminal同事务、generation reset、过期410 | P0基本流/结果一致性阻塞；禁续传仍需最终持久化正确 |
| R-16 | secret/PII进入SDK日志/trace/state/模型；安全负责人 | 4×5=20 | T06/T11/T18/T21/T26/T29全链canary，内容默认不导出、typed安全错误 | P0/P1硬阻塞；禁内容采集/不安全connector，trace失败不影响业务事实 |
| R-17 | 备份恢复/迁移导致账本落后于已发生外部写；运维负责人 | 3×5=15 | T30：恢复前禁外部写/调度，校验PG与对象manifest、旧schema/checkpoint fixture | P0基础恢复阻塞；人工查单、旧宿主结束历史run，RPO/RTO未定不宣称灾备SLA |

R-14–17 同样需要实验；补充风险不取代 R-01–13。优先五项按硬阻塞和扇出选 **R-11、R-01、R-02、R-05、R-04**，R-16 作为贯穿的安全停线条件。R-03 与 R-04 在同一 PoC 拆为生产事务/投递恢复两类，避免重复建设。

## 4. 风险与任务追踪

完整任务见 [Backlog](03_Phase0_Task_Backlog.md)，依赖见 [DAG](04_Phase0_Dependency_DAG.md)。所有风险当前“未验证”；验证资料不足时任务记录阻塞，PoC 在实际执行前仍 `NOT_STARTED`。每次实验后填写真实值、残余风险与 owner，不以通过部分正常流程关闭整个风险。
