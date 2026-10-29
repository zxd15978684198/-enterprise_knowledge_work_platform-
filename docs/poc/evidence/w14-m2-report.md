# W14 / M2 报告：证据元数据与指纹台账

- 波次：W14（机制批），分支 `feature/w14-m1-m2-m3`
- 任务来源：`P0_Core_Gate_Review.md` §2.4 D2（证据可校验性）+ §6.3 M2
- 执行人：Lead（同 M1，派发通道失效后自执行）
- 日期：2026-10-29

## 1. 交付物

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `experiments/phase0/evidence_meta.py` | 475 | 台账校验器：规范化指纹、task/wave 归属、derived_from 溯源、unregistered 检测 |
| `tests/phase0/test_evidence_meta.py` | 258 | 13 条单元测试（指纹语义 2、缺口识别 7、台账漂移 3、真实仓库回归 1） |
| `docs/poc/evidence/SHA256SUMS` | 10 | 8 份 JSON 的规范化指纹 + 复算命令 |
| `docs/poc/evidence/evidence_manifest.json` | — | 机器可读台账（task/wave/时间戳来源/derived_from 及其 sha256） |

## 2. 指纹口径（为什么不是「文件字节 sha256」）

```text
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
sha256 = sha256(canonical.encode("utf-8"))
```

- **键序、缩进、行尾不影响**：同一语义多次重新序列化不会假漂移；
- **任何取值变化都会变**：手改一个数字立刻暴露；
- 为什么不用原始字节：探针每次跑都会改 `generated_at`，原始字节必然漂移，校验会退化成噪音；
  这正好对应 T31 评审 §2.3 的结论——「hash 只能作**记录时点**的定位，不能逐字节复算」。
  本台账把这句话变成工具事实：**指纹对语义稳定、对取值敏感**。

## 3. 台账实跑结果（`--check`）

```text
$ uv run python evidence_meta.py --check
evidence=8 with_problem=0 task_from_manifest=2 no_timestamp=1 global_problems=0
exit 0
```

| 证据 | task | wave | 时间戳来源 | sha256 前 12 |
| --- | --- | --- | --- | --- |
| `ann-bench.json` | P0-T25 | W13 | payload | `41d104a07e8c` |
| `docreader_image_inventory.json` | P0-T09R | W7R | payload | `9ce631798f5c` |
| `p0-t17-live-run.json` | P0-T17 | W5 | payload（`collected_at`） | `f39fc294d731` |
| `poc01_owner_process_level.json` | P0-T05R | W7R | payload | `8827573665a8` |
| `rag-citations.json` | P0-T26 | W8 | payload | `199ae4e5b935` |
| `research-batch.json` | P0-T28 | W9 | **absent** | `20b3845aa554` |
| `retrieval-baseline.json` | P0-T25 | W7 | payload | `c4810d4c3a82` |
| `security-probes.json` | P0-T29 | W9 | payload | `aa617a9ab242` |

## 4. 这次登记暴露的 3 个真实缺口（本节是 M2 的价值所在）

| # | 缺口 | 量化 | 处置 |
| --- | --- | --- | --- |
| G1 | **载荷缺 `wave` 字段** | 8 份中 **6** 份没有（只有 `ann-bench.json` 有 `wave`，`retrieval-baseline.json` 有 `wave=W7`） | 台账补登 `wave`，并标 `wave_source=manifest`；**不改写历史证据**，缺口在台账里可见 |
| G2 | **载荷缺 `task` 字段** | 2 份：`rag-citations.json`、`research-batch.json` | 同 G1，标 `task_source=manifest`；`test_repo_evidence_ledger_is_clean` 断言这两份必须被识别出来，防止「补了台账就把缺口洗掉」 |
| G3 | **无任何时间戳** | `research-batch.json`（既无 `generated_at` 也无 `collected_at`） | 台账声明 `timestamp_field=None`，工具不报 NO_TIMESTAMP 但仍输出 `timestamp_source=absent` |

这三条不是 M2 造出来的，而是台账第一次全量扫描时**必然暴露**的结构债；它们也是 T31 评审 D2 的实证补充。

## 5. 校验规则（`--check` 会拒的情况）

| 问题码 | 触发条件 |
| --- | --- |
| `MISSING_FILE` | 台账登记了但文件不存在 |
| `UNREGISTERED` | 证据目录里出现未登记的 `*.json`（台账自身除外） |
| `TASK_MISMATCH` / `WAVE_MISMATCH` | 载荷声明的 task/wave 与台账不一致 |
| `DERIVED_MISSING` | `derived_from` 指向的 md/代码不存在 |
| `DERIVED_NO_CITATION` | `derived_from` 的 md **没有出现该任务编号**（防止挂错报告） |
| `LEDGER_MISSING` / `LEDGER_MISMATCH` | `SHA256SUMS` 缺条目 / 指纹与当前规范化内容不一致 |
| `NO_TIMESTAMP` | 台账要求有时间戳但载荷里没有 |

## 6. 实跑的检查

```text
$ uv run python evidence_meta.py --write   → exit 0
$ uv run python evidence_meta.py --check   → exit 0（evidence=8 with_problem=0 global_problems=0）
$ uv run python -m pytest ../../tests/phase0/test_evidence_meta.py -q
  13 passed in 1.62s
```

测试覆盖的判定逻辑：键序/缩进不变性与取值敏感性、非 ASCII 不转义、TASK_MISMATCH、DERIVED_MISSING、
DERIVED_NO_CITATION、manifest 兜底来源、`timestamp_field=None` 与缺失时间戳的区分、`collected_at` 回退、
台账往返 + 手改检测、UNREGISTERED、台账自身不算未登记、真实仓库回归（8 份全绿 + 两份 manifest_source 必现）。

## 7. 未解决 / 遗留

1. **不能证明「证据内容正确」**：M2 只保证「归属清楚、没被手改、能追溯到报告」。证据是否支持判定仍由门评审负责。
2. **`SHA256SUMS` 需要随证据重生成**：全量 pytest 会重写 6 份 tracked 证据（时间戳/计数抖动），此后 `--check` 会报 `LEDGER_MISMATCH`。正确做法是**重跑后执行一次 `--write` 并随波次提交**；本条应写进门禁命令（已在 §3.1 维护条款与波次记录 §3 标注）。
3. **`derived_from` 只到「报告级」**：没有做到「JSON 里每个结论 → 报告哪一行」的双向映射，那是 T32 §7 的 P1 文档债务。
