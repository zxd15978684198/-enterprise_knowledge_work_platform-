# 兼容性、许可证与 Phase 0 风险

日期：2026-10-27。源码 commit：`662abaf9f944e37689d04c42160034b2cdec6829`。事实及编号证据见 [源码审计](01_Source_Architecture.md)，目标建议见 [架构映射](02_Architecture_Mapping.md)。A=源码静态已验证，D=仅官方文档描述，N=新建议，U=待运行或外部核验。

## 1. 审计边界与当前阻塞

本地 WeKnora 源码可读、基线未变化，完成本轮报告没有硬阻塞。当前没有获取单独 BrowserSkill upstream checkout、启动解析/模型/数据库/浏览器服务或安装依赖；shell 未发现 Go。协议互通、性能、攻击防护及故障恢复均未实测。本文件不是法律意见、漏洞定级报告或已经完成的全量依赖审计。

PRD 以 v0.8.3 作需求参考，本地是 `v0.8.3-202-g662abaf9f`。应固定当前 commit 做研究；是否另做 v0.8.3 对照需后续决定。旧报告只记录 tree SHA，不能推定与本轮 commit 等价。后续文档需继续标记固定基线，不能无声更新 reference。

## 2. 兼容矩阵

| 边界 | A / 已核实 | 不能承诺的兼容性（U） | N / 后续处理 |
| --- | --- | --- | --- |
| API/认证/前端 | Handler 与业务权限分层、session 两种 QA（I01、R12） | REST 路径、错误码、multipart、Cookie/API Key、SSE payload 与 Vue 全量兼容 | 先列 P0 API 契约/前端依赖，按需 adapter；不直连旧表 |
| workspace/tenant | caller、执行 tenant、共享 KB、浏览器 scope 各有边界（R05、B01） | TenantID 改名 workspace_id 即完成隔离；后台 payload 即可信权限 | 明确 Principal/资源 owner/执行 scope；跨工作区 ID、引用与恢复做拒绝用例 |
| checkpoint | context summary 与 RunSnapshot 分离、snapshot version 校验（A14–A16） | 原 JSON 可直接导入 LangGraph；旧工具执行状态可无损恢复 | 运行与 schema 版本化；旧数据迁移另立 ADR，P0 不默认迁移 |
| Browser wire | bsk-auth、system.handshake、Unix JSON RPC、session/instance 绑定（B04–B08） | 任意最新版扩展/daemon、MCP 或 Playwright 协议可互换 | 固定成套版本，capability probe，真实 golden conversation |
| Browser 所有权 | DB lease、跨节点签名路由、pause/epoch（B03、B08、B10） | owner 崩溃无损接管；断网后能安全重放 click/submit | unknown_outcome 独立状态；重连只恢复连接，显式恢复任务 |
| 浏览器跨平台 | 原 gateway 使用 `/tmp` 与 Unix socket（B05、B07） | Windows 原生运行与 Linux gateway 行为一致 | 优先容器/桥接或实现 OS transport，不能只替换进程命令 |
| DocReader proto | 文件/URL、Read/ReadStream/ListEngines/PreprocessImage（C01） | gRPC OK 等于解析成功；stream 表示 Markdown/上传也流式 | 同时检查 status/meta.error/EOF 完整性，限制单帧及总大小 |
| Parser engine | Go local + remote、Python engine fallback、URL 忽略传入 engine（C03、C05、C06） | 只接 gRPC 即覆盖 Go simple/anydoc 和 P0 TXT | 显式支持矩阵；未知/回退 engine 可观察；TXT 用独立适配 |
| 解析产物 | Markdown、图 bytes/ref、SourceBlock（C01、I04） | Python 直接复制 parser 输出即可原文引用/图片访问 | storage、资源绑定、正文变换与 locator 重映射单独验证 |
| 队列 | Asynq 标准模式、Lite 分支、retry/死信（I03、I12、I13） | Celery 默认行为等于 Asynq；所有任务 transaction outbox | 指定 ack、重投、取消、最大重试、派发和死信策略并故障注入 |
| 文档版本 | attempt guard、source replacement、清理补偿（I04–I09） | 已存在不可变 DocumentVersion 与全程原子索引切换 | 新版本隔离构建、稳定幂等键、发布 CAS；旧 Worker 不能激活 |
| 检索 | ParadeDB operator + pgvector、加权归一 RRF（R05–R08） | 普通 PG FTS 等于 BM25；不同模型得分可以直接比较 | FTS/BM25 双基线；按模型空间分索引；保留 score/match diagnostics |
| SSE | 事件缓存与授权续传、先持久化后 completed（R12–R15） | token 流可跨任意宕机继续、buffer 永不丢失 | run_seq/cursor/TTL/已完成快照与断线恢复契约 |

## 3. 重点风险与验证出口

下面的“风险”是从已读实现与 PRD 差距推导的设计风险，不指控存在已可利用漏洞。

| ID / 优先级 | 证据与失败窗口 | N / 缓解及验收 |
| --- | --- | --- |
| SEC-01 / 高：共享执行越权 | A：原 HybridSearch 必须对每个 KB 显式授权，浏览器用原 caller（R05、B01） | 所有查询/对象签名/审批恢复使用后端 scope；跨 workspace 的 KB/chunk/message/run ID 均拒绝且不泄露内容 |
| RUN-01 / 高：批准已消费但副作用未知 | A：Gate 消费 approved→executing，ConfirmedTool 后执行，再 MarkExecuted（A17、A18） | 工具调用 ID、参数 hash、外部 idempotency key/receipt；在“外部成功后、保存前”杀进程，不自动重复写；不可去重则人工核查 |
| RUN-02 / 高：Checkpoint 与业务状态分叉 | A：摘要 checkpoint best-effort、run snapshot 单独保存（A14–A16） | 明确 business run ↔ graph thread ↔ task/job 映射与唯一状态拥有者；审批后宕机/重启/重复恢复都只发布一份结果 |
| ING-01 / 高：旧 attempt/版本覆盖 | A：attempt<=0 不拦截，旧 payload 可回退 OpenAttempt，初始创建构造未见显式 Attempt（I02、I04、I05、I09） | 核验所有生产者与包装；新系统强制版本/attempt，不允许隐式新开版本；v1 慢任务晚于 v2 返回时不能覆盖 v2 |
| ING-02 / 高：DB/Redis 派发裂缝 | A：普通上传直接 Enqueue；Wiki 有专用 pending-op 事务（I02、I10、I11） | 新业务用 Outbox/可靠派发；模拟提交 DB 后 Redis 不可达与重启，恢复后任务不丢、不重复发布 |
| ING-03 / 高：索引与富化总状态混淆 | A：enabled 可与 processing 共存；finalizing 由 counter 控制（I07、I08） | 单独定义 searchable、ingestion、enrichment 状态；避免 parse failed 隐藏已成功索引或富化悬挂；重投终结不得重复扣别的子任务名额 |
| BRO-01 / 高：连接所有者与重放 | A：lease、nonce、epoch、断线禁止重放（B07、B08、B10） | 双副本并发配对/owner crash/租约超时测试；写动作超时不自动切节点执行 |
| BRO-02 / 高：版本假兼容 | A：relayHandshake 只检查非空 protocol；实际 pin 见 B11 | 将扩展/daemon/protocol/capabilities 分开记录；错配版本必须明确拒绝或降级，不只判断“连上了” |
| WEB-01 / 高：提示注入与 SSRF | A：route guard + SSRFProxy（C07、C08）仅是网络实现证据 | 重定向/DNS rebinding/iframe/下载测试；外部正文只作为数据；页面诱导更换系统指令、取密钥或写外站均不能提升权限 |
| DOC-01 / 中高：stream 半完成 | A：首帧完整 meta 后图流可能异常（C02），客户端累计 images（C04） | 首帧后取消/中断，不能把缺图半成品发布；测试大 meta/单张大图/大量页，观察客户端与服务端 RSS |
| DOC-02 / 中高：source locator 漂移 | A：Unicode codepoint offsets，正文后续会被规范化/换图（C01、I04） | 中文/emoji/组合字符、表格/图片替换后定位准确；无可验证位置时降级为文档级引用并标注 |
| OBS-01 / 高：内容/秘密观测外泄 | A：Registry PipelineInfo 包含 args，think/act 有 debug preview，stream plugin 传 prompt/content 给 pipeline 日志（A08、A06、A07、R11） | 本轮未验证所有 logger/trace 的脱敏链，不能断言已泄漏；用 canary 凭据/PII 扫描日志、trace、state、错误，默认记录 hash/计数/标识而非原文 |
| GOV-01 / 中高：并发预算分散 | A：工具 8、模型后台 governor、child Runtime 限额各自存在（A07、A22、A23） | run/child/tool/embedding 共享预算预留与结算；重试/重规划算成本，超预算停止派发并释放资源 |
| DAG-01 / 高：重规划无限/重复执行 | U：未证实原有通用 Task DAG/Evaluator/Replanner 契约 | schema/DAG 无环/任务数上限/revision/并行合并/失效产物；重规划不能改写已执行副作用或绕过审批 |

## 4. 许可证与第三方依赖

### 4.1 已读证据与可据此作出的判断

| 本地固定版本证据 | A / 声明 | 复用/分发边界 |
| --- | --- | --- |
| [LICENSE:1–35](../../references/WeKnora/LICENSE#L1) | WeKnora 自有代码 MIT，明确排除第三方组件 | 复制实质代码/材料需保留版权及许可；不把 MIT 自动套给所有组件 |
| [LICENSE:252–263](../../references/WeKnora/LICENSE#L252) | anydoc 有 Tencent modifications；Wails 等另列 | 实际选入组件、构建标签与修改内容需逐项归档 |
| [THIRD_PARTY_NOTICES.md:1–39](../../references/WeKnora/THIRD_PARTY_NOTICES.md#L1) | MySQL Driver、go-m1cpu 的 MPL-2.0 及对应源码 | 原 Go 二进制的依赖不自动变成 Python 产品依赖；若保留桥接二进制须评估整套分发材料 |
| [THIRD_PARTY_NOTICES.md:42–79](../../references/WeKnora/THIRD_PARTY_NOTICES.md#L42) | OpenCC 字典 Apache-2.0；build-only cbindgen 条件说明；源码归档打包时获取 | 字典数据也有许可；离线构建/分发需准备对应源归档；生成工具与运行时组件区分 |
| [THIRD_PARTY_NOTICES.md:81–143](../../references/WeKnora/THIRD_PARTY_NOTICES.md#L81) | RapidOCR/模型、OpenCV Apache-2.0；ONNX Runtime MIT；NumPy BSD-3-Clause | OCR 模型权重、native wheels、附带组件通知分别评估；不要只保留 Python 包主 LICENSE |
| [frontend/src/assets/browserskill/LICENSE:1–21](../../references/WeKnora/frontend/src/assets/browserskill/LICENSE#L1) | 随仓 BrowserSkill 资产附 MIT / Tencent 版权 | 这是本地资产许可证据，不能代替本轮未获取的 pinned upstream 全量依赖审计 |
| [scripts/build_browserskill.sh:13–52](../../references/WeKnora/scripts/build_browserskill.sh#L13) | daemon/extension 同 SHA，构建时复制 upstream LICENSE | 发布需保留产物、版本、哈希、许可/通知；扩展商店分发与用户授权另行核验 |

以上只报告原文件声明和工程处理建议，不对特定分发方式作法律结论。PyKnora 根许可证尚未选定，不由本轮擅自指定。

### 4.2 DocReader 依赖与复用成本

**A**：`docreader/pyproject.toml:1–37` 声明 Python >=3.10.18，使用 grpcio/protobuf、Pydantic、Playwright、MarkItDown、textract、PDF/Office 库及 OCR/native 依赖；很多是下限范围，不能当锁定版本。另有 uv.lock，部署应核对实际锁定环境和 CPU/OS/架构，而不是直接安装所有最新包。[C10]

**D**：官方入库文档说明 opendataloader 需 Java 11+、anydoc 受 cgo/构建标签影响。[D02] **U**：本轮没有安装/执行这些引擎，未验证 Python 3.12/3.14、Windows/Linux、镜像大小、浏览器下载和字体/系统包兼容。

**N**：先以容器化解析服务控制依赖边界，生成实际 SBOM 与锁定产物清单。仅在确认需要的格式和模型后裁剪依赖；不凭包名猜许可证，不把原项目的 NOTICE 版本认定为当前 PyKnora 已安装版本。BrowserSkill upstream 若不可联网，提供 fixed commit 的 bundle/源码+产物哈希/许可证离线包；缺失就阻塞真实互通验证，不能用接口 mock 冒充成功。

## 5. Phase 0 必验证假设

下面是 N / 建议实验与退出条件，不是已经执行的 PoC 计划或测量结果。质量/容量阈值以 PRD 待评审目标为准，不虚构基线分数。

| 实验 | 假设与最小场景 | 必要证据 / 退出条件 |
| --- | --- | --- |
| P0-H1：LangGraph 恢复/审批 | 模型提出敏感写 → interrupt → 进程退出 → 有效主体批准 → resume；重复审批、权限撤销、参数修改 | 无越权；成功副作用不重复；未知执行结果进入核查；checkpoint 与业务 run 能关联 |
| P0-H2：Context/工具协议 | 长历史、超大工具输出、摘要失败、provider overflow、长度截断参数、多 provider | 工具调用/结果配对；完整参数才可执行；近期用户约束和关键证据保留；所有重试计成本 |
| P0-H3：Task DAG | 自然语言计划转 schema；环/缺依赖/超任务数/child 失败/重规划 revision | 非法计划调度前拒绝；只启动 ready 节点；失败传播/取消明确；重规划不能重复已完成副作用 |
| P0-H4：DocReader/golden set | PDF/DOCX/TXT/MD/HTML；带图/表/中文 emoji；扫描大 PDF；engine 错配、auth/TLS/deadline | 记录真实支持矩阵；TXT 回退明确；meta/error/流异常可判定；图不丢、locator 正确、取消能清理 |
| P0-H5：异步入库 | DB 后断 Redis、重复投递、Worker kill、v1/v2 竞态、删除/取消、后处理失败 | 恢复无丢任务；每版本一次发布；旧版本不可覆盖；索引半成品不可见；富化失败可诊断 |
| P0-H6：RAG | 受控文档+标注问答；FTS/向量/混合对照；RRF/Rerank 缺失或失败；跨 workspace 探测 | 记录 Recall/MRR/引用质量、成本与延迟；无权限泄漏；无证据拒答；不把 FTS 与 BM25 混同 |
| P0-H7：Browser 双后端 | pinned 扩展+daemon 真设备；Playwright 控制站；配对/续期/撤销、人工接管、断线、多节点 | 授权与 scope 正确；状态可区分；断线写不重放；平台兼容有真实记录；mock 与真实结果分开 |
| P0-H8：Research 沉淀 | 搜索→受控 HTTP/JS 网页→快照/证据→候选审核→异步入库→RAG 引用 | 来源/许可/版本齐全；重复正文不重复索引；低质量和注入内容不能自动发布 |
| P0-H9：日志与出站安全 | canary secret/PII、恶意文档指令、重定向/私网/DNS rebinding、对象签名越权 | 日志/trace/state 无秘密；禁用目的地不可访问；引用与产物访问也有授权 |

建议顺序：H1/H2 与 H4/H5 先确定恢复和摄取基座，之后 H6；H3 与 H7 的接口可设计，但端到端 H8 依赖这些结果；H9 贯穿所有实验。没有真实服务/设备时记录 blocked，不写“兼容通过”。

## 6. 下一轮前置问题

- Wiki 在 PRD §6 F-14 是 P0，而摘要/数据模型/API/里程碑多处指向 P1；需要评审，不在研究阶段擅改需求。
- 是否保留 Vue、旧 API 或旧数据迁移；若需要，补足 compatibility contract 与迁移 golden data。
- BrowserSkill 是 Python gateway 直接接管还是先保留独立 Go bridge；目标部署是否要求原生 Windows。
- 首批允许采集域名、审批角色、可存全文/只存摘要的规则以及可用模型与设备尚需落实。

交付限制：本轮只新增三份研究报告并更新实施状态；没有修改参考源码、Python 业务代码或依赖，没有运行业务单元/集成测试。静态文档校验结果在状态文件记录。
