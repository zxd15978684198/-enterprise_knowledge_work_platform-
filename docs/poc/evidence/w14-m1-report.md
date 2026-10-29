# W14 / M1 报告：计划状态列维护机制

- 波次：W14（机制批），分支 `feature/w14-m1-m2-m3`
- 任务来源：`P0_Core_Gate_Review.md` §2.4 D1 + §6.3 M1
- 执行人：Lead（原计划派 `w1-t09-docreader`，派发通道启动即失败，改由 Lead 执行；同一现象见 W14 声明块 §1 与 `wave-w14-record.md` §2）
- 日期：2026-10-29

## 1. 改了什么

| 文件 | 位置 | 变更 |
| --- | --- | --- |
| `docs/poc/02_Phase0_PoC_Plan.md` | 第 3 行 | 在 W0 历史快照句后追加括注：该句保持原样，实时状态见 §3 两列 + §3.1 条款 |
| 同上 | §3 总览表 | 表头加「依据（W14 复核，2026-10-29）」列；九行「当前结果」从全 `NOT_STARTED` 更新为经证据核对的值，并把依据定位到具体文件:行 |
| 同上 | §3 后 | 新增 **§3.1 状态列维护条款**：唯一维护人、更新时机、依据、允许取值、与门级判据的关系、历史保护 |
| 同上 | §13 | 保留 W0 段原文，追加「截至 W14 的实时状态」段：8×`PASS（限域）` + PoC-09 `PARTIAL`，门级仍不通过 |

## 2. 九项 PoC 旧值 → 新值

| PoC | 旧 | 新 | 依据 |
| --- | --- | --- | --- |
| PoC-01 | NOT_STARTED | **PASS（限域：单活）** | `wave-w7r-record.md:61`（T05R `12 passed / exit 0`）、`wave-w3-record.md:100`、`wave-w4-record.md:154` |
| PoC-02 | NOT_STARTED | **PASS（限域）** | `wave-w3-record.md:174`（T07）、`wave-w4-record.md:155`（T08 `applied=100 / duplicate=900`） |
| PoC-03 | NOT_STARTED | **PASS（限域）** | `wave-w5-record.md:140`（T12）、`wave-w6-record.md:111`（T13 万次并发读零混版本） |
| PoC-04 | NOT_STARTED | **PASS（限域）** | `wave-w7r-record.md:62`（T09R digest）、`wave-w4-record.md:156`（T11 golden 50/50） |
| PoC-05 | NOT_STARTED | **PASS（限域）** | `wave-w1-record.md:55`（T14）、`wave-w3-record.md:92`（T15）、`wave-w4-record.md:157`（T16）、`wave-w5-record.md:86`（T17 真配对）、`fix-w5-t18-record.md:94`（T18） |
| PoC-06 | NOT_STARTED | **PASS（限域）** | `wave-w3-record.md:177`（T19）、`wave-w4-record.md:158`（T20）、`wave-w5-record.md:141`（T21） |
| PoC-07 | NOT_STARTED | **PASS（限域）** | `wave-w4-record.md:91`（T22）、`wave-w5-record.md:142`（T23）、`wave-w6-record.md:68`（T24） |
| PoC-08 | NOT_STARTED | **PASS（限域）** | `wave-w7-record.md:68`（T27 `66 passed`）、`wave-w9-record.md:38`（T28） |
| PoC-09 | NOT_STARTED | **PARTIAL** | `wave-w7-record.md:67`（T25，ANN 当时 `NOT_RUN`）、`wave-w8-record.md:80`（T26 替身模型 `citation_accuracy=1.0`）；ANN 已由 W13 补跑，但质量阈值待 ADR-0014 批准 → 不得判质量 PASS |

**没有一项被升级为无保留 `PASS`**：六项有独立报告的走评审 §2.2 口径（5×`PASS（限域）` + PoC-09 `PARTIAL`），PoC-05/07/08 逐条回到波次记录取判定行，未凭印象升级。

## 3. 为什么 PoC-09 保持 PARTIAL（即使 ANN 已补跑）

W13 只解除了「ANN 不可用」这一阻塞（`ann-bench.json`：HNSW recall 0.998–1.000），但 PoC-09 的验收包含 Recall@5 / 引用准确率 / 事实忠实度等**需批准阈值的质量指标**（`02_Phase0_PoC_Plan.md` §12 验收段：门槛未定 → `PARTIAL`）。ADR-0014 尚未批准阈值，故保持 `PARTIAL`。

## 4. 保持 `NOT_STARTED` 的项目

无。九项均有可定位证据；但**注意：`PASS（限域）` 不等于门通过**，G-P0/G-FULL 状态未变（见 §13 新增段与两份门评审）。

## 5. 实际执行的检查

```text
git diff --stat docs/poc/02_Phase0_PoC_Plan.md
 docs/poc/02_Phase0_PoC_Plan.md | 26 ++++++++++++++++++++++----
```

- 逐行核对：`Select-String` 抽取 `wave-w*-record.md` 中 9 个任务的判定行，确认「任务 → 结论」不是从报告正文推断，而是取判定表格行。
- 未运行 pytest（本任务只改 Markdown，无代码）。
- 未改动其他文件。

## 6. 未解决 / 遗留

1. 本机制的**自动校验**依赖 M3 的 `verdict_lint.py`（规则「含 `NOT_RUN` 的行不得标无保留 `PASS`」）；M3 落地后应把 §3.1 的取值规则纳入门禁命令。
2. `docs/poc/03_Phase0_Task_Backlog.md` 的任务级状态列仍是历史值，本轮未动（属 T32 §7 的文档债务，不在 M1 授权范围）。
