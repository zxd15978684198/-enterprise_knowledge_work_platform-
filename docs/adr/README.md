# 架构决策记录

本目录保存 ADR。2026-10-27 新增五份编号 ADR；2026-10-28 新增 [0006](0006_Single_Active_Checkpoint_Runner.md)；2026-10-29 依 W11（T32 全量出口评审 §6.1）建议新增 [0007](0007_Redis_License_Scope.md)–[0014](0014_Quality_Threshold_Approval.md)。**十四份状态均为提议，当前没有已接受 ADR**；用户规范和 PRD 的既定职责继续适用。

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| [0001](0001_Runtime_Boundaries.md) | 运行时职责与状态边界 | 提议 |
| [0002](0002_Outbox_Version_Publication.md) | 可靠派发与索引版本发布 | 提议 |
| [0003](0003_Approval_Side_Effects.md) | 审批、interrupt 与副作用核查 | 提议 |
| [0004](0004_Parser_Browser_Reuse.md) | DocReader 与浏览器双适配复用 | 提议 |
| [0005](0005_Workspace_Events_Privacy.md) | 工作区、引用、事件与隐私 | 提议 |
| [0006](0006_Single_Active_Checkpoint_Runner.md) | P0 单活 Checkpoint Runner | 提议 |
| [0007](0007_Redis_License_Scope.md) | 缓存/队列许可：Redis 7.4 RSALv2/SSPLv1 或迁 Valkey | 提议 |
| [0008](0008_Psycopg_LGPL_Usage.md) | psycopg（LGPL-3.0）合规使用与许可门禁 | 提议 |
| [0009](0009_Deployment_Single_Active_Scope.md) | P0/P1 部署范围与单活边界（补充 0006） | 提议 |
| [0010](0010_Pgvector_Core_Profile.md) | pgvector 进 core profile 与 T02 证据重采 | 提议 |
| [0011](0011_Identity_Domain_C07.md) | 身份域与来源审阅范围（C-07 决策） | 提议 |
| [0012](0012_Real_Backend_Scope.md) | 真实后端接入范围（MinIO/Celery/provider） | 提议 |
| [0013](0013_Browser_Channel_Positioning.md) | 双浏览器通道产品定位 | 提议 |
| [0014](0014_Quality_Threshold_Approval.md) | 质量阈值与基线批准流程 | 提议 |

FTS/BM25 选型、Wiki 优先级及产品未决项见 [总体 TDD 决策清单](../tdd/01_System_Architecture.md)。

使用 `NNNN_主题.md` 命名，编号递增。每份包含：日期、状态（提议/已接受/已替代/已撤销）、关联需求与证据、背景、决策提议、备选方案、后果、验证条件及替代关系。已接受记录发生变化时新增 ADR 并关联旧记录。

## 待评审（按裁决方分组）

- **需法务/产品**：[0007](0007_Redis_License_Scope.md)（Redis 许可三选一，建议迁 Valkey）、[0008](0008_Psycopg_LGPL_Usage.md)（LGPL 披露措辞）。
- **需产品/架构**：[0009](0009_Deployment_Single_Active_Scope.md)（单活范围与多实例前置条件）、[0012](0012_Real_Backend_Scope.md)（真实后端豁免与接入顺序）、[0013](0013_Browser_Channel_Positioning.md)（Playwright P0 必需还是 P1 可选）。
- **需安全**：[0011](0011_Identity_Domain_C07.md)（C-07 身份域三选一）、[0014](0014_Quality_Threshold_Approval.md)（安全相关阈值批准权）。
- **需工程执行（前置授权）**：[0010](0010_Pgvector_Core_Profile.md)（重采 T02 证据 + 补跑 T25 ANN）。
- 关联既有记录：[0006](0006_Single_Active_Checkpoint_Runner.md) 的验证条件合并评审；0009 依赖 0006 一并裁决。

新增决策继续编号，不把设计提议自动升级为已接受；任何「已接受」必须由对应裁决方在本目录留下明确评审记录。
