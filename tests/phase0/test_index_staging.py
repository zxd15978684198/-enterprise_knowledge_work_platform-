"""P0-T12 真实验收：版本 staging、attempt fencing、chunk/locator/manifest 与可见性。

全部用例打真实 PostgreSQL（schema ``p0_t12``），没有 mock、没有 skip/xfail；
对象存储用**明确标注的假存储**（真实 MinIO/S3 本机不可用 → NOT_RUN），
embedding 用**确定性假向量**（真实模型 NOT_RUN），pgvector 本机不可用（NOT_RUN）。

对照验收
--------
1. ``test_old_attempt_writes_zero_rows`` / ``test_generation_bump_stales_old_attempt``
   —— desired version/generation + attempt fencing：旧 attempt 写当前分区 0 行，
   返回 ``STALE_ATTEMPT`` / ``STALE_GENERATION``；同 version 新 attempt 语义见
   ``test_same_version_new_attempt_resets_staging_and_manifest``。
2. ``test_staging_is_invisible_until_activation`` —— staging 完成（甚至 ready）后
   新检索命中 0；本任务不切 active 指针。
3. ``test_chunk_ordinal_unique_and_repeat_batch_is_idempotent`` /
   ``test_chunk_reference_model_and_locator_schema`` /
   ``test_manifest_requires_every_required_key`` —— 序号唯一、locator 复用 T11
   offset-free schema、source/version 引用 + checksum、manifest 缺项不能 ready。
4. ``test_embedding_is_deterministic_and_dimension_checked`` —— 同输入同输出、
   维度/数量不符拒绝。
5. ``test_migration_creates_schema_and_ledger_sha_matches_file`` /
   ``test_migration_reapply_fails_explicitly`` / ``test_empty_schema_rebuild``
   —— 迁移 004 表/约束/索引齐全、重复 apply 显式失败、空库可重建、台账 sha 一致。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import index_staging as ji
import locator_map as lm
import psycopg
import pytest
from parsing_assets import build_md_simple, build_txt_document
from simple_reader import read_document

MIGRATION = ji.MIGRATION_PATH
TABLES = (
    "phase0_schema_migrations",
    "phase0_sources",
    "phase0_documents",
    "phase0_document_versions",
    "phase0_index_attempts",
    "phase0_index_chunks",
    "phase0_index_manifests",
    "phase0_index_events",
)
REQUIRED_CONSTRAINTS = (
    "phase0_documents_source_fk",
    "phase0_versions_document_fk",
    "phase0_versions_ready_check",
    "phase0_attempts_version_fk",
    "phase0_attempts_generation_check",
    "phase0_chunks_ordinal_key",
    "phase0_chunks_embedding_len_check",
    "phase0_chunks_locator_check",
    "phase0_chunks_source_fk",
    "phase0_manifests_ready_check",
)
REQUIRED_INDEXES = (
    "phase0_versions_document_status_idx",
    "phase0_attempts_one_running",
    "phase0_chunks_version_idx",
    "phase0_chunks_attempt_idx",
    "phase0_events_document_idx",
)


# ---------------------------------------------------------------------------
# fixtures（conftest.py 属他人文件，DB fixture 必须写在这里）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def installed_schema() -> Iterator[str]:
    conn = ji.connect()
    try:
        ji.reset_schema(conn)
        digest = ji.apply_migration(conn)
        conn.commit()
    finally:
        conn.close()
    assert len(digest) == 64
    yield ji.SCHEMA
    conn = ji.connect()
    try:
        ji.reset_schema(conn)
    finally:
        conn.close()


@pytest.fixture()
def conn(installed_schema: str) -> Iterator[psycopg.Connection]:
    connection = ji.connect()
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


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _md_bytes() -> bytes:
    return build_md_simple(
        "Staging Doc",
        ["alpha paragraph one", "beta paragraph two", "gamma paragraph three"],
    )


def _prepare(
    conn: psycopg.Connection,
    store: ji.InMemoryObjectStore,
    workspace: UUID,
    *,
    content: bytes | None = None,
    file_name: str = "doc.md",
    fmt: str = "md",
    generation: int = 1,
) -> tuple[ji.SourceRef, UUID, ji.Attempt, list[ji.ChunkInput], object]:
    payload = content if content is not None else _md_bytes()
    parsed = read_document(payload, file_name=file_name)
    assert parsed.ok, parsed.error
    source = ji.create_source(
        conn,
        workspace_id=workspace,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content=payload,
        file_name=file_name,
        format=fmt,
        store=store,
    )
    document_id = uuid4()
    ji.ensure_document(conn, document_id=document_id, source=source,
                       desired_generation=generation)
    attempt = ji.begin_attempt(
        conn, workspace_id=workspace, document_id=document_id, generation=generation
    )
    chunks = ji.from_parsed_document(parsed)
    return source, document_id, attempt, chunks, parsed


def _stage_all(conn: psycopg.Connection, attempt: ji.Attempt,
               chunks: list[ji.ChunkInput]) -> ji.BatchResult:
    result = ji.stage_chunk_batch(conn, attempt, chunks)
    conn.commit()
    return result


def _make_ready(conn: psycopg.Connection, store: ji.InMemoryObjectStore,
                attempt: ji.Attempt) -> tuple[ji.VersionState, dict]:
    manifest = ji.build_manifest(conn, attempt, store=store)
    record = ji.write_manifest(conn, attempt, manifest)
    assert record.status == "ready", record.missing_items
    version = ji.mark_version_ready(conn, attempt)
    conn.commit()
    return version, manifest


def _chunk_rows(conn: psycopg.Connection, workspace: UUID, document_id: UUID,
                version: int) -> list[dict]:
    return ji.list_chunks(conn, workspace, document_id, version)


def _events(conn: psycopg.Connection, workspace: UUID) -> list[str]:
    rows = conn.execute(
        f"SELECT outcome FROM {ji.SCHEMA}.phase0_index_events "
        "WHERE workspace_id = %s ORDER BY event_seq",
        (workspace,),
    ).fetchall()
    return [row["outcome"] for row in rows]


def _simulate_t13_activation(conn: psycopg.Connection, workspace: UUID,
                             document_id: UUID, version: int) -> None:
    """**模拟 T13**（本任务不实现激活）：仅为证明可见性探针不是空实现。"""

    conn.execute(
        f"UPDATE {ji.SCHEMA}.phase0_document_versions "
        "SET status = 'active', updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND status = 'ready'",
        (workspace, document_id, version),
    )
    conn.execute(
        f"UPDATE {ji.SCHEMA}.phase0_documents "
        "SET active_version = %s, updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s",
        (version, workspace, document_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 验收 5：迁移 004
# ---------------------------------------------------------------------------
def test_migration_creates_schema_and_ledger_sha_matches_file(
    conn: psycopg.Connection, installed_schema: str
) -> None:
    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (ji.SCHEMA,),
        ).fetchall()
    }
    assert set(TABLES) <= tables

    constraints = {
        row["conname"]
        for row in conn.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = %s::regnamespace",
            (ji.SCHEMA,),
        ).fetchall()
    }
    assert set(REQUIRED_CONSTRAINTS) <= constraints

    indexes = {
        row["indexname"]
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (ji.SCHEMA,)
        ).fetchall()
    }
    assert set(REQUIRED_INDEXES) <= indexes

    ledger = conn.execute(
        f"SELECT filename, sha256 FROM {ji.SCHEMA}.phase0_schema_migrations "
        "WHERE migration_id = %s",
        (ji.MIGRATION_ID,),
    ).fetchone()
    assert ledger is not None
    assert ledger["filename"] == MIGRATION.name
    assert ledger["sha256"] == hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    assert ledger["sha256"] == ji.migration_sha256()


def test_migration_reapply_fails_explicitly(
    conn: psycopg.Connection, installed_schema: str
) -> None:
    with pytest.raises(psycopg.errors.DuplicateSchema) as excinfo:
        ji.apply_migration(conn)
    assert ji.SCHEMA in str(excinfo.value)  # 明确失败，不是静默跳过
    conn.rollback()
    ledger = conn.execute(
        f"SELECT count(*) AS n FROM {ji.SCHEMA}.phase0_schema_migrations"
    ).fetchone()
    assert ledger == {"n": 1}


def test_empty_schema_rebuild(installed_schema: str) -> None:
    conn = ji.connect()
    try:
        ji.reset_schema(conn)
        first = ji.apply_migration(conn)
        conn.commit()
        ji.reset_schema(conn)
        second = ji.apply_migration(conn)
        conn.commit()
        tables = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = %s",
            (ji.SCHEMA,),
        ).fetchone()
    finally:
        conn.close()
    assert first == second == ji.migration_sha256()
    assert int(tables["n"]) >= len(TABLES)


# ---------------------------------------------------------------------------
# 验收 2：staging 不可见
# ---------------------------------------------------------------------------
def test_staging_is_invisible_until_activation(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)
    version, _ = _make_ready(conn, store, attempt)
    assert version.status == "ready"
    assert version.chunk_count == len(chunks)

    document = ji.get_document(conn, workspace, document_id)
    assert document.active_version is None  # T12 不发布
    assert ji.search_visible_chunks(conn, workspace_id=workspace, query="alpha") == []
    assert ji.search_visible_chunks(conn, workspace_id=workspace, query="paragraph") == []

    # 只有 T13 切换 active 指针后才可见——这一步是测试模拟，不是本模块能力。
    _simulate_t13_activation(conn, workspace, document_id, 1)
    visible = ji.search_visible_chunks(conn, workspace_id=workspace, query="alpha")
    assert len(visible) == 1
    assert visible[0]["document_id"] == document_id
    assert visible[0]["index_version"] == 1
    assert ji.get_document(conn, workspace, document_id).active_version == 1


# ---------------------------------------------------------------------------
# 验收 1：attempt / generation fencing
# ---------------------------------------------------------------------------
def test_old_attempt_writes_zero_rows(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt_one, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    first = _stage_all(conn, attempt_one, chunks)
    assert first.inserted == len(chunks)

    attempt_two = ji.begin_attempt(
        conn, workspace_id=workspace, document_id=document_id, generation=1
    )
    conn.commit()
    assert ji.get_attempt(conn, workspace, attempt_one.attempt_id).status == "aborted"
    assert _chunk_rows(conn, workspace, document_id, 1) == []  # 旧 staging 分区被清空
    assert ji.get_version(conn, workspace, document_id, 1).chunk_count == 0

    with pytest.raises(ji.StaleAttemptError) as excinfo:
        ji.stage_chunk_batch(conn, attempt_one, chunks)
    assert excinfo.value.code == "STALE_ATTEMPT"
    conn.commit()
    assert _chunk_rows(conn, workspace, document_id, 1) == []  # 0 行
    assert "stale_attempt" in _events(conn, workspace)

    # 数据库兜底：绕过 API 直接插一条旧 attempt 的 chunk 也会被触发器拒绝。
    staged = _stage_all(conn, attempt_two, chunks)
    assert staged.inserted == len(chunks)
    sample = _chunk_rows(conn, workspace, document_id, 1)[0]
    with pytest.raises(psycopg.errors.CheckViolation) as db_error:
        conn.execute(
            f"INSERT INTO {ji.SCHEMA}.phase0_index_chunks "
            "(workspace_id, chunk_id, document_id, index_version, attempt_id, generation, "
            " chunk_ordinal, kind, text, text_sha256, locator, checksum, source_id, "
            " source_version_id, embedding, embedding_dim, embedding_profile) "
            "SELECT workspace_id, gen_random_uuid(), document_id, index_version, %s, "
            "       generation, 90, kind, text, text_sha256, locator, checksum, source_id, "
            "       source_version_id, embedding, embedding_dim, embedding_profile "
            f"FROM {ji.SCHEMA}.phase0_index_chunks "
            "WHERE workspace_id = %s AND chunk_id = %s",
            (attempt_one.attempt_id, workspace, sample["chunk_id"]),
        )
        conn.commit()
    assert "STALE_ATTEMPT" in str(db_error.value)
    conn.rollback()


def test_generation_bump_stales_old_attempt(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt_one, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt_one, chunks[:1])

    document = ji.bump_desired_generation(conn, workspace_id=workspace,
                                          document_id=document_id, now=_now())
    conn.commit()
    assert document.desired_generation == 2
    assert ji.get_version(conn, workspace, document_id, 1).status == "staging"
    assert ji.get_version(conn, workspace, document_id, 2).status == "staging"

    with pytest.raises(ji.StaleGenerationError) as excinfo:
        ji.stage_chunk_batch(conn, attempt_one, chunks)
    assert excinfo.value.code == "STALE_GENERATION"
    conn.commit()
    assert len(_chunk_rows(conn, workspace, document_id, 1)) == 1  # 一行都没变
    assert "stale_generation" in _events(conn, workspace)

    # 旧 generation 也不能 ready；新 generation 的 attempt 才可继续。
    with pytest.raises(ji.StaleGenerationError):
        ji.mark_version_ready(conn, attempt_one)
    conn.rollback()

    attempt_two = ji.begin_attempt(
        conn, workspace_id=workspace, document_id=document_id, generation=2
    )
    conn.commit()
    staged = _stage_all(conn, attempt_two, chunks)
    assert staged.inserted == len(chunks)
    version, _ = _make_ready(conn, store, attempt_two)
    assert version.status == "ready"
    assert version.index_version == 2
    assert ji.get_document(conn, workspace, document_id).active_version is None
    assert ji.search_visible_chunks(conn, workspace_id=workspace, query="alpha") == []


def test_same_version_new_attempt_resets_staging_and_manifest(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt_one, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt_one, chunks[:2])
    manifest = ji.build_manifest(conn, attempt_one, store=store)
    partial = {key: value for key, value in manifest.items() if key != "chunk_digest"}
    draft = ji.write_manifest(conn, attempt_one, partial)
    assert draft.status == "draft"
    conn.commit()

    attempt_two = ji.begin_attempt(
        conn, workspace_id=workspace, document_id=document_id, generation=1
    )
    conn.commit()
    version = ji.get_version(conn, workspace, document_id, 1)
    assert version.current_attempt_id == attempt_two.attempt_id
    assert version.chunk_count == 0
    assert version.status == "staging"
    assert _chunk_rows(conn, workspace, document_id, 1) == []
    assert ji.get_manifest(conn, workspace, document_id, 1) is None
    assert ji.get_attempt(conn, workspace, attempt_one.attempt_id).status == "aborted"

    staged = _stage_all(conn, attempt_two, chunks)
    assert staged.inserted == len(chunks)
    ready, _ = _make_ready(conn, store, attempt_two)
    assert ready.winning_attempt_id == attempt_two.attempt_id
    assert ji.get_attempt(conn, workspace, attempt_two.attempt_id).status == "succeeded"

    # ready 之后同版本不允许再开 attempt（要走新 generation）。
    with pytest.raises(ji.VersionNotStageable) as excinfo:
        ji.begin_attempt(conn, workspace_id=workspace, document_id=document_id,
                         generation=1)
    assert excinfo.value.code == "VERSION_NOT_STAGEABLE"


# ---------------------------------------------------------------------------
# 验收 3：序号唯一、引用模型、locator、manifest 必需项
# ---------------------------------------------------------------------------
def test_chunk_ordinal_unique_and_repeat_batch_is_idempotent(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    first = _stage_all(conn, attempt, chunks)
    assert (first.inserted, first.skipped) == (len(chunks), 0)
    second = _stage_all(conn, attempt, chunks)
    assert (second.inserted, second.skipped) == (0, len(chunks))
    assert second.chunk_count == len(chunks)
    rows = _chunk_rows(conn, workspace, document_id, 1)
    assert len(rows) == len(chunks)
    assert [row["chunk_ordinal"] for row in rows] == list(range(len(chunks)))

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_index_chunks SET chunk_ordinal = 1 "
            "WHERE workspace_id = %s AND chunk_id = %s",
            (workspace, rows[0]["chunk_id"]),
        )
        conn.commit()
    conn.rollback()
    assert len(_chunk_rows(conn, workspace, document_id, 1)) == len(chunks)


def test_chunk_reference_model_and_locator_schema(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    source, document_id, attempt, chunks, parsed = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)

    # locator schema 复用 T11：字段里没有偏移/跨度，且能逐条 round-trip。
    fields = lm.locator_fields()
    assert not {name for name in fields if "offset" in name}
    assert not ({"start", "end", "span"} & fields)

    rows = _chunk_rows(conn, workspace, document_id, 1)
    assert len(rows) == len(chunks)
    for row, block in zip(rows, parsed.blocks, strict=True):
        locator = lm.Locator.from_dict(row["locator"])
        assert locator == block.locator
        assert set(row["locator"]) >= {"schema", "kind", "format"}
        assert not ({"offset", "start", "end", "span"} & set(row["locator"]))
        assert row["source_id"] == source.source_id
        assert row["source_version_id"] == source.source_version_id
        assert row["text_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
        assert row["checksum"] == ji.chunk_checksum(
            text_sha256=row["text_sha256"],
            locator=row["locator"],
            source_id=source.source_id,
            source_version_id=source.source_version_id,
        )
        assert row["embedding_dim"] == ji.EMBEDDING_DIM
        assert len(row["embedding"]) == ji.EMBEDDING_DIM
        assert row["embedding_profile"] == ji.EMBEDDING_PROFILE
        assert row["attempt_id"] == attempt.attempt_id
        assert row["generation"] == attempt.generation


def test_manifest_requires_every_required_key(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)
    complete = ji.build_manifest(conn, attempt, store=store)
    assert set(ji.REQUIRED_MANIFEST_KEYS) <= set(complete)

    for key in ji.REQUIRED_MANIFEST_KEYS:
        broken = {name: value for name, value in complete.items() if name != key}
        record = ji.write_manifest(conn, attempt, broken)
        assert record.status == "draft", key
        assert record.missing_items == (key,), key
        with pytest.raises(ji.ManifestIncomplete) as excinfo:
            ji.mark_version_ready(conn, attempt)
        assert excinfo.value.code == "MANIFEST_INCOMPLETE"
        conn.commit()
        assert ji.get_version(conn, workspace, document_id, 1).status == "staging"

    ready = ji.write_manifest(conn, attempt, complete)
    assert ready.status == "ready" and ready.missing_items == ()
    version = ji.mark_version_ready(conn, attempt)
    conn.commit()
    assert version.status == "ready"
    assert version.manifest_checksum == ready.checksum
    assert ji.get_manifest(conn, workspace, document_id, 1).status == "ready"


def test_manifest_object_missing_blocks_ready(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    source, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)
    store.delete(source.object_key)
    with pytest.raises(ji.ObjectMissing) as excinfo:
        ji.build_manifest(conn, attempt, store=store)
    assert excinfo.value.code == "OBJECT_MISSING"
    assert "NOT_RUN" in store.backend  # 明确标注：真实对象存储未验证

    # 恢复对象后同一路径可以继续（说明是检查而非卡死状态）。
    store.put(source.object_key, _md_bytes())
    version, _ = _make_ready(conn, store, attempt)
    assert version.status == "ready"
    assert ji.get_version(conn, workspace, document_id, 1).chunk_count == len(chunks)


def test_ready_requires_manifest_at_database_level(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)
    checksum = "a" * 64

    # 没有 manifest：直接 UPDATE 到 ready 被触发器拒绝。
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_document_versions "
            "SET status = 'ready', winning_attempt_id = %s, manifest_checksum = %s, "
            "    row_version = row_version + 1 "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
            (attempt.attempt_id, checksum, workspace, document_id),
        )
        conn.commit()
    assert "MANIFEST_INCOMPLETE" in str(excinfo.value)
    conn.rollback()

    # 有 manifest 但缺项（draft）：仍然拒绝。
    manifest = ji.build_manifest(conn, attempt, store=store)
    broken = {key: value for key, value in manifest.items() if key != "embedding_dim"}
    draft = ji.write_manifest(conn, attempt, broken)
    assert draft.status == "draft"
    with pytest.raises(psycopg.errors.CheckViolation) as draft_error:
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_document_versions "
            "SET status = 'ready', winning_attempt_id = %s, manifest_checksum = %s, "
            "    row_version = row_version + 1 "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
            (attempt.attempt_id, checksum, workspace, document_id),
        )
        conn.commit()
    assert "MANIFEST_INCOMPLETE" in str(draft_error.value)
    conn.rollback()

    # manifest ready 但 chunk_count 被人为改坏：也不会放行。
    good = ji.write_manifest(conn, attempt, manifest)
    assert good.status == "ready"
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation) as count_error:
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_document_versions "
            "SET status = 'ready', winning_attempt_id = %s, manifest_checksum = %s, "
            "    chunk_count = chunk_count + 5, row_version = row_version + 1 "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = 1",
            (attempt.attempt_id, good.checksum, workspace, document_id),
        )
        conn.commit()
    assert "chunk_count" in str(count_error.value)
    conn.rollback()
    assert ji.get_version(conn, workspace, document_id, 1).status == "staging"


# ---------------------------------------------------------------------------
# 验收 4：embedding 一致性
# ---------------------------------------------------------------------------
def test_embedding_is_deterministic_and_dimension_checked(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    first = ji.deterministic_embedding("alpha paragraph one")
    second = ji.deterministic_embedding("alpha paragraph one")
    other = ji.deterministic_embedding("beta paragraph two")
    assert first == second
    assert first != other
    assert len(first) == ji.EMBEDDING_DIM
    assert all(-1.0 <= value <= 1.0 for value in first)

    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()

    with pytest.raises(ji.EmbeddingDimensionMismatch) as dim_error:
        ji.stage_chunk_batch(
            conn, attempt, chunks,
            embeddings=[(0.1,) * (ji.EMBEDDING_DIM - 1)] * len(chunks),
        )
    assert dim_error.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    conn.rollback()

    with pytest.raises(ji.EmbeddingCountMismatch) as count_error:
        ji.stage_chunk_batch(conn, attempt, chunks, embeddings=[(0.1,) * ji.EMBEDDING_DIM])
    assert count_error.value.code == "EMBEDDING_COUNT_MISMATCH"
    conn.rollback()
    assert _chunk_rows(conn, workspace, document_id, 1) == []

    staged = _stage_all(conn, attempt, chunks)
    assert staged.inserted == len(chunks)
    rows = _chunk_rows(conn, workspace, document_id, 1)
    for row in rows:
        assert tuple(row["embedding"]) == ji.deterministic_embedding(row["text"])

    # DB 层也拒绝维度与向量长度不一致（不是只靠 Python）。
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_index_chunks SET embedding_dim = 4 "
            "WHERE workspace_id = %s AND chunk_id = %s",
            (workspace, rows[0]["chunk_id"]),
        )
        conn.commit()
    conn.rollback()


def test_half_batch_failure_leaves_no_rows(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, document_id, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()

    # 批次内一个非法 chunk：在写任何行之前就拒绝。
    with pytest.raises(ji.BatchRejected):
        ji.stage_chunk_batch(
            conn, attempt,
            [chunks[0], ji.ChunkInput(kind="paragraph", text="", locator=chunks[0].locator)],
        )
    conn.rollback()
    assert _chunk_rows(conn, workspace, document_id, 1) == []

    # 数据库层半批失败（locator 缺 kind）→ 调用方回滚后一行不剩。
    valid = _chunk_rows_after_stage(conn, attempt, chunks[0])
    assert valid == 1
    conn.rollback()
    assert _chunk_rows(conn, workspace, document_id, 1) == []


def _chunk_rows_after_stage(conn: psycopg.Connection, attempt: ji.Attempt,
                            chunk: ji.ChunkInput) -> int:
    """在未提交事务里 stage 一个 chunk，再插一个非法 locator，最后回滚。"""

    ji.stage_chunk_batch(conn, attempt, [chunk])
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"INSERT INTO {ji.SCHEMA}.phase0_index_chunks "
            "(workspace_id, chunk_id, document_id, index_version, attempt_id, generation, "
            " chunk_ordinal, kind, text, text_sha256, locator, checksum, source_id, "
            " source_version_id, embedding, embedding_dim, embedding_profile) "
            "SELECT workspace_id, gen_random_uuid(), document_id, index_version, "
            "       attempt_id, generation, 77, kind, text, text_sha256, "
            "       '{\"schema\":1,\"offset\":3}'::jsonb, checksum, source_id, "
            "       source_version_id, embedding, embedding_dim, embedding_profile "
            f"FROM {ji.SCHEMA}.phase0_index_chunks "
            "WHERE workspace_id = %s AND attempt_id = %s LIMIT 1",
            (attempt.workspace_id, attempt.attempt_id),
        )
    return 1


# ---------------------------------------------------------------------------
# 审计与契约细节
# ---------------------------------------------------------------------------
def test_index_events_are_append_only(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    _, _, attempt, chunks, _ = _prepare(conn, store, workspace)
    conn.commit()
    _stage_all(conn, attempt, chunks)
    assert "attempt_started" in _events(conn, workspace)

    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_index_events SET outcome = 'chunks_staged' "
            "WHERE workspace_id = %s",
            (workspace,),
        )
        conn.commit()
    assert "只允许追加写" in str(excinfo.value)
    conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"DELETE FROM {ji.SCHEMA}.phase0_index_events WHERE workspace_id = %s",
            (workspace,),
        )
        conn.commit()
    conn.rollback()


def test_txt_parsing_reuses_t11_locators(
    conn: psycopg.Connection, store: ji.InMemoryObjectStore, workspace: UUID
) -> None:
    payload = build_txt_document(["first plain paragraph", "second plain paragraph"])
    _, document_id, attempt, chunks, parsed = _prepare(
        conn, store, workspace, content=payload, file_name="doc.txt", fmt="txt"
    )
    conn.commit()
    _stage_all(conn, attempt, chunks)
    rows = _chunk_rows(conn, workspace, document_id, 1)
    assert len(rows) == len(chunks) == len(parsed.blocks)
    for row, block in zip(rows, parsed.blocks, strict=True):
        assert lm.Locator.from_dict(row["locator"]) == block.locator
        assert row["kind"] == block.kind
    version, _ = _make_ready(conn, store, attempt)
    assert version.status == "ready"
    assert ji.search_visible_chunks(conn, workspace_id=workspace, query="plain") == []
