"""P0-T03：合成身份 / Scope / 最小 PG 契约的真实 PostgreSQL 用例。

验收依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T03 任务卡）
          docs/poc/07_Contract_Closure.md C-06（无 Run Job 身份传播）
契约实现：experiments/phase0/contracts.py + experiments/phase0/scope.py
迁移：    experiments/phase0/db/migrations/001_scope_core.sql（schema p0_t03）

本文件不使用 skip / xfail / mock：全部用例跑真实 PG（127.0.0.1:5433，schema p0_t03）。
RLS 结论必须由**非超级用户角色** p0_app（NOSUPERUSER NOBYPASSRLS）证明；
超级用户连接只用于建 fixture、读 schema 快照与模拟管理员侧变更。

C-07 仍 BLOCKED：本文件只覆盖合成 Principal，不覆盖生产登录/来源审阅/身份域。
"""

from __future__ import annotations

import hashlib
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import contracts  # noqa: E402
import scope as scope_module  # noqa: E402

# ---------------------------------------------------------------------------
# 固定标识（可读、可复算；与生产无关）
# ---------------------------------------------------------------------------
WS_A = UUID("0a0a0a0a-0000-4000-8000-00000000000a")
WS_B = UUID("0b0b0b0b-0000-4000-8000-00000000000b")
PRINCIPAL_A = UUID("a11a11a1-0000-4000-8000-00000000000a")
PRINCIPAL_B = UUID("b22b22b2-0000-4000-8000-00000000000b")
POLICY_A = UUID("a9a9a9a9-0000-4000-8000-0000000000aa")
POLICY_B = UUID("b9b9b9b9-0000-4000-8000-0000000000bb")
SOURCE_A = UUID("50050050-0000-4000-8000-00000000000a")
SOURCE_B = UUID("50050050-0000-4000-8000-00000000000b")
KB_A = UUID("6b6b6b6b-0000-4000-8000-00000000000a")
KB_B = UUID("6b6b6b6b-0000-4000-8000-00000000000b")
BINDING_A = UUID("b1d1b1d1-0000-4000-8000-00000000000a")
BINDING_B = UUID("b1d1b1d1-0000-4000-8000-00000000000b")
BINDING_EXPIRED = UUID("b1d1b1d1-0000-4000-8000-00000000000e")
BINDING_TAMPER = UUID("b1d1b1d1-0000-4000-8000-000000000007")
JOB_A = UUID("70b070b0-0000-4000-8000-00000000000a")
JOB_B = UUID("70b070b0-0000-4000-8000-00000000000b")
JOB_EXPIRED = UUID("70b070b0-0000-4000-8000-00000000000e")
JOB_TAMPER = UUID("70b070b0-0000-4000-8000-000000000007")

TENANT_TABLES = (
    "phase0_workspaces",
    "phase0_principals",
    "phase0_policy_snapshots",
    "phase0_sources",
    "phase0_knowledge_bases",
    "phase0_execution_bindings",
    "phase0_execution_binding_resources",
    "phase0_jobs",
)
RLS_TABLES = (*TENANT_TABLES, "phase0_deny_probe")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


ZERO_HASH = _sha256("p0-t03-probe")


# ---------------------------------------------------------------------------
# fixture / 连接
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Seed:
    """会话级种子：两个空间各一套最小事实 + p0_app 连接串。"""

    app_dsn: str
    scope_a: contracts.WorkspaceScope
    scope_b: contracts.WorkspaceScope
    principal_a: contracts.Principal
    principal_b: contracts.Principal


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return scope_module.read_dsn()


@pytest.fixture(scope="session")
def migration(pg_dsn: str) -> scope_module.MigrationStamp:
    """空库 → 应用迁移 001；同时清掉上一轮遗留的 p0_t03 / p0_app。"""

    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        scope_module.reset_database(admin)
    with psycopg.connect(pg_dsn) as conn:
        stamp = scope_module.apply_migration(conn)
        conn.commit()
    return stamp


@pytest.fixture(scope="session", autouse=True)
def cleanup_pg_artifacts(
    pg_dsn: str, migration: scope_module.MigrationStamp
) -> Iterator[None]:
    """会话结束删除本任务在 PG 里建的 p0_t03 与 p0_app。

    自动清理的原因：p0_app 是**全局**角色名，残留会与下一轮运行或其它任务的同名角色冲突；
    证据快照由独立脚本采集，不依赖本 fixture 刻意留下的残留。
    `migration` 仅用于建立依赖顺序，保证本 fixture 在迁移与种子之后才拆。
    """

    yield
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        scope_module.reset_database(admin)


@pytest.fixture(scope="session")
def app_dsn(pg_dsn: str, migration: scope_module.MigrationStamp) -> str:
    """给 p0_app 设置运行期随机密码（秘密只存在于内存，不入库不入日志）。"""

    password = secrets.token_urlsafe(24)
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        scope_module.set_role_password(admin, role=scope_module.APP_ROLE, password=password)
    return scope_module.dsn_with_credentials(
        pg_dsn, user=scope_module.APP_ROLE, password=password
    )


@pytest.fixture(scope="session")
def seeded(pg_dsn: str, app_dsn: str, migration: scope_module.MigrationStamp) -> Seed:
    principal_a = contracts.synthesize_principal(
        workspace_id=WS_A,
        principal_id=PRINCIPAL_A,
        membership_subject_ref="synth-subject-a",
        kind="human",
        role="owner",
    )
    principal_b = contracts.synthesize_principal(
        workspace_id=WS_B,
        principal_id=PRINCIPAL_B,
        membership_subject_ref="synth-subject-b",
        kind="service",
        role="member",
    )
    now = datetime.now(UTC)
    refs_a = (
        contracts.ResourceRef(workspace_id=WS_A, resource_kind="source", resource_id=SOURCE_A),
    )
    refs_b = (
        contracts.ResourceRef(workspace_id=WS_B, resource_kind="source", resource_id=SOURCE_B),
        contracts.ResourceRef(workspace_id=WS_B, resource_kind="kb", resource_id=KB_B),
    )
    binding_a = contracts.ExecutionBinding.create(
        principal=principal_a,
        policy_snapshot_id=POLICY_A,
        execution_binding_id=BINDING_A,
        operation="source.fetch",
        resource_refs=refs_a,
        created_at=now,
    )
    binding_b = contracts.ExecutionBinding.create(
        principal=principal_b,
        policy_snapshot_id=POLICY_B,
        execution_binding_id=BINDING_B,
        operation="source.ingest",
        resource_refs=refs_b,
        created_at=now,
    )
    binding_expired = contracts.ExecutionBinding.create(
        principal=principal_b,
        policy_snapshot_id=POLICY_B,
        execution_binding_id=BINDING_EXPIRED,
        operation="source.ingest",
        resource_refs=refs_b,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    binding_tamper = contracts.ExecutionBinding.create(
        principal=principal_b,
        policy_snapshot_id=POLICY_B,
        execution_binding_id=BINDING_TAMPER,
        operation="source.ingest",
        resource_refs=refs_b,
        created_at=now,
    )

    with psycopg.connect(pg_dsn) as conn:
        _insert_workspace(conn, WS_A, "workspace-a")
        _insert_workspace(conn, WS_B, "workspace-b")
        _insert_principal(
            conn,
            workspace_id=WS_A,
            principal_id=PRINCIPAL_A,
            subject_ref="synth-subject-a",
            kind="human",
            role="owner",
        )
        _insert_principal(
            conn,
            workspace_id=WS_B,
            principal_id=PRINCIPAL_B,
            subject_ref="synth-subject-b",
            kind="service",
            role="member",
        )
        _insert_policy(conn, workspace_id=WS_A, policy_snapshot_id=POLICY_A)
        _insert_policy(conn, workspace_id=WS_B, policy_snapshot_id=POLICY_B)
        _insert_source(conn, workspace_id=WS_A, source_id=SOURCE_A, url="https://a.example/doc")
        _insert_source(conn, workspace_id=WS_B, source_id=SOURCE_B, url="https://b.example/doc")
        _insert_kb(conn, workspace_id=WS_A, kb_id=KB_A, owner_principal_id=PRINCIPAL_A)
        _insert_kb(conn, workspace_id=WS_B, kb_id=KB_B, owner_principal_id=PRINCIPAL_B)
        for binding in (binding_a, binding_b, binding_expired, binding_tamper):
            _insert_binding(conn, binding)
            _insert_binding_resources(conn, binding)
        _insert_job(
            conn,
            workspace_id=WS_A,
            job_id=JOB_A,
            binding_id=BINDING_A,
            source_id=SOURCE_A,
            kind="fetch",
            dedup_key="job-a",
        )
        _insert_job(
            conn,
            workspace_id=WS_B,
            job_id=JOB_B,
            binding_id=BINDING_B,
            source_id=SOURCE_B,
            kind="ingest",
            dedup_key="job-b",
        )
        _insert_job(
            conn,
            workspace_id=WS_B,
            job_id=JOB_EXPIRED,
            binding_id=BINDING_EXPIRED,
            source_id=SOURCE_B,
            kind="ingest",
            dedup_key="job-expired",
        )
        _insert_job(
            conn,
            workspace_id=WS_B,
            job_id=JOB_TAMPER,
            binding_id=BINDING_TAMPER,
            source_id=SOURCE_B,
            kind="ingest",
            dedup_key="job-tamper",
        )
        # 篡改：直接把 DB 里的 binding_hash 改成另一条合法十六进制串。
        conn.execute(
            "UPDATE p0_t03.phase0_execution_bindings SET binding_hash = %s "
            "WHERE workspace_id = %s AND execution_binding_id = %s",
            (_sha256("tampered"), WS_B, BINDING_TAMPER),
        )
        conn.commit()

    return Seed(
        app_dsn=app_dsn,
        scope_a=contracts.issue_scope(principal_a, policy_snapshot_id=POLICY_A),
        scope_b=contracts.issue_scope(principal_b, policy_snapshot_id=POLICY_B),
        principal_a=principal_a,
        principal_b=principal_b,
    )


@pytest.fixture
def app_conn(seeded: Seed) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(seeded.app_dsn) as conn:
        yield conn


# ---------------------------------------------------------------------------
# 种子写入（超级用户，用于准备事实；RLS 断言一律走 p0_app）
# ---------------------------------------------------------------------------
def _insert_workspace(conn: psycopg.Connection[Any], workspace_id: UUID, name: str) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_workspaces (workspace_id, name) VALUES (%s, %s)",
        (workspace_id, name),
    )


def _insert_principal(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    subject_ref: str,
    kind: str,
    role: str,
) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_principals "
        "(workspace_id, principal_id, membership_subject_ref, kind, role) "
        "VALUES (%s, %s, %s, %s, %s)",
        (workspace_id, principal_id, subject_ref, kind, role),
    )


def _insert_policy(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, policy_snapshot_id: UUID
) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_policy_snapshots "
        "(workspace_id, policy_snapshot_id, revision, rules_hash) VALUES (%s, %s, 1, %s)",
        (workspace_id, policy_snapshot_id, _sha256(f"rules-{workspace_id}")),
    )


def _insert_source(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, source_id: UUID, url: str
) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_sources "
        "(workspace_id, source_id, canonical_url, canonical_hash, source_type) "
        "VALUES (%s, %s, %s, %s, 'url')",
        (workspace_id, source_id, url, _sha256(url)),
    )


def _insert_kb(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, kb_id: UUID, owner_principal_id: UUID
) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_knowledge_bases "
        "(workspace_id, kb_id, name, owner_principal_id) VALUES (%s, %s, %s, %s)",
        (workspace_id, kb_id, f"kb-{kb_id}", owner_principal_id),
    )


def _insert_binding(conn: psycopg.Connection[Any], binding: contracts.ExecutionBinding) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_execution_bindings "
        "(workspace_id, execution_binding_id, principal_id, policy_snapshot_id, operation, "
        " resource_scope_hash, binding_hash, parent_run_id, parent_task_id, "
        " task_graph_revision, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            binding.workspace_id,
            binding.execution_binding_id,
            binding.principal_id,
            binding.policy_snapshot_id,
            binding.operation,
            binding.resource_scope_hash,
            binding.binding_hash,
            binding.parent_run_id,
            binding.parent_task_id,
            binding.task_graph_revision,
            binding.created_at,
            binding.expires_at,
        ),
    )


def _insert_binding_resources(
    conn: psycopg.Connection[Any], binding: contracts.ExecutionBinding
) -> None:
    for ordinal, ref in enumerate(binding.resource_refs):
        source_id = ref.resource_id if ref.resource_kind == "source" else None
        kb_id = ref.resource_id if ref.resource_kind == "kb" else None
        conn.execute(
            "INSERT INTO p0_t03.phase0_execution_binding_resources "
            "(workspace_id, execution_binding_id, resource_ordinal, resource_kind, "
            " source_id, kb_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (binding.workspace_id, binding.execution_binding_id, ordinal, ref.resource_kind,
             source_id, kb_id),
        )


def _insert_job(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    job_id: UUID,
    binding_id: UUID,
    source_id: UUID,
    kind: str,
    dedup_key: str,
) -> None:
    conn.execute(
        "INSERT INTO p0_t03.phase0_jobs "
        "(workspace_id, job_id, execution_binding_id, source_id, kind, dedup_key) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (workspace_id, job_id, binding_id, source_id, kind, dedup_key),
    )


def _count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM p0_t03.{table}").fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# 0. 契约层：deny 默认（纯 Python，无 DB）
# ---------------------------------------------------------------------------
def test_client_supplied_scope_and_principal_are_rejected_by_default() -> None:
    """客户端不得自报作用域/主体/资源引用（AGENTS.md 关键不变量）。"""

    with pytest.raises(contracts.ClientScopeRejectedError):
        contracts.WorkspaceScope.from_client_headers(
            {"X-Workspace-Id": str(WS_A), "X-Role": "owner"}
        )
    with pytest.raises(contracts.ClientScopeRejectedError):
        contracts.WorkspaceScope.from_client_payload(
            {"workspace_id": str(WS_A), "principal_id": str(PRINCIPAL_A)}
        )
    with pytest.raises(contracts.ClientScopeRejectedError):
        contracts.Principal.from_client_payload(
            {"workspace_id": str(WS_A), "role": "owner"}
        )
    with pytest.raises(contracts.UntrustedConstructionError):
        contracts.Principal(
            workspace_id=WS_A,
            principal_id=PRINCIPAL_A,
            membership_subject_ref="forged",
            kind="human",
            role="owner",
            status="active",
            membership_revision=1,
        )
    with pytest.raises(contracts.UntrustedConstructionError):
        contracts.WorkspaceScope(
            workspace_id=WS_A, principal_id=PRINCIPAL_A, policy_snapshot_id=POLICY_A
        )


def test_disabled_principal_cannot_issue_scope_or_binding() -> None:
    disabled = contracts.synthesize_principal(
        workspace_id=WS_A,
        principal_id=PRINCIPAL_A,
        membership_subject_ref="synth-subject-a",
        kind="human",
        role="owner",
        status="disabled",
    )
    with pytest.raises(contracts.PrincipalInactiveError):
        contracts.issue_scope(disabled, policy_snapshot_id=POLICY_A)
    with pytest.raises(contracts.PrincipalInactiveError):
        contracts.ExecutionBinding.create(
            principal=disabled,
            policy_snapshot_id=POLICY_A,
            execution_binding_id=uuid4(),
            operation="source.fetch",
            resource_refs=(),
            created_at=datetime.now(UTC),
        )


def test_tampered_binding_hashes_fail_construction() -> None:
    principal = contracts.synthesize_principal(
        workspace_id=WS_A,
        principal_id=PRINCIPAL_A,
        membership_subject_ref="synth-subject-a",
        kind="human",
        role="owner",
    )
    ref = contracts.ResourceRef(workspace_id=WS_A, resource_kind="source", resource_id=SOURCE_A)
    binding = contracts.ExecutionBinding.create(
        principal=principal,
        policy_snapshot_id=POLICY_A,
        execution_binding_id=uuid4(),
        operation="source.fetch",
        resource_refs=(ref,),
        created_at=datetime.now(UTC),
    )
    # 即使持有可信构造令牌（后台冷路径），字段被篡改仍必须在构造期失败。
    with pytest.raises(contracts.InvalidContractValueError):
        contracts.ExecutionBinding(
            workspace_id=binding.workspace_id,
            execution_binding_id=binding.execution_binding_id,
            principal_id=binding.principal_id,
            policy_snapshot_id=binding.policy_snapshot_id,
            operation=binding.operation,
            resource_refs=binding.resource_refs,
            resource_scope_hash=ZERO_HASH,
            binding_hash=binding.binding_hash,
            created_at=binding.created_at,
            expires_at=None,
            parent_run_id=None,
            parent_task_id=None,
            task_graph_revision=None,
            _trust=contracts.backend_constructor_token(),
        )
    cross_ref = contracts.ResourceRef(
        workspace_id=WS_B, resource_kind="source", resource_id=SOURCE_B
    )
    with pytest.raises(contracts.InvalidContractValueError):
        contracts.ExecutionBinding.create(
            principal=principal,
            policy_snapshot_id=POLICY_A,
            execution_binding_id=uuid4(),
            operation="source.fetch",
            resource_refs=(cross_ref,),
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# 1. 迁移：schema / RLS / policy / 台账
# ---------------------------------------------------------------------------
def test_migration_creates_rls_schema_and_records_hash(
    migration: scope_module.MigrationStamp, pg_dsn: str
) -> None:
    assert migration.filename == "001_scope_core.sql"
    assert migration.sha256 == scope_module.migration_sha256()
    assert migration.size_bytes > 0

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'p0_t03' AND c.relkind = 'r'"
            )
        }
        assert tables == set(RLS_TABLES) | {"phase0_schema_migrations"}

        rls = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'p0_t03' AND c.relkind = 'r'"
            )
        }
        for table in RLS_TABLES:
            assert rls[table] == (True, True), f"{table} 必须 ENABLE+FORCE RLS"
        # 迁移台账故意不开 RLS 且不授权 p0_app：应用角色连读都做不到。
        assert rls["phase0_schema_migrations"] == (False, False)

        policies = conn.execute(
            "SELECT tablename, cmd, roles::text, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'p0_t03' ORDER BY tablename"
        ).fetchall()
        assert len(policies) == len(TENANT_TABLES)
        assert {row[0] for row in policies} == set(TENANT_TABLES)
        for tablename, cmd, roles, qual, with_check in policies:
            assert cmd == "ALL", tablename
            assert "p0_app" in roles, tablename
            assert "phase0_current_workspace_id" in qual, tablename
            assert "phase0_current_principal_permitted" in qual, tablename
            assert "phase0_current_workspace_id" in with_check, tablename
            assert "phase0_current_principal_permitted" in with_check, tablename

        functions = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT p.proname, p.prosecdef, p.provolatile FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'p0_t03'"
            )
        }
        assert functions == {
            "phase0_current_workspace_id": (False, "s"),
            "phase0_current_principal_permitted": (True, "s"),
            "phase0_lookup_job_route": (True, "s"),
        }

        ledger = conn.execute(
            "SELECT migration_id, filename, sha256 FROM p0_t03.phase0_schema_migrations"
        ).fetchall()
        assert ledger == [(migration.migration_id, migration.filename, migration.sha256)]

    # 重复执行必须显式失败（CREATE SCHEMA 冲突），而不是静默幂等。
    with psycopg.connect(pg_dsn) as conn:
        with pytest.raises(psycopg.errors.DuplicateSchema):
            scope_module.apply_migration(conn)
        conn.rollback()


def test_rls_actor_is_non_superuser_without_bypassrls(
    app_dsn: str, pg_dsn: str, seeded: Seed
) -> None:
    with psycopg.connect(app_dsn, autocommit=True) as conn:
        who = conn.execute("SELECT session_user, current_user").fetchone()
        assert who == ("p0_app", "p0_app")
        attributes = scope_module.role_security_attributes(conn)
        assert attributes == {
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolcanlogin": True,
        }
        owners = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'p0_t03' AND c.relkind = 'r'"
            )
        }
        assert "p0_app" not in owners, "p0_app 不得是任何表 owner（否则 FORCE RLS 之外仍有例外）"

    # 超级用户确实能绕过 RLS（对照组），说明上面的 0 行不是「表里没数据」。
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        assert admin.execute("SELECT count(*) FROM p0_t03.phase0_sources").fetchone() == (2,)


# ---------------------------------------------------------------------------
# 2. 无作用域 = deny 默认
# ---------------------------------------------------------------------------
def test_without_scope_non_superuser_sees_nothing(app_dsn: str) -> None:
    with psycopg.connect(app_dsn) as conn:
        scope_module.assert_scope_cleared(conn)
        for table in RLS_TABLES:
            assert _count(conn, table) == 0, f"{table} 在无作用域时必须 0 行"


def test_scoped_connection_limits_rows_to_own_workspace(seeded: Seed, app_conn: psycopg.Connection[Any]) -> None:
    with scope_module.scoped_connection(app_conn, seeded.scope_a):
        counts_a = {table: _count(app_conn, table) for table in TENANT_TABLES}
    assert counts_a == {
        "phase0_workspaces": 1,
        "phase0_principals": 1,
        "phase0_policy_snapshots": 1,
        "phase0_sources": 1,
        "phase0_knowledge_bases": 1,
        "phase0_execution_bindings": 1,
        "phase0_execution_binding_resources": 1,
        "phase0_jobs": 1,
    }
    with scope_module.scoped_connection(app_conn, seeded.scope_b):
        counts_b = {table: _count(app_conn, table) for table in TENANT_TABLES}
    assert counts_b == {
        "phase0_workspaces": 1,
        "phase0_principals": 1,
        "phase0_policy_snapshots": 1,
        "phase0_sources": 1,
        "phase0_knowledge_bases": 1,
        "phase0_execution_bindings": 3,
        # B 空间 3 个 binding（正常/过期/被篡改）× 2 条 resource_refs。
        "phase0_execution_binding_resources": 6,
        "phase0_jobs": 3,
    }


# ---------------------------------------------------------------------------
# 3. SET LOCAL：连接池复用不残留
# ---------------------------------------------------------------------------
def test_pooled_connection_does_not_retain_scope(seeded: Seed, app_conn: psycopg.Connection[Any]) -> None:
    conn = app_conn
    with scope_module.scoped_connection(conn, seeded.scope_a):
        assert _count(conn, "phase0_sources") == 1
    scope_module.assert_scope_cleared(conn)
    assert _count(conn, "phase0_sources") == 0

    # 模拟池里被另一个请求拿走：换成 B 的作用域，再写一条并提交。
    job_id = uuid4()
    with scope_module.scoped_connection(conn, seeded.scope_b, commit_on_exit=True):
        before = _count(conn, "phase0_jobs")
        conn.execute(
            "INSERT INTO p0_t03.phase0_jobs "
            "(workspace_id, job_id, execution_binding_id, source_id, kind, dedup_key) "
            "VALUES (%s, %s, %s, %s, 'ingest', %s)",
            (WS_B, job_id, BINDING_B, SOURCE_B, f"pool-{job_id}"),
        )
        assert _count(conn, "phase0_jobs") == before + 1
    scope_module.assert_scope_cleared(conn)
    assert _count(conn, "phase0_jobs") == 0

    # 提交后的行只在 B 空间可见，A 空间看不到（同一物理连接，作用域不串）。
    with scope_module.scoped_connection(conn, seeded.scope_b, commit_on_exit=True):
        assert conn.execute(
            "SELECT count(*) FROM p0_t03.phase0_jobs WHERE job_id = %s", (job_id,)
        ).fetchone() == (1,)
        conn.execute(
            "DELETE FROM p0_t03.phase0_jobs WHERE job_id = %s AND workspace_id = %s",
            (job_id, WS_B),
        )
    with scope_module.scoped_connection(conn, seeded.scope_a):
        assert conn.execute(
            "SELECT count(*) FROM p0_t03.phase0_jobs WHERE job_id = %s", (job_id,)
        ).fetchone() == (0,)

    # 调用方已有事务（UoW 内嵌套）：退出后同一事务内也必须清空。
    with conn.transaction():
        with scope_module.scoped_connection(conn, seeded.scope_b):
            assert _count(conn, "phase0_sources") == 1
        scope_module.assert_scope_cleared(conn)
        assert _count(conn, "phase0_sources") == 0


def test_scoped_connection_rejects_autocommit(seeded: Seed, app_dsn: str) -> None:
    """autocommit 下 SET LOCAL 被服务端忽略（实测 GUC 保持空串），必须早失败。"""

    with psycopg.connect(app_dsn, autocommit=True) as conn:
        manager = scope_module.scoped_connection(conn, seeded.scope_a)
        with pytest.raises(scope_module.ScopeSetupError):
            manager.__enter__()
        with pytest.raises(scope_module.ScopeSetupError):
            scope_module.apply_scope(conn, workspace_id=WS_A)


# ---------------------------------------------------------------------------
# 4. 无 policy 的表 = 全拒（deny 默认）
# ---------------------------------------------------------------------------
def test_table_without_policy_denies_reads_and_writes(
    seeded: Seed, app_conn: psycopg.Connection[Any]
) -> None:
    conn = app_conn
    with scope_module.scoped_connection(conn, seeded.scope_a):
        assert _count(conn, "phase0_deny_probe") == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO p0_t03.phase0_deny_probe (workspace_id, probe_id, note) "
                "VALUES (%s, %s, 'should-be-denied')",
                (WS_A, uuid4()),
            )


# ---------------------------------------------------------------------------
# 5. 复合 FK：100 轮 × 7 条边全部拒绝跨空间引用
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FkCase:
    constraint: str
    sql: str
    params: dict[str, object]


def _fk_cases(round_index: int) -> list[FkCase]:
    dedup = f"fk-{round_index}-{uuid4()}"
    return [
        FkCase(
            "phase0_binding_principal_fk",
            "INSERT INTO p0_t03.phase0_execution_bindings "
            "(workspace_id, execution_binding_id, principal_id, policy_snapshot_id, operation, "
            " resource_scope_hash, binding_hash, created_at) "
            "VALUES (%(ws)s, %(new_id)s, %(principal_a)s, %(policy_b)s, 'source.fetch', "
            "%(zero)s, %(zero)s, now())",
            {
                "ws": WS_B,
                "new_id": uuid4(),
                "principal_a": PRINCIPAL_A,
                "policy_b": POLICY_B,
                "zero": ZERO_HASH,
            },
        ),
        FkCase(
            "phase0_binding_policy_fk",
            "INSERT INTO p0_t03.phase0_execution_bindings "
            "(workspace_id, execution_binding_id, principal_id, policy_snapshot_id, operation, "
            " resource_scope_hash, binding_hash, created_at) "
            "VALUES (%(ws)s, %(new_id)s, %(principal_b)s, %(policy_a)s, 'source.fetch', "
            "%(zero)s, %(zero)s, now())",
            {
                "ws": WS_B,
                "new_id": uuid4(),
                "principal_b": PRINCIPAL_B,
                "policy_a": POLICY_A,
                "zero": ZERO_HASH,
            },
        ),
        FkCase(
            "phase0_binding_resources_binding_fk",
            "INSERT INTO p0_t03.phase0_execution_binding_resources "
            "(workspace_id, execution_binding_id, resource_ordinal, resource_kind, source_id) "
            "VALUES (%(ws)s, %(binding_a)s, 0, 'source', %(source_b)s)",
            {"ws": WS_B, "binding_a": BINDING_A, "source_b": SOURCE_B},
        ),
        FkCase(
            "phase0_binding_resources_source_fk",
            "INSERT INTO p0_t03.phase0_execution_binding_resources "
            "(workspace_id, execution_binding_id, resource_ordinal, resource_kind, source_id) "
            "VALUES (%(ws)s, %(binding_b)s, 5, 'source', %(source_a)s)",
            {"ws": WS_B, "binding_b": BINDING_B, "source_a": SOURCE_A},
        ),
        FkCase(
            "phase0_binding_resources_kb_fk",
            "INSERT INTO p0_t03.phase0_execution_binding_resources "
            "(workspace_id, execution_binding_id, resource_ordinal, resource_kind, kb_id) "
            "VALUES (%(ws)s, %(binding_b)s, 6, 'kb', %(kb_a)s)",
            {"ws": WS_B, "binding_b": BINDING_B, "kb_a": KB_A},
        ),
        FkCase(
            "phase0_jobs_binding_fk",
            "INSERT INTO p0_t03.phase0_jobs "
            "(workspace_id, job_id, execution_binding_id, source_id, kind, dedup_key) "
            "VALUES (%(ws)s, %(new_id)s, %(binding_a)s, %(source_b)s, 'fetch', %(dedup)s)",
            {
                "ws": WS_B,
                "new_id": uuid4(),
                "binding_a": BINDING_A,
                "source_b": SOURCE_B,
                "dedup": dedup,
            },
        ),
        FkCase(
            "phase0_jobs_source_fk",
            "INSERT INTO p0_t03.phase0_jobs "
            "(workspace_id, job_id, execution_binding_id, source_id, kind, dedup_key) "
            "VALUES (%(ws)s, %(new_id)s, %(binding_b)s, %(source_a)s, 'fetch', %(dedup)s)",
            {
                "ws": WS_B,
                "new_id": uuid4(),
                "binding_b": BINDING_B,
                "source_a": SOURCE_A,
                "dedup": dedup,
            },
        ),
    ]


def test_composite_fk_rejects_cross_workspace_references_100_rounds(
    seeded: Seed, app_conn: psycopg.Connection[Any]
) -> None:
    conn = app_conn
    attempted = 0
    rejected = 0
    succeeded = 0
    rounds = 100
    for round_index in range(rounds):
        for case in _fk_cases(round_index):
            attempted += 1
            try:
                with scope_module.scoped_connection(conn, seeded.scope_b, commit_on_exit=True):
                    conn.execute(case.sql, case.params)
            except psycopg.errors.ForeignKeyViolation as exc:
                assert exc.diag.sqlstate == "23503"
                assert exc.diag.constraint_name == case.constraint, (
                    f"{case.constraint} 未命中，实际 {exc.diag.constraint_name}: {exc.diag.message_primary}"
                )
                rejected += 1
            else:
                succeeded += 1

    expected_edges = 7
    assert attempted == rounds * expected_edges
    assert succeeded == 0, f"{succeeded} 次跨空间引用被写入"
    assert rejected == attempted, f"仅 {rejected}/{attempted} 被复合 FK 拒绝"
    print(
        f"[composite-fk] rounds={rounds} edges={expected_edges} attempts={attempted} "
        f"rejected_23503={rejected} written={succeeded}"
    )


# ---------------------------------------------------------------------------
# 6. 100 轮跨空间篡改：越权命中 0、可写 0
# ---------------------------------------------------------------------------
def test_cross_workspace_tampering_yields_zero_hits_and_zero_writes(
    seeded: Seed, app_conn: psycopg.Connection[Any], pg_dsn: str
) -> None:
    conn = app_conn
    # 正向对照：本空间内同一条记录可读可写（证明后面的 0 不是「一律无用」）。
    with scope_module.scoped_connection(conn, seeded.scope_a):
        assert conn.execute(
            "SELECT count(*) FROM p0_t03.phase0_sources WHERE source_id = %s", (SOURCE_A,)
        ).fetchone() == (1,)
        updated = conn.execute(
            "UPDATE p0_t03.phase0_sources SET row_version = row_version + 1 "
            "WHERE source_id = %s AND workspace_id = %s",
            (SOURCE_A, WS_A),
        ).rowcount
        assert updated == 1

    attackers = (
        (
            seeded.scope_b,
            WS_B,
            SOURCE_B,
            POLICY_B,
            {"source": SOURCE_A, "kb": KB_A, "binding": BINDING_A,
             "job": JOB_A, "principal": PRINCIPAL_A},
        ),
        (
            seeded.scope_a,
            WS_A,
            SOURCE_A,
            POLICY_A,
            {"source": SOURCE_B, "kb": KB_B, "binding": BINDING_B,
             "job": JOB_B, "principal": PRINCIPAL_B},
        ),
    )
    read_targets = (
        ("phase0_sources", "source_id", "source"),
        ("phase0_knowledge_bases", "kb_id", "kb"),
        ("phase0_execution_bindings", "execution_binding_id", "binding"),
        ("phase0_jobs", "job_id", "job"),
        ("phase0_principals", "principal_id", "principal"),
    )

    reads_attempted = 0
    writes_attempted = 0
    unauthorized_hits = 0
    unauthorized_writes = 0
    rounds = 100

    for round_index in range(rounds):
        attacker_scope, attacker_ws, attacker_source, attacker_policy, victim = attackers[
            round_index % len(attackers)
        ]
        for table, column, key in read_targets:
            reads_attempted += 1
            with scope_module.scoped_connection(conn, attacker_scope):
                hit = conn.execute(
                    f"SELECT count(*) FROM p0_t03.{table} WHERE {column} = %s",
                    (victim[key],),
                ).fetchone()
            assert hit is not None
            if int(hit[0]) != 0:
                unauthorized_hits += 1

        update_specs = (
            (
                "UPDATE p0_t03.phase0_sources SET canonical_url = canonical_url || '-hacked' "
                "WHERE source_id = %s",
                (victim["source"],),
            ),
            (
                "UPDATE p0_t03.phase0_knowledge_bases SET name = 'hacked' WHERE kb_id = %s",
                (victim["kb"],),
            ),
            (
                "UPDATE p0_t03.phase0_execution_bindings SET operation = 'pwn.op' "
                "WHERE execution_binding_id = %s",
                (victim["binding"],),
            ),
            (
                "UPDATE p0_t03.phase0_jobs SET status = 'succeeded' WHERE job_id = %s",
                (victim["job"],),
            ),
            (
                "DELETE FROM p0_t03.phase0_sources WHERE source_id = %s",
                (victim["source"],),
            ),
        )
        for sql, params in update_specs:
            writes_attempted += 1
            with scope_module.scoped_connection(conn, attacker_scope):
                affected = conn.execute(sql, params).rowcount
            if affected:
                unauthorized_writes += 1

        # 篡改 Job binding：attacker 空间 + victim 的 binding。
        writes_attempted += 1
        try:
            with scope_module.scoped_connection(conn, attacker_scope, commit_on_exit=True):
                conn.execute(
                    "INSERT INTO p0_t03.phase0_jobs "
                    "(workspace_id, job_id, execution_binding_id, source_id, kind, dedup_key) "
                    "VALUES (%s, %s, %s, %s, 'fetch', %s)",
                    (
                        attacker_ws,
                        uuid4(),
                        victim["binding"],
                        attacker_source,
                        f"tamper-job-{round_index}",
                    ),
                )
        except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InsufficientPrivilege):
            pass
        else:
            unauthorized_writes += 1

        # 篡改 execution binding：attacker 空间 + victim 的 principal。
        writes_attempted += 1
        try:
            with scope_module.scoped_connection(conn, attacker_scope, commit_on_exit=True):
                conn.execute(
                    "INSERT INTO p0_t03.phase0_execution_bindings "
                    "(workspace_id, execution_binding_id, principal_id, policy_snapshot_id, "
                    " operation, resource_scope_hash, binding_hash, created_at) "
                    "VALUES (%s, %s, %s, %s, 'source.fetch', %s, %s, now())",
                    (
                        attacker_ws,
                        uuid4(),
                        victim["principal"],
                        attacker_policy,
                        ZERO_HASH,
                        ZERO_HASH,
                    ),
                )
        except (psycopg.errors.ForeignKeyViolation, psycopg.errors.InsufficientPrivilege):
            pass
        else:
            unauthorized_writes += 1

    assert reads_attempted == rounds * len(read_targets)
    assert writes_attempted == rounds * 7
    assert unauthorized_hits == 0, f"跨空间读取命中 {unauthorized_hits} 次"
    assert unauthorized_writes == 0, f"跨空间写入成功 {unauthorized_writes} 次"
    print(
        f"[tamper] rounds={rounds} reads={reads_attempted} writes={writes_attempted} "
        f"unauthorized_hits={unauthorized_hits} unauthorized_writes={unauthorized_writes}"
    )

    # victim 行的关键字段确实未被改写。
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        assert admin.execute(
            "SELECT canonical_url FROM p0_t03.phase0_sources WHERE source_id = %s", (SOURCE_A,)
        ).fetchone() == ("https://a.example/doc",)
        assert admin.execute(
            "SELECT name FROM p0_t03.phase0_knowledge_bases WHERE kb_id = %s", (KB_A,)
        ).fetchone() == (f"kb-{KB_A}",)
        assert admin.execute(
            "SELECT operation FROM p0_t03.phase0_execution_bindings "
            "WHERE execution_binding_id = %s",
            (BINDING_A,),
        ).fetchone() == ("source.fetch",)
        assert admin.execute(
            "SELECT status FROM p0_t03.phase0_jobs WHERE job_id = %s", (JOB_A,)
        ).fetchone() == ("pending",)


# ---------------------------------------------------------------------------
# 7. membership 撤销：同一连接立即失效（DB 兜底 + 重授权）
# ---------------------------------------------------------------------------
def test_membership_revocation_denies_on_same_connection(
    seeded: Seed, app_conn: psycopg.Connection[Any], pg_dsn: str
) -> None:
    conn = app_conn
    context = scope_module.authorize_job(conn, JOB_A)
    assert context.principal_role == "owner"
    with scope_module.scoped_connection(conn, context.scope):
        assert _count(conn, "phase0_sources") == 1

    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE p0_t03.phase0_principals SET status = 'disabled', "
            "membership_revision = membership_revision + 1, updated_at = now() "
            "WHERE workspace_id = %s AND principal_id = %s",
            (WS_A, PRINCIPAL_A),
        )

    # (1) C-06：恢复/领取/每个敏感节点都必须重新读取当前 Principal → 同一连接立即拒绝。
    with pytest.raises(contracts.PrincipalInactiveError):
        scope_module.authorize_job(conn, JOB_A)

    # (2) 复用撤销前签发的 scope：RLS 兜底也不放行（读 0 行，写被 42501 拒绝）。
    with scope_module.scoped_connection(conn, context.scope):
        assert _count(conn, "phase0_sources") == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO p0_t03.phase0_sources "
                "(workspace_id, source_id, canonical_url, canonical_hash, source_type) "
                "VALUES (%s, %s, %s, %s, 'url')",
                (WS_A, uuid4(), "https://evil.example/x", _sha256("evil")),
            )


# ---------------------------------------------------------------------------
# 8. 无 Run Job 的身份传播：只信 DB binding + 当前 Principal
# ---------------------------------------------------------------------------
def test_authorize_job_without_run_reads_current_principal(
    seeded: Seed, app_conn: psycopg.Connection[Any], pg_dsn: str
) -> None:
    conn = app_conn
    first = scope_module.authorize_job(conn, JOB_B)
    assert first.run_id is None, "本用例覆盖的是无 Run 的 fetch/ingest Job"
    assert first.workspace_id == WS_B
    assert first.principal_id == PRINCIPAL_B
    assert first.execution_binding_id == BINDING_B
    assert first.scope.policy_snapshot_id == POLICY_B
    assert (first.principal_role, first.membership_revision) == ("member", 1)

    # 队列消息自报 role=owner：不参与授权，只记录为 ignored。
    claimed = scope_module.authorize_job(
        conn, JOB_B, payload={"job_id": str(JOB_B), "role": "owner"}
    )
    assert (claimed.principal_role, claimed.membership_revision) == ("member", 1)
    assert "role" in claimed.ignored_payload_fields

    # 消息里的 workspace / principal / job_id 与 DB 事实冲突 → fail closed。
    with pytest.raises(scope_module.PayloadClaimMismatchError):
        scope_module.authorize_job(
            conn, JOB_B, payload={"job_id": str(JOB_B), "workspace_id": str(WS_A)}
        )
    with pytest.raises(scope_module.PayloadClaimMismatchError):
        scope_module.authorize_job(
            conn, JOB_B, payload={"job_id": str(JOB_B), "principal_id": str(PRINCIPAL_A)}
        )
    with pytest.raises(scope_module.PayloadClaimMismatchError):
        scope_module.authorize_job(conn, JOB_B, payload={"job_id": str(JOB_A)})
    with pytest.raises(scope_module.JobNotFoundError):
        scope_module.authorize_job(conn, uuid4())

    # 管理员侧改角色 + 递增 membership_revision：同一连接下一次授权读到**当前**值。
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(
            "UPDATE p0_t03.phase0_principals SET role = 'owner', "
            "membership_revision = membership_revision + 1, updated_at = now() "
            "WHERE workspace_id = %s AND principal_id = %s",
            (WS_B, PRINCIPAL_B),
        )
    after = scope_module.authorize_job(conn, JOB_B)
    assert (after.principal_role, after.membership_revision) == ("owner", 2)
    # 旧身份没有被缓存进 payload/绑定：ignored 列表为空。
    assert after.ignored_payload_fields == ()


def test_expired_binding_and_tampered_row_fail_closed(
    seeded: Seed, app_conn: psycopg.Connection[Any]
) -> None:
    with pytest.raises(contracts.BindingExpiredError):
        scope_module.authorize_job(app_conn, JOB_EXPIRED)
    with pytest.raises(scope_module.BindingIntegrityError):
        scope_module.authorize_job(app_conn, JOB_TAMPER)


# ---------------------------------------------------------------------------
# 9. 空库重建（必须最后执行：会 DROP SCHEMA p0_t03 与 p0_app）
# ---------------------------------------------------------------------------
def test_empty_database_rebuild(pg_dsn: str, migration: scope_module.MigrationStamp) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        assert scope_module.reset_database(admin) is True
        assert admin.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'p0_t03'"
        ).fetchone() is None
        assert admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'p0_app'"
        ).fetchone() is None

    with psycopg.connect(pg_dsn) as conn:
        rebuilt = scope_module.apply_migration(conn)
        conn.commit()
    assert rebuilt.sha256 == migration.sha256 == scope_module.migration_sha256()

    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        tables = {
            row[0]
            for row in admin.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'p0_t03' AND c.relkind = 'r'"
            )
        }
        assert tables == set(RLS_TABLES) | {"phase0_schema_migrations"}
        assert admin.execute(
            "SELECT count(*) FROM pg_policies WHERE schemaname = 'p0_t03'"
        ).fetchone() == (8,)
        assert admin.execute(
            "SELECT count(*) FROM p0_t03.phase0_schema_migrations"
        ).fetchone() == (1,)
        assert scope_module.role_security_attributes(admin)["rolbypassrls"] is False
