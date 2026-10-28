"""P0-T13 真实验收：原子发布、可见性切换、旧 worker 防护、回滚与故障注入。

全部用例打真实 PostgreSQL（schema ``p0_t13`` 发布事实 + ``p0_t12`` 版本/chunk），
没有 mock、没有 skip/xfail。故障注入用 ``faults.FaultBarrier`` 的同步点，不用固定
sleep 猜时序；唯一处 sleep 是 ``test_statement_timeout_*`` 里用来**制造**超时的
``pg_sleep``（被测的正是超时本身）。

对照验收（P0-T13 卡 + ``docs/poc/05`` PoC-03）
------------------------------------------------
1. 发布前不可见 —— ``test_staging_complete_but_invisible_until_publish``。
2. 单事务原子切换 + 故障后可见版本不变 ——
   ``test_publish_switches_pointer_and_visibility_in_one_commit``、
   ``test_readers_never_see_partial_switch_during_hold``、
   ``test_crash_before_commit_leaves_visible_version_unchanged``、
   ``test_backend_kill_mid_transaction_leaves_no_partial_switch``、
   ``test_statement_timeout_in_transaction_rolls_back_switch``。
3. 并发/旧 epoch —— ``test_concurrent_publishers_only_one_wins``、
   ``test_old_attempt_write_after_switch_is_rejected``、
   ``test_late_old_version_publication_is_rejected``。
4. 幂等（不二次切指针、不二次投递 Job）——
   ``test_duplicate_publish_is_idempotent_no_second_switch_no_second_job``。
5. 回滚 —— ``test_rollback_republishes_history_as_new_version``、
   ``test_repeated_publish_rollback_cycles``。
6. 契约层 —— ``test_migration_008_shape_ledger_and_reapply``、
   ``test_publication_facts_are_immutable_and_jobs_require_publication``、
   ``test_manifest_tamper_blocks_publish_without_switch``、
   ``test_rejected_attempt_audit_is_persisted_when_committed``。
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import index_publication as ip
import index_staging as ji
import psycopg
import pytest
from faults import FaultBarrier
from parsing_assets import build_md_simple
from psycopg.pq import TransactionStatus
from simple_reader import read_document

REQUIRED_TABLES = (
    "phase0_schema_migrations",
    "phase0_publications",
    "phase0_publication_jobs",
    "phase0_publication_events",
)

STATUS_READY = "ready"


# ---------------------------------------------------------------------------
# fixtures（conftest.py 属他人文件，DB fixture 必须写在这里）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def installed() -> Iterator[dict[str, str]]:
    conn = ip.connect()
    try:
        ip.reset_schema(conn)
        staging = ip.ensure_staging_schema(conn)
        digest = ip.apply_migration(conn)
        conn.commit()
    finally:
        conn.close()
    assert len(digest) == 64
    yield {"digest": digest, "staging": staging}
    conn = ip.connect()
    try:
        ip.reset_schema(conn)
        if staging == "applied":
            ji.reset_schema(conn)
    finally:
        conn.close()


@pytest.fixture()
def conn(installed: dict[str, str]) -> Iterator[psycopg.Connection]:
    connection = ip.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def store() -> ji.InMemoryObjectStore:
    return ji.InMemoryObjectStore()


@pytest.fixture()
def workspace() -> UUID:
    return uuid4()


@pytest.fixture()
def barrier() -> FaultBarrier:
    return FaultBarrier()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _content() -> bytes:
    return build_md_simple("Publication Doc", ["alpha", "beta", "gamma"])


def _parsed() -> Any:
    parsed = read_document(_content(), file_name="doc.md")
    assert parsed.ok, parsed.error
    return parsed


def _texts(tag: str) -> list[str]:
    return [f"{tag} alpha", f"{tag} beta", f"{tag} gamma"]


def _chunks(parsed: Any, texts: list[str]) -> list[ji.ChunkInput]:
    blocks = [block for block in parsed.blocks if block.text.strip()]
    assert blocks
    chunks: list[ji.ChunkInput] = []
    for index, text in enumerate(texts):
        block = blocks[index % len(blocks)]
        chunks.append(ji.ChunkInput(kind=block.kind, text=text, locator=block.locator))
    return chunks


def _new_document(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
) -> UUID:
    source = ji.create_source(
        conn,
        workspace_id=workspace,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content=_content(),
        file_name="doc.md",
        format="md",
        store=store,
    )
    document_id = uuid4()
    ji.ensure_document(conn, document_id=document_id, source=source, desired_generation=1)
    conn.commit()
    return document_id


def _make_ready(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    document_id: UUID,
    generation: int,
    texts: list[str],
) -> ji.VersionState:
    """staging → ready（T12 API，只读复用），并提交。"""

    attempt = ji.begin_attempt(
        conn, workspace_id=workspace, document_id=document_id, generation=generation
    )
    ji.stage_chunk_batch(conn, attempt, _chunks(_parsed(), texts))
    manifest = ji.build_manifest(conn, attempt, store=store)
    record = ji.write_manifest(conn, attempt, manifest)
    assert record.status == STATUS_READY, record.missing_items
    version = ji.mark_version_ready(conn, attempt)
    conn.commit()
    return version


def _begin(conn: psycopg.Connection) -> psycopg.Connection:
    """显式开启事务边界（psycopg 惰性 BEGIN）。

    发布路径要求「调用方已编排事务」（避免不在事务里的伪原子），所以测试也必须
    真的先开一个事务，而不是靠 publish 内部偷偷开始。
    """

    if conn.info.transaction_status is TransactionStatus.IDLE:
        conn.execute("SELECT 1")
    assert conn.info.transaction_status is TransactionStatus.INTRANS
    return conn


def _publish(
    conn: psycopg.Connection,
    workspace: UUID,
    document_id: UUID,
    version: int,
    key: str,
    **kwargs: Any,
) -> ip.PublicationOutcome:
    request = ip.PublicationRequest(
        workspace_id=workspace,
        document_id=document_id,
        index_version=version,
        idempotency_key=key,
    )
    _begin(conn)
    return ip.publish(conn, request, **kwargs)


def _commit_publish(
    conn: psycopg.Connection,
    workspace: UUID,
    document_id: UUID,
    version: int,
    key: str,
    **kwargs: Any,
) -> ip.PublicationOutcome:
    outcome = _publish(conn, workspace, document_id, version, key, **kwargs)
    conn.commit()
    return outcome


def _visible_checksums(conn: psycopg.Connection, workspace: UUID) -> list[str]:
    return sorted(str(row["checksum"]) for row in ip.read_visibility(conn, workspace))


def _version_checksums(
    conn: psycopg.Connection, workspace: UUID, document_id: UUID, version: int
) -> list[str]:
    rows = ji.list_chunks(conn, workspace, document_id, version)
    return sorted(str(row["checksum"]) for row in rows)


def _version_status(
    conn: psycopg.Connection, workspace: UUID, document_id: UUID, version: int
) -> str:
    return ji.get_version(conn, workspace, document_id, version).status


def _backend_pid(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
    return int(row["pid"])


def _wait_until(predicate: Any, timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    """限期等待一个**事实**成立（不是固定 sleep 猜时序）；超时返回 False。"""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


@dataclass
class _Worker:
    """后台发布线程的结果盒（异常/结果/后端 pid 都留证据）。"""

    target: Any
    outcome: ip.PublicationOutcome | None = None
    error: BaseException | None = None
    pid: int | None = None
    commit_on_success: bool = True
    started: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="publication-worker", daemon=True)
        thread.start()
        assert self.started.wait(5.0), "worker 未在 5s 内启动"
        return thread

    def _run(self) -> None:
        connection = ip.connect()
        try:
            self.pid = _backend_pid(connection)
            self.started.set()
            self.outcome = self.target(connection)
            if self.commit_on_success:
                connection.commit()
        except BaseException as exc:  # noqa: BLE001 - 证据需要保留任意异常
            self.error = exc
        finally:
            self.finished.set()
            with contextlib.suppress(Exception):  # 连接可能已被 terminate
                connection.close()


def _thread(conn: psycopg.Connection, fn: Any) -> _Worker:
    """把 ``fn(conn)`` 放进后台线程；``conn`` 只用于声明依赖顺序（不跨线程共享）。"""

    assert conn is not None
    worker = _Worker(target=fn)
    return worker


# ---------------------------------------------------------------------------
# 契约层：迁移 008
# ---------------------------------------------------------------------------
def test_migration_008_shape_ledger_and_reapply(
    conn: psycopg.Connection, installed: dict[str, str]
) -> None:
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (ip.SCHEMA,),
        ).fetchall()
    }
    assert set(REQUIRED_TABLES) <= tables

    constraints = {
        row["conname"]
        for row in conn.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = %s::regnamespace",
            (ip.SCHEMA,),
        ).fetchall()
    }
    for name in (
        "phase0_publications_idem_key",
        "phase0_publications_target_key",
        "phase0_publications_kind_shape",
        "phase0_publication_jobs_dedup_key",
        "phase0_publication_jobs_publication_fk",
        "phase0_publication_events_publication_fk",
    ):
        assert name in constraints, name

    ledger = conn.execute(
        f"SELECT filename, sha256 FROM {ip.SCHEMA}.phase0_schema_migrations "
        "WHERE migration_id = %s",
        (ip.MIGRATION_ID,),
    ).fetchone()
    assert ledger is not None
    assert ledger["filename"] == ip.MIGRATION_PATH.name
    assert ledger["sha256"] == installed["digest"]
    assert ledger["sha256"] == ip.migration_sha256()

    conn.commit()  # 上面几条 SELECT 已开事务；apply_migration 要求 IDLE
    with pytest.raises(psycopg.errors.DuplicateSchema) as excinfo:
        ip.apply_migration(conn)
    assert ip.SCHEMA in str(excinfo.value)  # 显式失败，不静默跳过
    conn.rollback()
    count = conn.execute(
        f"SELECT count(*) AS n FROM {ip.SCHEMA}.phase0_schema_migrations"
    ).fetchone()
    assert count == {"n": 1}


def test_publication_facts_are_immutable_and_jobs_require_publication(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    outcome = _commit_publish(conn, workspace, document_id, 1, "k-immutable")

    # 发布事实不可变
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"UPDATE {ip.SCHEMA}.phase0_publications SET chunk_count = 0 "
            "WHERE workspace_id = %s AND publication_id = %s",
            (workspace, outcome.publication_id),
        )
    assert "只允许追加写" in str(excinfo.value)
    conn.rollback()

    # 审计不可变
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {ip.SCHEMA}.phase0_publication_events SET outcome = 'committed' "
            "WHERE workspace_id = %s",
            (workspace,),
        )
    conn.rollback()

    # Job 不能脱离发布事实存在（外键 + 同事务插入）
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            f"INSERT INTO {ip.SCHEMA}.phase0_publication_jobs ("
            "    workspace_id, job_id, publication_id, kind, dedup_key) "
            "VALUES (%s, %s, %s, 'index.enrichment', %s)",
            (workspace, uuid4(), uuid4(), f"orphan:{uuid4()}"),
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# 验收 1：发布前不可见
# ---------------------------------------------------------------------------
def test_staging_complete_but_invisible_until_publish(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    version = _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    assert version.status == STATUS_READY
    assert version.chunk_count == 3

    # 写全了、ready 了，但可见性必须为 0（staging 阶段不可见）
    assert ip.read_visibility(conn, workspace) == []
    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version is None
    assert state.visible_checksums == ()

    _commit_publish(conn, workspace, document_id, 1, "k-invisible")
    visible = ip.read_visibility(conn, workspace)
    assert len(visible) == 3
    assert {int(row["index_version"]) for row in visible} == {1}
    assert _visible_checksums(conn, workspace) == _version_checksums(
        conn, workspace, document_id, 1
    )


# ---------------------------------------------------------------------------
# 验收 2：单事务原子切换
# ---------------------------------------------------------------------------
def test_publish_switches_pointer_and_visibility_in_one_commit(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    first = _commit_publish(conn, workspace, document_id, 1, "k-v1")
    assert first.active_version == 1
    assert first.visible_chunk_count == 3
    assert first.job_created is True

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    pre_v2 = ip.publication_state(conn, workspace, document_id).document_row_version
    # 还没发布：读者仍只看 v1
    assert _visible_checksums(conn, workspace) == _version_checksums(
        conn, workspace, document_id, 1
    )

    second = _commit_publish(conn, workspace, document_id, 2, "k-v2")
    assert second.active_version == 2
    assert second.from_version == 1
    assert _visible_checksums(conn, workspace) == _version_checksums(
        conn, workspace, document_id, 2
    )
    assert _version_status(conn, workspace, document_id, 1) == "retired"
    assert _version_status(conn, workspace, document_id, 2) == "active"
    # 一次切换只让 documents.row_version +1（bump_generation 那一步单独 +1）
    assert second.document_row_version == pre_v2 + 1

    publications = ip.list_publications(conn, workspace, document_id)
    assert [(row["kind"], row["to_version"], row["from_version"]) for row in publications] == [
        ("publish", 1, None),
        ("publish", 2, 1),
    ]
    assert len(ip.list_jobs(conn, workspace, document_id)) == 2


def test_readers_never_see_partial_switch_during_hold(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    barrier: FaultBarrier,
) -> None:
    """切换语句已执行、事务未提交时，用独立连接做 120 次读采样：只见 v1 全部。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-read-v1")
    v1_checksums = _version_checksums(conn, workspace, document_id, 1)

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    v2_checksums = _version_checksums(conn, workspace, document_id, 2)
    assert v1_checksums != v2_checksums

    barrier.arm("after_activation_before_commit")
    worker = _Worker(
        target=lambda worker_conn: _publish(
            worker_conn, workspace, document_id, 2, "k-read-v2", barrier=barrier
        )
    )
    thread = worker.start()
    try:
        assert barrier.wait_armed("after_activation_before_commit", timeout=5.0)
        reader = ip.connect()
        try:
            samples = []
            for _ in range(120):
                rows = ip.read_visibility(reader, workspace)
                samples.append(sorted(str(row["checksum"]) for row in rows))
        finally:
            reader.close()
    finally:
        barrier.release("after_activation_before_commit")
        thread.join(10.0)

    assert worker.error is None, worker.error
    assert all(sample == v1_checksums for sample in samples), "采样期出现了混版本或空集"
    assert worker.outcome is not None and worker.outcome.visible_chunk_count == 3
    # 采样器关闭后提交才生效（worker 的 publish 不提交，这里由本连接补）
    conn.commit()
    assert _visible_checksums(conn, workspace) == v2_checksums


def test_hundred_rounds_of_contended_reads_never_see_mixed_versions(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    barrier: FaultBarrier,
) -> None:
    """T13 卡的量化验收：100 轮竞态，每轮 ≥100 次读取，混版本/半成品/跨空间命中 0。

    每一轮都让发布者在 ``after_activation_before_commit`` 停住（切换已写、未提交），
    此时独立连接做 100 次读，必须**只看到上一版**全部 chunk；同轮的另一个空间必须
    一条都读不到（跨空间命中 0）。轮次间的提交才让新版生效。
    """

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-hundred-v1")
    active_checksums = _version_checksums(conn, workspace, document_id, 1)
    foreign_workspace = uuid4()
    samples = 0

    for round_no in range(1, 101):
        ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
        conn.commit()
        generation = ip.publication_state(conn, workspace, document_id).desired_generation
        _make_ready(conn, store, workspace, document_id, generation, _texts(f"h{round_no}"))
        key = f"k-hundred-{round_no}"

        barrier.reset()
        barrier.arm("after_activation_before_commit", timeout=20.0)
        worker = _Worker(
            target=lambda worker_conn, version=generation, idem=key: _publish(
                worker_conn, workspace, document_id, version, idem, barrier=barrier
            )
        )
        thread = worker.start()
        try:
            assert barrier.wait_armed("after_activation_before_commit", timeout=10.0), (
                f"第 {round_no} 轮发布者没有到达切换后的同步点"
            )
            reader = ip.connect()
            try:
                for _ in range(100):
                    rows = ip.read_visibility(reader, workspace)
                    got = sorted(str(row["checksum"]) for row in rows)
                    assert got == active_checksums, (
                        f"第 {round_no} 轮读到混版本/半成品：{len(got)} 行"
                    )
                    assert {row["document_id"] for row in rows} == {document_id}
                    samples += 1
                assert ip.read_visibility(reader, foreign_workspace) == []
            finally:
                reader.close()
        finally:
            barrier.release("after_activation_before_commit")
            thread.join(20.0)

        assert worker.error is None, worker.error
        active_checksums = _version_checksums(conn, workspace, document_id, generation)

    assert samples == 10_000
    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version == 101
    assert state.visible_checksums == tuple(active_checksums)
    assert state.publication_count == 101  # 100 轮各一次 + 首发一次，没有重复事实


def test_crash_before_commit_leaves_visible_version_unchanged(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    barrier: FaultBarrier,
) -> None:
    """模拟「切换已执行、提交前崩溃」：事务回滚后指针/可见性/事实全部不变。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-crash-v1")

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    # 失败注入前的基线（bump_generation 那一步已按设计推进过 row_version）
    before = ip.publication_state(conn, workspace, document_id)

    barrier.arm("after_activation_before_commit")
    holder: dict[str, Any] = {}

    def crashing_publish(worker_conn: psycopg.Connection) -> ip.PublicationOutcome:
        outcome = _publish(worker_conn, workspace, document_id, 2, "k-crash-v2",
                           barrier=barrier)
        holder["outcome"] = outcome
        raise _SimulatedCrash("发布路径在提交前崩溃")

    worker = _Worker(target=crashing_publish)
    thread = worker.start()
    try:
        assert barrier.wait_armed("after_activation_before_commit", timeout=5.0)
        # 提交前：独立连接看不到任何中间态
        observer = ip.connect()
        try:
            during = ip.publication_state(observer, workspace, document_id)
        finally:
            observer.close()
        assert during.active_version == 1
        assert during.visible_checksums == before.visible_checksums
        assert during.publication_count == 1
    finally:
        barrier.release("after_activation_before_commit")
        thread.join(10.0)

    assert isinstance(worker.error, _SimulatedCrash)
    after = ip.publication_state(conn, workspace, document_id)
    assert after.active_version == 1
    assert after.document_row_version == before.document_row_version
    assert after.visible_checksums == before.visible_checksums
    assert after.publication_count == 1
    assert after.job_count == 1
    assert _version_status(conn, workspace, document_id, 2) == STATUS_READY


def test_backend_kill_mid_transaction_leaves_no_partial_switch(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    barrier: FaultBarrier,
) -> None:
    """切换语句执行后、提交前被 ``pg_terminate_backend`` 杀死：事务整体消失。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-kill-v1")

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    before = ip.publication_state(conn, workspace, document_id)

    barrier.arm("after_activation_before_commit")
    worker = _Worker(
        target=lambda worker_conn: _publish(
            worker_conn, workspace, document_id, 2, "k-kill-v2", barrier=barrier
        )
    )
    thread = worker.start()
    try:
        assert barrier.wait_armed("after_activation_before_commit", timeout=5.0)
        assert worker.pid is not None
        terminated = conn.execute(
            "SELECT pg_terminate_backend(%s) AS ok", (worker.pid,)
        ).fetchone()
        assert terminated["ok"] is True
        conn.commit()
    finally:
        barrier.release("after_activation_before_commit")
        thread.join(10.0)

    assert worker.error is not None, "被 terminate 后不应静默成功"
    assert "terminat" in str(worker.error).lower() or "connection" in str(worker.error).lower()
    after = ip.publication_state(conn, workspace, document_id)
    assert after.active_version == 1
    assert after.document_row_version == before.document_row_version
    assert after.visible_checksums == before.visible_checksums
    assert after.publication_count == 1
    assert after.job_count == 1
    assert _version_status(conn, workspace, document_id, 2) == STATUS_READY


def test_statement_timeout_in_transaction_rolls_back_switch(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """切换已写入事务、提交前语句超时：整笔回滚，可见版本不变。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-timeout-v1")

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    before = ip.publication_state(conn, workspace, document_id)

    conn.execute("SET LOCAL statement_timeout = '80ms'")
    _publish(conn, workspace, document_id, 2, "k-timeout-v2")
    with pytest.raises(psycopg.errors.QueryCanceled):
        conn.execute("SELECT pg_sleep(0.3)")
    conn.rollback()

    after = ip.publication_state(conn, workspace, document_id)
    assert after.active_version == 1
    assert after.document_row_version == before.document_row_version
    assert after.visible_checksums == before.visible_checksums
    assert after.publication_count == 1
    assert _version_status(conn, workspace, document_id, 2) == STATUS_READY


# ---------------------------------------------------------------------------
# 验收 4：幂等
# ---------------------------------------------------------------------------
def test_duplicate_publish_is_idempotent_no_second_switch_no_second_job(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    first = _commit_publish(conn, workspace, document_id, 1, "k-idem")
    assert first.job_created is True

    replay = _publish(conn, workspace, document_id, 1, "k-idem")
    conn.commit()
    assert replay.replayed is True
    assert replay.publication_id == first.publication_id
    assert replay.job_id == first.job_id
    assert replay.job_created is False
    assert replay.document_row_version == first.document_row_version  # 指针没有再动

    publications = ip.list_publications(conn, workspace, document_id)
    jobs = ip.list_jobs(conn, workspace, document_id)
    assert len(publications) == 1
    assert len(jobs) == 1
    outcomes = [str(row["outcome"]) for row in ip.list_events(conn, workspace, document_id)]
    assert outcomes.count("committed") == 1
    assert outcomes.count("idempotent_replay") == 1

    # 同一 key 用于不同版本 → 明确拒绝，不静默复用
    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    with pytest.raises(ip.IdempotencyConflict):
        _publish(conn, workspace, document_id, 2, "k-idem")
    conn.rollback()
    assert ip.publication_state(conn, workspace, document_id).active_version == 1


# ---------------------------------------------------------------------------
# 验收 3：并发与旧 worker
# ---------------------------------------------------------------------------
def test_concurrent_publishers_only_one_wins(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    barrier: FaultBarrier,
) -> None:
    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-race-v1")
    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    before = ip.publication_state(conn, workspace, document_id)
    v2_checksums = _version_checksums(conn, workspace, document_id, 2)

    barrier.arm("after_activation_before_commit")
    winner = _Worker(
        target=lambda worker_conn: _publish(
            worker_conn, workspace, document_id, 2, "k-race-a", barrier=barrier
        )
    )
    second = _Worker(
        target=lambda worker_conn: _publish(worker_conn, workspace, document_id, 2, "k-race-b")
    )

    winner_thread = winner.start()
    second_thread: threading.Thread | None = None
    try:
        assert barrier.wait_armed("after_activation_before_commit", timeout=5.0)
        second_thread = second.start()
        # B 必须真的阻塞在 documents 行锁上（按 pid 查 wait_event，不猜时延）
        assert _wait_until(
            lambda: _is_blocked(conn, second.pid), timeout_s=5.0
        ), "第二个发布者没有阻塞在行锁上"
        assert second.outcome is None
    finally:
        barrier.release("after_activation_before_commit")
        winner_thread.join(10.0)
        if second_thread is not None:
            second_thread.join(10.0)

    assert winner.error is None, winner.error
    assert second.error is None, second.error
    assert winner.outcome is not None
    assert second.outcome is not None
    assert winner.outcome.replayed is False
    assert second.outcome.replayed is True
    assert second.outcome.publication_id == winner.outcome.publication_id
    assert second.outcome.document_row_version == winner.outcome.document_row_version

    after = ip.publication_state(conn, workspace, document_id)
    assert after.active_version == 2
    assert len(after.versions) == 2
    assert after.publication_count == 2  # v1 与 v2 各一次，没有第二次切换
    assert after.job_count == 2
    assert after.document_row_version == before.document_row_version + 1
    assert after.visible_checksums == tuple(v2_checksums)


def _is_blocked(conn: psycopg.Connection, pid: int | None) -> bool:
    """该后端此刻是否在等锁（并发证据，不依赖时延猜测）。"""

    if pid is None:
        return False
    row = conn.execute(
        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
    ).fetchone()
    return row is not None and row["wait_event_type"] == "Lock"


def test_old_attempt_write_after_switch_is_rejected(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """切换后旧 attempt 再写：沿用 T12 的 STALE_ATTEMPT，不得静默成功。"""

    document_id = _new_document(conn, store, workspace)
    version1 = _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-stale-v1")
    attempt1 = ji.get_attempt(conn, workspace, version1.winning_attempt_id or uuid4())

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    version2 = _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    _commit_publish(conn, workspace, document_id, 2, "k-stale-v2")
    attempt2 = ji.get_attempt(conn, workspace, version2.winning_attempt_id or uuid4())

    chunks = _chunks(_parsed(), ["late alpha", "late beta", "late gamma"])
    for attempt in (attempt1, attempt2):
        with pytest.raises(ji.StaleAttemptError) as excinfo:
            ji.stage_chunk_batch(conn, attempt, chunks)
        assert excinfo.value.code == "STALE_ATTEMPT"
        conn.rollback()

    assert len(ji.list_chunks(conn, workspace, document_id, 1)) == 3
    assert len(ji.list_chunks(conn, workspace, document_id, 2)) == 3
    assert ip.publication_state(conn, workspace, document_id).active_version == 2


def test_late_old_version_publication_is_rejected(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """v1 晚到不得覆盖 v2：desired_generation 已推进 → STALE_GENERATION。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-late-v1")
    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    _commit_publish(conn, workspace, document_id, 2, "k-late-v2")
    v2_checksums = _version_checksums(conn, workspace, document_id, 2)

    with pytest.raises(ji.StaleGenerationError) as excinfo:
        _publish(conn, workspace, document_id, 1, "k-late-v1-retry")
    assert excinfo.value.code == "STALE_GENERATION"
    conn.commit()  # 提交这笔"被拒绝"的事务，审计才落盘（见下一个用例）

    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version == 2
    assert state.visible_checksums == tuple(v2_checksums)
    assert state.publication_count == 2


def test_rejected_attempt_audit_is_persisted_when_committed(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """stale 不得静默：拒绝原因写进审计（outcome + error_code）。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-audit-v1")
    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()

    with pytest.raises(ji.StaleGenerationError):
        _publish(conn, workspace, document_id, 1, "k-audit-stale")
    conn.commit()

    events = ip.list_events(conn, workspace, document_id)
    stale = [row for row in events if row["outcome"] == "stale_generation"]
    assert len(stale) == 1
    assert stale[0]["error_code"] == "STALE_GENERATION"
    assert "desired=" in str(stale[0]["detail"])


def test_enrichment_job_failure_does_not_change_core_version(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """C-01/C-09：富化 Job 失败不得把已成功的核心版本改成 failed（状态拥有者不同）。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    outcome = _commit_publish(conn, workspace, document_id, 1, "k-enrich")
    assert outcome.job_id is not None

    conn.execute(
        f"UPDATE {ip.SCHEMA}.phase0_publication_jobs "
        "SET status = 'failed', row_version = row_version + 1, updated_at = now() "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, outcome.job_id),
    )
    conn.commit()

    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version == 1
    assert _version_status(conn, workspace, document_id, 1) == "active"
    assert len(state.visible_checksums) == 3
    jobs = ip.list_jobs(conn, workspace, document_id)
    assert [row["status"] for row in jobs] == ["failed"]


# ---------------------------------------------------------------------------
# 验收 5：回滚
# ---------------------------------------------------------------------------
def test_rollback_republishes_history_as_new_version(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-rb-v1")
    v1_checksums = _version_checksums(conn, workspace, document_id, 1)

    ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
    conn.commit()
    _make_ready(conn, store, workspace, document_id, 2, _texts("v2"))
    _commit_publish(conn, workspace, document_id, 2, "k-rb-v2")
    assert _visible_checksums(conn, workspace) == _version_checksums(
        conn, workspace, document_id, 2
    )

    _begin(conn)
    outcome = ip.rollback_committed(
        conn,
        ip.RollbackRequest(
            workspace_id=workspace,
            document_id=document_id,
            target_version=1,
            idempotency_key="k-rb-1",
            reason="上游数据撤回，回到 v1 内容",
            expected_active_version=2,
        ),
        store=store,
    )
    assert outcome.kind == "rollback"
    assert outcome.to_version == 3
    assert outcome.rollback_of_version == 1
    assert outcome.from_version == 2

    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version == 3
    assert state.desired_generation == 3
    assert state.visible_checksums == tuple(v1_checksums)  # 内容回到 v1
    assert _version_status(conn, workspace, document_id, 2) == "retired"
    assert _version_status(conn, workspace, document_id, 3) == "active"
    publications = ip.list_publications(conn, workspace, document_id)
    assert [
        (row["kind"], row["to_version"], row["rollback_of_version"]) for row in publications
    ] == [
        ("publish", 1, None),
        ("publish", 2, None),
        ("rollback", 3, 1),
    ]
    assert len(ip.list_jobs(conn, workspace, document_id)) == 3
    outcomes = [str(row["outcome"]) for row in ip.list_events(conn, workspace, document_id)]
    assert "rolled_back" in outcomes

    # 同一回滚请求重复执行：不产生第二个可见性变化、不再建版本
    _begin(conn)
    replay = ip.rollback_committed(
        conn,
        ip.RollbackRequest(
            workspace_id=workspace,
            document_id=document_id,
            target_version=1,
            idempotency_key="k-rb-1",
            reason="上游数据撤回，回到 v1 内容",
        ),
        store=store,
    )
    assert replay.replayed is True
    assert replay.publication_id == outcome.publication_id
    state2 = ip.publication_state(conn, workspace, document_id)
    assert len(state2.versions) == 3
    assert state2.publication_count == 3
    assert state2.document_row_version == state.document_row_version


def test_repeated_publish_rollback_cycles(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    """连续 5 轮 publish/rollback：每轮可见内容与 active_version 一致，无混版本。"""

    document_id = _new_document(conn, store, workspace)
    _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    _commit_publish(conn, workspace, document_id, 1, "k-cycle-v1")

    for round_no in range(1, 6):
        ji.bump_desired_generation(conn, workspace_id=workspace, document_id=document_id)
        conn.commit()
        generation = ip.publication_state(conn, workspace, document_id).desired_generation
        tag = f"r{round_no}"
        _make_ready(conn, store, workspace, document_id, generation, _texts(tag))
        _commit_publish(conn, workspace, document_id, generation, f"k-cycle-{round_no}")
        state = ip.publication_state(conn, workspace, document_id)
        assert state.active_version == generation
        assert state.visible_checksums == tuple(
            _version_checksums(conn, workspace, document_id, generation)
        )
        assert len(state.visible_checksums) == 3

        # 回滚到上一版内容（新版本行）
        _begin(conn)
        rollback_outcome = ip.rollback_committed(
            conn,
            ip.RollbackRequest(
                workspace_id=workspace,
                document_id=document_id,
                target_version=generation - 1,
                idempotency_key=f"k-cycle-rb-{round_no}",
                reason=f"round {round_no} rollback",
            ),
            store=store,
        )
        assert rollback_outcome.rollback_of_version == generation - 1
        state = ip.publication_state(conn, workspace, document_id)
        assert state.visible_checksums == tuple(
            _version_checksums(conn, workspace, document_id, generation - 1)
        )

    publications = ip.list_publications(conn, workspace, document_id)
    assert len(publications) == 11  # 1 首发 + 5 轮 publish + 5 轮回滚
    assert len(ip.list_jobs(conn, workspace, document_id)) == 11


# ---------------------------------------------------------------------------
# 契约层：manifest 被篡改必须挡住发布
# ---------------------------------------------------------------------------
def test_manifest_tamper_blocks_publish_without_switch(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    document_id = _new_document(conn, store, workspace)
    version = _make_ready(conn, store, workspace, document_id, 1, _texts("v1"))
    assert version.manifest_checksum is not None

    tampered = "f" * 64
    conn.execute(
        f"UPDATE {ji.SCHEMA}.phase0_document_versions "
        "SET manifest_checksum = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
        (tampered, workspace, document_id),
    )
    conn.commit()

    with pytest.raises(ip.ManifestMismatch):
        _publish(conn, workspace, document_id, 1, "k-tamper")
    conn.rollback()

    state = ip.publication_state(conn, workspace, document_id)
    assert state.active_version is None
    assert state.publication_count == 0
    assert state.job_count == 0
    assert state.visible_checksums == ()
    assert _version_status(conn, workspace, document_id, 1) == STATUS_READY


class _SimulatedCrash(Exception):
    """模拟发布路径在提交前崩溃（不是生产错误类型，只用于故障注入）。"""
