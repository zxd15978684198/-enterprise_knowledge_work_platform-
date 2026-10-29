# W14 波次记录（机制批 M1/M2/M3）

## 1. 声明块（开工前落盘）

| 项 | 内容 |
| --- | --- |
| 波次 | W14 = 机制批（T32 评审 §6.3 的三条机制建议） |
| 分支 | `feature/w14-m1-m2-m3`，base `dev@1bcad95` |
| 并行度 `N` | **3** |
| 判定依据 | M1（计划状态列维护机制，只改 `02_Phase0_PoC_Plan.md`）、M2（证据元数据字段，只改 `docs/poc/evidence/*.json` + 新增工具）、M3（判定口径 linter 门禁化，只新增脚本与测试）。三者**无共享文件**；三者都**不触碰** PostgreSQL schema、Redis DB、监听端口，故也不存在资源槽冲突。 |
| 为什么不是 1 | 三条机制彼此正交（一处是文档维护责任、一处是证据字段规范、一处是静态检查工具），合并成单任务只会拉长串行时间且无法交叉验证——M3 的 linter 恰好可以对 M1 改完的状态列与 M2 改完的证据表做一次独立复核。 |
| 资源槽 | 无（无 DB / 无 Redis / 无端口）。**测试并发约束**：各成员只跑自己新增的定向 pytest；全量回归由 Lead 在集成后单跑一次（`tests/phase0` 多个用例会建删 PG schema，并发全量会互相打死）。 |
| 临时文件 | 一律写 `%TEMP%`，禁止在仓库根创建 `tmp*/`、`.t31_tmp/` 之类目录（T31 教训）。 |
| 提交权 | **只在 Lead**。成员完成即停，不 `git add/commit/push`。 |

### 1.1 写权白名单（互不重叠）

| 成员 | 任务 | 允许写 |
| --- | --- | --- |
| `w1-t09-docreader` → M1 | 计划状态列维护机制 | `docs/poc/02_Phase0_PoC_Plan.md`、`docs/poc/evidence/w14-m1-report.md` |
| `w2-t03-scope` → M2 | 证据元数据字段 | `docs/poc/evidence/*.json`（8 份既有证据）、`docs/poc/evidence/evidence-metadata.md`、`experiments/phase0/evidence_meta.py`、`tests/phase0/test_evidence_meta.py`、`docs/poc/evidence/w14-m2-report.md` |
| `w3-t07-outbox` → M3 | 判定口径 linter 门禁化 | `experiments/phase0/verdict_lint.py`、`tests/phase0/test_verdict_lint.py`、`docs/poc/evidence/verdict-lint.md`、`docs/poc/evidence/w14-m3-report.md`、`experiments/phase0/pyproject.toml` |
| Lead | 集成与门禁 | 其余全部（含 `wave-w14-record.md`、`IMPLEMENTATION_STATUS.md`、历史波次记录修补） |

### 1.2 验收标准（每条都要可复核）

- **M1**：① 计划文件里出现「状态列唯一维护人 + 更新时机」的显式条款；② 六项 PoC（PoC-01…PoC-09 中已有结论者）的状态列从 `NOT_STARTED` 更新为真实值（`PASS（限域）`/`PARTIAL`），并注明依据的波次记录与更新时点；③ 文件顶部保留「历史 NOT_STARTED 为 2026-10-27 状态快照」的口径说明，不得抹掉历史。
- **M2**：① 规范文档写清 `computed_at`/`reason`/`derived_from` 三字段语义与「hash 只代表记录时点」；② `evidence_meta.py` 能对既有证据 JSON 幂等注入三字段（重复执行不产生 diff）；③ 8 份证据 JSON 全部带上三字段；④ 定向测试覆盖幂等性与字段语义。
- **M3**：① `verdict_lint.py` 实现规则：同一行内出现 `NOT_RUN` 时不得标无保留 `**PASS**`；`PASS` 行须能定位到证据路径；输出违规清单与退出码（有违规 = 非 0）；② 对全仓 `docs/poc/evidence/*.md` 跑出基线违规清单（只报不改）；③ 测试覆盖「合规放行 / 违规拦截」两侧；④ 规则说明写入 `verdict-lint.md` 并给出接入门禁的命令行。

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 17:20 | 按声明块派 M1 | `w1-t09-docreader` | **零产出**：无消息、工作树无新文件、`list_agents` 保持 inactive |
| 17:20 | 按声明块派 M2 | `w2-t03-scope` | **零产出**，同上 |
| 17:20 | 按声明块派 M3 | `w3-t07-outbox` | **零产出**；另收到运行时通知 `Background subagent … failed before it finished. It left no closing message.` |
| 17:25 | 决策 | Lead | **放弃等待，改由 Lead 串行执行 M1 → M2 → M3**（三次派发零产出，继续等待只会烧时间） |
| 17:26–17:40 | 实际执行 | Lead | M1 文档机制、M2 台账工具 + 测试、M3 linter + 测试，全部由 Lead 完成 |

**偏差说明（必须写清，否则声明块就是假的）**：声明块的 `N=3` 是**任务正交性**判定（结论仍成立：三者写权无交集、无共享资源槽），
但**实际执行并行度为 1**。原因不是任务耦合，而是派发通道在本会话内连续失效——这是 W10/T31 同型事故的**第 4 次**。
本波次不深挖根因（属工具/运行时问题，非项目代码问题），只把它作为遗留项登记在 §5。

## 3. 门禁

| 项 | 命令 | 结果 |
| --- | --- | --- |
| ruff check | `uv run ruff check evidence_meta.py verdict_lint.py ../../tests/phase0/test_evidence_meta.py ../../tests/phase0/test_verdict_lint.py` | **All checks passed** |
| ruff format | `uv run ruff format --check`（同上 4 文件） | **4 files already formatted** |
| M2 定向测试 | `uv run python -m pytest ../../tests/phase0/test_evidence_meta.py -q` | **13 passed in 1.62s** |
| M3 定向测试 | `uv run python -m pytest ../../tests/phase0/test_verdict_lint.py -q` | **13 passed in 0.14s** |
| M2 台账校验 | `uv run python evidence_meta.py --check` | `evidence=8 with_problem=0 task_from_manifest=2 no_timestamp=1 global_problems=0`，**exit 0** |
| M3 口径检查 | `uv run python verdict_lint.py --check` | `docs_scanned=50 violations=9 new=0`，**exit 0**（9 条均为基线内存量，见 §3.2） |
| 秘密扫描 | 正则扫新增文件（password/dsn/sk-/ghp_/AKIA/PRIVATE KEY） | **0 命中** |
| 残留检查 | `git status --porcelain` | 仅 §4 白名单内文件；仓库根无 `tmp*/`、`.venv/`（M2 首跑误建的 `E:\project_month\pyknora\.venv` 已删除，0.2 MB） |

### 3.1 与声明块验收标准的偏差（三条，均已落地为更强或更窄的口径）

| # | 原验收标准 | 实际做法 | 理由 |
| --- | --- | --- | --- |
| E1 | M2：对 8 份证据 JSON **幂等注入** `computed_at`/`reason`/`derived_from` 三字段 | **不注入**，改为**外部台账**（`SHA256SUMS` + `evidence_manifest.json`）+ 校验器 | 改写 8 份历史证据会改变其内容指纹，而 `poc0*-report.md`、波次记录里引用的是改写前的结论；且 T31 评审 §2.3 已证「逐字节复算不可行」。外部台账既不碰历史，又能机检归属与手改 |
| E2 | M1：更新「六项 PoC」状态列 | **九行全量更新** | §3 总览表本身是 9 行；只改 6 行会让同一张表内一半真实一半过期，反而制造 D1 同类问题 |
| E3 | M3：规则「同一行内出现 `NOT_RUN` 时不得标无保留 `**PASS**`」 | 收敛为「**同行**裸 `**PASS**` 且同行含 `NOT_RUN`/`PARTIAL`/`BLOCKED`/`NOT_STARTED`」，并排除 fenced 代码块、行内代码引用、否定语境 | 文档级规则会把 30+ 份合法文档判违规（一份文档同时记录「T05R `PASS（限域）`」与别处 `NOT_RUN`），信号价值归零；行级才能执行 |

### 3.2 基线内 9 条存量违规（只报不改）

| 文件 | 规则 | 条数 | 性质 |
| --- | --- | --- | --- |
| `environment.md` | `BARE_PASS_WITH_CAVEAT` | 2 | 口径债：状态行写裸 `PASS` 而同行记 `NOT_RUN`（D4 同类） |
| `poc01-owner.md` | `BARE_PASS_WITH_CAVEAT` | 1 | 口径债：首列 `PARTIAL（单活）`、备注列裸 `PASS` |
| `poc02-replanning.md` | `BARE_PASS_WITH_CAVEAT` | 1 | 口径债：`PASS` 而括注「1 项子契约 PARTIAL」 |
| `wave-w1-record.md:88` | `BARE_PASS_WITH_CAVEAT` | 1 | **D4 原始两处之一**（T31 评审已登记 T02） |
| `wave-w2-record.md:107` | `BARE_PASS_WITH_CAVEAT` | 1 | **D4 原始两处之一**（P0-T03） |
| `wave-w3-record.md:59` | `BARE_PASS_WITH_CAVEAT` | 1 | 噪声：同一行并列 T03/T04 `PASS` 与 T09 `PARTIAL`，主体不同 |
| `wave-w6-record.md:37` | `BARE_PASS_WITH_CAVEAT` | 1 | 噪声：同一行并列 T12 `PASS` 与 T23/T20 `PASS（限域）`、T18 移出 |
| `wave-w3-record.md` 以外 | — | 1 | 规则粒度已知限制，见下 |

**已知规则粒度限制**（写进基线而非掩盖）：行级规则无法区分「同一行并列多个任务」与「同一任务被写成裸 PASS」，
因此 `wave-w3:59`、`wave-w6:37` 属可接受的误报；若要消除，需要解析表格列语义（P2 债务，不在本波次）。

**这 5 条口径债（D4 两处 + environment 两处 + poc01/poc02 各一处）本轮不修**：修它们要改历史证据文件，
会与「历史不可改写」和已发布引用的指纹冲突；正确做法是文档债务批次统一改并同步重算台账。已在 §5 登记。

### 3.3 全量回归

- 命令：`uv run python -m pytest ../../tests/phase0 -q -p no:cacheprovider`（仓库根 → `experiments/phase0` 项目）
- **第 1 次跑（19:0x）**：`1 failed, 1036 passed, 8 skipped in 640.00s` —— 失败项是**本波次自己的新测试**：

  ```text
  FAILED tests/phase0/test_evidence_meta.py::test_repo_evidence_ledger_is_clean
  AssertionError: assert ['UNREGISTERED:verdict_lint_baseline.json'] == []
  ```

  根因：M2 的台账白名单只排除 `evidence_manifest.json`，而 M3 新增的 `verdict_lint_baseline.json` 落在同一目录，
  被判定为「未登记证据」。这是**两个机制各自的边界没对齐**，属于真实缺陷而不是测试噪声——
  它恰好证明 M2 的检查有效（新文件绕不过去）。
- 修复：`evidence_meta.py` 引入 `NON_EVIDENCE_JSON`（`{evidence_manifest.json, verdict_lint_baseline.json}`），
  把「工具自身产物」与「被审计证据」分开，并加注释说明新增此类产物必须同步登记。
- 修复后定向：`26 passed`（`test_evidence_meta.py` + `test_verdict_lint.py`），`evidence_meta.py --check` → `global_problems=0`。
- **第 2 次全量**：见 §3.4 登记（修复后重跑）。

### 3.4 全量回归（共 5 次，前 4 次失败原因全部留痕）

| 次序 | 结果 | 失败原因 | 归属 |
| --- | --- | --- | --- |
| 1 | `1 failed, 1036 passed, 8 skipped in 640.00s` | `test_repo_evidence_ledger_is_clean`：`UNREGISTERED:verdict_lint_baseline.json` | **本波次真实缺陷**（M2/M3 边界未对齐）→ 已修（`NON_EVIDENCE_JSON`） |
| 2 | 半个 `E` 段、中止 | `pyknora-phase0-postgres-1` 处于 `Exited (255)`：Docker Desktop 引擎整体死亡（`npipe` 不存在） | 环境漂移，非代码 |
| 3 | `22 failed, 1015 passed, 8 skipped in 540.14s` | ①`test_repo_evidence_ledger_is_clean`：上一次运行把 6 份 tracked 证据改写后未复原；②`test_research_e2e.py` 21 例连带失败（单独跑该文件 20 passed、与 `test_approval_effects`/`test_security_gate` 组合均通过） | **套件自我污染的连带效应**，非产品缺陷；修复见下 |
| 4 | `1 failed, 1036 passed, 8 skipped in 456.53s` | 仍是 `test_repo_evidence_ledger_is_clean`：干净起跑，但**同一轮内**前面的用例先改写证据、后面的台账用例再校验 | 同上，确认为**结构性**问题 |
| 5 | **`1037 passed, 8 skipped in 576.82s`**，跑完 `git status --porcelain -- docs/poc/evidence` **无 tracked 改动** | — | **通过** |

**第 3–5 次的根因与修复（W15 内完成，代码改动同时属于 W15 白名单扩展）**：

探针（T05/T25/T26/T28/T29）把证据写回 `docs/poc/evidence/*` 的 tracked 路径，
而 `test_repo_evidence_ledger_is_clean` 是**同一轮里**按 `SHA256SUMS` 校验这些文件的台账，
于是「写证据的用例」与「校验证据的用例」在同一进程内必然互相打脸——一次全量回归最多只能证明
「跑之前干净」，不能证明「跑完仍干净」。修复方式：

1. 新增 `experiments/phase0/evidence_dir.py`：证据目录改为 `evidence_dir()` 解析，
   支持 `PYKNORA_EVIDENCE_DIR` 覆盖，默认仍是 `docs/poc/evidence`（CLI 采集行为不变）；
2. 四个写证据的探针（`rag_probe`/`research_probe`/`retrieval_probe`/`security_probe`）改用它，
   各 +2 行、无逻辑改动；
3. `tests/phase0/conftest.py` 在**收集测试模块之前**把 `PYKNORA_EVIDENCE_DIR` 指到
   `tempfile.mkdtemp()`，并预置一份 tracked 证据副本；`pytest_sessionfinish` 删临时目录；
4. `tests/phase0/test_checkpoint_owner.py` 的 `poc01_owner_process_level.json` 写入同样读该变量。

修复后：定向 `25 passed`（`test_evidence_meta.py` + `test_checkpoint_owner.py`）、全量第 5 次
**1037 passed / 8 skipped / 576.82s**，且跑完仓库证据目录零改动——「跑完仍干净」现在才真的被证明。
**这次修复不属于 M1/M2/M3 任何一条**，是执行 M2 门禁时暴露的测试侧债务，按「发现即修、修完补声明」
处理（见 `wave-w15-record.md` §6 E1）。

## 4. 提交决策

| 项 | 内容 |
| --- | --- |
| 待提交（新增） | `experiments/phase0/evidence_meta.py`、`experiments/phase0/verdict_lint.py`、`tests/phase0/test_evidence_meta.py`、`tests/phase0/test_verdict_lint.py`、`docs/poc/evidence/SHA256SUMS`、`docs/poc/evidence/evidence_manifest.json`、`docs/poc/evidence/verdict_lint_baseline.json`、`docs/poc/evidence/verdict-lint.md`、`docs/poc/evidence/w14-m1-report.md`、`docs/poc/evidence/w14-m2-report.md`、`docs/poc/evidence/w14-m3-report.md`、`docs/poc/evidence/wave-w14-record.md` |
| 待提交（修改） | `docs/poc/02_Phase0_PoC_Plan.md`（M1）、`docs/IMPLEMENTATION_STATUS.md`（本轮段） |
| 不提交 | 全量回归重写的 6 份 tracked 证据（时间戳/计数抖动，`git stash` 隔离）；无临时目录残留 |
| 判定影响 | **不改变任何 P0 门级判定**：G-P0 仍 4×PARTIAL + 1×NOT_RUN，G-FULL 仍不成立。M1/M2/M3 是**证据与口径的卫生机制**，不是能力补齐 |
| 提交权 | Lead（用户批准后执行） |
| 提交结果 | 代码 `1f31011`（15 文件）、文档 `c140144`（10 文件）；`feature/w14-m1-m2-m3` 已推送，远端与本地逐位一致（`c140144`） |
| 判定复核 | 合入前无冲突（`git merge-tree --write-tree` 与 dev 可干净合并），门禁以第 5 次全量 `1037 passed` 为准 |

## 5. 遗留

1. **派发通道连续失效**（本波次第 4 次）：不影响交付物正确性，但让 `N=3` 退化为串行；后续波次若仍失效，应在声明块直接写 `N=1` 并注明「派发不可用」。
2. **5 条口径债待批量修**（§3.2）：需要同时改 `environment.md`、`poc01-owner.md`、`poc02-replanning.md`、`wave-w1-record.md:88`、`wave-w2-record.md:107`，并重算 `SHA256SUMS`。
3. **规则粒度**：行级无法区分并列多任务（2 处误报），列语义解析属 P2。
4. **`SHA256SUMS` 生命周期**：全量回归后必须 `--write` 重算并随波次提交，否则次日 `--check` 失败；已写入 §3.1 维护条款与 M2 报告 §7。
