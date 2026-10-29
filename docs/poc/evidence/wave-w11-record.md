# W11 波次记录（P0-T32 全量 Phase 0 / P1 前置证据评审）

```yaml
wave: W11
branch: feature/w11-t32
base: dev @ 65a2f89
parallel_tasks: [P0-T32]
parallelism_n: 1
planned_tasks: [P0-T32]
blocked_tasks: []
write_scope:
  - docs/poc/evidence/Phase0_Full_Gate_Review.md
  - docs/poc/evidence/wave-w11-record.md
  - docs/IMPLEMENTATION_STATUS.md
resource_slots:
  pg_schema: p0_t32      # 本任务为纯文档评审，预期不建 schema；若需查询则用此前缀
  redis_db: 17           # 本波次 N=1，预留取 17
  ports: [5630]
gate:
  - 引用一致性：本文档与评审文档中的全部仓库内路径必须存在（除已登记缺口）
  - 全量回归：pytest -c experiments/phase0/pyproject.toml tests/phase0（本波次零 .py 改动，作为「未破坏」证据）
  - ruff：本波次零 .py 改动 → N/A；存量必须为 0（W10 已清 4 条 I001）
  - 秘密扫描：password/dsn/sk-/ghp_/AKIA/PRIVATE KEY 零命中
  - 注入测试：把 BrowserSkill mock 结果声明为 real → 证据检查必须 REJECT（T32 强制注入项）
  - 越界检查：git status 仅写权内文件
verdict_fields: [判定, 理由, 证据, 残余风险, 下一步]
```

## 1. 任务与依赖

| 项 | 值 |
| --- | --- |
| 任务 | P0-T32 全量 Phase 0 / P1 前置证据评审（文档） |
| Backlog | `docs/poc/03_Phase0_Task_Backlog.md:541-556` |
| 依赖 | P0-T28（W9）、P0-T18（`fix/w5-t18`）、P0-T24（W6）、P0-T31（W10）——**全部已 PASS（限域）** |
| 输入 | T31 结论（`P0_Core_Gate_Review.md` 424 行）、九项 PoC 证据、安全矩阵、许可清单、10 份波次记录、`05_Phase0_Acceptance.md` G-FULL 判据 |
| 交付 | `docs/poc/evidence/Phase0_Full_Gate_Review.md`、`docs/IMPLEMENTATION_STATUS.md`；**仅列 ADR 状态建议，不改 ADR** |
| 判定口径 | 九项 PoC 各给**唯一**五态结果；G-FULL 未满足不得标完成；受限准入必须列禁用能力/接受者/残余风险/补测任务 |

## 2. 派发记录

| 时间 | 动作 | 对象 | 结果 |
| --- | --- | --- | --- |
| 2026-10-29 15:5x | 建分支 | — | `feature/w11-t32` @ `65a2f89`（dev 顶点） |
| 2026-10-29 15:5x | 执行 | **Lead 本人** | 本波次为纯文档汇总门禁，且 T31 暴露子 agent 在长文档连续写作上两次失败（首派静默、二派中途退出），故不再派发，由 Lead 执行并在 §3 保留完整命令输出 |

## 3. 门禁

| 项 | 命令/方法 | 结果 |
| --- | --- | --- |
| 引用一致性 | 抽取本文档与 `Phase0_Full_Gate_Review.md` 全部仓库内路径引用并逐个 `os.path.exists` | **cited=34 / exists=28 / known_gap=6 / missing=0**（6 条为已登记缺口：`sbom.md`、`docs/remote-extension-connection.md`、`tests/phase0/pyproject.toml`、`poc04/06/07-report.md`） |
| 全量回归 | `pytest -c experiments/phase0/pyproject.toml tests/phase0 -q -o addopts= -p no:cacheprovider` | **1006 passed / 0 failed / 0 errors / 8 skipped in 838.95s (0:13:58)**（本波次零 `.py` 改动，作为「未破坏」证据） |
| ruff | 本波次无 `.py` 改动 | **N/A**（存量 `I001` 已在 W10 清零，`ruff check experiments/phase0 tests/phase0` = `All checks passed`） |
| 口径 linter | 规则：行内含 `NOT_RUN` 者不得标裸 `**PASS**`，扫 11 份 `wave-w*-record.md` + 本波新增文档 | **violations=2** —— `wave-w1-record.md:88`（T02）、`wave-w2-record.md:107`（P0-T03），均为历史记录且 T31 已登记为 D4；**本轮新增文档 0 违规** |
| 秘密扫描 | `password=`/`dsn=`/`postgres://`/`sk-`/`ghp_`/`AKIA`/`BEGIN PRIVATE KEY` 扫全部波次记录 + 本波文档 | 本波文档 **0 命中**；全库 3 条命中均为「描述扫描规则」的文字与 T29 合成 canary，非凭据 |
| 注入测试 | 声明集 4 条（2 真 2 伪）+ 6 条规则校验器 | `TOTAL claims=4 rejected=2 expect_mismatch=0`；exit 1（门禁生效形态）。伪造项被 `R3 UNSCOPED_PASS_WITH_NOT_RUN`、`R4 MARKER_NOT_FOUND` 拦下 |
| 越界检查 | `git status --porcelain` | 仅写权内 3 个文件 + 既有未跟踪 `docs/plans/README.md`；一次性中间物 `.t32_tmp/` 已删 |

**偏差记录**

- E1 首版引用检查把裸文件名（`docreader_sbom.md` 等）按仓库根解析 → 误报 29 条；改为「含 `/` 按仓库根、否则按 `docs/poc/evidence/`」，并顺手把 3 处裸引用改为全路径（`docs/poc/05_Phase0_Acceptance.md`、`docs/poc/07_Contract_Closure.md`、`wave-w1…w10` 展开），复检 `missing=0`。
- E2 口径 linter 的 2 条基线与本轮无关（W1/W2 记录），按「不改写历史波次记录」原则只复述不修改。
- E3 `.t32_tmp/`（`claims.json` + `claim_check.py`）为一次性中间物；`%TEMP%` 下 Python 无写权限（T31 已实测 `PermissionError`），故置于仓库根并在收尾删除；复现逻辑固化在评审文档 §8.1/§8.3。

## 4. 提交决策

- **待提交（3 个文件）**：`docs/poc/evidence/Phase0_Full_Gate_Review.md`（新）、`docs/poc/evidence/wave-w11-record.md`（本文件）、`docs/IMPLEMENTATION_STATUS.md`（改）。
- **不提交**：`.t32_tmp/`（已删）、全量回归重写的 tracked 证据（若被改写则 `git stash` 隔离，同 W10 处理）。
- **核心结论**：**G-FULL 不成立 —— 九项 PoC 全部 PARTIAL，0 项无保留 PASS**；Phase 0 不得标记完整完成，可提出受限范围准入（见评审文档 §7）。
- **用户裁决**：选 1 —— 提交 + 推送 + `--no-ff` 合入 `dev`。

### 4.1 落地结果（回填）

| 环节 | 结果 |
| --- | --- |
| 提交 | `eb390db`（`docs(poc): T32 全量出口评审与 W11 收口`，3 files changed, 397 insertions(+), 1 deletion(-)）；`git diff --cached --check` exit 0 |
| 推送分支 | `feature/w11-t32` → `origin`，`[new branch]`，本地/远端同为 `eb390db8b7c0aece711caf6c00eef3ef0af6eb82` |
| 合入 dev | `git merge --no-ff feature/w11-t32`（ort）→ `50b5cfd`，`merge_exit=0`，无冲突 |
| 推送 dev | `65a2f89..50b5cfd  dev -> dev`，`pushdev_exit=0`；`git ls-remote` 显示 `dev = 50b5cfda85e39deef41503775214f4c2154681b1`，与本地逐位一致 |
| 全量回归副作用 | 重写的 6 份 tracked 证据已 `git stash push -m w11-fullsuite-regen-6-evidence`（`stash@{0}`）；`security-probes.json` 复算 sha256 = `84c09916…616f6`，与评审文档 §5.1 引用一致 |
