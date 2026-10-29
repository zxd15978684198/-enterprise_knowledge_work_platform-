# ADR-0010：pgvector 进入 core profile 与 T02 证据重采

- 日期：2026-10-29
- 状态：**已接受**（2026-10-29，W13 执行确认；三条验证条件已逐项落实，见文末「执行结果」；阈值校准仍归 ADR-0014，不在本 ADR 内）
- 依据：`experiments/phase0/compose.yaml`（`pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`）；`docs/poc/evidence/environment.md` §14；`docs/poc/evidence/fix-w1-t02-pgvector-record.md`；`poc09-report.md`（ANN `NOT_RUN`）；`Phase0_Full_Gate_Review.md` §6.1 ADR-NEW-4、§6.3 DG-4

## 背景

初版环境使用 `postgres:17-alpine`，镜像内不含 `vector` 扩展，PoC-09 的 ANN 检索因此只能标 `NOT_RUN`，G5 与 PoC-09 的判定被卡住。随后在环境排障中发现 `pgvector/pgvector:pg17` 可用（容器内实测 `vector_ext=0.8.7`、`PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2)`），`fix/w1-t02-pgvector` 分支已把 `compose.yaml` 切到该镜像并**以追加方式**在 `environment.md` §14 记录变更（不改写历史输出），全量回归 1006 通过。

副作用必须显式承认：镜像从 musl 系 alpine 变为 glibc 系 Debian，**原 T02 环境的 digest 证据已作废**，需要用新 digest 重采；`05:73` 要求镜像许可与维护记录完整，本镜像的汇总许可亦需登记。此外，「静默换镜像」本身就是一次范围变更，必须由 ADR 授权，否则后续任何波次都可以用同样理由绕过证据链。

## 决策提议

1. **pgvector 成为 core profile 的硬前提**：`experiments/phase0/compose.yaml` 的 `postgres` 服务固定使用 pin 的 digest（`pg17@sha256:ac08538c…`），不允许浮动 tag；`--profile core` 起不来 vector 扩展即视为环境故障，而不是跳过 ANN。
2. **重采 T02 证据的首个动作是备份 + 记录新 digest**：新 digest、PG 版本串、`vector` 扩张版本、数据卷原地复用状态必须写进 `environment.md` 的新增小节（追加，不改历史），并在波次记录中标注「本波次因 ADR-0010 重采」。
3. **补 G5 的固定顺序**（与 `Phase0_Full_Gate_Review.md` §9.2 一致）：备份演练 → 重采 T02 环境证据 → 补跑 T25 ANN → 阈值校准（ADR-0014）。顺序不得调整，因为 ANN 结论必须建立在「环境证据已重采」之上。
4. **禁止静默换镜像**：之后任何镜像/发行版替换，必须先有 ADR（或本 ADR 的显式修订），再在同一波次内完成「证据重采 + 全量回归」，不得跨波次拖延。

## 替代方案

1. 保持 alpine 镜像，ANN 走外部向量库（Qdrant/Milvus 等新服务）：多一个部署面与许可面，且 P0 已有 PostgreSQL/BM25 双路对照，收益不足，留待 P1 检索选型时重新评估。
2. 在 alpine 上自行编译 `vector` 扩展：需要构建链与镜像自维护，违反「优先复用成熟镜像」的项目取向，拒绝。
3. 继续维持 ANN `NOT_RUN`、把 PoC-09 永久标 PARTIAL：等于放弃 G5 的一个强制项，只在产品明确接受「无 ANN」并走范围变更时才成立（即 DG-4 长期生效），不作为默认路径。

## 后果与验证条件

后果：core profile 的镜像体积与基础发行版变化（musl→glibc），T02 全部环境输出需要一次重采；`environment.md` 出现「历史输出 + §14 变更记录」的双层结构，读者必须按小节顺序理解。验证条件：(1) `compose.yaml` 中 `postgres` 镜像带 digest；(2) 重采后的 T02 证据包含新 digest、PG 版本串与 `vector` 版本；(3) 补跑的 ANN 结果带 recall/latency 指标且方法可复现；(4) 波次记录写明「因 ADR-0010 重采」，不得让读者误以为是原证据。

实现契约：[01](../tdd/01_System_Architecture.md) 存储选型、[05_Phase0_Acceptance](../poc/05_Phase0_Acceptance.md) G5、`docs/poc/evidence/environment.md` §14、`docs/poc/evidence/fix-w1-t02-pgvector-record.md`。与 ADR-0004/0007 的关系：同属「第三方组件进入核心依赖」的许可与范围治理。

## 执行结果（W13，2026-10-29）

决策 3 的三步顺序已按序执行完毕，波次记录见 `docs/poc/evidence/wave-w13-record.md`。

| 步骤 | 落地 | 证据 |
| --- | --- | --- |
| ① 备份演练前置 | 在动环境之前先做全量备份/恢复演练：`pg_dump -Fc` → 全新库恢复 → 63 张表逐表行数零差异；另用 T25 真向量列（`vector(4096)` + HNSW）验证含向量表可往返 | `docs/poc/evidence/restore-runbook.md` §11；`restore-drill.json` |
| ② 重采 T02 环境证据 | 新 digest `sha256:ac08538c…`、`PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2)`、`vector 0.8.7`、数据卷原地复用（创建时间未变）；追加进 `environment.md` §15，不改写历史 | `docs/poc/evidence/environment.md` §15 |
| ③ 补跑 T25 ANN | pgvector HNSW 与 IVFFlat 在同一份合成聚簇语料（2 万 × 128 维）上实测 recall/延迟，索引名与 `EXPLAIN` 一并落盘 | `docs/poc/evidence/ann-bench.json` |

验证条件逐条核对：(1) `compose.yaml` 的 `postgres` 镜像带 digest ✅；(2) 重采证据含新 digest、PG 版本串、`vector` 版本 ✅；(3) ANN 结果带 recall/latency 且方法可复现（确定性 seed，跑一次 30 秒，`--rows/--dim` 可调）✅；(4) 波次记录写明「因 ADR-0010 重采」✅。

ANN 实测（2 万行 × 128 维，k=10，100 条查询，recall 对精确全扫描求均值）：HNSW `ef_search=40/64/128` → **0.998 / 0.999 / 1.000**，`p50 4.8–13.2ms`；IVFFlat `probes=1/8/32` → **0.971 / 1.000 / 1.000**。**新增限制（本 ADR 未覆盖，须写入实现）**：`WHERE workspace_id = ...` 的 post-filter 变体在 50% 过滤率下只返回 5/10 行（pgvector 0.8 未开 `hnsw.iterative_scan`），recall 恒为 0.5 且调大 `ef_search` 无效——检索实现必须走**指定分区/子表前置过滤**或开启迭代扫描，不能依赖 post-filter。

**第四条后果（补充）**：ANN 基准只证明「索引近似误差与延迟」，**不证明语义召回质量**——向量是合成聚簇数据、不是真实 embedding，DG-4 因此不解除，`poc09-report.md` 的语义质量仍为 `NOT_RUN`。
