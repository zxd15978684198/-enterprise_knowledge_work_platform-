# P0 核心出口证据评审（P0-T31）

状态：**已完成**（2026-10-29，静态复核 + 注入测试；结论见 §9）

## 1. 评审范围与输入清单
### 1.1 评审对象与判定口径

- **评审目标**：判断 P0 **核心出口**是否就绪——PoC-01/02/03/04/06/09 强制项 + 授权/网络/内容安全门 + 最小恢复演练 + C-01–C-10 实验契约 + 依赖/镜像/数据许可 + 质量阈值与 PRD 性能差距；**不**把六项 PoC 的 partial 折算成 Phase 0 完成（`docs/poc/03_Phase0_Task_Backlog.md:524-539`）。
- **G-P0 判据原文出处**：`docs/poc/05_Phase0_Acceptance.md:68-76`（5 门 + 「缺一项即记 PARTIAL/FAIL/BLOCKED」规则），`:78-82`（G-FULL 与「受限范围准入」）。
- **判定词**：按 `docs/poc/05_Phase0_Acceptance.md` §2 —— PASS / PASS（限域）/ PARTIAL / FAIL / BLOCKED / NOT_RUN；限域必须写边界。
- **本次硬规则**：① NOT_RUN/PARTIAL 不得写成 PASS；② 质量阈值未校准不得判质量 PASS（05:74）；③ 安全失败零豁免（05:75-76）；④ mock/替身成功不得当真实网络隔离证据（`docs/poc/06_Phase0_Execution_Guide.md` §5）；⑤ 无实跑即写 NOT_RUN 并说明理由。
- **方法**：静态核对（不跑全量 pytest，Lead 负责）+ 证据哈希重算 + 波次门禁表交叉核对 + `%TEMP%` 临时目录注入测试（§8）。

### 1.2 评审基线

| 项 | 值 |
| --- | --- |
| 分支 | `feature/w10-t31` |
| HEAD | `ae0a5ae` |
| 工作树改动 | 无未提交的**已跟踪**改动；仅 7 项未跟踪（下方原文） |

```
$ git status --porcelain
?? .idea/
?? .t31_tmp/
?? CLAUDE.md
?? docs/plans/README.md
?? docs/poc/evidence/P0_Core_Gate_Review.md
?? docs/poc/evidence/wave-w10-record.md
?? docs/teacher/
```

`.t31_tmp/` 为本次评审临时目录（仓库内，收尾删除）；`P0_Core_Gate_Review.md`、`wave-w10-record.md` 为本波次产物；其余为本地/文档噪音。**已跟踪文件零 dirty**，故 HEAD 即评审基准。

### 1.3 输入清单

**A. 判据与计划（4）**

| 文件 | 行数 | sha256 前 16 | 用途 |
| --- | --- | --- | --- |
| `docs/poc/05_Phase0_Acceptance.md` | 88 | `9f80c33cae403930` | G-P0/G-FULL 判据、五态与证据字段 |
| `docs/poc/03_Phase0_Task_Backlog.md` | 560 | `fce491e901d22675` | P0-T31 任务定义、32 任务范围 |
| `docs/poc/02_Phase0_PoC_Plan.md` | 123 | `a95f6d52922e474b` | **权威 PoC 编号**（§3 总览表）与六项 P0 PoC 退出条件 |
| `docs/tdd/08_Test_Strategy.md` | 147 | `036de63d787037d2` | §8 (H1/H2 阻断项)、§2 证据结构 |
| `docs/poc/07_Contract_Closure.md` | 231 | `905cdbdd846870b1` | C-01–C-10 状态与最小 TDD 修订建议包 |

**B. PoC ↔ 证据文件映射（按 `02_Phase0_PoC_Plan.md:28-38` §3 权威编号重建；文件名不可直接对号）**

> 目录实况：`docs/poc/evidence/` 下**只有 2 份** `poc*-report.md`（`poc08-report.md`、`poc09-report.md`）。PoC-01/02/03/04/06 **无独立报告文件**，其证据分散在任务级证据 md、测试文件与 `wave-w1..w9-record.md` §3 中；下表逐项列明替代证据位置，凡「无独立报告文件」者，**scenario 结论只能由任务级证据 + 门禁表复核得出，不能按文件名对号判 PASS**。

| 权威 PoC | 权威 Task | 独立报告文件？ | 实际证据位置（替代证据） | 自述结论 | 复核结论（本评审，见 §2） |
| --- | --- | --- | --- | --- | --- |
| PoC-01 | T05、T06 | **无独立报告文件** | `poc01-owner.md`(253 行)、`poc01-owner-amendment-01.md`(78)、`poc01_owner_process_level.json`(64)、`wave-w3-record.md:141-177`、`wave-w4-record.md:154`、`wave-w7r-record.md:61`、`tests/phase0/test_checkpoint_owner.py`(697) | T05 `PARTIAL（单活）`；T06 `PASS（限域）` | **PARTIAL**（单活而非 PASS，见 §2.2-P01） |
| PoC-02 | T07、T08 | **无独立报告文件** | `wave-w3-record.md:174`、`wave-w4-record.md:155`、`tests/phase0/test_outbox_transaction.py`(1122)、`job_probe.py`(1495)+`job_reconciler.py`(382)、`experiments/phase0/db/migrations/003_jobs_outbox.sql`(333) | T07/T08 均 `PASS（限域）` | **PASS（限域）** |
| PoC-03 | T12、T13 | **无独立报告文件**（`poc03-publication.md` 是 T13 单任务证据） | `poc03-publication.md`(208)、`wave-w5-record.md:140`、`wave-w6-record.md:111`、`test_index_staging.py`(741)、`test_publication_faults.py`(1062)、`004_version_index.sql`(452)、`008_publication.sql` | T12/T13 均 `PASS（限域）` | **PASS（限域）** |
| PoC-04 | T09、T10、T11 | **无独立报告文件** | `docreader_manifest.md`(315)、`docreader_manifest_amendment-01.md`(230)、`docreader_sbom.md`(291)、`docreader_fixture_plan.md`(190)、`docreader_image_inventory.json`(39424)、`wave-w3-record.md:175`、`wave-w4-record.md:156`、`wave-w6-record.md:86`、`wave-w7r-record.md:62`、`test_docreader_wire.py`、`test_parser_golden.py`(570) | T09R/T10/T11 均 `PASS（限域）` | **PASS（限域）** |
| PoC-06 | T19、T20、T21 | **无独立报告文件** | `harness-integration.md`(208)、`wave-w3-record.md:177`、`wave-w4-record.md:158`、`wave-w5-record.md:141`、`test_gateway_registry.py`、`test_budget_context.py`、`test_harness_integration.py`、`harness_graph.py`(1425)、`model_gateway.py`(1090)、`budget_ledger.py`(1288) | T19/T20/T21 均 `PASS（限域）` | **PASS（限域）** |
| PoC-09 | T25、T26 | `poc09-report.md` 仅为 **T26** 报告；T25 无独立报告文件 | `poc09-report.md`(204)、`retrieval-baseline.json`(399)、`rag-citations.json`(353)、`wave-w7-record.md:67`、`wave-w8-record.md:80`、`test_retrieval_permissions.py`(641)、`test_rag_events.py`(910) | T25/T26 均 `PASS（限域）` | **PARTIAL**（试验可复现，但质量阈值未校准→不得判质量 PASS，见 §3-G5） |

已核验的 PoC-01/02/03/04/06/09 六项中：**4 项 PASS（限域）+ 1 项 PARTIAL（PoC-01 单活）+ 1 项 PARTIAL（PoC-09 质量未校准）**；「PASS（限域）」不等于 G-P0 门通过（见 §3）。

**C. T29/T30 与安全/恢复专项**

| 文件 | 行数 | sha256 前 16 | 结论口径 |
| --- | --- | --- | --- |
| `security-matrix.md` | 609 | `cd3738760e23b095` | `PASS（限域）`，含 6 项 NOT_RUN |
| `security-probes.json` | 5264 | `84c0991644ef720b` | 549 probes / 536 refused / 0 breaches |
| `restore-runbook.md` | 317 | `1a6110a5c5d49161` | 单机恢复 PASS；多实例/RPO/RTO NOT_RUN |
| `poc05-recovery.md` | 266 | `0bc0a700a28429c3` | owner crash/revoke/unknown_outcome 限域 |
| `poc05-browserskill-live.md` | 151 | `dbdc12fd78bd8c26` | `PASS（限域，单活）` 13/31 映射 |
| `browserskill_protocol_manifest.md` | 535 | `d44b41c2ad5ada82` | BSK 0.3.1 版本/配对/许可 |
| `pgvector-probe.md` | 426 | `42b3baa89e07b0d4` | 可用性 PASS（未进 core compose） |
| `poc08-report.md` | 131 | `25b658150a67ca16` | Research 限域（非生产） |

**D. 波次门禁记录（第一手「谁真跑过什么」，10 份）**

| 文件 | 行数 | sha256 前 16 |
| --- | --- | --- |
| `wave-w1-record.md` | 103 | `1c2d59f4b1ee31ce` |
| `wave-w2-record.md` | 118 | `3acc86fc7e4ad32d` |
| `wave-w3-record.md` | 211 | `3125980b4f382006` |
| `wave-w4-record.md` | 169 | `9c736bf2f086d8d4` |
| `wave-w5-record.md` | 189 | `4b3c01ffd3faf0b2` |
| `wave-w6-record.md` | 124 | `b67010abbf2b96ed` |
| `wave-w7-record.md` | 119 | `048b039e4cc2c5db` |
| `wave-w7r-record.md` | 95 | `534858649d5b5e15` |
| `wave-w8-record.md` | 101 | `0e1490bd94e36ea9` |
| `wave-w9-record.md` | 87 | `7a7e84da49d38856` |

**E. 依赖/环境/清单（生产前待办的依据）**

| 文件 | 行数 | sha256 前 16 | 用途 |
| --- | --- | --- | --- |
| `experiments/phase0/pyproject.toml` | 38 | `4186b228f292a9f1` | 依赖声明（含 dev 组） |
| `experiments/phase0/uv.lock` | 960 | `02ad6b8396299393` | 实际锁定版本；锁 hash 基线 |
| `experiments/phase0/compose.yaml` | 98 | `8ffd1e8610200d61` | PG 17-alpine@266d3c25 / Redis 7.4-alpine@6ba19c50 |
| `environment.md` | 430 | `5a86c9be82e89e00` | 环境/锁/镜像/门禁（T02） |
| `docreader_sbom.md` | 291 | `d9d9b85dd958635f` | **仅 DocReader 镜像** SBOM |
| `fix-w1-t02-pgvector-record.md` / `fix-w5-t18-record.md` | 146/117 | `a390ca0c57af6cf0`/`bf202f6c64f22a1e` | 两处修正记录 |
| `retrieval-baseline.json` 等 7 个 JSON | 合计 7257 | 见 B/C 行 | 机器可读原始结果 |

盘点：`docs/poc/evidence/` 既有 **40 份**（34 md + 6 个结构化 JSON 中 7 个 json 文件，另加本波次 1 份 = 41），合计约 **5.4 万行**；测试 30 文件 / 21,980 行；`experiments/phase0/*.py` 34 模块 / 41,251 行。

### 1.4 输入侧的已知缺口（影响见 §4）

| 缺口 | 事实 | 被谁引用 |
| --- | --- | --- |
| `docs/poc/evidence/sbom.md` **不存在** | 证据目录仅 `docreader_sbom.md`；`Get-ChildItem` 与 `git ls-files` 双查均无 | `wave-w10-record.md:26-27` 声称 T30 交付「`sbom.md`/依赖锁与许可证据」 |
| `tests/phase0/pyproject.toml` **不存在** | 实际配置在 `experiments/phase0/pyproject.toml` | `control-site.md`（T04）复现命令段笔误 |
| `docs/remote-extension-connection.md` **不存在** | 仓库无此文件 | `browserskill_protocol_manifest.md:503` |
| `experiments/phase0/.py` | 非真实路径（正则统计产生的伪命中） | 本评审数据提取脚本 |
| 证据无「hash 重算/漂移说明」机制 | 证据文件引用旧文件 hash，门禁后合法修改会造成大面积不一致（量化见 §2.3） | 全部证据文件 |

## 2. 逐 scenario 核对表

### 2.1 方法与口径

本次**只做静态复核**，不重跑全量 pytest（Lead 职责分工，实跑证据以波次门禁记录为准）。四项动作：

1. **证据存在性 / 行数 / hash 重算**：对 §1.3 全部输入逐项核对，量化漂移见 §2.3。
2. **波次门禁表交叉**：把 `wave-w1..w9-record.md` §3 的判定行与任务级证据、测试命令退出码对齐（提取脚本见 §8.4）。
3. **自述结论 vs 未覆盖项约束**：凡行内含 `NOT_RUN`/`PARTIAL`，判定不得写成无保留 `PASS`（`05_Phase0_Acceptance.md:73-76`）。
4. **注入测试**：故意缺一条证据、篡改锁文件 hash、伪造一条越级判定，验证"证据缺失/被改/口径越级"是否会被拒绝（§8）。

凡本次未实跑者，结论只写"记录一致 / 不一致"，不得升格为实跑证据。

### 2.2 六项 PoC（scenario 级）

| 权威 PoC | 关键 scenario（记录原文摘要） | 记录自述 | 交叉核对锚点 | 一致性 | 复核结论 |
| --- | --- | --- | --- | --- | --- |
| PoC-01 | 单写者/fencing/50 轮失锁/崩溃接管（T05R）；审批绑定+调用账本+未知写核查（T06） | T05R `PASS（限域：单活）`；T06 `PASS（限域）` | `wave-w7r-record.md:61`（`12 passed / exit 0`）、`wave-w3-record.md:100`（原生 saver `put` 在锁被杀后仍成功）、`wave-w4-record.md:154` | 一致 | **PASS（限域：单活）**；多实例仍 PARTIAL。**注**：§1.3.B 该行沿用了 T05 修复前的 `PARTIAL（单活）`，按 T05R 应更正（D7） |
| PoC-02 | Outbox 事务原子性 / SKIP LOCKED / 五态死信（T07）；消费幂等 + Job 对账（T08） | T07、T08 均 `PASS（限域）` | `wave-w3-record.md:174`、`wave-w4-record.md:155`（`applied=100 / duplicate=900`） | 一致 | **PASS（限域）** |
| PoC-03 | 版本 staging 不可见 / 单事务原子切换 / 竞态故障注入（T12、T13） | T12、T13 均 `PASS（限域）` | `wave-w5-record.md:140`、`wave-w6-record.md:111`（10 000 次并发读零混版本） | 一致 | **PASS（限域）** |
| PoC-04 | DocReader proto/镜像/许可预检（T09R）；Read/ReadStream 真 wire（T10）；五格式 golden（T11） | T09R/T10/T11 均 `PASS（限域）` | `wave-w7r-record.md:62`（镜像 digest 落地）、`wave-w4-record.md:156`（golden 50/50） | 一致 | **PASS（限域）**，残余权限在"真实样本 + OCR 权重许可" |
| PoC-06 | 单 Provider Gateway + 窄工具 Registry（T19）；Context/预算预留（T20）；Harness 节点集成与取消（T21） | T19/T20/T21 均 `PASS（限域）` | `wave-w3-record.md:177`、`wave-w4-record.md:158`、`wave-w5-record.md:141` | 一致 | **PASS（限域）**，残余在真实供应商与 `stream()` |
| PoC-09 | 授权检索 + 三路对照（T25）；RAG 引用与最终事件一致性（T26） | T25、T26 均 `PASS（限域）` | `wave-w7-record.md:67`（T25 `12 passed`，ANN `NOT_RUN`）、`wave-w8-record.md:80`（T26 `21/21`，`citation_accuracy=1.0` 40/40 于替身模型） | 一致 | **PARTIAL**：试验可复现，但 ANN 未跑 + 质量阈值未获批 → 不得判质量 PASS（`05:74`） |

**小结**：六项中 **5 项 PASS（限域）+ 1 项 PARTIAL（PoC-09）**；"PASS（限域）"只说明"在写明的边界内成立"，不等于 G-P0 门通过（§3）。

### 2.3 证据 hash 漂移量化

| 口径 | 结果 |
| --- | --- |
| 本评审独立正则（仅匹配「路径 \| 尺寸 \| 行数 \| 64 位 hex」四列行） | 可解析路径 **9** 条：`HASH_MATCH=0 / HASH_DRIFT=9 / FILE_MISSING=0` |
| 子 agent 全量对账（`.t31_tmp/hash_drift.txt`，扫描 34 份证据 md） | `files_scanned=34 md; hash MATCH=31 DRIFT=119 cited_path_missing=0`，逐条带 `cited→cur + lines±N` |

**结论**：**引用路径 0 缺失（可定位性成立），但 119 处引用 hash 与 HEAD 不符（逐字节可复算性不成立）**。原因经核为合法演进：W7R 复跑改证据、`fix/w1-t02-pgvector` 换镜像、`f86765e` 去 EOF 空行、门禁后修复代码。因此：

- hash **只能当作"记录时点"**，不能当门禁判据；判据须用"文件存在 + 结构完整 + 门禁命令 exit 0 + 证据 JSON 可复算"。
- 建议给证据表加 `computed_at` 与 `reason/derived_from` 字段（§6 机制建议 M2）。

### 2.4 交叉核对发现的不一致（D1–D7）

| 编号 | 位置 | 事实 | 影响 |
| --- | --- | --- | --- |
| D1 | `docs/poc/02_Phase0_PoC_Plan.md` 状态列 | 六项 PoC 仍全为 `NOT_STARTED`，与波次记录 PASS 冲突 | 计划状态列**不可作判据**；状态维护职责未定（M1） |
| D2 | `wave-w9-record.md:86` | T29 汇报正文写"6 项 NOT_RUN"，证据 JSON `not_run` 实为 **5 项 + 1 项 RESOURCE_DEVIATION** | 已在同文件自纠；**以证据文件为准** |
| D3 | T25 结论时效 | T25 跑于 pgvector 修复之前，ANN 仍 `NOT_RUN`；`pgvector-probe.md` 已证 0.8.7 可用 | 属**可补做**项，不构成 FAIL |
| D4 | `wave-w1-record.md:88`、`wave-w2-record.md:107` | T02、P0-T03 标无保留 `**PASS**`，但行内分别含"端口占用演练 NOT_RUN""C-07 BLOCKED / C-10 NOT_RUN" | 判定口径越级（§8.3 linter 实测 2 处），建议下调为 `PASS（限域）` |
| D5 | `sbom.md` / `tests/phase0/pyproject.toml` / `docs/remote-extension-connection.md` | 三者均不存在（前二者属引用笔误/声称偏差） | 文档一致性缺陷，不影响判定，但影响可复现 |
| D6 | `.t31_tmp/t31_digest.txt:167` | 子 agent 自报行数与门禁实测不符（如 `checkpoint_probe.py` 1881 vs 1674） | 以 **sha256** 为准，行数只作参考 |
| D7 | `P0_Core_Gate_Review.md` §1.3.B PoC-01 行 | 沿用修复前结论 `PARTIAL（单活）` | 应按 T05R 更正为 `PASS（限域：单活）`（本文件 §2.2 已更正） |

## 3. G-P0 逐门评价

判据出处：`docs/poc/05_Phase0_Acceptance.md:68-76`（5 门），`:78-82`（G-FULL 与受限范围准入）。

### 3.1 G1 —— PoC 强制项有真实证据

| 判据 | 实测 | 结论 |
| --- | --- | --- |
| P0-01/02/03/04/06/09 六项强制 PoC 均有可复现证据；单活降级只能形成限域建议 | 5 项 `PASS（限域）`，PoC-09 `PARTIAL`（ANN NOT_RUN + 阈值未批准） | **PARTIAL** |

### 3.2 G2 —— 授权/网络/内容安全门 + 最小恢复演练

| 判据 | 实测 | 结论 |
| --- | --- | --- |
| T29 安全回归 0 breach；T30 最小恢复演练成立 | T29：`549 probes / 536 refused / 0 breaches`（`security-probes.json:5264`），`PASS（限域）` 含 5 项 `NOT_RUN`；T30：21 passed，**单实例**备份/还原/迁移/回滚，多实例与 RPO/RTO `NOT_RUN` | **PARTIAL**（安全侧成立，恢复侧只到单机） |

### 3.3 G3 —— C-01–C-10 实验契约闭合

| 判据 | 实测（`docs/poc/07_Contract_Closure.md`） | 结论 |
| --- | --- | --- |
| 十条契约均有状态且无阻塞项 | `DEFINED` 5（C-01/02/06/08/09）、`NEEDS_POC` 4（C-03/04/05/10）、`BLOCKED` 1（C-07 身份域/来源审阅） | **PARTIAL** |

### 3.4 G4 —— 依赖/镜像/模型/数据许可

| 判据 | 实测 | 结论 |
| --- | --- | --- |
| 依赖锁、镜像 digest、许可与数据来源明确 | `uv.lock` 锁定（960 行）、`docreader_sbom.md`(291) 覆盖 DocReader 镜像；但 **22 个非 OCR 依赖许可 / apt 包 / OCR 权重 = UNKNOWN**（T09R 四项 UNKNOWN）、Redis 7.4 为 RSALv2/SSPLv1、psycopg 为 LGPL-3.0 均**无 ADR 结论**；全仓 `sbom.md` **不存在** | **PARTIAL** |

### 3.5 G5 —— 质量基线与 PRD 性能阈值

| 判据 | 实测 | 结论 |
| --- | --- | --- |
| 质量阈值已校准并有达标证据 | Recall@5 ≥ 0.85 与引用精度 0.95 在 Backlog 均为**"候选待评审"**（未批准）；ANN 未跑；唯一实测为 T26 `citation_accuracy=1.0`（40/40，样本 100 例）且**模型为替身** | **NOT_RUN**（不得判质量 PASS） |

### 3.6 小结与逐任务判定汇总

**G-P0：5 门 0 门 PASS —— 4 门 PARTIAL + 1 门 NOT_RUN。**

值得注意的结构性事实：**任务级 28/30 成立（多数为 PASS（限域）），门级却 0/5 通过**。差别不在"做没做"，而在"限域"与"无保留"之间的缺口——G-P0 要的是无保留证据或经批准的边界，而当前每门都至少有 1 项 `NOT_RUN`。

| 任务 | 判定 | 依据（一句话） |
| --- | --- | --- |
| T01 | 文档交付 | `01_Technical_Risk_Register.md`(42)、`07_Contract_Closure.md`(231)；不参与 P0 门计数 |
| T02 | **PASS（限域）**（记录为 PASS，建议下调，D4） | `test_environment.py` 13 passed（含 pgvector 可用性）；端口占用负向演练 NOT_RUN、Redis/psycopg 许可待 ADR |
| T03 | **PASS（限域）**（记录为 PASS，建议下调，D4） | 16 passed；C-07 BLOCKED、C-10 NOT_RUN |
| T04 | PASS（限域） | 33 条 manifest（21 向量 + 12 负向）；F15 私网 IPv6/WS/DNS rebinding、F16 提示注入页为能力缺口 |
| T05 / T05R | PASS（限域：单活） | T05R `12 passed / exit 0`；原生 saver 无法 fencing（`wave-w3:100`） |
| T06 | PASS（限域） | 20 passed；未压测双执行者真并发进入 `executing` |
| T07 | PASS（限域） | 事务原子性/SKIP LOCKED/epoch CAS/五态死信实测 |
| T08 | PASS（限域） | 13 passed，`applied=100/duplicate=900`；真实 Celery、XAUTOCLAIM NOT_RUN |
| T09 / T09R | PASS（限域） | 镜像 digest 落地；4 类 UNKNOWN 已明列 |
| T10 | PASS（限域） | proto + stub 真 wire（错误码/上限/取消/超时） |
| T11 | PASS（限域） | 70 passed，golden 50/50、定位 50/50；素材为合成样本 |
| T12 | PASS（限域） | staging 不可见 + attempt/generation fencing；对象存储 fake、ANN NOT_RUN |
| T13 | PASS（限域） | 10 000 次并发读零混版本；B4–B7 未覆盖 |
| T14 | PASS（限域） | 协议/许可/版本审查；真机 OS 矩阵 PARTIAL |
| T15 | PASS（限域） | 契约/动作枚举/能力位全绿；T17 后已与 daemon 对齐 |
| T16 | PASS（限域） | 109 passed、26 条 SSRF 向量；Playwright 未装 → 真浏览器 NOT_RUN |
| T17 | PASS（限域） | `fix/w5-t17` 真配对收口（13/31 动作映射） |
| T18 | PASS（限域） | `fix/w5-t18` owner/撤销/未知结果人工接管 |
| T19 | PASS（限域） | Gateway/Registry/intent key；无真实供应商、`stream()` 未实现 |
| T20 | PASS（限域） | 27 passed；启发式 tokenizer、真实价格快照 NOT_RUN |
| T21 | PASS（限域，缺口已修） | 原生 `StateGraph`+`interrupt`+Postgres saver 主路径已补 |
| T22 | PASS（限域） | 51 passed；默认上限 32/64/8 与 72 边口径冲突待评审 |
| T23 | PASS（限域） | ready 判定/依赖传播/双调度 CAS；单活、30 轮 |
| T24 | PASS（限域） | 22 例；真实 `BudgetLedger` 未接线 |
| T25 | PASS（限域） | 12 passed；ANN/召回 NOT_RUN（pgvector 现已可用，可补做） |
| T26 | PASS（限域） | 21 passed；引用 1.0（40/40，替身模型） |
| T27 | PASS（限域） | 66 passed；候选/去重/审阅追加历史 |
| T28 | PASS（限域） | Research 全链控制站（`poc08-report.md` 131 行） |
| T29 | PASS（限域） | 549 probes / 536 refused / 0 breaches；5 项 NOT_RUN |
| T30 | PASS（限域） | 21 passed；多实例与 RPO/RTO NOT_RUN |
| T31 | 本次评审 | 本文件 |
| T32 | NOT_STARTED | 依赖 T31 结论 |

## 4. 失败与阻塞清单

### 4.1 FAIL（1 项）

| 项 | 事实 | 后果 |
| --- | --- | --- |
| T05 原生 saver fencing | 锁连接被杀后原生 `put` 仍成功（`wave-w3-record.md:100`） | 只能单活 runner；横向扩展安全性未证 → 直接压住 G2 与 PoC-01 的多实例限域 |

### 4.2 BLOCKED（1 项）

| 项 | 事实 | 解锁条件 |
| --- | --- | --- |
| C-07 身份/来源审阅域 | "P0 来源审阅草案可实验，身份/API/字段范围待决定"（`07_Contract_Closure.md`） | 产品 + 安全评审给出身份域与 URL 审阅入口范围；本项目约束**不自创域模型** |

### 4.3 PARTIAL（按门归集，19 项）

| 归属门 | 项 |
| --- | --- |
| G1 | 端口占用负向演练（T02）；RLS 跨空间未复用 `p0_t03` 策略（T26）；F15 私网 IPv6/WebSocket/DNS rebinding、F16 提示注入页（T04 能力缺口）；双执行者真并发未压测（T06）；真实 Celery / XAUTOCLAIM（T08）；合成样本非真实样本（T11）；对象存储 fake（T12）；B4 真实重试器、B5/B6/B7（T13）；真机 OS 矩阵、31 动作全覆盖（T14/T17）；HTTPS pin+SNI（T16）；启发式 tokenizer、真实价格（T20）；默认上限与 72 边口径（T22）；单活与 30 轮（T23）；`BudgetLedger` 未接线、kill 打断（T24）；ANN/召回（T25）；PoC-09 质量阈值（T26） |
| G2 | 多实例恢复、RPO/RTO（T30） |
| G3 | C-03/C-04/C-05/C-10 `NEEDS_POC` |
| G4 | 22 依赖许可 / apt 包 / OCR 权重 UNKNOWN；Redis、psycopg 许可无 ADR |
| G5 | Recall@5 0.85、引用精度 0.95 未获批 |

### 4.4 证据与文档缺陷

见 §2.4 的 D1–D7，以及 §2.3 的 hash 漂移机制缺失（M2）。这些**不改变门结论**，但会削弱证据链可复算性，属"可修但不该带进 T32"的项。

## 5. 未校准 / 未执行项

| # | 项 | 类型 | 阻断门 | 解锁条件 |
| --- | --- | --- | --- | --- |
| 1 | 真实 chat 模型链路（T26） | NOT_RUN | G1 / PoC-09 | 厂商 key 与额度；替身不得当语义理解证据 |
| 2 | pgvector ANN / Recall@5（T25） | NOT_RUN（可补做） | G5 | 授权补跑；已证 0.8.7 可用 |
| 3 | 质量阈值 0.85 / 0.95 校准 | 未批准 | G5 | 产品 + 技术联合评审批准阈值与样本量 |
| 4 | 真实 MinIO/S3（T12/T13） | NOT_RUN | G1 / PoC-03 | 对象存储接入与签名校验 |
| 5 | Redis 事件投递 / SSE 断网重连（T26） | NOT_RUN | G1 / PoC-09 | 真实投递与重连用例 |
| 6 | 真实 Celery worker（T08） | NOT_RUN | G1 / PoC-02 | 依赖准入 celery 后补跑 |
| 7 | Playwright 真浏览器（T16） | NOT_RUN | G1 | 安装 Playwright + 真隔离环境 |
| 8 | 真实 DocReader 服务镜像端到端（T10） | NOT_RUN | G1 / PoC-04 | 镜像 digest + 许可补齐后起真实服务 |
| 9 | 多实例恢复 / RPO / RTO（T30） | NOT_RUN | G2 | 多实例拓扑 + 备份指标定义 |
| 10 | 真机 OS 矩阵 / 31 动作全覆盖（T14/T17） | PARTIAL | G1 | 真机矩阵与剩余 18 动作 |
| 11 | F1–F18 故障矩阵逐条（T04 起） | NOT_RUN | G1 | 每场景归属任务补跑并回填 |
| 12 | 端口占用负向演练（T02） | NOT_RUN | G1 | 占用端口后起服务应显式失败 |
| 13 | 私网 IPv6 / WebSocket / DNS rebinding / 提示注入页 | 能力缺口 | G1（安全） | 策略补全 + 用例 |
| 14 | HTTPS pin + SNI 路径（T16） | 未实测 | G1 | 证书链场景 |
| 15 | 真实文档样本与真实解析质量（T11） | NOT_RUN | G1 / PoC-04 | 真实样本（含 OCR）+ 人工抽检 |
| 16 | 真实模型 plan（T22） | NOT_RUN | G1 / PoC-06 | 模型额度 |
| 17 | 真实价格快照（T20） | NOT_RUN | G1 / PoC-06 | 供应商价格表 |
| 18 | OCR 权重 / apt 包 / 22 依赖许可 | UNKNOWN | G4 | 供应链清单 + 许可结论 |
| 19 | 双执行者真并发进入 `executing`（T06） | 未压测 | G1 / PoC-01 | 并发注入用例 |

## 6. TDD/ADR 变更建议

### 6.1 建议新增 ADR（5 条，均"待评审"状态，本次不代替评审）

| 编号（建议） | 主题 | 触发事实 | 建议决策要点 |
| --- | --- | --- | --- |
| ADR-NEW-1 | 缓存/队列组件的许可选择 | Redis 7.4 为 RSALv2/SSPLv1 | 接受 SSPL 边界 / 改 Valkey / 只用内部不发布；须给出部署形态结论 |
| ADR-NEW-2 | psycopg LGPL-3.0 合规使用 | 依赖 `psycopg[binary]` | 动态链接 + 不修改 + 可替换声明；附 NOTICE |
| ADR-NEW-3 | 部署范围：单活 runner | T05 原生 saver 无法 fencing | 明确 P0/P1 阶段单活为准，多实例属后续 ADR；写入残余风险模板 |
| ADR-NEW-4 | pgvector 进 core profile | `pgvector-probe.md` 已证可用，但换镜像会作废 T02 digest/E1/E2 证据 | 立项授权 + 备份演练 + 重新采集 T02 证据 |
| ADR-NEW-5 | 身份域与来源审阅范围（C-07） | C-07 `BLOCKED` | 产品/安全评审给出域模型边界；**不得由工程侧自创** |

### 6.2 TDD 修订建议

| 文件 | 修订点 | 依据 |
| --- | --- | --- |
| `docs/tdd/01_*.md` §6.4 | 补"原生 saver 不可 fencing"的实测结论与单活前提 | T05/T05R |
| `docs/tdd/02_*.md` | `stream()` 未实现，port 需在 T21 前定形 | T19 `NOT_RUN` |
| C-03 / C-05 / C-10 | 由 `NEEDS_POC` 更新为"已验证/待验点"，并写清 epoch 映射、upstream 协议分离、`asset_bindings` 插入顺序 | `07_Contract_Closure.md` |
| T21 集成 | 原生 `StateGraph`+`interrupt()`+Postgres saver 主路径已落地，需同步进 TDD（此前用端口替身） | `wave-w5-record.md:141` |

### 6.3 机制建议

| 编号 | 建议 | 解决 |
| --- | --- | --- |
| M1 | `02_Phase0_PoC_Plan.md` 状态列指定唯一维护人 + 波次收口时同步更新 | D1 |
| M2 | 证据表增加 `computed_at`、`reason`、`derived_from`，明确"hash 为记录时点" | §2.3 漂移 |
| M3 | 把 §8.3 的"判定口径 linter"（含 `NOT_RUN` 的行不得标无保留 `PASS`）纳入门禁脚本 | D4 |

## 7. 生产前待办

**P0（阻断生产上线，必须先解决）**

1. C-07 身份域与来源审阅范围（BLOCKED）。
2. 许可三件套：Redis、psycopg、OCR 权重 / 22 依赖（§6.1 ADR-NEW-1/2）。
3. 多实例恢复与 RPO/RTO（G2 唯一硬缺口）。
4. 质量阈值批准 + ANN 补跑（G5）。
5. 安全能力缺口：私网 IPv6 / WebSocket / DNS rebinding / 提示注入页 / HTTPS pin+SNI。
6. 真实后端替换：MinIO/S3、Celery、DocReader 镜像、真模型。

**P1（可带条件进入 P1 开发，但需登记）**

7. 全仓 SBOM（当前仅 DocReader 镜像）。
8. 秘密/日志/trace 脱敏审计（含证据 JSON 中的 DSN 处理）。
9. F1–F18 故障矩阵逐条回填、真机 OS 矩阵、31 动作全覆盖。
10. PRD 性能指标与 Phase 0 实测的差距说明。

## 8. 注入测试记录

### 8.1 证据完整性注入（`verifier.py`：清单 hash 比对，任一不符 exit 1）

| 用例 | 注入动作 | 预期 | 实测 | exit |
| --- | --- | --- | --- | --- |
| 0（对照） | 无注入，4 份证据 hash 与清单一致 | ACCEPT | `VERDICT=ACCEPT problems=0/4` | 0 |
| A | 删掉一条被引用证据 `poc08-report.md` | REJECT | `MISSING poc08-report.md` / `VERDICT=REJECT problems=1/4` | 1 |
| B | 篡改 `uv.lock` 1 字节（模拟数值被改） | REJECT | `MISMATCH uv.lock cited=02ad6b839629.. cur=134a1d1a62d3..` | 1 |

结论：**"缺一条证据"与"换一个 hash"都会被拦下**，且对照组不会误报——校验器本身可用作门禁脚本（M2/M3 落地基础）。

### 8.2 判定口径 linter 注入（规则：行内含 `NOT_RUN` 者不得标无保留 `**PASS**`）

| 用例 | 输入 | 实测 |
| --- | --- | --- |
| 0（基线） | 10 份 `wave-w*-record.md` | **violations=2** —— `wave-w1-record.md:88`（T02）、`wave-w2-record.md:107`（P0-T03）（即 D4） |
| A | 伪造行 `\| P0-T99 \| **PASS** \| 真实模型链路 NOT_RUN ... \|` | 拦下，`violations=1` |
| B（对照） | 同一行改为 `**PASS（限域）**` | 放行，`violations=0`（符合限域口径） |

### 8.3 复现方法（自包含，不依赖临时目录）

校验器（存为任意临时目录下的 `verifier.py`，同目录放 `manifest.json`：`{"文件名": "sha256"}`）：

```python
import hashlib, io, json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
man = json.load(io.open(os.path.join(d, "manifest.json"), encoding="utf-8"))
bad = 0
for name, want in man.items():
    fp = os.path.join(d, name)
    if not os.path.exists(fp):
        print("MISSING " + name); bad += 1; continue
    if hashlib.sha256(open(fp, "rb").read()).hexdigest() != want:
        print("MISMATCH " + name); bad += 1
print("VERDICT=%s problems=%d/%d" % ("REJECT" if bad else "ACCEPT", bad, len(man)))
sys.exit(1 if bad else 0)
```

口径 linter（规则：行内出现 `NOT_RUN` 时判定不得为无保留 `**PASS**`）：

```python
import glob, io, os
def lint(paths):
    bad = []
    for p in paths:
        for i, l in enumerate(io.open(p, encoding="utf-8").read().split("\n")):
            t = l.strip()
            if not t.startswith("|"):
                continue
            cells = [c.strip() for c in t.strip("|").split("|")]
            if any(c == "**PASS**" for c in cells) and "NOT_RUN" in t:
                bad.append((os.path.basename(p), i + 1))
    return bad
hits = lint(sorted(glob.glob("docs/poc/evidence/wave-w*-record.md")))
print("violations=%d %s" % (len(hits), hits))
```

运行环境：`experiments/phase0/.venv/Scripts/python.exe`（CPython 3.12.13），三个用例的实测输出见 §8.1/§8.2 表格。

### 8.4 说明

- 注入全部在仓库内临时目录 `.t31_tmp/` 完成（`%TEMP%` 下 Python 无写权限，实测 `PermissionError`），**收尾已删除**；被注入的只是副本，`experiments/phase0/uv.lock` 原件未改动（收尾 `git status` 无该文件）。
- 证据提取脚本与中间物（`waves3`、`hash_drift`、`scenarios` 等）随 `.t31_tmp/` 一并清理；可复现逻辑已固化在 §8.3，不再依赖任何临时文件。

## 9. 最终准入建议

**结论：G-P0 不成立（5 门 0 门 PASS：4 PARTIAL + 1 NOT_RUN），不得宣布 P0 核心出口就绪，不得据此进入生产。**

依据：

1. `05_Phase0_Acceptance.md:73-76` 明确"缺一项即记 PARTIAL/FAIL/BLOCKED"，而 G1（PoC-09 质量）、G2（多实例恢复）、G3（C-07）、G4（许可）、G5（阈值）各至少缺 1 项。
2. `05:74` 禁止把未校准阈值判为质量 PASS —— G5 只能 `NOT_RUN`。
3. `05:75-76` 安全零豁免 —— 安全侧本身达标（0 breach），但恢复侧与安全能力缺口（F15/F16）仍在。
4. 任务级 28/30 成立不改变门级结论：**"限域成立"≠"门通过"**，这正是本次评审要防止的折算。

**可提出的"受限范围准入"建议**（`05:78-82` 允许的形式，需产品/安全/架构三方评审，本评审不代替）：

- **可准入（带条件）**：PoC-02 可靠作业、PoC-03 版本发布、PoC-04 解析链、PoC-06 Harness 单 Provider 链路、PoC-01 单活 checkpoint —— 条件：单活部署、单租户、无外网、合成数据、真实动作禁用。
- **明确禁用**：浏览器真实登录/占座等外部写操作、真实模型语义质量结论、ANN 检索质量结论、多实例高可用承诺、对象存储真实一致性承诺、任何"P0 已完成"对外表述。

**下一步（按顺序）**：

1. 用户裁决 §6.1 五条 ADR 的立项与评审顺序（ADR-NEW-1/2/5 与安全/产品相关，建议优先）。
2. 补做 G5：授权后跑 pgvector ANN + 阈值校准（注意会作废 T02 digest/E1/E2 证据，需同批重采）。
3. 再执行 **T32** 全量 Phase 0 / P1 前置证据评审 —— 本文件（T31）作为其输入，不得跳过。
4. 把 M1/M2/M3 三项机制落进门禁脚本，避免 T32 重复本次人工核对成本。
