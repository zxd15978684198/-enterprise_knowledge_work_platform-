# ADR-0007：缓存与队列组件许可（Redis 7.4 / Valkey）

- 日期：2026-10-29
- 状态：**提议**（许可结论需法务与产品裁决，工程侧只提供事实与选项）
- 依据：`P0_Core_Gate_Review.md:297`；`Phase0_Full_Gate_Review.md` §5.2；`experiments/phase0/compose.yaml`；`docs/poc/05_Phase0_Acceptance.md:73`（未知许可不得进入发布候选）

## 背景

P0 的队列与缓存选型为 Redis，`experiments/phase0/compose.yaml` 固定镜像 `redis:7.4-alpine@sha256:6ba19c50b56777f5c11c58eff8ddb69d0d5f27ba131cffb63f180ccfd43c66af`，Python 客户端为 `redis` 8.1.0（`experiments/phase0/uv.lock`）。上游自 Redis 7.4 起改为 **RSALv2 与 SSPLv1 双许可**，不再是 BSD 类许可。T31 已把该项登记为「许可未定」（`P0_Core_Gate_Review.md:297`），T32 复核结论为本项在裁决前构成发布阻塞：`05:73` 明确「未知许可不进入发布候选」。

`05:73` 同时要求「实际所用依赖/镜像/模型权重/数据许可与安全维护记录完整」。当前状态是：运行时可用、证据完整、**许可结论缺失**，因此不能靠「内部先用着」绕过，必须落成一条编号决策。

## 决策提议

三选一，由法务/产品裁决；工程侧建议 **B**，若发布形态确定只做内部自建部署则 **A** 可接受：

| 选项 | 内容 | 代价 | 适用条件 |
| --- | --- | --- | --- |
| A | 接受 SSPL/RSALv2 边界，部署形态仅「内部自建、不对第三方提供托管服务」，保留 `redis:7.4-alpine` | 需在部署声明与 NOTICE 中记录许可边界；对外销售/托管形态被永久排除 | 产品确认永不做托管/分发形态 |
| B | 切换到 Valkey（BSD-3-Clause），镜像与客户端同步替换 | 需验证 Kombu/Celery 与 Valkey 兼容、镜像 digest 重采、重跑 T08/T29 关键用例 | 存在任何对外分发/托管可能（推荐） |
| C | 保留 Redis 但剥离出核心依赖（缓存可选、队列换自研或 PostgreSQL 表队列） | 工程成本最高，且会推翻 T08 的 Outbox/重试证据 | 仅当许可裁决完全不可得时 |

无论选哪个，**P0 不得在未裁决状态下标记发布候选**；裁决前该事项在状态文件中保持 `PARTIAL`（G5 的一环）。

## 替代方案

1. 继续使用 Redis 7.4 且不记录许可边界：违反 `05:73`，拒绝。
2. 用 Redis 7.2（BSD-3 时代）降版本规避：会引入 EOL 与安全维护风险，且与 `compose.yaml` 的 digest 证据冲突，拒绝。
3. 仅在本机实验环境使用、不进入发布物：可作为 A 的弱化形态，但必须写进部署声明的「禁分发」条款，不能只在口头约定。

## 后果与验证条件

后果：选 B 会使 P0 证据链出现一次镜像替换（与 ADR-0010 的 pgvector 替换同类，必须先登记再重采）；选 A 会把「不得托管分发」变成产品约束，后续 SaaS 形态需重新立项。验证条件：(1) `compose.yaml` 中的镜像与 digest 与 ADR 结论一致；(2) 发布候选的 NOTICE/许可清单包含本组件及结论；(3) 若选 B，T08/T29 用例在 Valkey 上重跑并留记录；(4) 状态文件中本项在裁决前不得写「已完成」。

实现契约：[01](../tdd/01_System_Architecture.md) 依赖与许可章节、[05_Phase0_Acceptance](../poc/05_Phase0_Acceptance.md) `G5`、`docs/poc/evidence/Phase0_Full_Gate_Review.md` §5.2。与 ADR-0002 的关系：不改变 Outbox 语义，只约束其 broker 实现。
