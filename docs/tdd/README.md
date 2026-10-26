# 技术设计文档

此处 TDD 指 Technical Design Document。2026-10-27 已交付八份设计提议，尚未通过架构评审/PoC，不代表业务已实现。

1. [总体架构及统一契约](01_System_Architecture.md)：先读 ID、状态拥有者矩阵和决策清单。
2. [Agent Harness](02_Agent_Harness.md)
3. [Task Orchestration](03_Task_Orchestration.md)
4. [LangGraph Workflows](04_LangGraph_Workflows.md)
5. [Browser Adapter](05_Browser_Adapter.md)
6. [Knowledge Ingestion](06_Knowledge_Ingestion.md)
7. [Data / API / Security](07_Data_API_Security.md)：数据约束、REST/SSE 和稳定错误码。
8. [Test Strategy](08_Test_Strategy.md)：需求追踪、十一类故障、阶段门槛。

编号决策见 [ADR 索引](../adr/README.md)，均为提议；统一术语以 01 为准，接口错误码以 07 为准。代码块为自有类型草案/协议示例，不承诺可直接运行或是第三方精确 API。

未通过 PoC 的协议兼容、恢复、性能和安全假设须标为待验证。
