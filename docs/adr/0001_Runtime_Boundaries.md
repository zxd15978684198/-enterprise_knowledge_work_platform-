# ADR-0001：运行时职责与状态边界

- 日期：2026-10-27
- 状态：**提议**（无已接受前序 ADR）
- 依据：AGENTS.md；PRD §7/13、F-09/10；审计 A14–A18/A23、R01–R02

## 背景

业务事实、Graph 恢复、子任务和后台作业必须协作，不能各自实现一套总状态机。上下文摘要不同于执行 checkpoint，原 child-agent 也不同于通用 DAG。

## 决策提议

FastAPI 管业务；四个独立 LangGraph 管推理控制流/原生 checkpoint/interrupt；Harness 管模型、上下文、工具、安全与预算；Task Orchestration 按需管有限 DAG；Celery 只做可靠唤醒和固定作业。PG 保存业务事实，原生 checkpointer 保存执行状态。run↔thread 1:1，conversation 含多 run，多轮消息由业务加载。P0 Graph 由独立 Linux Celery worker 承载，单写者约束见 TDD。

## 替代方案

自研快照引擎会重复框架并扩大恢复风险；万能 Graph 会耦合所有模式；把推理状态机放 Celery 会失去统一 interrupt 语义。常驻 Graph Runner 服务作为后续可逆部署替代，需吞吐/延迟证据。

## 后果与验证条件

需要业务事实/执行状态对账、旧 Graph 版本宿主和单 owner 控制；不能跨库原子提交。H1/H2/H3 验证 interrupt 重入、单 writer/checkpointer 连接失效、状态修复和复杂任务调度。失败时先单活降级，不声称安全横扩。评审后才能改已接受。

实现契约：[01](../tdd/01_System_Architecture.md)、[04](../tdd/04_LangGraph_Workflows.md)。替代关系：无。
