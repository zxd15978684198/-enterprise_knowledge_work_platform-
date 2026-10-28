# P0-T30 备份恢复、迁移与版本回滚演练（可执行 runbook + 实跑证据）

> Phase 0 波次 W7。本文同时是**证据**（真实命令与真实输出）与**runbook**（照着能复现的步骤、预期、失败处置、清理）。
> 任务卡：`docs/poc/03_Phase0_Task_Backlog.md` L507–522。契约来源：`docs/poc/07_Contract_Closure.md` C-03、C-04；
> `docs/tdd/01_System_Architecture.md` §6.4；`docs/tdd/04_LangGraph_Workflows.md` §7–9。

## 1. 波次与任务声明

```yaml
wave: W7
task: P0-T30
branch: feature/w7-t25-t27-t30
base: 8f82061
workdir: E:\project_month\pyknora
pg_schema: p0_t30     # 资源槽位（本轮只在临时数据库中活动，不在共享库建 p0_t30 对象）
redis_db: 13          # 声明了但本任务不需要 Redis
write_scope:
  - experiments/phase0/restore_probe/        # 新建包（7 个文件）
  - tests/phase0/test_restore_upgrade.py
  - docs/poc/evidence/restore-runbook.md
readonly_dependencies:
  - experiments/phase0/db/migrations/001_..008_*.sql   # 只演练，不改、不新增编号
  - experiments/phase0/scope.py                        # read_dsn()
  - experiments/phase0/checkpoint_probe.py             # T05 的替身 checkpoint 存储
```

**已知约束**（源自任务书与波次记录）：

| 约束 | 内容 |
| --- | --- |
| core 卷 | 不得对 `pgdata`/`redisdata` 动手；本任务全程只在**临时数据库**里活动 |
| 迁移编号 | T30 不新增编号迁移；只对 001–008 做 apply/reapply 演练 |
| T05 前提 | T05 判定为 **PARTIAL / 单活 runner**（原生 `PostgresSaver` 无法 fence）→ 本任务只验证**单实例**备份还原 |
| 多实例/RPO/RTO | 一律 `NOT_RUN`，写进 §8 残余风险 |
| 禁用 | `git add/commit/push`、`docker compose up/down/restart`、`DROP DATABASE` 核心库、`skip`/`xfail` 冒充通过 |

## 2. 结论摘要

| 验收点 | 判定 | 一句话依据 |
| --- | --- | --- |
| ① 备份→还原到一致状态 | **PASS** | 源库与还原库均 63 表 / 83 行 / 25 张非空表，逐表全文摘要与行数**零差异**；7 项业务指针全等 |
| ② 迁移可重复执行 | **PASS** | 8 份迁移（001–008）全部 `applied` 且各写 1 行台账；再执行 8/8 显式失败 `42P06 already exists`，无静默跳过 |
| ③ 版本回滚演练 | **PASS** | 升级后指针确实变化（active[2]=2、publications 3→5）；回滚库与升级前快照**零差异**，可用性四行齐备 |
| ④ 三类故障注入 | **PASS** | 备份中断/还原中断/目标占用 三类都抛**显式错误码**，无一静默成功 |
| ⑤ runbook 可照着执行 | **PASS** | 本文 §7 给出命令、预期输出、失败处置与清理；所有数字来自 §5 的实跑 |
| 多实例切换 / leader election | **NOT_RUN** | T05 是单活前提，横扩未验证（见 §8） |
| RPO / RTO 校准 | **NOT_RUN** | 只测得单机单次时长（§5.6），未定 SLA，不能对外承诺 |
| 对象存储（MinIO）恢复 | **NOT_RUN** | Phase 0 无对象存储，graph state 只存引用，未验证引用失效路径 |

## 3. 方法与一个必须说明的环境事实

**沙箱里没有任何 PostgreSQL 客户端**：宿主 `where pg_dump` 无结果；`pg_dump`/`pg_restore`/`psql`（17.11）只存在于容器
`pyknora-phase0-postgres-1` 内。而**从 Python 子进程访问 Docker 不可行**——本环境 Docker Desktop 只暴露命名管道，
Python 侧一律 `permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`；
从 WSL 用 `cmd.exe /c docker ps` 可以，但从 Python 调 `wsl.exe` 返回 `rc=-1`。

因此本演练采用**合法的等价路径**：让数据库服务器自己执行同一份二进制。

```sql
COPY (SELECT 1) TO PROGRAM
  'pg_dump -h /var/run/postgresql -U pyknora -Fc --no-owner --no-privileges
     -f /tmp/p0_t30_source.dump p0_t30_source
     2> /tmp/p0_t30_source.dump.err; echo $? > /tmp/p0_t30_source.dump.rc; exit 0';
```

- 产物格式、`pg_dump`/`pg_restore` 版本与生产一致（17.11），`--no-owner --no-privileges` 让产物可跨库重放。
- 状态码/错误写文件（`*.rc`、`*.err`），再用 `COPY tmp FROM PROGRAM` 读回；`pg_restore -l` 校验目录项。
- 生产环境用宿主机 `pg_dump -h 127.0.0.1 -p 5433`，本节只是**执行位置不同**，产物与校验方式相同。这一点列入 §8 残余风险。

## 4. 门禁实跑记录

| 步骤 | 命令 | 真实结果 |
| --- | --- | --- |
| 单测 | `uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_restore_upgrade.py` | `21 passed in 37.12s`，exit 0（末次复核复跑 `21 passed in 79.31s`，exit 0；时长随机器负载波动） |
| 单测（任务书原式，带 `-q`） | 同上 + ` -q` | exit 0，`21 dots [100%]`（配置已含 `addopts=-q`，叠成 `-qq` 后汇总行被抑制，非失败） |
| Lint | `experiments/phase0/.venv/Scripts/ruff.exe check experiments/phase0/restore_probe tests/phase0/test_restore_upgrade.py` | `All checks passed!` |
| 格式 | `ruff format experiments/phase0/restore_probe tests/phase0/test_restore_upgrade.py` | 已格式化（`N files left unchanged` / reformatted 后复跑全绿） |
| 完整演练 | `python -m restore_probe`（cwd=`experiments/phase0`） | exit 0，`verdict.consistency/rollback/faults = PASS`，六段计时合计 51.59s |
| 清理复核 | `python -c "...pgctl.list_databases(pgctl.admin_connection())"`（即 §7.5） | `['postgres', 'pyknora']`；`pg_shdepend rows for role p0_app: 0` |

## 5. 验收逐项证据

### 5.1 备份 → 还原的一致状态

备份：`/tmp/p0_t30_source.dump`，**277 607 B**，sha256 `afdff31442ef27dd8d618191bcef904b6f6d33a64779f6ae289947d214eeb2e1`，
**508** 个目录项，耗时 0.516s。还原：508 条目，5.734s。

| 维度 | 备份时刻（源库快照） | 还原库 | 差异 |
| --- | --- | --- | --- |
| base table 数 | 63 | 63 | `missing=[] extra=[]` |
| 总行数 | 83 | 83 | `row_mismatches=[]` |
| 非空表数 | 25 | 25 | — |
| 逐表全文摘要（`to_jsonb(t)::text` 排序后 sha256） | 63/63 取全量摘要 | 63/63 取全量摘要 | `digest_mismatches=[]` |

关键指针（同一快照内读取，`REPEATABLE READ, READ ONLY`）：

| 指针 | 备份时刻 | 还原后 |
| --- | --- | --- |
| `documents.active_version` | `5 docs / active[1]=3 active[2]=0 active[null]=2 sum=3` | 完全相同 |
| `publications` | `3 pubs / committed=3 chunk_sum=21 v2=0` | 完全相同 |
| `p0_t05` checkpoint runs | `2 runs / seq_sum=5 epochs=2` | 完全相同 |
| `p0_t05` checkpoint rows | `5 rows / max_seq=3` | 完全相同 |
| `jobs` | `6 jobs / succeeded=4` | 完全相同 |
| `outbox` | `6 outbox / dispatched=4` | 完全相同 |
| `tool_receipts` | `4 receipts / distinct_intents=4` | 完全相同 |

**checkpoint 一致性**：还原后对两个 run 各做一次 `restore_read`（全新连接 join `phase0_checkpoints` 与 `phase0_runs`），
`restore_checkpoints == source_checkpoints` 且 `restore_read_consistent=True`——即还原后的 checkpoint 最新行与其 owner
（`invocation_id`/`owner_epoch`）是同一对，没有「读到旧 seq / 读到别人的 checkpoint」。

### 5.2 迁移可重复执行（001–008）

| # | 文件 | 字节 | sha256 | schema | apply | reapply |
| --- | --- | --- | --- | --- | --- | --- |
| 001 | `001_scope_core.sql` | 25 653 | `60fd02e893d474a08588107d95020b2fbf7c756ee6916e6773e772d230be614a` | `p0_t03` | applied + 台账行 | failed `42P06` |
| 002 | `002_approvals.sql` | 24 835 | `99cc67ed877035a96c17ce2b80dbc9de7c62f71be3692e6fd79fa65272dac655` | `p0_t06` | applied + 台账行 | failed `42P06` |
| 003 | `003_jobs_outbox.sql` | 18 637 | `5599c8b12551b803b3ab6df7edc39295088a2b80af1a358bc122687968d14d6a` | `p0_t07` | applied + 台账行 | failed `42P06` |
| 004 | `004_version_index.sql` | 24 452 | `d1758097a94f039727f0fc81f7bd2c139a63852c8ba3492af19dd4a54ea798eb` | `p0_t12` | applied + 台账行 | failed `42P06` |
| 005 | `005_budget.sql` | 17 802 | `d752f2e275617af058bbc37414a27bf15ba3c8f66d0f9ffbc5520ed156ca34ee` | `p0_t20` | applied + 台账行 | failed `42P06` |
| 006 | `006_tasks.sql` | 13 648 | `2b58f250f495b3473dc8d8d14c53c920ecd65e96344ad8f66b9988f8fcd6ad15` | `p0_t23` | applied + 台账行 | failed `42P06` |
| 007 | `007_candidates.sql` | 37 374 | `280f77673f3abd86ef50212ac2e7faa113c3232f654f26b4b0586b3f065c4aef` | `p0_t27` | applied + 台账行 | failed `42P06` |
| 008 | `008_publication.sql` | 14 671 | `52d2739f378bf9bcec61e30cd61346addd4c7271caa2f1fe97d558ce36a36f67` | `p0_t13` | applied + 台账行 | failed `42P06` |

- `missing_slots=[]`、`ledger_problems=[]`，台账共 **8 行**，每行 `sha256` 与磁盘字节一致（`assert_ledger_complete` 逐项通过）。
- 重复执行的错误信息形如 `schema "p0_t03" already exists`（SQLSTATE **42P06**）——**显式失败，不静默跳过**。
  实测二次执行连台账行都不写（`ledger_row=false`），所以「重复执行」在证据上是可辨认的。
- 结论：**编号迁移当前只能前进一次**。这不是缺陷，而是「迁移不是幂等 DDL」的事实；升级策略必须靠版本目录 + 显式
  `up/down` 脚本，不能指望「再跑一遍自愈」。该结论已写进 §8。

### 5.3 版本回滚演练（升级 → 回滚）

升级动作（模拟「新版本要改数据形态」）：

```text
1. ALTER TABLE p0_t12.phase0_documents ADD COLUMN state_schema_version integer NOT NULL DEFAULT 1
2. UPDATE documents SET active_version=2, state_schema_version=2  (2 docs)
3. INSERT INTO p0_t13.phase0_publications ... to_version=2        (2 rows)
```

| 指针 | 升级前（=备份时刻） | 升级后 | 回滚后 |
| --- | --- | --- | --- |
| `documents.active_version` | `active[1]=3 active[2]=0 sum=3` | `active[1]=1 active[2]=2 sum=5` | `active[1]=3 active[2]=0 sum=3` |
| `publications` | `3 pubs / committed=3 chunk_sum=21 v2=0` | `5 pubs / committed=5 chunk_sum=49 v2=2` | `3 pubs / committed=3 chunk_sum=21 v2=0` |
| `checkpoints.runs` / `rows` | `seq_sum=5` / `5 rows max_seq=3` | 不变 | 不变 |
| `jobs` / `outbox` / `tool_receipts` | `6/4` `6/4` `4/4` | 不变 | 不变 |

回滚 = 用备份产物还原到 `p0_t30_rollback`（508 条目）。回滚库与升级前快照 **63 表 / 83 行 / 零摘要差异**。

回滚后可用性（不是「表在」而是「能答业务问题」）：

| 检查 | 值 |
| --- | --- |
| active documents | 3 |
| committed publications | 3 |
| succeeded jobs | 4 |
| document → max published version | `doc…057:1; doc…536:1; doc…4d6:1`（全部回到 v1） |

结论：`upgrade_differs_from_backup=True`（升级确实改了东西，不是空转）+ `rollback_diff` 零差异 = **回滚是真的回到旧版本，
而不是「看起来还行」**。数据侧用「schema 回退 + 指针回退」表达，未删除任何核心数据。

### 5.4 三类故障注入（都必须显式失败）

| 故障 | 注入方式 | 期望 | 实测 |
| --- | --- | --- | --- |
| 备份中途断开 | 服务器侧 `statement_timeout=250ms` 取消 `COPY ... TO PROGRAM 'pg_dump'` | `BACKUP_INTERRUPTED` | `BACKUP_INTERRUPTED`，`child process was terminated by signal 2: Interrupt` |
| 还原中途中断 | 同上，取消 `pg_restore` | `RESTORE_INTERRUPTED` | `RESTORE_INTERRUPTED`，目标库留下 10 个 schema 但从未被判定成功 |
| 目标库已存在同名对象 | 同一份产物再导一次到已还原的库 | `RESTORE_TARGET_OCCUPIED` | `RESTORE_TARGET_OCCUPIED`（`pg_restore --exit-on-error` 先报 `schema "p0_t03" already exists`） |

**一个必须记录的细节**：被取消的 dump 产物状态**取决于取消时机**，同一用例两次运行结果不同：

| 运行 | `partial_size` | `pg_restore -l` 条目 | 真正还原 |
| --- | --- | --- | --- |
| A | 328 142 B | 512 | `RESTORE_PROBE_ERROR`（失败） |
| B | 0 B | 无法列出 | `RESTORE_PROBE_ERROR`（失败） |

**教训**：「`pg_restore -l` 能列出目录」**不等于**「产物可用」——A 运行恰好证明了这一点。因此演练里对残缺产物不是靠推断，
而是**真的往一次性库放一次**并把结论记进证据；runbook 也据此要求恢复前必须校验 `sha256` + 条目数 + 实际还原。

### 5.5 无 `sleep` 同步

故障注入不用固定 `sleep`：中断由**服务器侧 `statement_timeout`** 触发（到点由 PG 自己发取消），定位与等待全部是
`pg_stat_activity` / 返回值驱动的轮询与状态读取。单测里对时间只有 `elapsed_s` 记录，没有「等 N 秒再断言」。

### 5.6 真实时长（单机、本机容器）

| 阶段 | 秒 |
| --- | --- |
| 迁移 + 种入（8 迁移 + 13 张业务表 61 行 + 2 个 checkpoint run） | 4.172 |
| 备份（277 607 B，508 条目） | 0.766 |
| 还原 + 全量核验（63 表全文摘要） | 7.140 |
| 升级 + 回滚（两次还原 + 指针比对） | 13.922 |
| 故障注入（3 类） | 23.922 |
| 清理（4 库 + 目录 + 服务端产物） | 1.672 |
| **合计** | **51.59** |

这只是**开发机上限量级**，不是 RTO：数据量 83 行、单库单实例、无并发写入、无对象存储同步。RPO 更是无从谈起（无 WAL 归档）。

## 6. 事故记录（必须留档）

本轮演练**两次触发共享集群 reinit**，由我造成，已向 Lead 报告：

| 项 | 内容 |
| --- | --- |
| 时间 | `2026-10-28T02:34:32Z`、`2026-10-28T02:42:42Z`（本地 10:34 / 10:42） |
| 现象 | `terminating any other active server processes` → `reinitializing` → crash recovery 约 12–15s，期间 `FATAL: the database system is in recovery mode` |
| 根因 | 我对**正在执行 `COPY ... TO PROGRAM 'pg_dump/pg_restore'` 的后端**调了 `pg_terminate_backend()`（SIGTERM）。postmaster 把 COPY 子进程的 SIGTERM 退出判定为 crash（`child process was terminated by signal 15`），于是判定整个后端树崩溃并 reinit 集群 |
| 影响 | 容器未重启（`RestartCount=0`、`StartedAt` 未变）、数据无损（未提交事务被回滚）、`pyknora` 库与 `p0_t02_probe`/`p0_t05` 完好 |
| 修复 | **彻底删除**信号式注入（`kill_backend`/`find_program_backend`/`wait_for_program_backend` 全删），改为服务器侧 `statement_timeout` 取消；`terminate_database_sessions` 仅保留给 teardown，并在 docstring 写明**绝不可对准 COPY PROGRAM 后端** |
| 门禁建议 | 任何「注入连接中断」的演练（T05 同类）在 `COPY ... TO PROGRAM` 场景下都不得用 `pg_terminate_backend`；应用层连接中断可以，但 pg_dump/pg_restore 必须用 statement_timeout / `pg_cancel_backend` |

**声明**：本任务全程未创建、未重启、未重建 core 容器或卷（`docker compose` 零调用）；备份/还原只走 SQL 与数据库主机内命令。

## 7. 可执行 runbook

### 7.1 前置检查

```powershell
# 1) 集群可达（DSN 从 .env.example 读取，证据里必须脱敏）
#    期望：能列出 postgres / pyknora，且没有残留的 p0_t30_* 库
python -c "import sys;sys.path.insert(0,'experiments/phase0');from restore_probe import pgctl;print(pgctl.list_databases(pgctl.admin_connection()))"
```

预期：`['postgres', 'pyknora']`。**若已有 `p0_t30_*` 残留**：先执行 §7.5 清理，不要直接开跑（`create_database` 会显式报
`DATABASE_ALREADY_EXISTS`，这是设计，不是 bug）。

### 7.2 跑完整演练

```powershell
cd E:\project_month\pyknora\experiments\phase0
.\.venv\Scripts\python.exe -X utf8 -m restore_probe          # 摘要 JSON 打到 stdout
```

预期末段：

```json
"verdict": {"consistency":"PASS","rollback":"PASS","faults":"PASS",
            "multi_instance":"NOT_RUN","rpo_rto":"NOT_RUN","object_storage":"NOT_RUN"}
```

退出码：`consistency/rollback/faults` 全 PASS 才 0，否则 1（不会用 0 掩盖失败）。

### 7.3 跑单测（门禁用）

```powershell
cd E:\project_month\pyknora
uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_restore_upgrade.py
```

预期：`21 passed`，exit 0。测试自带 session 级 `run_full_drill()` 与 `finally` 清理，**跑完不该有残留库**。

### 7.4 失败处置

| 症状 | 含义 | 处置 |
| --- | --- | --- |
| `DATABASE_ALREADY_EXISTS` | 上一轮没清理干净 | 跑 §7.5，或手工 `drop_database` 该库后重跑 |
| `DATABASE_NOT_A_DRILL_TARGET` | 有人传了非 `p0_t30_*` 的名字给 `ensure_absent` | 保护机制，**不要绕过**；确认名字拼写 |
| `BACKUP_UNREADABLE` / `PROGRAM_FAILED` | 产物不可读或 pg_dump 非零退出 | 看 `/tmp/p0_t30_*.dump.err`（`pgctl.tail_file`）；检查磁盘与 socket |
| `RESTORE_TARGET_OCCUPIED` | 目标库有对象 | 正常保护；换库名或先清空 |
| 还原后 `digest_mismatches` 非空 | 产物与源库不一致 | **不要继续**，按「数据损失」处理并保留产物 |
| 集群反复 reinit | 见 §6 | 立即停止信号式注入；改用 statement_timeout |

### 7.5 清理（必须执行）

```powershell
cd E:\project_month\pyknora
$env:PYTHONUTF8='1'
.\experiments\phase0\.venv\Scripts\python.exe -X utf8 -c "import sys;sys.path.insert(0,'experiments/phase0');from restore_probe import pgctl;c=pgctl.admin_connection();print(pgctl.list_databases(c));print('dropped',pgctl.ensure_absent(c,pgctl.DRILL_DATABASES));print(pgctl.list_databases(c))"
```

预期：第二行 `dropped` 覆盖四个库，第三行只剩 `['postgres','pyknora']`。
`run_full_drill` 正常情况下自己就做完了这四件事（报告里 `cleanup.dropped_databases` 有四个名字、
`remaining_databases=['postgres','pyknora']`、`workdir_state='removed'`、`server_artifacts_purged=True`）；
上面这段是**中断后**的手工兜底（服务端产物逐个走 `pgctl.remove_artifact(dsn, path)`）。

清理四件事：①四个 `p0_t30_*` 库（`ensure_absent` 只接受 `DRILL_DATABASES` 里的名字，传别的会抛
`DATABASE_NOT_A_DRILL_TARGET`——这是保护，不要绕过）②`/tmp/p0_t30_*` 服务端产物
③`experiments/phase0/p0_t30_restore/` 工作目录（`cleanup_workdir`）④角色残留
（演练库执行 001 会 `CREATE ROLE p0_app` 并把对象挂到该角色，跨库依赖会阻断 T03 的 `DROP ROLE`；清理后
`pg_shdepend` 对 `p0_app` 应为 0——实测复核为 `0`）。

## 8. 残余风险与 NOT_RUN 清单

1. **单实例限制（最重要）**：T05 已证原生 `PostgresSaver` 的 saver 连接不受 advisory lock fence。本 runbook 的所有结论
   都建立在「同一时刻只有一个 writer」上。多实例/主备切换、leader election **未验证**，不得据此宣称可横扩。
2. **无对象存储**：`p0_t12.phase0_sources.object_key` 指向的原始文件不在本次备份范围内。真实恢复后若对象缺失，
   文档会「有记录无内容」。需要用「对象校验 + 缺失显式标记」补验证（Phase 1）。
3. **RPO/RTO 未校准**：只有单机单次时长（§5.6），没有 WAL 归档、没有持续写入、没有 SLA。不能对外承诺数字。
4. **执行位置差异**：本沙箱用「数据库主机内 `pg_dump`」代替「宿主 `pg_dump`」（§3）。产物格式与校验等价，但生产必须
   再验证一次宿主侧路径与网络带宽。
5. **迁移只能前进一次**：8 份迁移重复执行全部 `42P06`。升级/回滚必须走版本目录 + `up/down` 脚本，不能靠重跑；
   引入「迁移框架」属于 T31+ 决策，本任务不自研。
6. **`007_candidates.sql` 由 T27 同波次落地**：本轮演练首次包含它（p0_t27 12 张表），若 T27 后续再改 007，
   sha256 会变，台账与证据需重跑（本文件的 sha256 是**当前磁盘状态**）。
7. **故障注入的可复现性**：备份中断的产物状态依赖取消时机（§5.4）。断言只要求「显式失败码」，不断言具体字节数。

## 9. 交付哈希（sha256，`Get-FileHash`）

| 文件 | 行数 | sha256 |
| --- | --- | --- |
| `experiments/phase0/restore_probe/__init__.py` | 50 | `7b4812e4232eb8e9aa72c5851132e8af98e909a4e8a9bba594f0ad4d3d979add` |
| `experiments/phase0/restore_probe/__main__.py` | 10 | `4467f23ca9591de23146ee344d8da3c24f122e19fbef0609766ce1490bab6051` |
| `experiments/phase0/restore_probe/drill.py` | 547 | `087ce82e7fa10f9bb6f6d4a77cd0ef68bcbe3ce4d046c3990874d436aaa1f6fc` |
| `experiments/phase0/restore_probe/inventory.py` | 251 | `576141de414c451f43057cf1a87e067fa60d18928cf32a91811b2e2383d449a0` |
| `experiments/phase0/restore_probe/migrations.py` | 274 | `f10831a3c3eab7c3102564978e1a07c3e92a95a7832ab767afa767b62c49c99a` |
| `experiments/phase0/restore_probe/pgctl.py` | 844 | `76bc9208b781407d708a0529e77b11a3d7d27c2b6fe40b664edb5286d9ffe7a4` |
| `experiments/phase0/restore_probe/seed.py` | 461 | `d68b99f5c5c9f665a4d5a3d046013536dec8371ab1466fc1d43381cb9e5599c4` |
| `tests/phase0/test_restore_upgrade.py` | 371 | `c1f718db240a1f0a1cad50c9717d8f73b2e4d33241dbc0c0fbdc858c3c92cdf9` |

## 10. 给 T31 门禁评审的建议

1. **把「备份→还原→逐表摘要比对」当成 Phase 0 的固定门禁**：只比行数会被「行数对、内容错」骗过；本文的全量
   `to_jsonb` 摘要（63 表）成本仅 7 秒。
2. **门禁必须检查清理**：跑完复核 `select datname from pg_database` 与 `pg_shdepend`（角色残留会阻断 T03 的
   `DROP ROLE p0_app`）。建议写进 `wave-*-record.md` 的门禁表。
3. **禁止对 COPY PROGRAM 后端发 SIGTERM**（§6）；把这条写进 `docs/poc/06` 的故障注入章节，避免后续任务重蹈。
4. **多实例/RPO/RTO 保持 NOT_RUN**，除非先由 T05 的 ADR-0001 给出单活/多活结论。不要用本 runbook 的数字冒充 SLA。
5. 迁移「只能前进一次」这一事实应进入升级策略 ADR：T31 需要选「迁移框架」还是「显式 up/down 脚本 + 版本目录」。
