# Phase 0 验收、故障注入与 Exit Gate

日期：2026-10-27。当前无业务实验结果，九项均NOT_STARTED。以下是未来验证的强制条件/建议采样规模；文档检查通过不等于运行门禁通过。需求/TDD追踪见 [计划](02_Phase0_PoC_Plan.md)、[Backlog](03_Phase0_Task_Backlog.md)，不擅自更改PRD验收编号。

## 1. 五态结果与证据协议

| 结果 | 判定规则 | 后续 |
| --- | --- | --- |
| PASS | 本PoC所有强制验收项、环境/数据/许可和真实集成要求均通过，有可复验证据 | 仅说明该scope/版本/样本下成立，不自动Accepted ADR或宣布产品上线 |
| PARTIAL | 有实际运行证据，部分通过，仍有未完成/失败/限域降级项 | 明确哪些依赖可继续、哪些不能；强制项缺失不能算整体PASS |
| FAIL | 核心假设被证伪，如越权、重复未知写、双writer、半成品发布 | 停止依赖该假设的任务，提出ADR/TDD最小修订/替代并重测 |
| BLOCKED | 已实际尝试准备/运行但被外部环境、权限、模型、设备或材料阻断，无法判定核心假设 | 记录尝试命令/缺失物/负责人/解除条件；不能用mock抵消 |
| NOT_STARTED | 未开始执行验证任务；本轮九项全部如此 | 规划已完成也不能改成PASS或BLOCKED |

子测试结果可使用TDD08 CheckResult的passed/failed/blocked/not_run；PoC聚合使用上表五态，字段分别为test_status与poc_result，不创造同名含义冲突。若部分测试PASS而其他前置BLOCKED，聚合PARTIAL并保留blocked子集；没有任何可用运行结果且因外部被挡则BLOCKED。静态资料任务完成仅满足准备工作，不代表真实协议通过。

每次运行证据必须含：PoC/Task/scenario ID、Git SHA和dirty diff hash、Python/依赖lock/proto/镜像/浏览器/模型配置hash、fixture manifest/数据权利、命令/开始结束时间、预期/实际/次数、DB事实快照或查询结果、checkpoint原生API安全摘要、下游计数/receipt、对象checksum、错误码、trace/event关联、未执行项、残余风险和owner。秘密/个人信息不进入普通证据；内部定位可用合成opaque ID。

## 2. 九项验收与退出规则

| PoC / 当前结果 | 强制PASS项（量化） | 失败阻塞范围 | 可延期但必须明示 | ADR/TDD重评与降级 |
| --- | --- | --- | --- | --- |
| PoC-01 / NOT_STARTED | 真实Postgres跨进程interrupt/resume；50轮每关键窗口+50重复resume；无越权/未经确认重复写；独立锁/saver连接断开仍单writer；业务与checkpoint可对账 | R-01/02/11失败阻T06/T21及依赖恢复路径；不安全写全停 | 非必要多provider、恢复UI；多实例未过只能PARTIAL单活继续 | ADR-0001/0003，TDD01 §6/04 §7/8；单活/关闭自动接管、关闭未知写，不自研checkpoint |
| PoC-02 / NOT_STARTED | rollback 50轮执行0；100Job×10消息+双worker最终一业务结果/Job；断Redis/发成未标记/ACK丢/死信redrive/DB巡检可复验；恢复后60秒测试deadline内全到明确终态 | 丢Job、回滚事件可执行、重复结果阻T12/T21/T23后台依赖 | 高吞吐调优、生产beat/多broker；恢复延迟可先量测，60秒变更须记录新阈值/原因再重测 | ADR-0002，TDD01/06/07；CDC/替换broker或单dispatcher限域，仍需幂等 |
| PoC-03 / NOT_STARTED | 100轮竞态×≥100次新查询，半成品/混版本/旧attempt覆盖/越权0；删除/取消后不复活；引用正确版本；对象失败不发布；富化失败不改核心成功 | 任何可见性/权限失败阻T25/T28；无可靠发布不能进入P0 | 大规模吞吐/外部搜索引擎/复杂富化；不能延期核心manifest完整性 | ADR-0002/0005，TDD06 §5/6、07 §3；停新发布保留旧active，串行或新协议重评 |
| PoC-04 / NOT_STARTED | 真实Read/ReadStream PDF/DOCX；50标准五格式≥49成功且每格式≥9；拒绝集正确分类100%；声称精确locator全部匹配；图片/流不完整不得发布；auth/deadline与许可清单完整 | 真协议/伪定位/缺图静默发布阻T11/T12/引用；缺服务则BLOCKED | P0外格式、非首发OCR引擎；文档级引用降级只可PARTIAL精确定位项 | ADR-0004、TDD06 §3/4；替代parser/裁剪profile须产品范围确认，不无声删格式 |
| PoC-05 / NOT_STARTED | 真扩展/daemon及Playwright各有真实read；pair/renew/revoke/Handoff/未知动作；≥20网络/授权向量适用项全过；50轮owner/未知写零重放；capability/unsupported证据完整 | SSRF/越权/未知写重放阻相应通道；缺BSK阻T18/T28完整AC-E02，独立P0不受其通道影响 | 原生Windows daemon、非支持动作；缺真BSK最多PARTIAL，不能双后端PASS | ADR-0004/0003/0005，TDD05；关闭该通道/只读/单owner，公开Playwright不冒充用户会话 |
| PoC-06 / NOT_STARTED | 单真实provider协议/usage；≥20错误/上下文fixture；4只读并发写屏障正确；100预算竞争/50结算幂等；secret/PII canary0；取消/Job唤醒/trace故障；执行恢复仅原生LangGraph | 重造恢复、越权、预算逃逸/秘密泄漏阻T21/T22/T26/28相应路径 | 第二provider、插件市场/动态工具、复杂摘要优化 | ADR-0001/0003/0005，TDD02；缩Runtime门面、关闭不安全工具/内容导出 |
| PoC-07 / NOT_STARTED | 一次真实结构化plan，≥20非法DAG全拒；100调度/revision竞争无提前执行/旧覆盖；失败传播/取消、有限重规划2次、授权失效不复用、预算归集一次 | Task事实与Graph分叉/重复副作用阻T24/T27/T28；普通RAG不依赖Planner | 泛化用户DSL/多Agent/大型DAG；模型质量未校准列PARTIAL | ADR-0001/0003，TDD03/04；固定有限模板/串行研究，不能无限重规划 |
| PoC-08 / NOT_STARTED | 30任务控制站全链与三通道代表正例；发布候选100%provenance/许可/证据/审核；禁采/无证据/越权发布0；去重不重索引/来源独立；两轮变化/不变正确；预算/安全/审阅可追溯 | 污染/越权/无证据发布阻P1知识沉淀；缺任何关键前置不绕过 | 生产周期调度器、Wiki/连接器、外部搜索供应商；测试本地search必须标scope | ADR-0002/0004/0005，TDD03/04/06；人工审核、只保指针/摘要/禁自动发布 |
| PoC-09 / NOT_STARTED | 100QA真实三路检索/embedding/chat、引用版本/ACL100%正确，无证据不确定；50重复finalize一terminal，消息未提交completed0；质量指标有金标准/阈值评审；流generation不混 | 越权/伪引用/伪completed阻P0/T28；质量门槛未定PARTIAL不可完整PASS | P1公共续传、外部BM25引擎；10万chunk/20对话指标可列容量扩展但不能声称达标 | ADR-0005/检索DEC-06，TDD04 §3/07 §6；调整tokenizer/RRF/Rerank或评测BM25，禁异构原始分数相加 |

数量是建议的实验最低规模，批准改阈值要保存旧结果/原因/重新运行证据；不能只降低标准让失败消失。≥98%解析成功率与精确定位100%并非同一指标，不能互相替代。

## 3. 故障注入矩阵与恢复断言

每注入使用可控barrier/代理/进程终止定位提交点；不以长sleep猜时序。至少核对“事务前、事务后、网络已发/未发、receipt前后、checkpoint前后”，运行时由test专用身份操作，生产禁用注入口。

| 场景 / 风险 | PoC / Task | 精确窗口和恢复触发者 | 最终强制断言 |
| --- | --- | --- | --- |
| F1 DB成功Redis失败 / R-03/04 | 02 / T07,T08 | 业务+Job+Outbox commit后断Redis；dispatcher/reconciler重试 | Job不丢；202只表示登记；Outbox pending/retry_wait可恢复，无伪成功 |
| F2 Job重投 / R-04 | 02,03 / T08,T13 | 多worker同job_id、claim后/结果后ACK前kill；JobService对账 | CAS/epoch有效，终态消息no-op，重复不改版本/结果；业务结果1份 |
| F3 外部成功receipt前崩溃 / R-02 | 01,06 / T06,T21 | executing事务提交→控制站写成功→kill；ToolCall reconciler查key/人工核查 | unknown_outcome/needs_reconciliation；无key禁止自动重放；未确认不得伪称0次执行 |
| F4 批准后撤权/改参 / R-02/11 | 01,05 / T06,T18 | approved提交后撤主体/工具版本/参数；恢复执行前再校验 | 无新网络写；FORBIDDEN/APPROVAL_MISMATCH/APPROVAL_EXPIRED；新参数新意图审批 |
| F5 Graph节点重入 / R-01/02 | 01,06 / T05,T06,T21 | receipt已commit但checkpoint失败；prepare在interrupt前重跑；重复resume | 稳定intent key/tool_call_id读原结果；不重复已确认副作用；单writer |
| F6 旧版本晚于新版本 / R-05 | 03 / T12,T13 | v1 staging挂起→v2 T3激活→v1晚到；发布服务CAS | v1 superseded、v2 active；winning attempt一致、旧worker只能清自己的staging |
| F7 取消/删除后结果 / R-05/11 | 03,07,08 / T13,T23,T28 | 网络/Job发出后先commit tombstone/generation，再回结果 | 新读取不见删除资源；不可复活；未知外部写仍核查，终态cancelled不改成功 |
| F8 核心成功富化失败 / R-05 | 03,08 / T13,T28 | T3包含core succeeded和enrichment Outbox；摘要固定失败/重复完成 | searchable不变；富化partial_failed/failed；不重复扣counter/重解析 |
| F9 浏览器owner/结果未知 / R-07/08 | 05 / T17,T18 | 动作已发断WS/owner；新epoch建连旧回复迟到；用户继续 | disconnected→paused；旧epoch不写当前状态；写不自动重放；用户未resume不恢复任务 |
| F10 SSE/消息/游标失败 / R-15 | 09 / T26,T29 | token已持久已发，finalizer事务失败；P1边界游标过期；finalizer对账 | Message+Run+terminal同commit；失败无completed，过期410取快照不重跑Graph，generation不混 |
| F11 并行/重规划争预算 / R-14 | 06,07,08 / T20,T24,T28 | reserve/settle/revision CAS竞争最后额度；取消时请求已发/未知 | purpose/settlement唯一；固定锁序；未知不释放；父run归集一次；超实际预留真实记录/冻结 |
| F12 单锁连接丢失 / R-01 | 01 / T05 | advisory lock连接断、saver保持活、旧进程试重连，第二host接管 | 两个有效writer0；若不成立FAIL/PARTIAL单活，无“lease过期等于安全”假定 |
| F13 无RunJob身份传播 / R-11 | 02 / T03,T08,T29 | 篡改payload role/workspace、撤execution principal；reconciler重领 | 只凭可信execution binding+当前权限；跨空间/撤权执行0 |
| F14 对象缺失/FK环 / R-05/06 | 03,04 / T11,T12,T13 | 对象写后DBrollback，asset/source循环插入失败，manifest缺对象 | 孤儿不发布、GC不删有引用对象、事务不可留下半绑定资源 |
| F15 浏览器/HTTP每跳出站 / R-08 | 04,05 / T11,T16,T29 | 私网IPv6/重定向/iframe/WS/download/DNS rebinding控制站 | 适用禁止向量到达0；BSK无法控制的场景必须拒绝/unsupported，不能忽略 |
| F16 secret/PII/提示注入 / R-12/16 | 全部 / T06,T11,T18,T21,T26,T28,T29 | 错误体/网页/文档/工具带canary和越权指令，trace exporter自动采样风险 | 普通log/trace/state/模型无secret/PII；外部文本不能扩权；未脱敏产物不转模型 |
| F17 DAG旧revision/失败 / R-09 | 07 / T22,T23,T24 | 非法图、结果commit后checkpoint回退、双replan、失败依赖/optional | 非法计划0执行，事实不被旧state覆盖；必需失败阻断；复用权限再次验证 |
| F18 还原/升级失配 / R-17 | 核心 / T30 | 恢复到写发生前账本、对象/PG不同时间点、不兼容checkpoint版本 | 先禁外部写/调度、对账/人工核查；不自动重放外部动作、不静默新建thread |

当前F1–F18全部未运行。人工Handoff/真实扩展测试需用户/测试人员配合，自动化不得绕过限制。

## 4. Phase 0 Exit Gate

### G-DOC：本轮规划交付门（静态）

七份实际文档+状态更新，风险/PoC/任务/需求全追踪，32任务16要素与owner/集成影响齐全，DAG无环/图表一致，链接/fences/Mermaid检查，原TDD/ADR/PRD/源码/依赖保持原内容。G-DOC通过只表示规划可评审；当前业务PoC仍NOT_STARTED。

### G-P0：P0 核心技术准入门（由T31评审）

1. PoC-01/02/03/04/06/09的P0相关强制项有真实证据，安全/恢复基础项不得豁免；单活降级只能形成明确限域建议，原多实例假设仍PARTIAL，需ADR评审后决定是否准入。
2. T29授权/网络/内容安全门全部通过；T30最小恢复/版本兼容演练有记录；未定生产RPO/RTO保留风险，不冒充灾备SLA。
3. C-01–C-10实验契约已闭合，生产契约相关TDD修订有明确评审结果，身份域/来源审核API和字段例外不可留给实现猜；正式接受ADR不由测试程序自动执行。
4. 实际所用依赖/镜像/模型权重/数据许可与安全维护记录完整；未知许可不进入发布候选；费用、域名、审批/核查责任、秘密处理规则明确。
5. 质量基线与候选阈值由产品/测试负责人确认；Recall@5 0.85/引用0.95未校准时该项不PASS。PRD性能目标记录实际差距和硬件边界；阶段0只证明/测量，P0正式交付仍需产品验收AC-01–06。

缺一项：记录PARTIAL/FAIL/BLOCKED及补测/决策任务，不宣称P0生产上线。BrowserSkill/DAG生产能力不属于P0门禁，但其共用安全基座缺陷若影响P0必须阻断，不能以“P1功能”忽略共用缺陷。

### G-FULL：全量 Phase 0 退出门（由T32评审）

G-P0满足；九项PoC所有强制项均PASS，PoC-05真扩展/daemon+Playwright真实证据与安全能力矩阵齐全，PoC-07/08包含DAG/replan/质量/审阅/异步入库/RAG回源；全部高风险已验证或经正式范围变更排除且明确禁用。**若仍有强制项PARTIAL/BLOCKED，不可标完整Phase 0 PASS**。

可提出“受限范围准入”供产品/安全/架构负责人评审，但要列禁用能力、接受者、残余风险、补测任务，且这不是原G-FULL通过。禁止仅改图/删测试绕过门禁。ADR依然须单独决策；PASS不自动Accepted，实验FAIL触发提议修订而不是立即Superseded现记录。

## 5. 度量、CI与失败后的动作

已实测业务基线：无。硬安全/一致性要求为测试集零违反；采样次数不是数学证明。性能候选继承PRD：CRUD P95<500ms（不含模型/解析）、10万chunk检索P95<1s、首tokenP95<3s、20活跃对话；实验必须报告数据/机器/模型/配额/冷暖条件。新增60秒恢复deadline、并发4、32节点等为实验配置建议，不是服务SLA。

CI单元/契约每次变更；真实PG/Redis/对象/DocReader集成对对应任务运行，浏览器/模型成本任务单独受控触发；真BSK人工参与记录单列，不能因CI skip归绿而PASS。失败后只修本任务/契约建议，不顺便重构全项目；换技术/缩范围先提出ADR/TDD影响并保留失败证据。进入下一阶段顺序见 [执行指南](06_Phase0_Execution_Guide.md)。
