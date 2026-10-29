# 判定口径 linter 使用说明（verdict_lint.py）

> 工具：`experiments/phase0/verdict_lint.py`　测试：`tests/phase0/test_verdict_lint.py`（13 例）
> 基线：`docs/poc/evidence/verdict_lint_baseline.json`
> 引入波次：W14 / M3（来源 `P0_Core_Gate_Review.md` §2.4 D3、§6.3 M3）

## 1. 它解决的问题

同一个坑在 P0 里反复出现三次：**任务级 `PASS（限域）` 被写成门级 `PASS`**、同一行承认 `NOT_RUN` 却把结论写"通过"、
`G-FULL` 未通过却被下游引用为"已完成"。这类错误人写时看不见，只能靠评审复读；本工具把它变成静态检查。

## 2. 四条规则

| 规则 | 拦截条件 | 为什么 |
| --- | --- | --- |
| `BARE_PASS_WITH_CAVEAT` | 同一行出现裸 `**PASS**`，且同行含 `NOT_RUN` / `PARTIAL` / `BLOCKED` / `NOT_STARTED` | 口径要求「限域」必须写出来，因为限定条件本身是判定的一部分 |
| `NOT_RUN_CLAIM` | 文档含 `NOT_RUN`，却同时声称 `G-P0 通过` / `G-FULL 通过` / `Phase 0 通过`（否定语境如「不是 G-FULL 通过」不算） | 未跑完的链路不能支撑门级结论 |
| `TASK_ID_FORMAT` | 任务编号不是 `P0-Tnn`（两位数字），如 `P0-T5`、`P0-T005` | 编号不齐会让跨波次检索漏项 |
| `WAVE_RECORD_SECTIONS` | `wave-w*-record.md` 缺 `## 1.`–`## 4.`（声明/派发/门禁/提交） | 波次记录少一节就无法审计「当时凭什么这么判」 |

**刻意的降噪**：fenced 代码块整段跳过；行内代码（`` `**PASS**` ``）视为被引用的字面量——否则"解释这条规则"的文档会自己违规。

## 3. 用法

```bash
cd experiments/phase0

# 日常检查：只在新增违规时失败（exit 1）
uv run python verdict_lint.py --check

# 机器可读（给 CI / 报告引用）
uv run python verdict_lint.py --check --json

# 存量违规变动后重建基线（会写 verdict_lint_baseline.json）
uv run python verdict_lint.py --write-baseline
```

输出形如：

```text
docs_scanned=50 violations=9 new=0 by_rule[BARE_PASS_WITH_CAVEAT=8 WAVE_RECORD_SECTIONS=0]
```

- `violations`：全仓命中总数（含基线内）；
- `new`：基线之外的新增数，**只有它决定退出码**。

## 4. 基线机制（基线 ≠ 永久豁免）

`verdict_lint_baseline.json` 按 `(路径, 规则)` 记录**允许的条数**与 `reason`：

- 存量 9 条被豁免，不阻塞新工作；
- 同类**新增**一条立刻失败（`new=1` → exit 1）——这是基线与"关掉检查"的本质区别；
- 每条基线都带 `reason`，审计时可判断它该修还是该永远保留（当前 9 条的性质见 `wave-w14-record.md` §3.2）。

## 5. 接入门禁（推荐命令）

```bash
# 文档类波次的最小门禁
uv run python verdict_lint.py --check && uv run python evidence_meta.py --check
```

## 6. 已知限制

1. **行级粒度**：无法区分「一行并列多个任务」与「同一任务写成裸 PASS」，因此 `wave-w3-record.md:59`、`wave-w6-record.md:37` 属可接受误报；修它需要解析表格列语义（P2）。
2. **不做语义判断**：只认字面模式，不判断「这个 PASS 是否真的被证据支持」——后者是门评审的职责。
3. **不检查英文/大小写变体**：`Pass`、`passed` 不在规则内（本项目判定词表固定为大写）。
