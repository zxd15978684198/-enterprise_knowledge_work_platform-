# W15 波次记录（P1 前置：D5 缺口补齐批）

- 分支：`feature/w15-gap-fill`（base `dev`）
- 执行者：Lead + 3 个 teammate（**N=3，真实并行**）
- 日期：2026-10-29

## 1. 任务声明

### 1.1 本波次做什么

补掉 T31 评审登记的 **D5 输入缺口**与**证据可维护性债务**，共三项，互相不读同一份草稿：

| # | 任务 | 依据 | 写权（唯一） |
| --- | --- | --- | --- |
| A | SBOM 与许可清单落地（`sbom.md`） | T31 §2.4 D5、ADR-0007、ADR-0008 | `docs/poc/evidence/sbom.md` |
| B | 浏览器远程扩展连接说明（清 D5 缺口） | T31 §2.4 D5、`poc05-browserskill-live.md`、`docs/poc/evidence/browserskill_protocol_manifest.md` | `docs/remote-extension-connection.md` |
| C | 证据指纹生命周期说明 | T31 §2.3、W14 §3.1、`evidence_meta.py` | `docs/poc/evidence/evidence-integrity.md` |

> **声明块勘误（执行中修正，留痕不洗白）**：本节初稿把 B 的取证依据写成 `fix-w5-t17-record.md` 与 `poc07-report.md`，
> 实际仓库里**两个文件都不存在**（`poc07-report.md` 已被 `Phase0_Full_Gate_Review.md:31` 登记为缺口）。
> 真实 T17 证据是 `docs/poc/evidence/poc05-browserskill-live.md`，协议清单真实路径是
> `docs/poc/evidence/browserskill_protocol_manifest.md`（不是 `experiments/phase0/` 下）。
> 该错误由任务 B 在执行中查出并纠正，未新建同名文件冒充证据。

### 1.2 为什么可以并行（DAG 判定）

- 三者**写权文件互不相交**（三个不同路径，且都在 `docs/` 下，不碰代码与证据 JSON）；
- 三者**无共享资源槽**：不写数据库、不占端口、不改 tracked 证据、不跑全量 pytest；
- 三者**无先后依赖**：A 读依赖清单，B 读 T17 记录，C 读台账工具；三者互不引用对方产出。

因此 `N=3`；`blocked_by = []`。

### 1.3 硬约束（对三个 teammate 一致）

1. **只写 1.1 表里那一个文件**；不新增、不删除、不改动任何其他文件（含 `.gitignore`、状态文件、证据 JSON、`SHA256SUMS`）。
2. **不执行任何 git 写操作**（不 add/commit/push/switch/stash）。由 Lead 统一提交。
3. **事实必须可溯源**：每个数字、版本、命令输出要么来自仓库文件（写 `文件:行`），要么来自自己实际执行的命令（贴命令 + 输出）。**不许推测版本号或许可证**；查不到就写「待验证：<缺什么>」。
4. **禁止**伪造 `PASS`、编造行数、把"应该可以"写成"已验证"。
5. 中文文档；Markdown；不用 emoji；不引用外部网络结论而不给 URL。
6. 完成后只回一条简短消息：文件路径、行数、你实际跑过的命令、以及「哪些是查不到而标了待验证」。

### 1.4 验收标准

| 任务 | 验收 |
| --- | --- |
| A | `sbom.md` 含：生成方式与复现命令、直接/传递依赖计数（来源 `uv.lock`）、许可清单表（≥ 全部直接依赖）、**风险许可项**（Redis RSALv2/SSPLv1、psycopg LGPL-3.0、pgvector）与 ADR-0007/0008 对照、结论（是否阻塞 P1） |
| B | `docs/remote-extension-connection.md` 含：前置条件、安装/注册步骤（Windows 实际路径）、连通性验证命令与预期输出、常见故障、安全与授权边界；**每一步能对回仓库里的 T17 记录**，查不到标待验证 |
| C | `evidence-integrity.md` 含：指纹漂移的成因（全量回归重写证据）、`SHA256SUMS` + manifest 的职责边界、维护协议（何时 `--write`、何时必须同批提交）、`verdict_lint` 与台账的分工、三条失败模式与处置 |

### 1.5 资源槽

无（不占 PG schema / Redis DB / 端口）。这是本波次能真并行的关键前提。

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 17:56 | 按声明块派 A/B/C 给 3 个 teammate | `w1-t09-docreader` / `w2-t03-scope` / `w3-t07-outbox` | **三者全部零产出**：无消息、`list_agents` 保持 inactive、工作树无新文件。其中一条运行时通知 `Background subagent … failed before it finished. It left no closing message.` |
| 19:19 | 判定 | Lead | 团队通道连第 5 次失效（W10/T31 起同型事故），不再重试 teammate 通道 |
| 19:20 | 改走独立后端并行派发 | 3 个 `subagent`（非 teammate） | **三个全部成功**：C `19:26:24` 落盘 123 行、B `19:28:54` 落盘 269 行、A `19:29:00` 落盘 319 行 |
| 19:39 | 环境修复 | Lead | Docker Desktop 引擎整体死亡（`npipe` 不存在）→ 全量回归 15 个 `E`；从 `D:\Docker\Docker Desktop.exe` 拉起，引擎 5s 就绪，`compose --profile core up -d postgres redis` → 两容器 healthy，`PostgreSQL 17.11 (Debian)` |

### 2.1 三个执行中发现的事实（不改写，留痕）

1. **声明块 §1.1 有两处路径错**（任务 B 查出）：`fix-w5-t17-record.md`、`poc07-report.md` 在仓库中**都不存在**，协议清单真实路径在
   `docs/poc/evidence/` 而不是 `experiments/phase0/`。已在 §1.1 就地勘误并注明「未伪造同名文件」。
2. **`evidence_meta.py` 在第 5 次派发期间被改过**（任务 C 现场记录）：19:22 首次 `--check` 是 exit 1（5 条 `LEDGER_MISMATCH`
   + `UNREGISTERED:verdict_lint_baseline.json`），19:23:31–52 之间校验器与台账被另一写入方更新，复跑 exit 0。
   该变更就是 Lead 修复 W14 全量失败（`NON_EVIDENCE_JSON`）的动作——**两条工作流在同一目录并发的实证**，
   任务 C 把它写成「校验器自身无指纹 + 无锁无快照」，属正确指认。
3. **仓库根 `uv.lock`（8 行空 lock，未跟踪）由 Lead 在 19:31 删除**：来源是某次在仓库根执行 `uv` 的副产物（无 `pyproject.toml`），
   不是任何波次产出；删前确认 `git ls-files` 未跟踪、`git check-ignore` 未忽略。任务 A 的 §8-V6 已登记该文件去留问题，结论：**不入库**。

## 3. 门禁

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 引用一致性 | 人工核对三份新文档内的 `文件:行` 指针（B 主动纠正 2 处、A/C 各标 V 项） | 无伪造路径；A 的 V1–V10、B 的 §7 12 项、C 的 §5 三条均显式标「待验证」 |
| 事实口径 | 三份文档是否把「限域」写成「通过」 | `verdict_lint.py --check` 见下 |
| 判定口径 | `uv run python verdict_lint.py --check`（`experiments/phase0`） | 见 §3.1 |
| 证据台账 | `uv run python evidence_meta.py --check` | 见 §3.1 |
| 秘密扫描 | 正则扫 3 份新文档（password/dsn/sk-/ghp_/AKIA/PRIVATE KEY） | 见 §3.1 |
| ruff | §6 E1 的代码修复后：7 个改动文件 | **All checks passed**（详见 §3.1） |
| 残留 | `git status --porcelain` | 仅白名单文件；`experiments/phase0/tmp*`（全量回归产生）已清 |

### 3.1 实跑数字

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 引用一致性 | 三份新文档的全部反引号路径做「多重根」解析（仓库根 / `docs/` / `docs/poc/evidence/` / `experiments/phase0/` / `tests/phase0/`） | `sbom.md` 8 条引用 → 2 条不在仓库（`licenses/LICENSE.txt`、`psycopg-3.3.6.dist-info/licenses/LICENSE.txt`，均为 **venv 内文件路径**，正文已写明是 site-packages 内实读）；`remote-extension-connection.md` 9 条 → 3 条属**勘误行/已登记缺口**（`experiments/phase0/browserskill_protocol_manifest.md` 是「路径不对」那一行本身）；`evidence-integrity.md` 43 条 → 3 条为模板占位符 `wave-wN-record.md`、已登记缺口 `tests/phase0/pyproject.toml`、已标注的伪路径 `experiments/phase0/.py`。**无新增伪路径** |
| 判定口径 | `uv run python verdict_lint.py --check` | `docs_scanned=55 violations=8 new=0 by_rule[BARE_PASS_WITH_CAVEAT=8]` → **exit 0**（8 条全在基线，W15 三份新文档 0 条新增） |
| 证据台账 | `uv run python evidence_meta.py --check` | `evidence=8 with_problem=0 task_from_manifest=2 no_timestamp=1 global_problems=0` → **exit 0** |
| 秘密扫描 | 正则扫 5 份新/改文档（password/dsn/sk-/ghp_/AKIA/PRIVATE KEY） | 唯一命中是本记录自身的「扫描规则」行（自指），**0 实命中** |
| ruff | 本波次原为纯文档；§6 E1 扩展后：`ruff check` 7 个改动文件 | **All checks passed** |
| 全量回归 | `pytest tests/phase0 -q -p no:cacheprovider` | **1037 passed / 8 skipped / 576.82s**（W14 基线 1036 + W15 新增 1 条 `evidence_dir` 相关断言计入既有文件）；跑完 `git status --porcelain -- docs/poc/evidence` **无 tracked 改动** |
| 残留 | `git status --porcelain` | 仅 §4 白名单；`experiments/phase0/tmp*`（全量回归产生）已清；仓库根无 `uv.lock` |

## 4. 提交决策

| 项 | 内容 |
| --- | --- |
| 待提交（新增） | `docs/poc/evidence/sbom.md`、`docs/remote-extension-connection.md`、`docs/poc/evidence/evidence-integrity.md`、`docs/poc/evidence/wave-w15-record.md` |
| 待提交（修改） | `docs/IMPLEMENTATION_STATUS.md`（W14+W15 两轮段）、`docs/poc/02_Phase0_PoC_Plan.md`（若 W14 未提交则同批） |
| 待提交（§6 E1 代码修复） | `experiments/phase0/evidence_dir.py`（新增）、`experiments/phase0/rag_probe.py`、`research_probe.py`、`retrieval_probe.py`、`security_probe.py`、`tests/phase0/conftest.py`、`tests/phase0/test_checkpoint_owner.py` |
| 不提交 | 全量回归重写的 tracked 证据（本波次改用临时证据目录后**已不再发生**，历史 stash 保留不动）；根 `uv.lock`（已删）；`.idea/`、`CLAUDE.md`、`docs/teacher/`、`docs/plans/README.md`（开工前既有未跟踪，非本波次） |
| 判定影响 | **不改变任何门级判定**：补齐的是输入缺口与证据可维护性说明，不是能力补齐。G-P0 仍 4×PARTIAL + 1×NOT_RUN |
| 提交权 | Lead（用户批准后执行） |
| 提交结果 | 与 W14 同分支同批：代码 `1f31011`、文档 `c140144`、回填 `b34f57a`；`feature/w14-m1-m2-m3` 远端 = `b34f57a` |
| 合入结果 | 用户裁决「合入」→ `--no-ff` 合并 `2c00b84`，`dev` 从 `1bcad95` 前进三笔并推送（`1bcad95..2c00b84`） |

## 5. 遗留

1. **A 的 4 条决策点**（Redis 许可边界、psycopg 披露材料三缺三、pgvector 镜像汇总许可、发行态口径）需要用户/法务裁决，属 ADR-0007/0008/0010 的执行项，不是文档能闭合的。
2. **B 的 12 项待验证**里有两项影响运维：daemon 端口 52800 是否固定、`doctor` 在 `status="na"` 时退出码为 1（写 CI 关卡会假红）。
3. **B 指出 `poc07-report.md` 已被登记为缺口**（`Phase0_Full_Gate_Review.md:31`）：本波次没有补它，因为它是 PoC-07 的验收报告而非 D5 缺口；需要单独波次。
4. **C 指出 `research-batch.json` 找不到写盘调用**（`research_probe.py:51` 仅常量）：这份证据可能无法从仓库复现，需定向确认。
5. **派发通道**：teammate 通道 5 次失效、`subagent` 通道 3/3 成功——后续并行默认用 `subagent`，并在声明块直接写清用的是哪条通道。


## 6. 追加登记

### E1 写权白名单扩展（文档批之外的代码修复）

- **事实**：W14 全量回归四次未绿，第 3/4 次定位到「探针写证据 + 台账用例校验证据」在同一轮内互斥
  （详见 `wave-w14-record.md` §3.4）。不修它就没有可信的门禁数字，因此本波次在文档任务之外改了代码。
- **改动**（6 个文件，均最小侵入）：新增 `experiments/phase0/evidence_dir.py`；
  `rag_probe.py`/`research_probe.py`/`retrieval_probe.py`/`security_probe.py` 各改用
  `evidence_dir()`（各 +2 行，无逻辑改动）；`tests/phase0/conftest.py` 注入临时证据目录与清理钩子；
  `tests/phase0/test_checkpoint_owner.py` 的 `poc01` 证据路径同样读环境变量。
- **为什么这么修而不是让用例跳过**：跳过只是把矛盾藏起来，台账校验会失去意义；
  而让探针永远不会写错地方也做不到（CLI 采集必须写回仓库）。把「写哪里」变成参数、测试侧覆盖，
  是唯一同时保住两侧语义的做法。
- **口径**：此次扩展**不改变任何判定**，只让门禁数字可信；已同步更新 `wave-w14-record.md` §3.4
  与本文件 §4 的「不提交」清单（原写「无 `.py` 改动」，现改为列出 6 个文件）。
