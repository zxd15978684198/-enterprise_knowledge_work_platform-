# ADR-0005：空间隔离、引用再授权与持久化事件

- 日期：2026-10-27
- 状态：**提议**，身份全局目录例外尚待确认
- 依据：AGENTS.md；PRD F-01/06/15、AC-02/03/06；审计 R05/R10–R15、SEC-01

## 背景

检索、引用/对象、浏览器 caller 与 Graph 恢复都可能成为越权入口。SSE token 草稿、最终消息和 checkpoint 不属于同一恢复机制。

## 决策提议

全部业务资源显式 workspace_id、复合 FK、repository 必填可信 Scope；同空间 grant+应用授权，RLS 纵深防御。P0 principal 在空间内，身份系统 subject 仅 opaque 映射，身份域全局目录若实际需要，须先评审此例外；P0 不支持跨空间共享。引用每次访问重新授权，敏感资产走代理，短期签名不能承诺即时撤权。

业务事件先 PG 持久化后 SSE 发送，run 内 event_seq/幂等 fact 唯一；最终 Message+Run+completed 同事务。P0 在线流/结果查询，P1 才开放公共 cursor 重放，过期明确 410。不把 SSE 续传当重启 Graph。日志/trace/state/模型不含凭据或个人敏感信息，默认关闭 SDK 原文采集。

## 替代方案

仅前端过滤/仅 UUID/RLS 默认配置均不足；Redis-only 事件更便宜但不满足同等最终提交语义。永久签名或缓存 URL 易绕过撤销；跨空间共享需独立资源所有者/授权协议，不提前加入。

## 后果与验证条件

增加 PG 事件写入与逐次授权成本；需游标清理、私有内容代理和数据保留策略。H6/H9 覆盖两空间全资源/游标/恢复/对象访问、RLS 连接污染、撤权、最终消息保存失败、秘密/PII canary；事件 TTL/签名 TTL/身份体系和保留期评审后锁定。

契约：[07](../tdd/07_Data_API_Security.md)、[08](../tdd/08_Test_Strategy.md)。替代关系：无。
