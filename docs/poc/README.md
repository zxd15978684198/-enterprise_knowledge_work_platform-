# Phase 0 验证

本目录保存 Phase 0 架构验证规划、任务 Backlog、验收标准及后续实测证据。2026-10-27 已交付下列规划；九项 PoC 均为 **NOT_STARTED**，没有安装依赖、启动容器或运行实验。

1. [ADR 就绪度与设计一致性评审](00_ADR_Readiness_Review.md)
2. [技术风险登记表](01_Technical_Risk_Register.md)
3. [九项 PoC 计划](02_Phase0_PoC_Plan.md)
4. [32 项单会话任务 Backlog](03_Phase0_Task_Backlog.md)
5. [依赖 DAG、执行波次与关键路径](04_Phase0_Dependency_DAG.md)
6. [验收、故障注入与 Exit Gate](05_Phase0_Acceptance.md)
7. [逐任务执行指南及下一轮提示词](06_Phase0_Execution_Guide.md)

第一项为 P0-T01（只写实验契约闭合文档）。TDD/ADR 保持提议原状，五份 ADR 未自动接受。风险评分和阈值是建议，不是实测概率或性能。P0 核心准入与九项全量 Phase 0 退出分开，缺真实 BrowserSkill 不得宣称双后端通过。

每项实验需记录目标、前置条件、依赖、最小实现范围、真实样本、运行命令、预期与实际结果、退出条件和风险；缺少服务、模型或设备时记录阻塞，不用模拟结果冒充真实集成通过。
