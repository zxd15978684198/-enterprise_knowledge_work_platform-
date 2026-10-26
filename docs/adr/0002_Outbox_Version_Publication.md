# ADR-0002：可靠派发与不可变索引版本发布

- 日期：2026-10-27
- 状态：**提议**
- 依据：PRD AC-04/05、§13；审计 I02/I04–I11、ING-01–03

## 背景

普通上传源码直接 Enqueue，不能继承为统一 Outbox 保证；原 attempt guard 不等于不可变版本和原子激活。DB/Redis/对象存储间存在提交裂缝。

## 决策提议

业务版本/Job/Outbox 同 PG 事务登记，dispatcher 至少一次派发，DB Reconciler 补漏；Job CAS+租约 fencing 管重复执行。SourceVersion/DocumentVersion 内容不可变，index_version 构建隔离，发布事务比较 desired_version/generation/current_attempt 并切活动指针/winning attempt。pgvector/FTS 和业务指针同 PG 事务域；核心可检索与富化分离。对象先写不可变 key，DB 失败留孤儿经引用感知 GC 清理。

## 替代方案

直接写 DB 后发 Redis 易丢唤醒；broker-only 恢复不能证明发布一致；原地覆盖 chunk 易混版本；外部搜索引擎须另设双写/别名发布协议，不适用本原子承诺。

## 后果与验证条件

多版本存储、GC、对账器增加成本；Exactly-once 不作为承诺。H5 必须注入 DB 后断 Redis、重复投递、lease 过期、v1/v2 反序、删/取消后返回、对象缺失、发布后 ack 丢失、富化失败，证明无重复可见发布且旧活动版本保留策略正确。依赖 PG 索引同事务域和对象不可变，不满足需重审。

契约：[06](../tdd/06_Knowledge_Ingestion.md)、[07](../tdd/07_Data_API_Security.md)。替代关系：无。
