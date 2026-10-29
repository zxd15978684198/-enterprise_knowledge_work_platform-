# Phase 0 风险驱动 PoC 计划

日期：2026-10-27。**九项均为 NOT_STARTED**，无工程执行/性能基线。本轮没有安装依赖、启动容器/PoC 或修改业务代码。（↑ 该句是 W0 规划时点的状态快照，**保持原样不改写**；实时状态见 §3 总览表的「当前结果 / 依据」两列与 §3.1 维护条款。）原始风险依据：[研究风险](../research/03_Compatibility_Risks.md)，本轮编号见 [风险登记](01_Technical_Risk_Register.md)。任务路径均为未来预计产物，当前不存在不代表已实现。

## 1. 目标、范围、证据与依赖原则

PoC 用于证伪关键架构假设，不是提前交付完整产品。隔离代码建议在 `experiments/phase0/`，共享测试在 `tests/phase0/`，证据在 `docs/poc/evidence/`；不把实验实现自动提升为 `src/pyknora/` 生产架构。公共 DTO/Schema 只允许一个权威来源，由 T01 约定；隔离实验可复用而非各 PoC 私造 Scope、Job、审批状态。未来修改这些路径或依赖需对应单任务授权，本轮仅规划。

低风险边界原则沿用户规范，具体 R-01/02/04/05 仍有实验门禁。P0 产品不包含 BrowserSkill、通用 DAG、Research、Wiki，但提前验证 P1 高风险假设不等于改变产品优先级。九 PoC 全量出口与 P0 核心出口分开，缺 BrowserSkill 不能把全量 Phase 0 说成 PASS，也不应无依据阻塞普通 RAG 的独立验证。

研究 H 编号映射：H1→PoC-01；H2→PoC-06；H3→PoC-07；H4→PoC-04；H5→PoC-02/03；H6→PoC-09；H7→PoC-05；H8→PoC-08；H9→每项安全断言/T29。TDD 的 F1–F11 是故障场景，非新产品需求，沿用到 [验收矩阵](05_Phase0_Acceptance.md)。

## 2. 公共环境 profile 与测试数据

| Profile | 服务/工具（未来需要） | 样本/授权与边界 |
| --- | --- | --- |
| E0 资料/契约 | 现有 Git/文本工具、Python 标准库；无需安装/容器 | T01/T09/T14 可先静态审查；真实 upstream 缺失要记录，不能编协议 |
| E1 核心执行 | Python 3.12+验证主版本、uv、pytest/ruff、FastAPI/Pydantic、LangGraph+Postgres checkpointer、PG/pgvector、psycopg/迁移工具、可控下游服务 | 双空间合成Principal、许可明确的计数/查单服务；实际发行版及镜像由T02锁定；不从本机3.14推定兼容 |
| E2 可靠作业 | E1+Redis/Celery、dispatcher/reconciler两个进程、网络断路/进程kill能力 | 只读DB快照、10份相同消息/并发争抢、假时钟仅用于边界单元，集成用真实进程 |
| E3 解析/对象 | E1/E2+DocReader gRPC镜像、私有MinIO或经许可的S3兼容测试服务、native依赖/OCR字体（按scope） | 五格式各10份标准样本+独立拒绝集，已知页/段/图片/表格/Unicode定位；生成器/样本许可和hash清单 |
| E4 浏览器 | E1+Playwright匹配浏览器镜像/隔离容器、强制出站代理/网络规则、pinned BrowserSkill daemon/extension/网关、真实Chrome/Edge设备 | 自有控制站，登录/验证码人工演示，独立测试账号；不访问真实用户cookie、不绕过限制 |
| E5 模型/评测 | E1/E3+授权Chat/Embedding/Rerank endpoint、OTel本地collector或安全trace sink | 模型先用确定性替身测控制流，再用真实endpoint测协议/用量；100条QA/30个研究任务，最大费用硬限额 |

最小 Compose 未来可按 profile 启动 PG、Redis、对象存储、DocReader、control-site、OTel；Graph/Worker 可在Linux测试容器，API只薄测试入口。BrowserSkill真实设备不等于一个Docker service。镜像从可信来源锁 digest，Redis/MinIO/DocReader/BrowserSkill/模型权重分别核验许可；不复用未脱敏生产数据/备份。准备/命令/CI见 [执行指南](06_Phase0_Execution_Guide.md)。

## 3. PoC 总览

| PoC ID | 风险/PRD | Task IDs | 外部依赖（硬门禁） | 当前结果 | 依据（W14 复核，2026-10-29） |
| --- | --- | --- | --- | --- | --- |
| PoC-01 | R-01/02/11/16；F-09、AC-E09、AC-06 | P0-T05、P0-T06 | T01–T04；T07/T08不是初测前提，后续T21补可靠唤醒集成 | **PASS（限域：单活）** | `wave-w7r-record.md:61`（T05R `12 passed / exit 0`）、`wave-w3-record.md:100`、`wave-w4-record.md:154`；多实例 fencing 仍 PARTIAL |
| PoC-02 | R-03/04/11；AC-05、F-03/04 | P0-T07、P0-T08 | T03/T04；真实PG/Redis/Celery | **PASS（限域）** | `wave-w3-record.md:174`（T07）、`wave-w4-record.md:155`（T08 `applied=100 / duplicate=900`） |
| PoC-03 | R-05/11/17；AC-01/04/05 | P0-T12、P0-T13 | T08/T11；真实PG/pgvector/对象服务 | **PASS（限域）** | `wave-w5-record.md:140`（T12）、`wave-w6-record.md:111`（T13，10 000 次并发读零混版本） |
| PoC-04 | R-06/13/16；F-04、AC-01/03 | P0-T09、P0-T10、P0-T11 | T02/T04/T09，T11还需T03授权fixture | **PASS（限域）** | `wave-w7r-record.md:62`（T09R 镜像 digest）、`wave-w4-record.md:156`（T11 golden 50/50）；残余：真实样本 + OCR 权重许可 |
| PoC-05 | R-07/08/11/13；F-11、AC-E02/08/09 | P0-T14–P0-T18 | T15基础port；BSK真动作需T06，upstream/设备真实可用 | **PASS（限域）** | T14/T15/T16 见 `wave-w1-record.md:55`、`wave-w3-record.md:92`、`wave-w4-record.md:157`；T17 `wave-w5-record.md:86`（`fix/w5-t17` 真配对）；T18 `fix-w5-t18-record.md:94`。残余：T15 能力位未与真 daemon 握手、`tab borrow` `NOT_RUN`、`LeaseRegistry` 仅进程内 |
| PoC-06 | R-10/14/16；F-02/09/16、AC-E08/09/10 | P0-T19、P0-T20、P0-T21 | T03/T04；集成T21需T05/T06/T08/T20 | **PASS（限域）** | `wave-w3-record.md:177`（T19）、`wave-w4-record.md:158`（T20）、`wave-w5-record.md:141`（T21）；残余：真实供应商、`stream()` 未实现 |
| PoC-07 | R-09/14；F-10、AC-E01/09 | P0-T22、P0-T23、P0-T24 | T19；调度需T05/T08/T20 | **PASS（限域）** | `wave-w4-record.md:91`（T22）、`wave-w5-record.md:142`（T23）、`wave-w6-record.md:68`（T24）；聚合验收 `poc07-report.md`（W16）；残余：单活前提、checkpoint 用替身、30 轮非 100 轮、T20 真实台账未接线 |
| PoC-08 | R-12及全链；F-10/12、AC-E01–E10 | P0-T27、P0-T28 | T11/T16/T24，最终T18/T26/T13/T21 | **PASS（限域）** | `wave-w7-record.md:68`（T27 `66 passed`）、`wave-w9-record.md:38`（T28）、T29 安全回归同波次；残余：真实模型/真实后端 |
| PoC-09 | R-05/06/11/15；F-05/06、AC-02/03 | P0-T25、P0-T26 | T13/T19，最终T05/T21 | **PARTIAL** | `wave-w7-record.md:67`（T25，当时 ANN `NOT_RUN`）、`wave-w8-record.md:80`（T26 `21/21`，替身模型 `citation_accuracy=1.0`）；ANN 已于 W13 补跑（`ann-bench.json`，HNSW recall 0.998–1.000），但**质量阈值未获批准（ADR-0014）→ 不得判质量 PASS** |

本表的“依赖”与 Backlog 是硬依赖；PoC 本身可拆分先做静态/局部任务，不能把未完成子路线聚合成 PASS。PoC-08 只评估 AC-E07 的最小两轮变化/观察，不交付生产周期调度器。

### 3.1 状态列维护条款（M1，W14 立项）

| 项 | 约定 |
| --- | --- |
| 唯一维护人 | 当前波次 **Lead**（轮次交接时在 `docs/IMPLEMENTATION_STATUS.md` 前沿行声明） |
| 更新时机 | 每个波次 `docs/poc/evidence/wave-wN-record.md` 的 §2/§3 收口时，**必须同步**本表「当前结果」与「依据」两列 |
| 更新依据 | 该波次记录的判定行 + `docs/poc/evidence/` 下的报告文件（`poc0*-report.md`、`fix-*-record.md`、`P0_Core_Gate_Review.md`） |
| 状态取值 | 只允许 `NOT_STARTED` / `PARTIAL` / `PASS（限域）` / `PASS` / `FAIL` / `BLOCKED`；**含 `NOT_RUN` 或未覆盖边界者一律不得写成无保留 `PASS`**（`05_Phase0_Acceptance.md:73-76`，由 `verdict_lint.py` 机检） |
| 与判据的关系 | 本表是**结论索引**，不是判据：门级判定以 `P0_Core_Gate_Review.md` / `Phase0_Full_Gate_Review.md` 为准，任务级判定以各波次记录为准 |
| 历史保护 | W0 时点的「九项均为 NOT_STARTED」等历史表述不得改写，只能追加「截至 Wxx」的更新行，保证可追溯 |

## 4. PoC-01：Checkpoint、审批与副作用恢复

- 目标/假设：同一run/thread单writer，原生interrupt可跨进程恢复；重复resume不重复未经确认的写。业务Run、Approval、ToolCall各拥有者见评审§3，Graph状态只由框架写。
- 最小范围：固定三节点 prepare→approval_wait→execute/finalize；真实Postgres saver；合成用户审批薄FastAPI入口；两个下游：支持key去重/查单和完全不支持key。无真实业务外部写、无Planner/完整UI。
- 环境/数据：E1，两个进程/双空间、一个可审计计数器。审批hash与当前membership可单独撤销；state只含refs。
- 提交边界：业务意图+待批先提交；interrupt保存独立事务；批准+resume唤醒事实提交后宿主恢复；审批consume+ToolCall executing同事务；外部写在事务外；receipt提交后返回StateDelta；checkpoint不参加业务事务。
- 方法：先正常重启再故障断点；对锁连接和saver连接分别切断，旧writer保持活跃，第二进程竞争；50轮每崩溃窗口/50次重复resume；检查checkpoint分支、DB owner/epoch、下游实际写次数。事务前未发出确定为0，成功已确认最终1；未知不可断言未执行，只能断言不自动重发。
- 验收/产物：结果/receipt/hash/安全事件可关联，零越权、零未知写自动重放，双writer实验必须通过或明确单活降级结果。T05/T06证据+对应 [TDD04 §7/8](../tdd/04_LangGraph_Workflows.md)。
- 退出：副作用重复/越权/双writer破坏核心假设→FAIL；缺真实PG为BLOCKED；仅单活可安全运行则PARTIAL（多实例假设失败），不得悄悄PASS。细则见验收表。

## 5. PoC-02：Outbox 与 Celery

- 目标：业务提交后最终有可核查作业；回滚不发任务；至少一次投递由业务幂等承受。范围为固定无外部写consumer和受控Job，不做通用任务平台。
- 环境 E2。Outbox拟验证状态：`pending/dispatching/retry_wait/dispatched/dead_letter`；唯一拥有者OutboxService。claim短事务pending/retry_wait→dispatching，生成lease_epoch；事务外发送后CAS dispatched；发送失败→retry_wait；过期lease→retry_wait；可判定poison/次数耗尽→dead_letter（仍保留行和原因）。人工redrive经授权+记录原因/旧epoch作废，回pending，沿用outbox_id/job_id/dedup_key；业务终态Job不可靠redrive复活，应新受控重试命令。
- 消费键为workspace+job_id（及Job kind/dedup唯一约束），attempt_id仅真实领取生成；重复transport消息不创建新业务Job。T01提出将Outbox枚举及redrive纳入TDD修订，本轮不直接改TDD。
- 顺序/方法：T1业务+Job+Outbox同事务；dispatcher读已提交行claim→Redis发送→标记；consumer JobService CAS；处理结果提交再ACK。注入rollback/Redis断、发成未标记kill、consumer领取后kill、发布结果后ACK前kill、两个dispatcher/worker、broker清空、重试耗尽。DB巡检扫未派发和过期Job，不只靠Redis。
- 验收：100逻辑Job，每个10次重复投递；恢复依赖后在测试deadline内100个进入明确终态且每个只有一份业务结果；50次生产事务回滚无下游执行；死信人工重放可追踪且不改变已成功事实。建议恢复deadline60秒，实验参数非生产SLA。
- 退出：丢任务/rollback消息可执行/重复结果/死信无法恢复→FAIL并阻T12；临时依赖不可用记BLOCKED；吞吐/扫描间隔可调不绕过硬约束。替代CDC/队列换型要重评ADR-0002，不承诺Celery提供exactly-once。

## 6. PoC-03：摄取与原子索引发布

- 目标：检索只见已发布且当前授权的版本；所有读取join active指针+winning_attempt。业务状态由Ingestion/IndexPublication/Job各服务在共享UoW转换，Worker不直写其他域。
- 最小范围：一个KB、两空间、同文两版本、可控embedding（先确定性替身测一致性，再一条真实embedding通路）；pgvector+FTS、S3私有对象/manifest、核心与富化分离；不做全格式UI。
- 输入：PoC-04标准化解析产物，PoC-02可靠Job；T12 staging/引用，T13发布与故障。源许可/ReviewDecision在测试内真实记录，不能伪造批准boolean。
- 方法：在staging、manifest-ready、T3提交前/中/后、ACK前kill；v1慢v2快、同版本不同attempt、删除与重建/取消、新embedding维度；对象写后DB回滚/对象缺失；富化失败。并发reader连续采样活动chunk/source/citation元组，用PG单snapshot查，不跨请求伪造原子视图。
- 验收：100轮version/attempt竞态，每轮≥100次读取；零半成品/混attempt/跨workspace命中，v1不能替换v2；删除提交后的新查询零命中；旧版本历史引用仍指原source_version或明确不可访问。核心成功富化失败保持searchable，孤儿GC不删引用中对象。
- 退出：半成品/权限泄漏/旧覆盖→FAIL阻PoC-09/08；缺对象服务BLOCKED不能靠本地文件mock声称S3兼容。性能优化可延期，串行降级需评审/重测。关联 [TDD06 §5/6](../tdd/06_Knowledge_Ingestion.md)。

## 7. PoC-04：DocReader 真实协议/定位

- 目标：实际服务与固定proto兼容，PDF/DOCX/Markdown及图片表格的定位可验证；TXT/MD/HTML路线明确。
- A/本轮只读：参考commit同基线；[proto](../../references/WeKnora/docreader/proto/docreader.proto)、[main.py](../../references/WeKnora/docreader/main.py)、[registry](../../references/WeKnora/docreader/parser/registry.py)已定向复读。ReadRequest无workspace，Read响应error字段与gRPC status不同；ReadStream先meta后image，不是Markdown增量流；SourceBlock为Unicode code point半开区间。所有运行结果仍U。
- T09先清单/proto hash/镜像/依赖许可；T10才生成隔离client并连真实服务；T11处理资产/变换/五格式golden。不在references内生成stub或修改server；若需补丁另立授权实验副本。
- E3测试50标准样本+拒绝集；PDF页/bbox、DOCX段落/表格、MD/TXT范围、HTML快照；中文emoji/CRLF/换图转表。请求bytes，禁任意远端URL/override；真实auth/TLS、deadline/cancel、ReadStream→Read仅UNIMPLEMENTED降级，meta.error、缺meta/错序/图片断流由代理/故障shim测试，不能将shim当真实互通证据。
- 隐私负例覆盖DocReader服务端自身日志：main.py会记录file_name/URL/异常，调用仅用opaque资产文件名和已脱敏metadata，不把原用户文件名/敏感URL送入该日志路径。错误body/traceback仍需真实canary扫描，不能只清洗Python client日志；若隔离服务无法满足，禁用该路径或另行评审实验镜像补丁，禁止直接修改参考源码。
- 验收：五格式整体≥49/50标准成功（98%建议门槛），每格式≥9/10；所有“声称精确”的定位必须100%匹配gold locator，拒绝集100%正确分类，无静默截断/缺图发布。输出逐格式真实能力矩阵、RSS/耗时原始记录；缺定位可文档级降级但对应精确定位项PARTIAL，不能由总成功率遮盖。
- 退出：协议不兼容/安全缺失/伪定位→FAIL；缺镜像/OCR/许可资料→相关子集BLOCKED；只有无关高级格式失败可延期。更换parser/裁剪格式触发ADR-0004和TDD06评审。

## 8. PoC-05：浏览器双适配

- 前置：T14获取/检查BrowserSkill真实源码/协议/许可（目标upstream `da6bf4eed2dd7256567e152df8c903c87f6598c3`、daemon/extension成套0.3.1）；当前WeKnora只有接入层/构建清单，不能当独立upstream已审。没有材料先BLOCKED，不猜wire方法。
- T15统一业务port与capability交集/unsupported；T16真实Playwright；T17真实BSK配对/读取；T18断线/revoke/Handoff/多owner和未知动作恢复。E4、自有控制站、测试账号、人操作登录/验证码。
- 公共语义为授权scope/session、read artifact、deadline/cancel、稳定tool_call、错误/unknown_outcome；BSK特有为用户标签页授权/配对/Handoff；Playwright特有为临时context/服务端网络隔离。不存在的方法明确unsupported，不强制互相模拟。bridge或Python直连需T14证据后定，不能先大规模复制Go。
- 方法：授权前/后、token续期/重放/revoke、pause、owner crash/迟到旧epoch；控制站动态JS、redirect/DNS rebinding/iframe/下载/WS/IPv6；命令发出后超时/cancel。cookie/profile/截图PII不跨空间，不进state/模型/trace。用户网络无法覆盖子资源策略的BSK能力直接标unsupported/禁用。
- 验收：至少20个网络/权限控制向量（每后端适用项必须全通过），50轮未知写/owner故障零自动重放；每后端有真实read/provenance/Handoff支持矩阵，设备/版本/hash证据完整。mock只验port，不能代替真扩展。
- 退出：私网/权限逃逸或未知写重放→FAIL；Playwright通过BSK缺设备→PARTIAL或BSK子集BLOCKED，聚合不能PASS。关闭BSK/只读/单owner是显式降级，重评ADR-0004及相关TDD05。

## 9. PoC-06：Harness 最小内核

- 最小范围：单Provider ModelGateway、三个工具（只读/受审批写/确定性错误）、PolicyEngine、配对完整上下文窗口、预算账本/trace。AgentRuntime只提供node可调用门面，checkpointer由PoC-01复用；无插件市场、动态代码、Skills、通用Agent Framework。
- E1/E5；T19模型/registry，T20context/预算，T21与Graph/Job集成。实际provider至少一次结构化tool/usage请求，其余错误矩阵用可记录替身；费用先定硬上限。
- 方法：未知工具/schema变更、length截断参数、只读并行/写屏障、429/5xx模型retry与工具retry区分；上下文超限/摘要失败保留tool pairs；最后额度并发争抢、unknown usage不释放、cancel/deadline、trace exporter失败、SDK错误脱敏。
- 验收：≥20种上下文/错误fixture完整消息配对；100轮双并发预算+重规划模拟无重复扣费/账本超预留；50次repeat settle幂等；只读4并发写屏障顺序可观察；secret/PII canary零出现；原生checkpoint/interrupt调用图唯一。供应商真实超预留需记账冻结，不将账本测试等同绝对费用上限。
- 退出：自建第二套恢复/秘密泄漏/预算逃逸→FAIL；真实模型不可用→相关项BLOCKED；仅第二Provider/高级摘要策略可延期。关联 [TDD02](../tdd/02_Agent_Harness.md)。

## 10. PoC-07：DAG 与状态协调

- T22结构化Planner/schema/无环；T23 ready/执行/失败传播；T24Evaluator/Replanner/revision/复用。E1/E2/E5，用TDD03九节点行业标准研究DAG，实际Planner一次、固定计划用于故障回归。
- Graph节点做规划/评价/路由/interrupt和共享Harness，Celery只执行固定collect/extract/ingest作业；OrchestrationService唯一写Task事实。普通RAG不调用Planner。初值32节点/64边/深度8/并发4/最多2次重规划，PoC上限非容量承诺。
- 方法：环/自依赖/缺节点/超规模、depends失败与optional、两个scheduler、checkpoint回退但Task完成、旧revision迟到、取消/未知写、预算最后额度并发、产物授权变化。
- 验收：≥20非法计划100%在执行前拒绝；100轮ready/revision竞争无提前执行/双领取/旧结果覆盖；已确认成功产物且input/profile/policy相同才复用，成本归父run一次；重规划到上限必须停止。Evaluator缺证据不视通过，不能模型输出“通过”覆盖硬规则。
- 退出：不满足依赖仍执行/重复写/预算逃逸→FAIL阻PoC-08；质量阈值待标注可PARTIAL，不能进自动发布。失败可固定模板/串行研究，重评TDD03/04而非强塞RAG。

## 11. PoC-08：Research 最小端到端

- 依赖PoC-01/02/03/04/05/06/07/09关键项通过；T27先质量/去重/审阅，T28全链。E1–E5、本地许可站点/允许目录；不依赖绕过登录/验证码/反爬。
- 流程：目标→Planner→搜索fixture（固定本地搜索适配，外部搜索需另记授权）→静态/JS/已授权用户浏览器→snapshot/质量/许可/PII→精确/近重簇→evidence→人工ReviewDecision→异步Job→active版本→RAG引用。默认人工批准，否决/过期/无权不能发布。
- 测试30研究任务含重复正文不同URL、转载冲突、改时间/许可不明、无locator/注入/PII、两轮来源变化/不变、取消和最后预算竞争；正例至少一条全链通过每通道，负例必须明确待审/拒绝。BSK不可用时只能形成子路线结果，不冒充AC-E02整体通过。
- 验收：发布候选100%有源URL+snapshot+时间/通道+证据与审批；无权/禁采/无证据发布数0，精确重复不重复可见索引；更新生成新版本，旧来源历史仍可核验；每任务预算/失败原因/审阅人opaque ID齐全。30任务任务成功率真实测量并分类，不先承诺未校准质量分数。
- 退出：污染入库/无证据或越权发布→FAIL；缺前置→BLOCKED，不能绕过直接写pgvector；只缺生产定时器/多站点连接器可延期，PoC不含Wiki实现。

## 12. PoC-09：RAG 质量与引用

- T25真实Embedding/pgvector/FTS/Hybrid+RRF+可选Rerank；T26LangGraph RAG/最终消息事件/评测。E3/E5，100QA标注金标准，包括无证据问题、跨空间同文、旧版引用。先复用PoC-04解析与PoC-03发布，不能另写一条无版本摄取。
- 方法：FTS/向量/Hybrid对照，中文分词/profile固定；拒绝混embedding space；Rerank不可用降级有diagnostics；问题改写/补搜有限。100QA统计Recall@5/MRR/NDCG/引用精确率/事实忠实度/拒答；10万chunk容量和20会话为可单列测量，不以PoC接口200代替质量。
- 流式：token先持久化、finalizer Message+Run+terminal同事务；部分发送后保存失败/进程kill、新generation reset、取消；P0不开放公共续传，测试内部事件重放/过期边界不等于交付P1 SSE续传。
- 验收：权限/版本/引用目标完整性100%，无证据明确不确定；Recall@5≥0.85、引用准确率≥0.95是待首次基准评审候选，不自动认定正式门槛。未定门槛/未标注→PARTIAL，不可宣称质量PASS。PRD性能目标另测边界并报告硬件与模型。
- 退出：越权/伪引用/伪completed→FAIL阻P0；质量/中文分词未达→评审检索方案（FTS不等于BM25）并重测；模型不可用BLOCKED。关联 [TDD04 §3](../tdd/04_LangGraph_Workflows.md)、[TDD07 §6](../tdd/07_Data_API_Security.md)。

## 13. 当前运行状态与后续

所有PoC结果NOT_STARTED，环境命令只草案；缺真实设备/模型/镜像/许可是已知前置缺口，不假装本轮尝试运行失败。完整PASS/PARTIAL/FAIL/BLOCKED规则、阻断和ADR重审见 [05](05_Phase0_Acceptance.md)。优先执行资料/契约T01，再并行T02/T09/T14；具体任务依赖以 [04](04_Phase0_Dependency_DAG.md) 唯一表为准。

**截至 W14（2026-10-29）的实时状态**：上一段是 W0 规划时点的状态，不改写。实际执行到 W13 收口：§3 总览表中 **8 项 `PASS（限域）` + PoC-09 `PARTIAL`**；门级仍**未通过**——P0 核心出口 G-P0 = 4 门 PARTIAL（G1/G2/G3/G4）+ 1 门 NOT_RUN（G5），全量出口 G-FULL 同样不成立，详见 `docs/poc/evidence/P0_Core_Gate_Review.md` 与 `Phase0_Full_Gate_Review.md`。**「限域 PASS」不等于门通过**，本表的维护责任与更新时机见 §3.1。
