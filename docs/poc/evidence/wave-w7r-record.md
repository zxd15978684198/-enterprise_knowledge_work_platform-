# Wave W7R 记录（P0-T05R / P0-T09R 收敛补口）

> 补口波次：不占用 W7 编号（W7 = T25/T27/T30，已声明未启动）。按 `docs/plans/2026-10-28-P0-T05-T09-convergence.md` 执行；门禁最后一步固定为「回填本文件 §4」。

## 1. 波次声明（启动时填写）

```yaml
wave: W7R
branch: fix/w7r-t05-t09        # 单修复分支（用户 2026-10-28 修订：不按任务拆分支）
base: f25e73f                  # dev tip（047b7a8 W7 声明之后合入 T17 修复）；波次内未切分支，产物在工作树内
started_at: 2026-10-28T10:52+08:00
parallel_tasks: [P0-T05R, P0-T09R]
N: 2
serial_tasks: []
blocked_tasks: []
per_task:
  - task: P0-T05R
    agent: t05-single-active
    write_scope:
      - tests/phase0/test_checkpoint_owner.py
      - docs/poc/evidence/poc01_owner_process_level.json
      - docs/adr/0006_Single_Active_Checkpoint_Runner.md
      - docs/poc/evidence/poc01-owner-amendment-01.md
    resource_slot: {pg_schema: p0_t05, redis_db: none}
  - task: P0-T09R
    agent: t09-docreader-sbom
    write_scope:
      - experiments/phase0/audit_docreader_image.ps1
      - docs/poc/evidence/docreader_image_inventory.json
      - docs/poc/evidence/docreader_sbom.md
      - docs/poc/evidence/docreader_manifest_amendment-01.md
    resource_slot: {docker: docreader-v0.8.3-local, redis_db: none}
```

### 1.1 N 的计算依据

| 层 | 结论 |
| --- | --- |
| 依赖层 | T05R 复用 T05 既有证据与 `p0_t05` schema；T09R 只依赖 registry 元数据与 docker 守护进程。两者无共同前置，可并行 |
| 写入权层 | 两组写权不相交；`docs/adr/README.md`、`docs/IMPLEMENTATION_STATUS.md` 归 Lead，收口时写 |
| 资源层 | T05R 复用 `p0_t05` 与 core postgres（5433），不新增 schema/redis；T09R 只占 docker，不占 schema/redis |

→ **N = 2**。

### 1.2 本波次已知约束

| 约束 | 内容 |
| --- | --- |
| 原文不改 | `poc/00–07`、`evidence/` 既有文件、`tdd/`、`adr/0001–0005`、`references/WeKnora` 全部只读；修订一律旁挂 |
| T05 口径 | 只写 `PASS（限域：单活）`；多实例仍 PARTIAL；全文不得出现「支持多实例」；新测试为负面证据用例，只追加 |
| T09 口径 | 镜像 digest 以「registry 侧已闭环 / docker 侧是否落地」双写，不冒充已验证；许可无法判定写 `unknown(原因)` + 阻断 + 替代 |
| 依赖纪律 | 不新增第三方依赖；`uv run --with` 需 Lead 批准；pytest 必须 `-c experiments/phase0/pyproject.toml` 且单文件（全量会挂起） |
| 质量纪律 | 禁 `skip`/`xfail` 冒充；未执行项记 `NOT_RUN`/`BLOCKED`；证据含命令、退出码、真实输出摘要、sha256 |
| Git 纪律 | 波次内禁止 `git add/commit/push/branch/switch`；收口后每任务各自分支提交，格式 `fix:{波次-任务名列表-}修复概述`（用户 2026-10-28 指令） |
| 终端纪律 | `pwsh` 首选；大输出写日志再读 |

## 2. 状态表（执行中更新）

| 任务 | 状态 | 负责人 | 备注 |
| --- | --- | --- | --- |
| P0-T05R | PASS（限域：单活） | t05-single-active | 单文件 pytest `test_checkpoint_owner.py` → **12 passed / exit 0**；ruff exit 0；测试文件 `git diff --numstat` = **+383/−0** 纯追加；进程级反证 JSON：子进程 33180 存活、锁 backend pid 47430 已释放、原生 `put` 仍 `accepted=true`（负面证据），替身路径 `STALE_OWNER_EPOCH` 拒绝 |
| P0-T09R | PASS（限域） | t09-docreader-sbom | 镜像落地 `wechatopenai/weknora-docreader@sha256:9af668ac…5266`（revision `3e22822c1` = `v0.8.3`）；四类 UNKNOWN：digest ✅ 三重闭环、OCR 权重 ✅ 闭合为「不存在」、pip 许可 61/72 判定、apt 许可 744/971 有 `License:`；11 项 pip + 227 项 apt + WebKit/grpc_health_probe/magika 训练数据/基础镜像 digest 仍 unknown，均逐项列阻断与替代 |

## 3. 门禁检查（波次末填写）

- [x] 两任务四项报告交齐（摘要/测试命令与结果/验收逐项/风险与后续）
- [x] 越界写入核对：`git diff --numstat` 仅 `tests/phase0/test_checkpoint_owner.py` +383/−0（纯追加）；`docs/adr/README.md` +1/−1 为 Lead 登记 ADR-0006；其余 7 项为新增文件；无其它改动
- [x] pytest（逐文件，`test_checkpoint_owner.py` 12 passed / exit 0）+ `ruff check experiments/phase0 tests/phase0`（All checks passed! exit 0）+ 秘密扫描（DSN/口令模式，7 份新文件 0 命中）
- [x] `git status` 审阅：W7 未跟踪产物（`retrieval_probe.py`、`candidate_review.py`、`restore_probe/`、`007_candidates.sql` 等）及 `.idea/`、`docs/teacher/`、用户 `tmp*` 均未被本波次触碰
- [x] 已追加 `docs/IMPLEMENTATION_STATUS.md` 小节并修正抬头口径
- [x] `docs/adr/README.md` 已登记 ADR-0006（提议）
- [x] 提交命名已按 `fix:{波次-任务名列表-}修复概述` 准备（见 §4，待用户授权）
- [x] **回填本文件 §4**

> 门禁口径：受影响单文件已实跑；全量 `pytest tests/phase0` 会无输出挂起（W3 起已知），故沿用 W6 的 778 passed 主集合 + 本轮单文件回归，不冒充全量重跑。

## 4. 提交决策与结果

- 用户指令（2026-10-28）：提交信息格式 `fix:{波次-任务名列表-}修复概述`
- 用户修订（2026-10-28 12:37，原话「要不创建一个修复分支吧，我觉得现在这样分支有点不合理」）：不按任务拆分支
- 采用：单修复分支 `fix/w7r-t05-t09`，一条提交 `fix:W7R-T05R-T09R-单活正式化与docreader镜像许可UNKNOWN闭合`
- 实际提交：本轮执行，`git log --oneline -1` 为准
- 推送：未推送（等用户指令）
- 本次提交清单（12 项，全部进同一提交；`.idea/`、`docs/teacher/`、`CLAUDE.md`、`docs/plans/README.md` 及 W7 未跟踪产物均排除）：
  - ` M docs/adr/README.md`（ADR-0006 登记）
  - ` M tests/phase0/test_checkpoint_owner.py`（+383/−0）
  - ` M docs/IMPLEMENTATION_STATUS.md`（抬头 + 本轮小节）
  - `?? docs/adr/0006_Single_Active_Checkpoint_Runner.md`
  - `?? docs/poc/evidence/poc01-owner-amendment-01.md`
  - `?? docs/poc/evidence/poc01_owner_process_level.json`
  - `?? docs/poc/evidence/docreader_manifest_amendment-01.md`
  - `?? docs/poc/evidence/docreader_sbom.md`
  - `?? docs/poc/evidence/docreader_image_inventory.json`
  - `?? experiments/phase0/audit_docreader_image.ps1`
  - `?? docs/plans/2026-10-28-P0-T05-T09-convergence.md`（计划文件，随 W7R 一并提交）
