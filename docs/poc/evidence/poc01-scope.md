# PoC-01 / P0-T03 证据：合成身份、Scope 与最小 PG 契约

| 字段 | 值 |
| --- | --- |
| PoC / Task | PoC-01（工作区隔离与授权边界） / **P0-T03** |
| 场景 ID | P0-T03-S1 迁移与 RLS 基线、P0-T03-S2 复合 FK 跨空间拒绝、P0-T03-S3 跨空间篡改零命中零可写、P0-T03-S4 连接作用域生命周期、P0-T03-S5 membership 撤销、P0-T03-S6 无 Run Job 身份传播（F13）、P0-T03-S7 空库重建 |
| 波次 | Phase 0 · W2（资源槽 `pg_schema=p0_t03`、`redis_db=1`） |
| Owner | `w2-t03-scope`（subagent，Team Lead `lead`） |
| 执行时间 | 2026-10-27 18:0x–18:4x +08:00（UTC 10:0x–10:4x） |
| 结论 | **S1–S4、S6、S7 PASS；S5 PASS；C-07 仍 BLOCKED、C-10 仍 NEEDS_POC（不属本任务范围）** |

本文件只覆盖 P0-T03 交付物；不宣称生产身份域、来源审阅或 `asset_bindings` 已实现。

---

## 1. 基线与依赖

| 项 | 值 |
| --- | --- |
| 仓库 | `E:\project_month\pyknora` |
| 分支 | `feature/w2-t03-t04` |
| HEAD | `6ff7b72941b130fe1284af44c052e345466a8820`（基线 `dev@6ff7b72`） |
| 已跟踪文件改动 | 无（`git diff` 0 字节） |
| 工作树 dirty hash（已跟踪差异） | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`（空 diff 的 sha256） |
| 未跟踪交付物 | 见 §1.1（本轮新增，未 `git add`） |

> 说明：本任务按约束**未做任何 git 写操作**，因此四个代码交付物处于 untracked 状态；上表的 dirty hash 是“已跟踪差异”为空集的哈希，逐文件内容哈希见 §1.1。

### 1.1 交付物哈希（`Get-FileHash -Algorithm SHA256`，UTF-8 原文件）

| 文件 | 行数 | 字节 | sha256 |
| --- | ---: | ---: | --- |
| `experiments/phase0/contracts.py` | 800 | 33338 | `e6389abbf7bfb709b258fe3dfab6f18b25a80aef9f2e90eec77faf035ef72644` |
| `experiments/phase0/scope.py` | 647 | 23501 | `78844a4543cf4b275073eb43d62daafa294afcde3ec94c17c1f655e9c89633f0` |
| `experiments/phase0/db/migrations/001_scope_core.sql` | 485 | 25168 | `1dd1f59673ffbfc9ee034c10d0caf56fca5923fa5b355ac98dfe6f689fa54aaa` |
| `tests/phase0/test_scope.py` | 1144 | 46414 | `e68847834c0f8a39acb8cb1a6fca4cc13d1b03942d9142358629d8a3514b07ba` |
| `docs/poc/evidence/poc01-scope.md` | 本文件 | — | 见交付报告（自引用哈希不写入自身） |

未修改（按范围归属 T02/T04）：`experiments/phase0/pyproject.toml`、`compose.yaml`、`.env.example`、`tests/phase0/conftest.py`、`experiments/phase0/control_site/`、`experiments/phase0/faults.py`。

### 1.2 依赖 lock

| 项 | 值 |
| --- | --- |
| lock 文件 | `experiments/phase0/uv.lock`（262 行 / 30518 B） |
| lock sha256 | `832ad32552e286123f0807d752fcaed0b53b30b07fb71fa0d554b6daeb3fc290` |
| Python | 3.12.13（`experiments/phase0/.venv`） |
| 关键包 | psycopg 3.3.6、sqlalchemy 2.1.6、redis 8.1.0、pytest 9.1.1、pytest-asyncio 1.4.1、ruff |
| 未安装 | `psycopg_pool`（本任务未新增依赖，池复用用同一物理连接复用模拟，见 §6 NOT_RUN） |

### 1.3 环境

| 项 | 值 |
| --- | --- |
| PostgreSQL | 容器 `pyknora-phase0-postgres-1`，`postgres:17-alpine`，healthy |
| 连接 | `postgresql://pyknora:***@127.0.0.1:5433/pyknora`（密码为 `experiments/phase0/.env.example` 中的本地占位值，非真实秘密） |
| 隔离 schema | `p0_t03`（本任务独占；不写 `public`） |
| 应用角色 | `p0_app`（`NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN`，无持久密码） |
| Redis | `pyknora-phase0-redis-1` 未使用（本任务无 Redis 路径） |

---

## 2. 实现摘要（与 C-06 / C-07 / C-10 的对应）

### 2.1 契约（`experiments/phase0/contracts.py`）

| 类型 | 字段（要点） | deny 默认 |
| --- | --- | --- |
| `Principal` | `workspace_id, principal_id, membership_subject_ref, kind(human/service), role(owner/member), status(active/disabled), membership_revision` | 直接构造必须持模块私有可信令牌，否则 `UntrustedConstructionError`；`from_client_payload()` 恒抛 `ClientScopeRejectedError` |
| `WorkspaceScope` | `workspace_id, principal_id, policy_snapshot_id`（TDD01 §4） | 同上；`from_client_headers()/from_client_payload()` 恒抛 |
| `ResourceRef` | `workspace_id, resource_kind(source/kb), resource_id` | `from_client_payload()` 恒抛；跨空间 ref 在 Binding 构造期被拒 |
| `ExecutionBinding` | C-06 全字段 + `resource_scope_hash, binding_hash` | 构造期重算两个哈希，不一致即 `InvalidContractValueError`（落库后被篡改同样 fail closed） |
| `AttemptContext` | `scope, binding, job_id, run_id, principal_role, membership_revision, authorized_at, attempt_id, ignored_payload_fields` | 校验 scope 与 binding 的 workspace/principal/policy 三方一致 |

信任令牌只是**误用护栏**（Python 无法构成安全边界），文档已在模块 docstring 明示；真正的边界是后端认证授权路径 + DB 事实（`authorize_job` 只读 DB 行）。

**C-07 BLOCKED（未实现，不缺实现能力而是缺决策）**：生产身份域（全局 User、subject→membership 映射、登录/会话）、P0 来源审阅端点与角色（可否自审）、PRD `approval_id` ↔ 实验 `review_decision_id` 映射。需产品 + 安全 + 架构三方决策；本轮只提供**合成** Principal，`phase0_sources.status` 仅为状态位，不构成审阅实现。

**C-10 NEEDS_POC**：`asset_bindings`（资产唯一归属、延迟 FK、插入环）未实现，迁移 §10 留位注释，待 T11/T12 真实 PG 证据后再定 DDL。

### 2.2 连接作用域（`experiments/phase0/scope.py`）

- GUC：`app.workspace_id` / `app.principal_id` / `app.policy_snapshot_id` / `app.execution_binding_id`。
- `apply_scope()` 用一条 `SELECT set_config(%s,%s,true), ...`（等价 `SET LOCAL`）；`clear_scope()` 写空串。
- `scoped_connection(conn, scope, commit_on_exit=False)`：
  - 拒绝 `autocommit=True`（实测服务端会忽略 `SET LOCAL`，GUC 保持空串）；
  - 调用方已有事务 → 只在本事务内设置，退出时清空为 deny，**不替调用方提交**；
  - 无事务 → 自开事务，退出按 `commit_on_exit` 提交或回滚（默认回滚）。
- 实测 `SET LOCAL` 事务外不可见：commit 后 `current_setting(...,true)` 为 `''`，`NULLIF(...,'')::uuid` 得 NULL → RLS 谓词不成立 → 0 行。

### 2.3 无 Run Job 再授权（C-06 / F13）

`authorize_job(conn, job_id, payload=None)`：
1. 仅凭 `job_id` 调 `p0_t03.phase0_lookup_job_route()`（`SECURITY DEFINER`，只返回 `workspace_id, execution_binding_id, run_id`）；
2. `SET LOCAL app.workspace_id` 后读 binding + principal **当前行**；
3. payload 只允许 `job_id/workspace_id/principal_id/role`：`job_id`、workspace、principal 任一冲突即 `PayloadClaimMismatchError`；`role` 永不作为授权输入，只记入 `ignored_payload_fields`；
4. binding 过期 → `BindingExpiredError`；principal 非 active → `PrincipalInactiveError`（membership 撤销兜底）；
5. 成功返回 `AttemptContext`，字段来自 DB，不来自消息。

### 2.4 迁移（`experiments/phase0/db/migrations/001_scope_core.sql`）与 RLS

- 10 张表全部在 `p0_t03`；含 `workspace_id` 的唯一键 7 组，供复合 FK 引用；7 条复合 FK 业务边 + 7 条 workspace 根边。
- RLS：8 张业务表 + `phase0_deny_probe` 全部 `ENABLE` + `FORCE ROW LEVEL SECURITY`；policy 只授 `p0_app`，谓词为
  `workspace_id = p0_t03.phase0_current_workspace_id() AND p0_t03.phase0_current_principal_permitted()`
  （第二个函数在 `app.principal_id` 有值时要求该 principal 当前 `status='active'`，为空时视为受信 workspace 级扫描）。
- `phase0_deny_probe`：明确 GRANT 了 SELECT/INSERT/UPDATE/DELETE 但**故意不建 policy**，用于证明“无 policy 即拒绝”而非“缺权限”。
- 迁移台账 `phase0_schema_migrations` 不授权应用角色、也不建 policy。
- 应用角色非表 owner + `NOBYPASSRLS` + `FORCE RLS` → 连表 owner/超级用户以外的任何会话都无法绕过。

---

## 3. 命令与真实输出

### 3.1 ruff

```text
$ uv run --project experiments/phase0 ruff check experiments/phase0/contracts.py experiments/phase0/scope.py tests/phase0/test_scope.py
All checks passed!
EXIT=0
```

（首轮曾 6 处告警：E501×4、SIM102×1、I001×1，已全部修正后复跑通过。）

### 3.2 pytest（真实 PG，16 个用例，无 skip/xfail/mock）

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_scope.py -v -s -p no:cacheprovider
============================= test session starts =============================
platform win32 -- Python 3.12.13, pytest-9.1.1, pluggy-1.7.0
rootdir: E:\project_month\pyknora\experiments\phase0
configfile: pyproject.toml
plugins: asyncio-1.4.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 16 items

experiments\phase0 ..........[composite-fk] rounds=100 edges=7 attempts=700 rejected_23503=700 written=0
.[tamper] rounds=100 reads=500 writes=700 unauthorized_hits=0 unauthorized_writes=0
.....

============================ 16 passed in 36.50s =============================
EXIT=0
```

用例与场景对照：

| # | 用例 | 覆盖 |
| ---: | --- | --- |
| 1 | `test_client_supplied_scope_and_principal_are_rejected_by_default` | 客户端自报作用域 deny 默认 |
| 2 | `test_disabled_principal_cannot_issue_scope_or_binding` | 撤销主体不能签发 scope/binding |
| 3 | `test_tampered_binding_hashes_fail_construction` | 字段级篡改 fail closed |
| 4 | `test_migration_creates_rls_schema_and_records_hash` | 迁移 up、台账哈希、重复执行失败 |
| 5 | `test_rls_actor_is_non_superuser_without_bypassrls` | 非超级用户角色属性 |
| 6 | `test_without_scope_non_superuser_sees_nothing` | 无 scope 时 9 张表全 0 行 |
| 7 | `test_scoped_connection_limits_rows_to_own_workspace` | 双空间可见性 |
| 8 | `test_pooled_connection_does_not_retain_scope` | 连接复用无残留 |
| 9 | `test_scoped_connection_rejects_autocommit` | autocommit 风险拦截 |
| 10 | `test_table_without_policy_denies_reads_and_writes` | 无 policy 即拒绝 |
| 11 | `test_composite_fk_rejects_cross_workspace_references_100_rounds` | 复合 FK 100 轮跨空间拒绝 |
| 12 | `test_cross_workspace_tampering_yields_zero_hits_and_zero_writes` | 100 轮篡改 0 命中 0 可写 |
| 13 | `test_membership_revocation_denies_on_same_connection` | 撤销即时生效 + 旧 scope 失效 |
| 14 | `test_authorize_job_without_run_reads_current_principal` | C-06 无 Run Job 身份传播 |
| 15 | `test_expired_binding_and_tampered_row_fail_closed` | 过期/落库后被篡改 |
| 16 | `test_empty_database_rebuild` | 空库重建可重复 |

### 3.2.1 指定命令形式（`-q`）的冷启动复跑

```text
$ uv run --project experiments/phase0 pytest -c experiments/phase0/pyproject.toml tests/phase0/test_scope.py -q -p no:cacheprovider
................                                                         [100%]
EXIT=0
```

两次 `-q` 运行都在 `p0_t03` 已删除的空库上启动（fixture 自行重建 schema 与角色），均 16 点全通过、`EXIT=0`。
重定向 stdout 时 Windows 下 pytest 的末行统计（`16 passed in …s`）会被缓冲丢弃，故计数证据以 §3.2 的 `16 passed in 36.50s` 为准。

`--junitxml` 在本机不可用（两次不同目录均 `PermissionError`）：

```text
$ … --junitxml=C:\Users\Administrator\t03_junit.xml
File "…\_pytest\junitxml.py", line 652, in pytest_sessionfinish
    with open(self.logfile, "w", encoding="utf-8") as logfile:
PermissionError: [Errno 13] Permission denied: 'C:\\Users\\Administrator\\t03_junit.xml'
................                                                         [100%]
```

用例本身仍全部通过（进度点已打印），退出码 1 仅来自 XML 写出失败；判定为宿主环境限制，非实现缺陷（§6 记 `NOT_RUN`）。

### 3.3 迁移首次落地（probe，独立脚本）

```text
$ uv run --project experiments/phase0 python "$env:TEMP\t03_mig1.py"
PROBE_RESULT = OK
```

### 3.4 Schema 快照（`pg_class` / `pg_policies` / `pg_constraint` / `pg_proc` 等价摘录）

```text
## summary
tables|policies|foreign_keys|functions
10|8|14|3

## tables
relname|relrowsecurity|relforcerowsecurity
phase0_deny_probe|t|t
phase0_execution_binding_resources|t|t
phase0_execution_bindings|t|t
phase0_jobs|t|t
phase0_knowledge_bases|t|t
phase0_policy_snapshots|t|t
phase0_principals|t|t
phase0_schema_migrations|f|f          <-- 台账表：无 policy、不授权应用角色
phase0_sources|t|t
phase0_workspaces|t|t

## policies
tablename|policyname|cmd|roles|qual|with_check
phase0_sources|phase0_sources_scope|ALL|{p0_app}|((workspace_id = p0_t03.phase0_current_workspace_id()) AND p0_t03.phase0_current_principal_permitted())|(同左)
...（8 条，均同上谓词，仅 p0_app）

## foreign_keys
phase0_binding_resources_binding_fk|p0_t03.phase0_execution_binding_resources|FOREIGN KEY (workspace_id, execution_binding_id) REFERENCES p0_t03.phase0_execution_bindings(workspace_id, execution_binding_id) ON DELETE CASCADE
phase0_binding_resources_kb_fk|p0_t03.phase0_execution_binding_resources|FOREIGN KEY (workspace_id, kb_id) REFERENCES p0_t03.phase0_knowledge_bases(workspace_id, kb_id) ON DELETE RESTRICT
phase0_binding_resources_source_fk|p0_t03.phase0_execution_binding_resources|FOREIGN KEY (workspace_id, source_id) REFERENCES p0_t03.phase0_sources(workspace_id, source_id) ON DELETE RESTRICT
phase0_binding_policy_fk|p0_t03.phase0_execution_bindings|FOREIGN KEY (workspace_id, policy_snapshot_id) REFERENCES p0_t03.phase0_policy_snapshots(workspace_id, policy_snapshot_id) ON DELETE RESTRICT
phase0_binding_principal_fk|p0_t03.phase0_execution_bindings|FOREIGN KEY (workspace_id, principal_id) REFERENCES p0_t03.phase0_principals(workspace_id, principal_id) ON DELETE RESTRICT
phase0_jobs_binding_fk|p0_t03.phase0_jobs|FOREIGN KEY (workspace_id, execution_binding_id) REFERENCES p0_t03.phase0_execution_bindings(workspace_id, execution_binding_id) ON DELETE RESTRICT
phase0_jobs_source_fk|p0_t03.phase0_jobs|FOREIGN KEY (workspace_id, source_id) REFERENCES p0_t03.phase0_sources(workspace_id, source_id) ON DELETE RESTRICT
phase0_kb_owner_fk|p0_t03.phase0_knowledge_bases|FOREIGN KEY (workspace_id, owner_principal_id) REFERENCES p0_t03.phase0_principals(workspace_id, principal_id) ON DELETE RESTRICT
...（另 7 条单列 workspace_id 根边，指向 phase0_workspaces）

## functions
proname|prosecdef|provolatile|result
phase0_current_principal_permitted|t|s|boolean
phase0_current_workspace_id|f|s|uuid
phase0_lookup_job_route|t|s|TABLE(workspace_id uuid, execution_binding_id uuid, run_id uuid)

## role_p0_app
rolname|rolsuper|rolbypassrls|rolcanlogin|rolcreatedb|rolcreaterole
p0_app|f|f|t|f|f

## ledger
migration_id|filename|sha256|applied_by|applied_at
001_scope_core|001_scope_core.sql|1dd1f59673ffbfc9ee034c10d0caf56fca5923fa5b355ac98dfe6f689fa54aaa|pyknora|2026-10-27 10:29:09.724603+00

## jobs_columns（execution_binding_id NOT NULL 的证据）
workspace_id|uuid|NO|
job_id|uuid|NO|
execution_binding_id|uuid|NO|
run_id|uuid|YES|
source_id|uuid|YES|
kind|text|NO|
dedup_key|text|NO|
payload_schema_version|integer|NO|1
status|text|NO|'pending'::text
current_attempt_id|uuid|YES|
lease_epoch|bigint|NO|0
created_at|timestamp with time zone|NO|now()
```

`phase0_lookup_job_route` 与 `phase0_current_principal_permitted` 为 `SECURITY DEFINER`（计划内），`phase0_current_workspace_id` 为 `SECURITY INVOKER`；三者均 `REVOKE ... FROM PUBLIC` 后仅 GRANT 给 `p0_app`。

---

## 4. 验收逐项核对

| # | 验收条件（`docs/poc/03_Phase0_Task_Backlog.md` L48–L63） | 方法 | 期望 | 实际 | 结论 |
| ---: | --- | --- | --- | --- | --- |
| 1 | 100 组跨 workspace 资源 ID / Job binding 篡改 → 越权命中 0、可写 0 | `test_cross_workspace_tampering_yields_zero_hits_and_zero_writes` | hits=0, writes=0 | `reads=500 writes=700 unauthorized_hits=0 unauthorized_writes=0`；正向对照同空间可读 1 行、可更新 1 行 | **PASS** |
| 2 | 100% 复合 FK 组合拒绝跨空间引用 | `test_composite_fk_rejects_cross_workspace_references_100_rounds` | 全部 SQLSTATE 23503 且命中预期约束 | `rounds=100 edges=7 attempts=700 rejected_23503=700 written=0` | **PASS** |
| 3 | membership 撤销后同一连接再次访问被拒 | `test_membership_revocation_denies_on_same_connection` | 重授权 `PrincipalInactiveError`；旧 scope 读 0 行、写被拒 | 同上，且 `membership_revision` +1 | **PASS** |
| 4 | 连接池复用不残留 scope（`SET LOCAL` 清理） | `test_pooled_connection_does_not_retain_scope` | 复用后 0 行、`assert_scope_cleared` 通过 | 无 scope 时 8+1 表全 0；嵌套事务退出后已清空 | **PASS**（同一物理连接复用，非真实池，见 §6） |
| 5 | 无 Run Job 恢复时重新读取当前 Principal（C-06） | `test_authorize_job_without_run_reads_current_principal` | run_id 为空仍取 DB 当前 role/revision；payload role 被忽略；workspace/principal/job_id 冲突拒绝 | role/revision 由 `("member",1)` 变为 `("owner",2)`；`ignored_payload_fields=("role",)` | **PASS** |
| 6 | 迁移 up + 空库重建（schema `p0_t03` 隔离，不污染 `public`） | `test_migration_creates_rls_schema_and_records_hash` + `test_empty_database_rebuild` | 重建后 sha256 一致、对象齐全、public 无 `phase0_*` | 重建后 10 表/8 policy/14 FK/3 函数，台账 sha256 与文件一致；`public` 无 `phase0_*` 表 | **PASS** |
| 7 | RLS 生效由**非超级用户角色**证明 | `test_rls_actor_is_non_superuser_without_bypassrls` + `test_without_scope_non_superuser_sees_nothing` + `test_table_without_policy_denies_reads_and_writes` | `session_user=current_user=p0_app`，`rolsuper=rolbypassrls=false`，非表 owner；无 policy 表读写均拒 | 全部满足；超级用户对照可见 2 行 | **PASS** |
| 8 | 合成身份 + deny 默认，不自报作用域 | 用例 1–3 | 客户端构造/自报一律拒绝 | 全部抛 `ClientScopeRejectedError` / `UntrustedConstructionError` | **PASS** |
| 9 | C-07 生产身份域 / 来源审阅 | — | 需产品+安全+架构决策 | 未实现，迁移 §10 与 `contracts.py` docstring 标注 | **BLOCKED**（非本任务缺陷） |
| 10 | C-10 `asset_bindings` | — | 需 T11/T12 真实 PG 证据 | 迁移留位注释，无表 | **NOT_RUN / NEEDS_POC** |

---

## 5. 预期 vs 实际（差异说明）

| 项 | 预期 | 实际 | 处理 |
| --- | --- | --- | --- |
| 测试首跑 | 全部通过 | 2 failed（均属测试自身写法/期望错误，非实现缺陷）：① 直接构造 `ExecutionBinding` 未传可信令牌，先被 trust 校验拦下；② B 空间 `phase0_execution_binding_resources` 期望 2 行，实际 6 行（3 个 binding × 2 条 ref） | ① 显式传 `backend_constructor_token()` 并直接把 `resource_scope_hash` 置为 `ZERO_HASH`；② 期望改 6 并注释原因。复跑 16 passed |
| junit-xml 输出 | 生成 XML | `PermissionError: [Errno 13] Permission denied` 两次不同目录均复现：`C:\Users\ADMINI~1\...\t03_junit.xml` 与 `C:\Users\Administrator\t03_junit.xml`（均发生在 `pytest_sessionfinish`），各导致一次运行的 exit code=1（用例本身全过） | 判定与实现无关；改用 `-v -s` / `-q` 文本输出作为证据；该现象列入 §6 `NOT_RUN` 与 §7 残余风险 |
| 共享终端干扰 | 前台命令稳定 | 与 T04 共用 pty，外部 Ctrl+C 曾两次在“写完进度点、尚未写汇总”时终止 pytest（exit 1） | 改用独立控制台 `Start-Process -WindowStyle Hidden` + runner 脚本落盘 `EXIT=` 与全文，最终 run 为 `EXIT=0` |

---

## 6. NOT_RUN / BLOCKED

| 项 | 状态 | 原因 / 缺什么 |
| --- | --- | --- |
| 生产身份域（User、subject→membership、登录/会话/Cookie） | **BLOCKED** | C-07：需产品 + 安全 + 架构决策（身份域例外范围、外部 IdP 假设） |
| P0 来源审阅端点 / 审阅角色 / 是否允许自审 | **BLOCKED** | C-07：需产品决策（P0 是否纳入 review 流程）与安全评审（自审禁止） |
| PRD `approval_id` ↔ 实验 `review_decision_id` 映射 | **BLOCKED** | C-07：需架构决策（是否引入 review 域模型） |
| `asset_bindings` 唯一归属 / 延迟 FK / 插入环 | **NOT_RUN（NEEDS_POC）** | C-10：待 T11/T12 真实 PG 证据 |
| 真实连接池（`psycopg_pool`）复用 | **NOT_RUN** | 未安装 `psycopg_pool`；本任务不得改 `pyproject.toml`。当前用同一物理连接的多次借还模拟，能证明 `SET LOCAL` 不残留，但不能替代真实池实现的行为 |
| 多进程/多 Worker 并发抢同一 Job | **NOT_RUN** | 属 C-06 领取幂等与 T08/T29 范围 |
| junit XML 测试报告 | **NOT_RUN** | 宿主对 `%TEMP%` 与 `%USERPROFILE%` 根目录写 XML 均 `PermissionError`（两次不同目录），用例本身全过；结果以文本证据（`16 passed in 36.50s`、`-q` 16 点 `EXIT=0`）为准 |
| Redis 相关路径 | **NOT_RUN** | 本任务不涉及（资源槽 `redis_db=1` 未使用） |
| 性能/压测（100 轮为正确性计数，非吞吐基准） | **NOT_RUN** | 本任务无性能目标 |
| 迁移回滚（down 迁移） | **NOT_RUN** | Phase 0 迁移策略未定义回滚要求；当前以“DROP SCHEMA + 重放”证明可重建 |

---

## 7. 残余风险

1. **信任令牌不是安全边界**：`contracts.backend_constructor_token()` 在同进程可被调用；真实边界依赖后端授权路径与 DB。生产需把构造点收敛到单一仓储层（建议 T05/T07 落实）。
2. **`SECURITY DEFINER` 路由函数的 owner 是超级用户 `pyknora`**：生产必须换成受限 worker 角色并单独评审（C-06 已标注 T29）。当前函数只返回 3 个标识符，风险窗口有限但不可直接照搬。
3. **workspace 级扫描放行**：`phase0_current_principal_permitted()` 在 `app.principal_id` 为空时放行本空间所有行；若未来有“连接只设 workspace、不设 principal”的业务路径，会退化为仅空间隔离。需在 T05/T07 明确“凡读写业务行必须同时设置 principal”。
4. **`FORCE ROW LEVEL SECURITY` + 非 owner 角色**已生效，但**迁移/后台角色与请求角色未分离**：当前 fixture/迁移都用超级用户。生产需拆成 `migration role` / `worker role` / `request role`。
5. **junit-xml 写入 `%TEMP%` 短路径时出现 PermissionError**：与本任务实现无关，但会影响 CI 里依赖 junit 的步骤；建议 T04/T02 在 CI 用长路径或临时目录。
6. **测试依赖固定 UUID 种子**：`test_scope.py` 用固定 UUID + session 级 seed，顺序敏感（重建用例必须最后）。若并行执行或用例乱序会失败；已在文件内注释，建议后续迁到 conftest（T04）时显式排序。
7. **`phase0_jobs.dedup_key` 未参与 C-06 授权**：仅作幂等键，重试/接管语义留给 C-09（T08）。

---

## 8. 清理

本任务在 `p0_t03` 内建的全部对象与 `p0_app` 角色已删除；未触碰 `public`、`p0_t02_probe`（T02 资源槽）与容器本身。

```text
## before
schemaname|tablename
p0_t03|phase0_deny_probe
p0_t03|phase0_execution_binding_resources
p0_t03|phase0_execution_bindings
p0_t03|phase0_jobs
p0_t03|phase0_knowledge_bases
p0_t03|phase0_policy_snapshots
p0_t03|phase0_principals
p0_t03|phase0_schema_migrations
p0_t03|phase0_sources
p0_t03|phase0_workspaces
public_phase0_tables = 0          <-- 未污染 public
nspname: p0_t02_probe / p0_t03 / public
p0_app_sessions = 0

$ DROP SCHEMA IF EXISTS p0_t03 CASCADE;
NOTICE:  drop cascades to 13 other objects
DETAIL:  drop cascades to table p0_t03.phase0_schema_migrations
 ... （10 张表 + 3 个函数）
DROP SCHEMA
DO
DROP ROLE
## cleanup_exit=0

## after
schema_rows_left        = 0
role_rows_left          = 0
public_phase0_tables    = 0
any_phase0_tables       = 0
functions_left          = 0
p0_app_sessions         = 0
```

清理脚本：`$TEMP\t03_cleanup.ps1`（`DROP SCHEMA ... CASCADE` → `DROP OWNED BY p0_app` → `DROP ROLE p0_app`，前后各取一次状态）。Redis 未使用（`redis_db=1` 无写入，无需清理）。

### 8.1 测试自带清理（避免残留污染后续任务）

`tests/phase0/test_scope.py` 里有一个 **session 级 autouse fixture** `cleanup_pg_artifacts`，在整轮用例结束后执行
`scope_module.reset_database(admin)`（删 `p0_t03` + `p0_app`）。原因：`p0_app` 是**全局角色名**，
若整目录跑测试后残留，会与下一轮运行或其它任务的同名角色冲突。

最终一轮（含该 fixture）跑完后独立复核：

```text
## post_pytest_state（psql 查询 pg_namespace / pg_roles / pg_tables / pg_stat_activity）
schema_rows              = 0
role_rows                = 0
public_phase0_tables     = 0
any_phase0_tables        = 0
p0_app_sessions          = 0
```

即：pytest 结束即自动回到“无本任务任何对象”的状态，无需人工再清理。

---

## 9. Owner

| 项 | 值 |
| --- | --- |
| Owner | `w2-t03-scope`（W2 子任务执行者） |
| 上游 | Team Lead `lead` |
| 复核建议 | T05（Scope 落库路径收敛）、T07（outbox/事务边界）、T29（worker 受限角色与 `SECURITY DEFINER` 评审） |
