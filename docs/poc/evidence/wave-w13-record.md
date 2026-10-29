# W13 波次记录：执行 ADR-0010（pgvector 进 core + 补 G5 的前置三步）

- 波次编号：W13
- 分支：`feature/w13-adr0010`（base `dev@d134319`）
- 并行度 N：**1**
- 日期：2026-10-29
- 授权：用户明确选择「开 W13 把 ADR-0010 做完」（`m05283`）

## 1. 声明块

| 项 | 内容 |
| --- | --- |
| 任务 | 按 ADR-0010 第 3 条固定顺序执行三步：①备份恢复演练（pg17 + pgvector 下复跑）②重采 T02 环境证据 ③补跑 T25 ANN；阈值校准（ADR-0014）**不在本波次范围** |
| 为什么 N=1 | ADR-0010 明写「顺序不得调整，因为 ANN 结论必须建立在环境证据已重采之上」；三步共享同一 PG 实例与同一份证据链，并行会重演「结论早于证据」的老问题 |
| 执行者 | Lead 自执行（子 agent 在 T31 两次静默失败，NN 编码+证据口径不宜再拆） |
| 写权（白名单） | `docs/poc/evidence/wave-w13-record.md`（新）、`docs/poc/evidence/restore-runbook.md`（追加 §11）、`docs/poc/evidence/environment.md`（追加 §15）、`experiments/phase0/ann_bench.py`（新）、`tests/phase0/test_ann_bench.py`（新）、`docs/poc/evidence/ann-bench.json`（新）、`docs/poc/evidence/poc09-report.md`（ANN 小节与 NOT_RUN 汇总）、`docs/adr/0010_Pgvector_Core_Profile.md`（状态：提议 → 已接受，附用户裁决）、`docs/IMPLEMENTATION_STATUS.md` |
| 只读依赖 | `docs/adr/0010_Pgvector_Core_Profile.md`、`docs/adr/0014_Quality_Threshold_Approval.md`、`docs/poc/03_Phase0_Task_Backlog.md`（P0-T25/T30 卡）、`docs/poc/05_Phase0_Acceptance.md`（G5）、`experiments/phase0/retrieval_probe.py`、`experiments/phase0/restore_probe/**`、`docs/poc/evidence/fix-w1-t02-pgvector-record.md` |
| 资源槽 | PG `127.0.0.1:5433` 库 `pyknora`，新 schema **`p0_t25_ann`**（ANN 基准，跑完即删）；T30 演练库 `p0_t30_*`（演练自带创建/删除）；Redis 未使用；不新增宿主端口 |
| 验收标准 | ADR-0010「后果与验证条件」四条全满足：(1) `compose.yaml` 中 `postgres` 镜像带 digest ✅（既有）；(2) 重采后的 T02 证据含新 digest、PG 版本串与 `vector` 版本；(3) ANN 结果带 **recall/latency** 指标且方法可复现；(4) 波次记录写明「因 ADR-0010 重采」。另：不得把 ANN 结果冒充「语义质量达标」（DG-4 仍生效） |

## 2. 派发记录

| 顺序 | 动作 | 结果 |
| --- | --- | --- |
| 0 | 建分支、读 ADR-0010/0014、确认 compose pin | `feature/w13-adr0010` @ `d134319`；compose `postgres` 已 pin `pgvector/pgvector:pg17@sha256:ac08538c…` |
| 1 | 备份恢复演练（ADR 步骤 1） | 见 §3.1；`restore-runbook.md` §11 |
| 2 | 重采 T02 证据（ADR 步骤 2） | 见 §3.2；`environment.md` §15 |
| 3 | 补跑 T25 ANN（ADR 步骤 3） | 见 §3.3；`ann_bench.py` + `ann-bench.json` + `poc09-report.md` 新小节 |
| 4 | 门禁 + 文档回填 | 见 §3.4 / §4 |

## 3. 执行结果

三步严格按 ADR-0010 第 3 条顺序执行，**每步都先把结果落盘再做下一步**，避免「结论早于证据」。

### 3.1 步骤 1：备份恢复演练（pg17 + pgvector 下复跑）

命令与 W7 时点**一字未改**（`experiments/phase0/restore_probe`），只换镜像后复跑；输出追加在 `restore-runbook.md` §11，§1–§10 的历史 alpine 输出不改写。

| 项 | 结果 |
| --- | --- |
| 退出码 / 耗时 | **0** / ≈90s（`16:37:55 → 16:39:22`） |
| `consistency` / `rollback` / `faults` | **PASS / PASS / PASS** |
| 逐表摘要 | 63 张表 `to_jsonb` 全量摘要，`missing/extra/row_mismatches/digest_mismatches` **全为空** |
| 故障注入 | 3/3 按预期码显式失败（`BACKUP_INTERRUPTED`、`RESTORE_INTERRUPTED`、`RESTORE_TARGET_OCCUPIED`） |
| 清理 | 4 个演练库已 DROP，`remaining_databases=[postgres,pyknora]`，`workdir_state=removed` |
| `multi_instance` / `rpo_rto` / `object_storage` | `NOT_RUN`（**不变**，换镜像不解锁这三项） |

换镜引入的新风险面（原镜像没有 `vector` 类型，T30 演练当时不可能覆盖）单列专项：

| 步骤 | 实测 |
| --- | --- |
| 业务库 `pg_dump -Fc` | `108316` 字节，exit 0 |
| 恢复到新库 | exit 0；3 份 schema 一致、表数 `21 == 21`、扩展 `plpgsql` + `vector` |
| 建 `vector(3)` 表 + HNSW 索引后二次 dump/restore | exit 0；`vector 0.8.7` 存在，行数 `5`，`pg_indexes` 含 `w13_vec_hnsw`（**索引定义随备份恢复**） |
| 恢复后近邻查询 | `1|0.0000`、`4|0.0061`、`5|0.8896`，与源库逐位一致 |

**结论**：`vector` 类型与 HNSW 索引在 `pg_dump`/`pg_restore` 下完整存活；但 `vector` 是**按库安装**的扩展，目标库若无 `CREATE EXTENSION` 且 dump 未带该语句，`pg_restore` 会因 `type "vector" does not exist` 失败——已写进 `environment.md` §14 的接口约定。

### 3.2 步骤 2：重采 T02 环境证据

追加 `environment.md` §15，替代 W1 时点的 digest 与版本串；历史小节（§1/§5/§13/§14）不改写。

| 项 | W1 时点（历史，勿引用） | 本次重采（2026-10-29） |
| --- | --- | --- |
| postgres 镜像 | `postgres:17-alpine@sha256:266d3c25…` | `pgvector/pgvector:pg17@sha256:ac08538c6f8b…` |
| 服务端版本串 | `PostgreSQL 17.11 on x86_64-pc-linux-musl` | `PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) on x86_64-pc-linux-gnu` |
| 扩展清单 | `plpgsql` 1.0 | `plpgsql` 1.0、**`vector` 0.8.7** |
| 数据卷 | `phase0-pgdata`（W1 创建） | 同卷，`created=2026-10-27T08:57:53Z`（早于换镜 ⇒ **原地复用**，未丢数据：3 份 schema/21 张表仍在） |
| redis 镜像 | `redis:7.4-alpine@sha256:6ba19c50…` | 同 digest（**未变**） |

`docker image inspect` 与 `compose.yaml` 的 pin 逐位一致，`compose ps` 显示的 IMAGE 为 digest 形态而非浮动 tag。

### 3.3 步骤 3：补跑 T25 ANN

新代码 `experiments/phase0/ann_bench.py`（+ 回归测试 `tests/phase0/test_ann_bench.py`），证据 `ann-bench.json`。

| 配置 | recall_mean | recall_min | p50 | server 执行 | 索引自证 |
| --- | --- | --- | --- | --- | --- |
| HNSW `ef_search=40` | 0.9980 | 0.9000 | 13.2ms | 0.61ms | `phase0_ann_chunks_hnsw` |
| HNSW `ef_search=64` | 0.9990 | 0.9000 | 4.8ms | 0.42ms | 同上 |
| HNSW `ef_search=128` | **1.0000** | 1.0000 | 7.7ms | 0.46ms | 同上 |
| IVFFlat `probes=1` | 0.9710 | 0.2000 | 7.1ms | 0.61ms | `phase0_ann_chunks_ivfflat` |
| IVFFlat `probes=8` | 1.0000 | 1.0000 | 9.0ms | 1.33ms | 同上 |
| IVFFlat `probes=32` | 1.0000 | 1.0000 | 11.8ms | 2.81ms | 同上 |
| 精确全扫描（ground truth） | 1.0（定义） | — | 15.5ms | — | 无索引 |

数据/参数：2 万行 × 128 维（100 个随机单位中心 + 按维度归一的高斯抖动，确定性 seed），100 条查询与数据同簇，k=10，距离 `<=>`，recall = `|ANN@k ∩ exact@k| / k` 逐查询均值。构建：load 2.5s、HNSW 2.6s、IVFFlat 0.8s，全程 33s。

**两个必须写进实现的限制**：

1. **post-filter 会欠召回**：`WHERE workspace_id = ...` 变体在 50% 过滤率下**只返回 5/10 行**（pgvector 0.8 未开 `hnsw.iterative_scan`），recall 恒 0.5 且把 `ef_search` 从 64 调到 128 毫无改善。检索实现必须用**按工作区分区/子表的前置过滤**或显式开迭代扫描。
2. **本基准不证明语义质量**：向量是合成聚簇数据而非真实 embedding，故 PoC-09 的语义召回仍 `NOT_RUN`、DG-4 不解除。

**过程中被数据推翻的三次假设**（保留为方法教训，避免后人重踩）：

| 假设 | 实测 | 修正 |
| --- | --- | --- |
| 均匀随机向量能测 recall | 128 维下近乎两两正交，ANN 与精确解几乎无差 → recall≈1.0 是**假通过** | 改合成聚簇数据（中心 + 抖动） |
| 抖动幅度直接取 `jitter=0.35` | 128 维 `gauss(0,1)` 模长约 11.3，噪声盖过单位中心，簇结构被抹平，recall 退化成 0.5 量级 | 抖动除以 `sqrt(dim)`，使噪声模长≈0.35 |
| 索引会被自动选上 | 2 千行规模规划器选「顺序扫描 + 排序」，`index_used` 为空、recall 恒 1.0 | 测量期 `SET LOCAL enable_seqscan=off`，并把 `index_used` 写进证据（**指标必须自证**） |

### 3.4 门禁

| 项 | 命令 / 方法 | 结果 |
| --- | --- | --- |
| ruff | `ruff check experiments/phase0/ann_bench.py tests/phase0/test_ann_bench.py` | **All checks passed**（含 `ruff format` 归零） |
| 新增测试 | `pytest tests/phase0/test_ann_bench.py -q -o addopts=` | **5 passed in 1.77s** |
| 全量回归 | `pytest tests/phase0 -q -o addopts=` | **1011 passed / 0 failed / 0 errors / 8 skipped in 609.97s (0:10:09)**（W12 基线 1006 + 本次新增 5 条，skip 数不变） |
| 秘密扫描 | 正则（password/postgres://[^:]+:[^@]+@/sk-/ghp_/AKIA/BEGIN PRIVATE KEY）扫本波次新增与修改文件 | **0 命中** |
| 残留检查 | `git status --porcelain` + `p0_t25_ann` 是否已删 | 工作树仅剩本波次 6 个文件（5 改 1 新×2）+ 既有未跟踪项；`p0_t25_ann` 由 `run()` 结束即 DROP、测试亦有断言 |

## 4. 提交决策

| 项 | 内容 |
| --- | --- |
| 待提交 | `ann_bench.py`（新）、`test_ann_bench.py`（新）、`ann-bench.json`（新）、`wave-w13-record.md`（新）、`environment.md` §15（追加）、`restore-runbook.md` §11（追加）、`poc09-report.md`（§3/§8/§9/§10 + §12 追加）、`docs/adr/0010_Pgvector_Core_Profile.md`（状态 + 执行结果）、`docs/IMPLEMENTATION_STATUS.md` |
| 不提交 | 全量回归重生成的 6 份 tracked 证据（时间戳/PID 抖动，按既有做法 `git stash` 隔离） |
| 判定 | ADR-0010 四条验证条件全满足 → **ADR-0010 转「已接受」**；G5 的 ANN 阻塞项**解除**，但 G5 整体仍不成立（阈值校准属 ADR-0014，未批） |
| 用户裁决 | 选 **1 = 提交 + 推送 + `--no-ff` 合入 dev** |

### 4.1 执行结果（真实 SHA）

| 动作 | 结果 |
| --- | --- |
| 代码提交 | `7c4402e` feat(poc)：`ann_bench.py` 825 行 + `test_ann_bench.py` 82 行 + `ann-bench.json` 292 行（3 文件 +1199） |
| 文档提交 | `e1bde5e` docs(poc)：6 文件 +298/−7（含 ADR-0010 状态与执行结果、`environment.md` §15、`restore-runbook.md` §11、`poc09-report.md` §12、状态文件） |
| 分支推送 | `feature/w13-adr0010` → `e1bde5ecb0763e055d045b5ba4d3f715c63bc399`，`git push -u` exit 0 |
| 合入 dev | `--no-ff` 合并生成 `6e00b9b`（`d134319..6e00b9b`，9 文件 +1497/−7） |
| dev 推送 | `git push origin dev` → `6e00b9b27680e94799d3162b679fe7ded2938d69`，本地/远端逐位一致 |
| 提交前检查 | 两笔提交前均跑 `git diff --cached --check`，均 exit 0（无 EOF 空行/空白问题） |
| 未提交 | 全量跑重写的 6 份证据已 `git stash` 为 `stash@{0}: w13-fullsuite-regen-6-evidence` |
