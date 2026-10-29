# 证据指纹生命周期说明（W15 / 任务 C）

- 任务来源：`docs/poc/evidence/wave-w15-record.md` §1.1 表 C（依据 `P0_Core_Gate_Review.md` §2.3、`wave-w14-record.md` §3.1、`experiments/phase0/evidence_meta.py`）
- 写权：本文件是唯一写入对象；本轮**未**改动任何 `*.json` 证据、未改 `SHA256SUMS`、**未执行** `evidence_meta.py --write`、未跑全量 pytest、未做 git 写操作。
- 适用边界：本文描述的是"证据的身份与可追溯性"，不改变任何门级判定（`wave-w14-record.md`:97：G-P0 仍 4×PARTIAL + 1×NOT_RUN）。

## 1. 这套机制由三个对象与一个判据链组成

| 对象 | 位置 | 当前规模 |
| --- | --- | --- |
| 指纹台账 | `docs/poc/evidence/SHA256SUMS` | 10 行 = 2 行注释 + 8 条指纹 |
| 元数据台账 | `docs/poc/evidence/evidence_manifest.json` | 200 行，`schema=pyknora.phase0.evidence-manifest.v1`，8 条 entry |
| 校验器 | `experiments/phase0/evidence_meta.py` | 403 行，`--check` / `--write` / `--json` |

指纹口径（`evidence_meta.py:158-171`）：对 `json.loads` 后的对象按 `sort_keys=True`、`separators=(",", ":")`、`ensure_ascii=False`、`allow_nan=False` 重新序列化，再对 UTF-8 字节求 sha256。
因此**缩进、键序、行尾风格都不影响指纹，只有"值"变化才影响**。这条设计是全文的前提：它让"重新生成一次 JSON"必然可能改指纹。

## 2. 漂移成因：为什么一次全量回归就会让台账变红

### 2.1 会被全量回归重写的 6 份 tracked 证据

`wave-w10-record.md`:78 已点名这份清单（"E2 全量 pytest 会重写 6 份 tracked 证据"），本轮逐条核对存在性与写入方：

| # | 文件 | 行数 | 写入方（仓库内） | 重写时抖动的字段 | 是否在 `SHA256SUMS` |
| --- | --- | --- | --- | --- | --- |
| 1 | `docs/poc/evidence/poc01_owner_process_level.json` | 64 | `tests/phase0/test_checkpoint_owner.py:464-465`（用例直接拼 `docs/poc/evidence/` 路径） | `generated_at`、`popen_pid`/`reported_pid` 等进程级取值 | 是 |
| 2 | `docs/poc/evidence/retrieval-baseline.json` | 399 | `experiments/phase0/retrieval_probe.py:127`、`:1788-1791` | `git.branch`/`git.head`/`git.dirty_sha256`、`environment.*`、timing | 是 |
| 3 | `docs/poc/evidence/rag-citations.json` | 353 | `experiments/phase0/rag_probe.py:53`、`:1760-1762` | `generated_at`、`model_port`、`latency_ms`（p50/p95/max）、`cases` | 是 |
| 4 | `docs/poc/evidence/research-batch.json` | 799 | `experiments/phase0/research_probe.py:51` 只有常量，**未见写盘调用**（见 §6 待验证） | `rows`/`summary`/`task_count`（实跑统计，无时间戳也会变） | 是 |
| 5 | `docs/poc/evidence/security-probes.json` | 5264 | `experiments/phase0/security_probe.py:67`、`:3443` | `generated_at`、`git.*`、`probes`、内嵌 `rag_baseline` | 是 |
| 6 | `docs/poc/evidence/security-matrix.md` | 593 | `experiments/phase0/security_probe.py:66`、`:3621`（由 JSON 渲染） | 与 `security-probes.json` 同步；md 内容本身也带结论数字 | **否**（台账只扫 `*.json`） |

### 2.2 三类"重写即改指纹"的机制

1. **时间戳抖动**：#1/#2/#3/#5 都把生成时间写进载荷（`generated_at` 或 `collected_at`）。这是"逐字节复算不可行"的根因（`P0_Core_Gate_Review.md` §2.3 已定性），也是台账改用"规范化语义指纹"的原因。
2. **运行态元数据抖动**：#2/#5 记录 `git.head`/`branch`/`dirty_sha256`——**换分支或工作树有未提交改动，指纹就会变**，与证据结论无关。这一条最容易被误判成"证据被改坏"。
3. **派生与级联**：#6 `security-matrix.md` 由 #5 渲染，删掉一个 probe 就会同时改两份；#5 的载荷内嵌 `rag_baseline`（`cases_embedded`/`citation_accuracy`/`sample_count` 等，取自 #3），所以**只重跑 `security_probe.py` 而 `rag-citations.json` 已变，会写出一份"半新半旧"的证据**。

反向要点：**"没有时间戳"不等于"不会漂"**。#4 `research-batch.json` 在台账里被声明为 `timestamp_field=None`（`evidence_meta.py:121`），但它仍会因 `rows`/`summary` 计数变化而改指纹——所以"加个 `computed_at` 就稳定了"是错的判断。

### 2.3 这不是偶发：仓库里已有 5 处同现象记录

| 出处 | 记录要点 |
| --- | --- |
| `wave-w10-record.md`:78 | 6 份清单 + 处置：`git checkout --` 被安全网阻止，改用 `git stash push -m "w10-fullsuite-regen-6-evidence"` 隔离，不入提交 |
| `wave-w8-record.md`:101 | 2 份（#1/#2）被重写，随 docs 提交纳入；根因"进程级用例写死 tracked 路径"待改为写 `.pytest_cache/` |
| `fix-w5-t18-record.md`:92、`:117` | 差异"仅为时间戳/PID/`branch`/`head` 元数据，无语义断言变化" |
| `fix-w1-t02-pgvector-record.md`:124 | 两份 diff 各 14/13 行，"与本次镜像变更无关" |
| `IMPLEMENTATION_STATUS.md`:312、`:330` | 明确登记为测试侧债务：证据产物应写临时目录 |

**结论**：漂移不是"谁手滑"，而是**证据产物被写进 tracked 路径**这一结构性债务的必然结果。台账把这件事变成可见的红色，但没有消除债务本身。

## 3. 职责边界：四个对象各管什么、不管什么

| 对象 | 管什么 | 不管什么 | 失效时的信号 |
| --- | --- | --- | --- |
| `SHA256SUMS` | 8 份 JSON 的**规范化指纹**；唯一目的是判"内容有没有变" | 不管归属、不管 task/wave、不管文件是否存在（缺文件只在 manifest 里体现为 `MISSING_FILE`） | `LEDGER_MISSING` / `LEDGER_MISMATCH` |
| `evidence_manifest.json` | 每份证据的 `task`/`wave`/`timestamp`/`derived_from`/`sha256` 与问题码；是**人可读的派生报告** | 自带 `generated_at`（每次 `--write` 必变），因此**不能自身当判据**；也不能证明结论正确 | 问题码列表 |
| `evidence_meta.py --check` | 执行者：把上面两者与磁盘现状对上（存在性、`task`/`wave` 一致性、`derived_from` 是否存在且引用该任务、指纹是否一致、目录里有没有未登记 JSON） | 不判断"证据是否支持 PASS"；不覆盖自身版本 | 退出码：有问题 = 1（`evidence_meta.py:399`） |
| `verdict_lint.py` + `verdict_lint_baseline.json` | **判定口径**：`BARE_PASS_WITH_CAVEAT` / `NOT_RUN_CLAIM` / `TASK_ID_FORMAT` / `WAVE_RECORD_SECTIONS` 四条字面规则（`verdict-lint.md`:14-19） | 不看 JSON、不看指纹；不做语义判断；`verdict_lint_baseline.json` 是**工具产物而非证据**，已列入 `NON_EVIDENCE_JSON`（`evidence_meta.py:47`） | 只有 `new` 影响退出码（`verdict-lint.md`:45） |
| `wave-wN-record.md` §3 | 人写的门禁结论与命令/退出码摘要；是"当时凭什么这么判"的审计对象 | 不可机检；写错只能靠 `verdict_lint` 的字面规则拦一部分 | `WAVE_RECORD_SECTIONS` |
| `docs/poc/02_Phase0_PoC_Plan.md` 状态列 | 面向读者的进度视图 | **不可作判据**（T31 §2.4 D1：该列曾整列过期） | 无（靠 M1 维护条款） |

一句话分工：**`SHA256SUMS` 判"变没变"，`evidence_manifest.json` 判"是谁的"，`verdict_lint` 判"话有没有说大"，波次记录判"当时怎么定的"。四者互不替代；任何一格缺失都不能用另一格补。**

## 4. 维护协议

### 4.1 什么时候必须 `--write`

| 触发条件 | 为什么 |
| --- | --- |
| 跑过全量回归 `pytest tests/phase0` | §2.1 的 6 份可能已被重写（时间戳/PID/计数） |
| 单独重跑任一探针（`rag_probe.py`/`retrieval_probe.py`/`security_probe.py`/`research_probe.py`/T05 进程级用例） | 同上，且 §2.2-3 会级联 |
| 新增或删除证据 JSON | 新文件不登记 → `UNREGISTERED`；删文件 → `MISSING_FILE` |
| 重命名/移动 `derived_from` 指向的报告或代码 | 指纹不变但 `DERIVED_MISSING` 出现，需同步改 `SPECS` 后重算 |
| 规范化口径本身变了（例如改 `separators`） | 全部指纹重算，属破坏性变更，须在波次记录里写明 |

**不需要 `--write` 的情形**：只读评审、只改 `docs/` 下与 `derived_from` 无关的文件、只改测试断言而不跑。此时 `--check` 应保持 exit 0——它变红就是信号，不是手续。

### 4.2 什么时候必须"同批提交"

1. `--write` 与**引发重算的那次证据变更**必须在同一个 commit。否则该 commit 的树必然 `--check` 红，下一个人会误判成"证据被手改"。
2. 只提交 `SHA256SUMS` 而把证据留在工作树，等于**给未入库的内容盖章**，比不提交更危险（下一个 clone 复算必然失败）。
3. `--write` 同时改 `SHA256SUMS` 与 `evidence_manifest.json`（`evidence_meta.py:333-349`），两者必须一起进同一个 commit；只提交一个会让 manifest 与台账指向不同时点。
4. 若 drifts 来自**不该发生的重写**（例如本轮只改文档），不要用 `--write` 掩盖：先按 §5-FM-1 判定，再用 `git stash`/`git restore` 隔离，并在波次记录 §4 写清处置。

### 4.3 波次收口检查清单（可直接复制）

```bash
cd experiments/phase0
uv run python evidence_meta.py --check            # 期望 exit 0
uv run python verdict_lint.py --check             # 期望 exit 0，new=0
cd ../..
git status --porcelain -- docs/poc/evidence       # 期望：只有本轮白名单文件
git diff --stat -- docs/poc/evidence              # 期望：空（或仅本轮声明的证据变更）
```

判读表：

| 现象 | 含义 | 处置 |
| --- | --- | --- |
| 两个 exit 0 + diff 空 | 证据与台账同步 | 可提交 |
| exit 1，只报 §2.1 那 6 份的 `LEDGER_MISMATCH` | 跑过回归没重算 | 复核 diff 仅为时间戳/PID/timing 后 `--write` 并同批提交 |
| exit 1，报 `UNREGISTERED:<name>` | 证据目录出现未登记 JSON | 是证据 → 加进 `SPECS`；是工具产物 → 加进 `NON_EVIDENCE_JSON` |
| exit 1，报 `DERIVED_MISSING` / `DERIVED_NO_CITATION` | 报告被移动或未引用该任务号 | 修 `SPECS` 路径或在报告里补任务号 |
| `verdict_lint` exit 1 且 `new>0` | 新增口径违规 | 改文案（写清"限域"），而不是改基线 |
| `git status` 出现未声明的证据变更 | 并发写入或他人重跑 | 停下来问清来源，不要直接 `--write` |

## 5. 三条失败模式与处置

### FM-1 证据手改未重算（`LEDGER_MISMATCH`，且不是重跑引起）

- **判据**：`git diff -- docs/poc/evidence/<file>` 的差异**落在结论字段**（如 `breaches`、`not_run`、`citation_accuracy`、`t05_verdict`）而不是 `generated_at`/PID/timing。
- **处置**：不 `--write`。回到产出该证据的探针重跑一次；若无法重跑，把证据标为 `PARTIAL`/`NOT_RUN` 并在波次记录写明"证据已不可复算"。`--write` 会把"人改的内容"变成"合法的内容"——这是本机制最危险的误用。
- **变体（真实发生）**：把不属于证据的 JSON 放进证据目录也会报红。本轮首次 `--check` 报 `UNREGISTERED:verdict_lint_baseline.json`，说明 `verdict_lint_baseline.json` 当时未被 `evidence_meta.py` 的忽略集覆盖；该缺口随后由 `NON_EVIDENCE_JSON`（`:47`）修掉。也就是说：**新增任何 JSON 产物，都要同时决定它是"证据"还是"工具产物"。**

### FM-2 台账重算未提交（本文写作时现场复现）

同一个命令在约 1 分钟内给出相反结论：

- 19:22 首次执行 `cd experiments/phase0 && uv run python evidence_meta.py --check`：`evidence=8 with_problem=5 task_from_manifest=2 no_timestamp=1 global_problems=1`，报 5 条 `LEDGER_MISMATCH` + `UNREGISTERED:verdict_lint_baseline.json`，**exit 1**。
- 19:23:31 `experiments/phase0/evidence_meta.py` 被改动（401 行 → 403 行，新增 `NON_EVIDENCE_JSON` 忽略集）；19:23:44 §2.1 的 5 份 JSON 被重写；19:23:52 `SHA256SUMS` 与 `evidence_manifest.json` 被重写（manifest `generated_at=2026-10-29T11:23:52+00:00`）。这些写入**不是本轮任务所为**（本轮被禁止 `--write` 与 git 写操作）。
- 复跑同一条命令：`evidence=8 with_problem=0 ... global_problems=0`，**exit 0**；且 `git diff -- docs/poc/evidence` 为空，即那 5 份已回到 HEAD 字节。
- **教训**：`--check` 的结论只对"静止的工作树"成立。它没有锁、没有快照、也没有记录"校验时用的是哪个版本的校验器"；并发写入会让同一条命令前后矛盾。因此**收口时必须在最后一次写操作之后、提交之前**跑，并把输出连同时间一起贴进波次记录。

### FM-3 引用不存在路径（`DERIVED_MISSING` 与文档级缺失）

T31 评审 §1.4（`:108-111`）查到的 4 条 missing，性质各不相同：

| 路径 | 性质 | 处置方向 |
| --- | --- | --- |
| `docs/poc/evidence/sbom.md` | **声称偏差**：`wave-w10-record.md`:26-27 称 T30 已交付，实际只有 `docreader_sbom.md` | 补文件（W15 任务 A）或改声称；不可两存 |
| `docs/remote-extension-connection.md` | **引用缺失**：`browserskill_protocol_manifest.md`:503 引用但仓库无此文件 | 补文件（W15 任务 B） |
| `tests/phase0/pyproject.toml` | **笔误**：实际配置在 `experiments/phase0/pyproject.toml`（`control-site.md` 复现命令段） | 改引用路径即可 |
| `experiments/phase0/.py` | **伪命中**：正则统计产生的非真实路径（T31 提取脚本） | 修脚本，不该进文档 |

处置原则：`DERIVED_MISSING` 只覆盖"台账登记过的 `derived_from`"，**覆盖不到文档正文里的引用**。T31 §2.3 的量化正说明这一点：引用路径 0 缺失，但 **119 处引用 hash 与 HEAD 不符**（`files_scanned=34 md; hash MATCH=31 DRIFT=119 cited_path_missing=0`）。所以 md 里的 hash 只能当"记录时点"，判据要用"文件存在 + 结构完整 + 门禁命令 exit 0 + 证据 JSON 可复算"。

## 6. 结论：能防什么、防不住什么

**这套机制能防（都有可执行判据）**

1. 证据被手改而台账没跟——`LEDGER_MISMATCH`，且能定位到具体文件。
2. 证据目录里冒出没人认领的 JSON——`UNREGISTERED`。
3. 证据挂错报告（把 T26 的证据挂到 T29 的波次记录）——`TASK_MISMATCH`/`WAVE_MISMATCH`/`DERIVED_NO_CITATION`。
4. 台账少一条（漏登记）——`LEDGER_MISSING`。
5. 判定口径越级（同一行既写 `NOT_RUN` 又写裸 `PASS`）——`verdict_lint` 的 `BARE_PASS_WITH_CAVEAT`。

**这套机制防不住（都仍未解决）**

1. **结论正确性**：工具只验"身份"，不验"这份证据是否支持这个 PASS"。后者仍是门评审的人工职责（`w14-m2-report.md`:86）。门级判定当前仍是 4×PARTIAL + 1×NOT_RUN。
2. **校验器自身的版本漂移**：`SHA256SUMS` 只覆盖 8 份 JSON，不覆盖 `evidence_meta.py`。本轮 19:23:31 那次 patch 没有任何指纹记录——工具改了，判据就变了，历史红/绿无法复现。
3. **并发写入**：无锁、无快照（§5-FM-2 的 1 分钟反例）。
4. **跨文件语义级联**：`security-probes.json` 内嵌 `rag_baseline`，`security-matrix.md` 完全不在台账里；"只重跑一个探针"可能产出半新半旧的证据，且 `.md` 的漂移对 `--check` 不可见。
5. **文档正文里的旧 hash**：119 处 DRIFT（T31 §2.3）不在任何自动检查范围内。
6. **5 条口径债**（`wave-w14-record.md` §3.2：`environment.md`×2、`poc01-owner.md`、`poc02-replanning.md`、`wave-w1-record.md:88`、`wave-w2-record.md:107`）——已登记、未修，`verdict_lint` 以基线形式放行，`new` 才拦。
7. **提交不等于入库**：`--check` 只看工作树，不看远端是否收到（W14 §4 的"待提交"清单即为证）。

**待验证 / 本轮未做**

- 本轮**未跑全量 pytest**（任务禁止），因此 §2.1"6 份会被重写"引用的是 `wave-w10-record.md`:78 与各 fix 记录，不是本轮实跑复现。
- `research-batch.json` 的仓库内写入路径**未找到**（`research_probe.py:51` 仅有常量、无写盘调用）。这意味着 6 份中最"无言"的那一份可能无法从仓库内复现——需要一次定向确认，属未结项。
- `verdict_lint` 与 `evidence_meta` 都**没有接入 CI**，靠人在收口时手工跑（§4.3）。

**复核命令（本文所有结论的唯一复算入口）**

```bash
cd experiments/phase0 && uv run python evidence_meta.py --check
```
