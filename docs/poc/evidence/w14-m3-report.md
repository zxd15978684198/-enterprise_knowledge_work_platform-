# W14 / M3 报告：判定口径 linter 门禁化

- 波次：W14（机制批），分支 `feature/w14-m1-m2-m3`
- 任务来源：`P0_Core_Gate_Review.md` §2.4 D3 + §6.3 M3
- 执行人：Lead（同 M1/M2，派发通道失效后自执行）
- 日期：2026-10-29

## 1. 交付物

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `experiments/phase0/verdict_lint.py` | 388 | 四条规则 + 基线机制 + JSON 输出 |
| `tests/phase0/test_verdict_lint.py` | 175 | 13 条测试（合规放行 4、违规拦截 5、基线 3、真实仓库回归 1） |
| `docs/poc/evidence/verdict_lint_baseline.json` | — | 存量 9 条，逐条带 `reason` |
| `docs/poc/evidence/verdict-lint.md` | 76 | 规则与用法说明（供后续波次直接接门禁） |

## 2. 实跑结果

```text
$ uv run python verdict_lint.py --check
docs_scanned=50 violations=9 new=0 by_rule[BARE_PASS_WITH_CAVEAT=8 WAVE_RECORD_SECTIONS=1]
exit 0

$ uv run python -m pytest ../../tests/phase0/test_verdict_lint.py -q
13 passed in 0.14s
```

（`WAVE_RECORD_SECTIONS=1` 是本文件写入前 `wave-w14-record.md` 尚无 §2–§4 造成的自检命中；补齐后重跑基线只剩 8 条 `BARE_PASS_WITH_CAVEAT`，见 §5。）

## 3. 规则收敛过程（重要：第一版是错的）

第一版按声明块字面实现「裸 `**PASS**` 即违规」，全仓跑出 **58 条**、`exit 1`：

| 规则 | 第一版命中 |
| --- | --- |
| `BARE_PASS` | **54** |
| `NOT_RUN_CLAIM` | 3 |
| `WAVE_RECORD_SECTIONS` | 1 |

54 条里绝大多数是**合法写法**：`poc03-publication.md` 的 A1–A7 验收逐条 `PASS`（子项判定，不是门级）、
`poc01-scope.md` 的 1–8 条用例逐条 `PASS`、`wave-w1/w2` 的历史表行。若照此落地，linter 的唯一结果是
"所有人都不看它"——这就是**规则必须精确到行、且必须能区分主体**的原因。

三处修正（均已写进代码注释与 `verdict-lint.md` §2）：

1. **裸 PASS 必须与同行 caveat 同时出现**才判违规（`BARE_PASS_WITH_CAVEAT`）→ 54 降到 8；
2. **fenced 代码块整段跳过**、**行内代码剥除**（`**PASS**` 写在反引号里是引用字面量，不是判定）→ 消除规则说明文档的自我违规；
3. **否定语境豁免**（"不是 G-FULL 通过"不是声称）→ `NOT_RUN_CLAIM` 从 3 降到 1（剩余 1 条是 Mermaid 流程图节点，见 §4）。

## 4. 剩下的 9 条：5 条口径债 + 2 条规则粒度误报 + 2 条历史

| 类别 | 条目 | 处置 |
| --- | --- | --- |
| **D4 原始两处** | `wave-w1-record.md:88`（T02）、`wave-w2-record.md:107`（P0-T03） | 基线保留，原因写明「T31 评审已登记」；修它要动历史证据，留待文档债务批次 |
| **同型口径债** | `environment.md:8`/`:39`（状态 `PASS` 而同行记 `NOT_RUN`）、`poc01-owner.md:219`、`poc02-replanning.md:9` | 同上，基线带具体原因 |
| **规则粒度误报** | `wave-w3-record.md:59`（同行并列 T03/T04 `PASS` 与 T09 `PARTIAL`）、`wave-w6-record.md:37` | 接受误报，限制写进 `verdict-lint.md` §6 |
| **自检** | `wave-w14-record.md` 缺 §2–§4 | **已修**（补写记录后消失） |

## 5. 基线现状（补齐波次记录后重跑）

```text
docs_scanned=50 violations=8 new=0 by_rule[BARE_PASS_WITH_CAVEAT=8]
exit 0
```

## 6. 为什么这算「门禁化」而不是「又一个脚本」

| 要求 | 本实现的落点 |
| --- | --- |
| 可重复、退出码可判 | `--check` 只在**新增**违规时 exit 1 |
| 存量不阻塞但要可见 | 基线按 `(路径, 规则)` 计数，逐条带 `reason` |
| 不能靠"我记得" | `test_repo_has_no_new_violations` 把真实仓库纳入单测回归 |
| 规则本身可审计 | 规则语义、降噪理由、已知误报都写进 `verdict-lint.md` |

## 7. 未解决 / 遗留

1. **不做语义判断**：linter 只保证「写法不自相矛盾」，不保证「PASS 有证据支持」——后者是门评审。
2. **行级粒度**：2 条并列多任务的误报需要列语义解析才能消除（P2）。
3. **5 条口径债未修**：修改会改变历史证据内容，需与 `SHA256SUMS` 重算同批进行。
