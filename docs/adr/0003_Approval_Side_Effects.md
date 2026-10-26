# ADR-0003：审批、原生 interrupt 与副作用核查

- 日期：2026-10-27
- 状态：**提议**
- 依据：PRD §14、AC-E09；审计 A14–A18、RUN-01/02

## 背景

Graph 节点可以重入，批准消费不等于外部执行成功；外部成功但 receipt 未落库的窗口不能由 checkpoint 自动消除。

## 决策提议

Approval 绑定主体、具体参数 hash、工具版本、scope、有效期；通过原生 interrupt 等待。批准后执行前重验当前权限；消费审批与 ToolCall prepared→executing 同 PG 事务。外部调用用稳定 key，持久化 receipt 后再返回 Graph。恢复读同 tool_call_id 的结果；无法证明结果的写标 unknown_outcome、Run needs_reconciliation，外部查询/去重可用才安全核查或重试，否则人工。不自建 interrupt 或快照引擎。

## 替代方案

模型输出 approved 无可信身份；本地锁不能去重跨系统写；“失败就重试”会重复提交。禁止所有写虽简单但不满足 P1 工具执行需求，可作为未验证连接器的默认关闭策略。

## 后果与验证条件

需要调用账本、人工核查 UI、预算未知消耗冻结；撤权无法追回已发出的写。H1/H9 杀进程覆盖 executing 前后/外部成功后/receipt 后/checkpoint 前各窗口，测试改参、过期、撤权、重复批准/恢复，证明未知写零自动重放。外部幂等 key 的保留期和查询语义需真实连接器证明。

契约：[02](../tdd/02_Agent_Harness.md)、[04](../tdd/04_LangGraph_Workflows.md)。替代关系：无。
