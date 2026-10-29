"""P0-T26 Graph 侧：LangGraph RAG、可核验引用与最终事件一致性。

契约依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T26：授权→有限改写/检索/不足拒答→生成→
  引用校验→**Message + Run + terminal 同一事务提交**；token 先持久、generation reset。
* ``docs/tdd/04_LangGraph_Workflows.md`` §3/§7：节点链、检索必须 filter-in-query；
  Rerank 不可用可降级并给 diagnostics；证据不足最多补搜一次；Graph 只返回执行结果，
  finalizer 幂等，DB 与 checkpointer 不同事务域。
* ``docs/tdd/07_Data_API_Security.md`` §5/§6：EventEnvelope 与 token 事件、每 run 唯一
  terminal、取消后不再改 terminal、P0 不交付公共续传。

**限域**：本模块的 ``ModelPort`` 使用确定性替身（真实 chat 模型链路 NOT_RUN），
因此结论最多 ``PASS（限域）``；检索是 p0_t26 内的两路（token 重叠 ≈ FTS + 确定性向量
+ RRF），未复用 T25 的 schema 与三路基线，T25 的 ANN ``NOT_RUN`` 结论不变。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from psycopg.pq import TransactionStatus

import events_probe as ep
from retrieval_probe import cosine, embed, tokenize

SCHEMA: Final = ep.SCHEMA
EMBEDDING_DIM: Final = 64
EMBEDDING_PROFILE: Final = "fake.lexical.v1"
DEFAULT_TOP_K: Final = 8
MIN_EVIDENCE_SCORE: Final = 0.010
MIN_CITATION_OVERLAP: Final = 3
MIN_CONTENT_OVERLAP: Final = 2
MIN_CITATION_RATIO: Final = 0.25
MAX_REWRITES: Final = 2
MAX_RETRIEVAL_ROUNDS: Final = 2
RRF_K: Final = 60
MAX_STREAM_CHARS: Final = 12_000
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE_PATH: Final = REPO_ROOT / "docs" / "poc" / "evidence" / "rag-citations.json"

_HANDLE_RE: Final = re.compile(r"\[\[(\d{1,3})\]\]")
_CHUNK_REF_RE: Final = re.compile(r"chunk:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
REFUSAL_TEXT: Final = "现有已授权资料不足以回答该问题（证据不足），因此不作推测性结论。"
SYSTEM_PROMPT: Final = (
    "你是企业知识库问答助手。只能使用本轮检索到的证据片段作答；"
    "引用证据时必须使用证据序号 [[n]]；证据不足时明确说明不确定，不得编造引用。"
)


class RagProbeError(RuntimeError):
    code: str = "RAG_PROBE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ModelCrashError(RagProbeError):
    """模拟模型进程中断：已流出的 token 保留，但必须以新 generation 重来。"""

    code = "MODEL_CRASH"


class RerankUnavailable(RagProbeError):
    code = "RERANK_UNAVAILABLE"


class StreamLimitExceeded(RagProbeError):
    code = "STREAM_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------
# 语料种子
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SeededChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    index_version: int
    chunk_ordinal: int
    text: str


@dataclass(frozen=True, slots=True)
class SeededDocument:
    workspace_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    index_version: int
    chunks: tuple[SeededChunk, ...]


@dataclass(frozen=True, slots=True)
class QualityCase:
    case_id: str
    kind: str
    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    kb_ids: tuple[uuid.UUID, ...]
    question: str
    expected: str
    gold_chunk_ids: tuple[uuid.UUID, ...] = ()
    forbidden_chunk_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class QualityCorpus:
    workspace_a: uuid.UUID
    owner_a: uuid.UUID
    member_a: uuid.UUID
    outsider_a: uuid.UUID
    kb_handbook: uuid.UUID
    kb_finance: uuid.UUID
    workspace_b: uuid.UUID
    owner_b: uuid.UUID
    kb_b: uuid.UUID
    workspace_c: uuid.UUID
    owner_c: uuid.UUID
    kb_empty: uuid.UUID
    docs: tuple[SeededDocument, ...]
    cases: tuple[QualityCase, ...]


def reset_corpus(conn: Any) -> None:
    """清空 p0_t26 的语料/运行数据（保留迁移台账），供测试隔离。"""

    conn.execute(
        "TRUNCATE p0_t26.phase0_run_events, p0_t26.phase0_event_cursors, "
        "p0_t26.phase0_event_counters, p0_t26.phase0_citation_rejections, "
        "p0_t26.phase0_message_citations, p0_t26.phase0_messages, "
        "p0_t26.phase0_runs, p0_t26.phase0_conversations, p0_t26.phase0_chunks, "
        "p0_t26.phase0_document_versions, p0_t26.phase0_documents, "
        "p0_t26.phase0_kb_grants, p0_t26.phase0_knowledge_bases, "
        "p0_t26.phase0_principals, p0_t26.phase0_workspaces CASCADE"
    )


def create_workspace(conn: Any, *, slug: str, workspace_id: uuid.UUID | None = None) -> uuid.UUID:
    workspace_id = workspace_id or uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_workspaces (workspace_id, slug) VALUES (%s, %s)",
        (workspace_id, slug),
    )
    return workspace_id


def add_principal(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    role: str = "member",
    status: str = "active",
    principal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    principal_id = principal_id or uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_principals "
        "(workspace_id, principal_id, role, status) VALUES (%s, %s, %s, %s)",
        (workspace_id, principal_id, role, status),
    )
    return principal_id


def add_kb(
    conn: Any, *, workspace_id: uuid.UUID, name: str, kb_id: uuid.UUID | None = None
) -> uuid.UUID:
    kb_id = kb_id or uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_knowledge_bases (workspace_id, kb_id, name) "
        "VALUES (%s, %s, %s)",
        (workspace_id, kb_id, name),
    )
    return kb_id


def grant_read(
    conn: Any, *, workspace_id: uuid.UUID, kb_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_kb_grants "
        "(workspace_id, kb_id, principal_id, can_read) VALUES (%s, %s, %s, true) "
        "ON CONFLICT (workspace_id, kb_id, principal_id) DO UPDATE SET can_read = true",
        (workspace_id, kb_id, principal_id),
    )


def revoke_read(
    conn: Any, *, workspace_id: uuid.UUID, kb_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_kb_grants SET can_read = false "
        "WHERE workspace_id = %s AND kb_id = %s AND principal_id = %s",
        (workspace_id, kb_id, principal_id),
    )


def add_document(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    kb_id: uuid.UUID,
    texts: Sequence[str],
    document_id: uuid.UUID | None = None,
    index_version: int = 1,
    activate: bool = True,
    embedding_profile: str = EMBEDDING_PROFILE,
    embedding_dim: int = EMBEDDING_DIM,
) -> SeededDocument:
    """写入一个文档版本（staging→active）与 chunk；active 指针一次切换。"""

    if not texts:
        raise RagProbeError("texts 不能为空")
    document_id = document_id or uuid.uuid4()
    attempt_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    existing = conn.execute(
        f"SELECT 1 FROM {SCHEMA}.phase0_documents WHERE workspace_id = %s AND document_id = %s",
        (workspace_id, document_id),
    ).fetchone()
    if existing is None:
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_documents "
            "(workspace_id, kb_id, document_id, source_id, source_version_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (workspace_id, kb_id, document_id, source_id, source_version_id),
        )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_document_versions "
        "(workspace_id, document_id, index_version, status, winning_attempt_id, chunk_count) "
        "VALUES (%s, %s, %s, 'staging', %s, %s)",
        (workspace_id, document_id, index_version, attempt_id, len(texts)),
    )
    chunks: list[SeededChunk] = []
    for ordinal, text in enumerate(texts):
        chunk_id = uuid.uuid4()
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_chunks "
            "(workspace_id, chunk_id, document_id, index_version, attempt_id, chunk_ordinal, "
            "kind, text, embedding, embedding_dim, embedding_profile, source_id, "
            "source_version_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                workspace_id,
                chunk_id,
                document_id,
                index_version,
                attempt_id,
                ordinal,
                "body",
                text,
                list(embed(text, dim=embedding_dim)),
                embedding_dim,
                embedding_profile,
                source_id,
                source_version_id,
            ),
        )
        chunks.append(
            SeededChunk(
                chunk_id=chunk_id,
                document_id=document_id,
                index_version=index_version,
                chunk_ordinal=ordinal,
                text=text,
            )
        )
    if not activate:
        return SeededDocument(
            workspace_id=workspace_id,
            kb_id=kb_id,
            document_id=document_id,
            index_version=index_version,
            chunks=tuple(chunks),
        )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions SET status = 'retired' "
        "WHERE workspace_id = %s AND document_id = %s AND status = 'active' "
        "AND index_version <> %s",
        (workspace_id, document_id, index_version),
    )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions SET status = 'active' "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_documents SET active_version = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s",
        (index_version, workspace_id, document_id),
    )
    return SeededDocument(
        workspace_id=workspace_id,
        kb_id=kb_id,
        document_id=document_id,
        index_version=index_version,
        chunks=tuple(chunks),
    )


# ---------------------------------------------------------------------------
# 检索：filter-in-query（授权 + 活动版本 + embedding space）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Evidence:
    handle: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    index_version: int
    chunk_ordinal: int
    kind: str
    text: str
    score: float
    rank: int


_VISIBLE_SQL: Final = f"""
SELECT c.chunk_id, c.document_id, c.index_version, c.chunk_ordinal, c.kind, c.text,
       c.embedding, c.embedding_dim, c.embedding_profile, d.kb_id
FROM {SCHEMA}.phase0_chunks AS c
JOIN {SCHEMA}.phase0_documents AS d
  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id
 AND d.active_version = c.index_version
JOIN {SCHEMA}.phase0_document_versions AS v
  ON v.workspace_id = c.workspace_id AND v.document_id = c.document_id
 AND v.index_version = c.index_version AND v.status = 'active'
 AND v.winning_attempt_id = c.attempt_id
JOIN {SCHEMA}.phase0_kb_grants AS g
  ON g.workspace_id = c.workspace_id AND g.kb_id = d.kb_id
 AND g.principal_id = %s AND g.can_read
WHERE c.workspace_id = %s
  AND c.embedding_dim = %s
  AND c.embedding_profile = %s
  AND d.kb_id = ANY(%s)
"""


def _visible_rows(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
    embedding_dim: int,
    embedding_profile: str,
) -> list[Mapping[str, Any]]:
    return conn.execute(
        _VISIBLE_SQL,
        (
            principal_id,
            workspace_id,
            embedding_dim,
            embedding_profile,
            list(kb_ids),
        ),
    ).fetchall()


def _rrf_merge(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    embedding_dim: int,
    top_k: int,
) -> list[tuple[Mapping[str, Any], float]]:
    terms = set(tokenize(query))
    fts_ranked: list[tuple[Mapping[str, Any], float]] = []
    vector_ranked: list[tuple[Mapping[str, Any], float]] = []
    query_vector = embed(query, dim=embedding_dim)
    for row in rows:
        text = row["text"]
        doc_terms = set(tokenize(text))
        overlap = len(terms & doc_terms)
        fts_score = overlap / (1 + len(terms)) if terms else 0.0
        sim = cosine(query_vector, row["embedding"])
        fts_ranked.append((row, fts_score))
        vector_ranked.append((row, sim))
    fts_ranked.sort(key=lambda item: (-item[1], str(item[0]["chunk_id"])))
    vector_ranked.sort(key=lambda item: (-item[1], str(item[0]["chunk_id"])))
    fused: dict[uuid.UUID, float] = {}
    rows_by_id: dict[uuid.UUID, Mapping[str, Any]] = {}
    for ranked in (fts_ranked, vector_ranked):
        for rank_index, (row, _score) in enumerate(ranked[: max(top_k * 8, 40)]):
            chunk_id = row["chunk_id"]
            rows_by_id[chunk_id] = row
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank_index + 1)
    ordered = sorted(fused.items(), key=lambda item: (-item[1], str(item[0])))
    return [(rows_by_id[chunk_id], score) for chunk_id, score in ordered[:top_k]]


def retrieve(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
    query: str,
    top_k: int = DEFAULT_TOP_K,
    embedding_dim: int = EMBEDDING_DIM,
    embedding_profile: str = EMBEDDING_PROFILE,
) -> tuple[Evidence, ...]:
    """在调用方事务快照内检索；只返回「已授权 + 活动版本」的 chunk。"""

    if not kb_ids:
        return ()
    rows = _visible_rows(
        conn,
        workspace_id=workspace_id,
        principal_id=principal_id,
        kb_ids=kb_ids,
        embedding_dim=embedding_dim,
        embedding_profile=embedding_profile,
    )
    fused = _rrf_merge(rows, query=query, embedding_dim=embedding_dim, top_k=top_k)
    evidence: list[Evidence] = []
    for index, (row, score) in enumerate(fused):
        evidence.append(
            Evidence(
                handle=index + 1,
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                index_version=int(row["index_version"]),
                chunk_ordinal=int(row["chunk_ordinal"]),
                kind=row["kind"],
                text=row["text"],
                score=score,
                rank=index + 1,
            )
        )
    return tuple(evidence)


def retrieve_in_snapshot(
    dsn: str,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[Evidence, ...]:
    """REPEATABLE READ 单快照检索：指针/版本/chunk 读到的必须是同一时刻。"""

    with closing(ep.connect(dsn)) as conn:
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            ep.require_active_membership(conn, workspace_id, principal_id)
            return retrieve(
                conn,
                workspace_id=workspace_id,
                principal_id=principal_id,
                kb_ids=kb_ids,
                query=query,
                top_k=top_k,
            )
        finally:
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()


# ---------------------------------------------------------------------------
# 引用：解析 + 校验（与检索同一可见性口径）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CitationCheck:
    citations: tuple[ep.CitationSpec, ...]
    rejections: tuple[dict[str, Any], ...]
    handles: tuple[int, ...]
    diagnostics: tuple[str, ...]


def parse_citation_handles(answer_text: str) -> tuple[int, ...]:
    return tuple(int(match) for match in _HANDLE_RE.findall(answer_text))


def _classify_missing_citation(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    chunk_id: uuid.UUID,
) -> str:
    row = conn.execute(
        f"SELECT c.workspace_id, c.index_version, d.active_version, v.status "
        f"FROM {SCHEMA}.phase0_chunks AS c "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
        f"LEFT JOIN {SCHEMA}.phase0_document_versions AS v "
        "  ON v.workspace_id = c.workspace_id AND v.document_id = c.document_id "
        " AND v.index_version = c.index_version "
        "WHERE c.chunk_id = %s LIMIT 1",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return "unknown_chunk"
    if row["workspace_id"] != workspace_id:
        return "cross_workspace"
    if row["active_version"] != row["index_version"] or row["status"] != "active":
        return "stale_version"
    granted = ep.principal_can_read_kb(
        conn,
        workspace_id=workspace_id,
        principal_id=principal_id,
        kb_id=conn.execute(
            f"SELECT d.kb_id FROM {SCHEMA}.phase0_documents AS d "
            "JOIN {SCHEMA}.phase0_chunks AS c "
            "  ON c.workspace_id = d.workspace_id AND c.document_id = d.document_id "
            "WHERE c.chunk_id = %s",
            (chunk_id,),
        ).fetchone()["kb_id"],
    )
    return "not_in_evidence" if granted else "revoked"


def validate_citations(
    conn: Any,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    run_id: uuid.UUID,
    evidence: Sequence[Evidence],
    answer_text: str,
    record: bool = True,
) -> CitationCheck:
    """只接受本轮真实 evidence handle 或 evidence 内的 chunk_id；其余记为拒绝。"""

    by_handle = {item.handle: item for item in evidence}
    by_chunk = {item.chunk_id: item for item in evidence}
    cited: list[ep.CitationSpec] = []
    rejections: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    seen_chunks: set[uuid.UUID] = set()
    seen_handles: set[int] = set()
    ordered_handles: list[int] = []
    for raw in parse_citation_handles(answer_text):
        ordered_handles.append(raw)
        if raw in seen_handles:
            rejections.append({"reason": "duplicate", "handle": raw})
            continue
        seen_handles.add(raw)
        item = by_handle.get(raw)
        if item is None:
            rejections.append({"reason": "not_in_evidence", "handle": raw})
            continue
        if item.chunk_id in seen_chunks:
            rejections.append({"reason": "duplicate", "handle": raw})
            continue
        visible = ep.chunk_visibility(
            conn,
            workspace_id=workspace_id,
            principal_id=principal_id,
            chunk_id=item.chunk_id,
        )
        if visible is None:
            reason = _classify_missing_citation(
                conn,
                workspace_id=workspace_id,
                principal_id=principal_id,
                chunk_id=item.chunk_id,
            )
            rejections.append({"reason": reason, "handle": raw, "chunk_id": str(item.chunk_id)})
            continue
        seen_chunks.add(item.chunk_id)
        cited.append(
            ep.CitationSpec(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                index_version=item.index_version,
                rank=item.handle,
                quote_hash=hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            )
        )
    for match in _CHUNK_REF_RE.findall(answer_text):
        chunk_id = uuid.UUID(match)
        if chunk_id in by_chunk:
            diagnostics.append("chunk_ref_resolved")
            continue
        rejections.append({"reason": "unknown_chunk", "chunk_id": match})
    if record and run_id is not None:
        for rejection in rejections:
            payload = dict(rejection)
            chunk_id = payload.pop("chunk_id", None)
            ep.record_citation_rejection(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                reason=str(payload["reason"]),
                chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
                detail=json.dumps(payload, ensure_ascii=False)[: ep.MAX_DETAIL],
            )
    return CitationCheck(
        citations=tuple(cited),
        rejections=tuple(rejections),
        handles=tuple(ordered_handles),
        diagnostics=tuple(diagnostics),
    )


# ---------------------------------------------------------------------------
# 模型端口（真实链路 NOT_RUN；替身只做确定性结构验证）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EvidenceView:
    handle: int
    chunk_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ModelPrompt:
    system: str
    question: str
    evidence: tuple[EvidenceView, ...]


class ModelPort(Protocol):
    def stream(self, prompt: ModelPrompt) -> Iterator[str]: ...


class DeterministicModel:
    """确定性替身：可配置注入伪引用，用于验证引用校验与降级路径。"""

    def __init__(
        self,
        *,
        inject_bad_handle: bool = False,
        max_cited: int = 1,
        stream_chars: int = 12,
    ) -> None:
        self._inject_bad_handle = inject_bad_handle
        self._max_cited = max_cited
        self._stream_chars = stream_chars

    def _choose(self, prompt: ModelPrompt) -> tuple[EvidenceView, ...]:
        if not prompt.evidence:
            return ()
        question_terms = set(tokenize(prompt.question))
        if not question_terms:
            return ()
        chunk_terms = [set(tokenize(view.text)) for view in prompt.evidence]
        document_frequency: dict[str, int] = {}
        for terms in chunk_terms:
            for term in terms:
                document_frequency[term] = document_frequency.get(term, 0) + 1
        common_limit = max(1, len(chunk_terms) // 2)
        content_terms = {
            term for term in question_terms if document_frequency.get(term, 0) <= common_limit
        }
        scored: list[tuple[int, int, EvidenceView]] = []
        for view, terms in zip(prompt.evidence, chunk_terms, strict=True):
            overlap = len(question_terms & terms)
            content_overlap = len(content_terms & terms)
            if overlap >= MIN_CITATION_OVERLAP and content_overlap >= MIN_CONTENT_OVERLAP:
                scored.append((overlap, content_overlap, view))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2].handle))
        if not scored:
            return ()
        if len(scored) > 1 and scored[1][0] >= scored[0][0]:
            # 两个证据同等匹配：不确定哪个是答案来源，按「证据不足」处理而不是随便挑一个。
            return ()
        return tuple(item[2] for item in scored[: self._max_cited])

    def _compose(self, prompt: ModelPrompt) -> str:
        if not prompt.evidence:
            return REFUSAL_TEXT
        chosen = self._choose(prompt)
        if not chosen:
            return REFUSAL_TEXT
        parts = [f"资料{view.handle}指出：{view.text[:56]}" for view in chosen]
        body = "；".join(parts) + "。"
        handles = "".join(f"[[{view.handle}]]" for view in chosen)
        text = f"根据已授权资料：{body}{handles}"
        if self._inject_bad_handle:
            text += "[[99]]"
        return text

    def stream(self, prompt: ModelPrompt) -> Iterator[str]:
        text = self._compose(prompt)
        for start in range(0, len(text), self._stream_chars):
            yield text[start : start + self._stream_chars]


class CrashAfterTokensModel:
    """模拟模型进程在流出若干 token 后崩溃（T26 注入场景）。"""

    def __init__(self, *, after_tokens: int = 3, stream_chars: int = 12) -> None:
        self._after = after_tokens
        self._stream_chars = stream_chars

    def stream(self, prompt: ModelPrompt) -> Iterator[str]:
        text = DeterministicModel()._compose(prompt)
        for emitted, start in enumerate(range(0, len(text), self._stream_chars)):
            if emitted >= self._after:
                raise ModelCrashError("模型进程中断（模拟 kill）")
            yield text[start : start + self._stream_chars]
        raise ModelCrashError("模型进程中断（模拟 kill）")


# ---------------------------------------------------------------------------
# token 持久化 sink：每个 token 先落库（短事务），再进入最终事务
# ---------------------------------------------------------------------------
class TokenSink:
    def __init__(
        self, dsn: str, *, run: ep.RunState, message: ep.MessageState, generation: int
    ) -> None:
        self._dsn = dsn
        self._run = run
        self._message = message
        self.generation = generation
        self.persisted = 0

    @property
    def message(self) -> ep.MessageState:
        return self._message

    def persist(self, text: str) -> None:
        with closing(ep.connect(self._dsn)) as conn:
            conn.execute("BEGIN")
            try:
                self._message = ep.persist_tokens(
                    conn,
                    run=self._run,
                    message=self._message,
                    generation=self.generation,
                    texts=[text],
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self.persisted += 1

    def close(self) -> None:
        return None


class FailingTokenSink(TokenSink):
    """在第 N 个 token 处制造**真实 DB 失败**（SELECT 1/0），验证不出现伪 completed。"""

    def __init__(
        self,
        dsn: str,
        *,
        run: ep.RunState,
        message: ep.MessageState,
        generation: int,
        fail_after: int = 3,
    ) -> None:
        super().__init__(dsn, run=run, message=message, generation=generation)
        self._fail_after = fail_after
        self.failures = 0

    def persist(self, text: str) -> None:
        if self.persisted >= self._fail_after:
            with closing(ep.connect(self._dsn)) as conn:
                conn.execute("SELECT 1 / 0")
        super().persist(text)


class Reranker(Protocol):
    def rerank(self, *, query: str, evidence: Sequence[Evidence]) -> Sequence[Evidence]: ...


class FailingReranker:
    def rerank(self, *, query: str, evidence: Sequence[Evidence]) -> Sequence[Evidence]:
        raise RerankUnavailable("rerank 服务不可用（模拟）")


# ---------------------------------------------------------------------------
# LangGraph：状态与节点
# ---------------------------------------------------------------------------
class RagState(TypedDict, total=False):
    """LangGraph 状态：只放引用与计数，不放大段 prompt/正文。"""

    query: str
    kb_ids: list[str]
    granted_kb_ids: list[str]
    rewritten: list[str]
    round_no: int
    evidence: list[dict[str, Any]]
    answer_text: str
    answer_kind: str
    uncertainty_note: str | None
    citations: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    diagnostics: list[str]
    enough: bool
    need_more: bool
    status: str
    error_code: str
    terminal_event_id: str
    replayed: bool


@dataclass
class GraphContext:
    dsn: str
    run: ep.RunState
    message: ep.MessageState
    generation: int
    workspace_id: uuid.UUID
    principal_id: uuid.UUID
    kb_ids: tuple[uuid.UUID, ...]
    model: ModelPort
    sink: TokenSink
    reranker: Reranker | None = None
    barrier: Any | None = None
    top_k: int = DEFAULT_TOP_K
    stage_timings: dict[str, float] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    evidence_by_handle: dict[int, Evidence] = field(default_factory=dict)


def _ctx(config: Any) -> GraphContext:
    return config["configurable"]["ctx"]


def _timed(ctx: GraphContext, name: str, started: float) -> None:
    ctx.stage_timings[name] = round((time.perf_counter() - started) * 1000.0, 3)


def _hit(ctx: GraphContext, name: str) -> None:
    if ctx.barrier is not None:
        ctx.barrier.hit(name)


def _rewrite_queries(query: str) -> tuple[str, ...]:
    base = " ".join(query.split())
    variants = [base]
    terms = list(dict.fromkeys(tokenize(base)))
    if terms:
        variants.append(" ".join(terms[:12]))
    unique: list[str] = []
    for variant in variants:
        if variant and variant not in unique:
            unique.append(variant)
    return tuple(unique[:MAX_REWRITES])


def _node_validate_scope(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    diagnostics = list(state.get("diagnostics", ()))
    granted: list[str] = []
    with closing(ep.connect(ctx.dsn)) as conn, conn.transaction():
        try:
            ep.require_active_membership(conn, ctx.workspace_id, ctx.principal_id)
        except ep.Unauthorized as exc:
            diagnostics.append(f"unauthorized:{exc.code}")
            return {"status": "unauthorized", "error_code": "UNAUTHORIZED"}
        for kb_id in ctx.kb_ids:
            if ep.principal_can_read_kb(
                conn,
                workspace_id=ctx.workspace_id,
                principal_id=ctx.principal_id,
                kb_id=kb_id,
            ):
                granted.append(str(kb_id))
            else:
                diagnostics.append(f"ungranted_kb:{kb_id}")
    _timed(ctx, "validate_scope", started)
    return {"granted_kb_ids": granted, "diagnostics": diagnostics, "status": "running"}


def _node_rewrite(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    variants = _rewrite_queries(state["query"])
    _timed(ctx, "rewrite", started)
    return {"rewritten": list(variants), "round_no": 0}


def _node_retrieve(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    round_no = int(state.get("round_no", 0)) + 1
    kb_ids = tuple(uuid.UUID(item) for item in state.get("granted_kb_ids", ()))
    evidence: list[Evidence] = []
    diagnostics = list(state.get("diagnostics", ()))
    if round_no == 1:
        queries = tuple(state.get("rewritten", (state["query"],)))
    else:
        terms = list(dict.fromkeys(tokenize(state["query"])))
        queries = (state["query"], " ".join(terms[:6]) if terms else state["query"])
    seen: set[uuid.UUID] = set()
    for query in queries:
        hits = retrieve_in_snapshot(
            ctx.dsn,
            workspace_id=ctx.workspace_id,
            principal_id=ctx.principal_id,
            kb_ids=kb_ids,
            query=query,
            top_k=ctx.top_k,
        )
        for hit in hits:
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            evidence.append(hit)
    evidence.sort(key=lambda item: (-item.score, str(item.chunk_id)))
    renumbered = [
        Evidence(
            handle=index + 1,
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            index_version=item.index_version,
            chunk_ordinal=item.chunk_ordinal,
            kind=item.kind,
            text=item.text,
            score=item.score,
            rank=index + 1,
        )
        for index, item in enumerate(evidence)
    ]
    ctx.evidence_by_handle = {item.handle: item for item in renumbered}
    _timed(ctx, f"retrieve_{round_no}", started)
    diagnostics.append(f"retrieval_round={round_no} hits={len(renumbered)}")
    return {
        "round_no": round_no,
        "evidence": [
            {
                "handle": item.handle,
                "chunk_id": str(item.chunk_id),
                "document_id": str(item.document_id),
                "index_version": item.index_version,
                "score": round(item.score, 6),
            }
            for item in renumbered
        ],
        "diagnostics": diagnostics,
    }


def _node_rerank(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    diagnostics = list(state.get("diagnostics", ()))
    items = [ctx.evidence_by_handle[item["handle"]] for item in state.get("evidence", ())]
    if ctx.reranker is not None and items:
        try:
            reranked = list(
                ctx.reranker.rerank(query=state["query"], evidence=tuple(items))
            )
        except RerankUnavailable as exc:
            diagnostics.append(f"rerank_degraded:{exc.code}")
        else:
            items = reranked
            diagnostics.append("rerank_applied")
    _timed(ctx, "rerank", started)
    return {"diagnostics": diagnostics, "evidence": state.get("evidence", [])}


def _node_assess(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    evidence = state.get("evidence", ())
    top_score = max((float(item["score"]) for item in evidence), default=0.0)
    enough = bool(evidence) and top_score >= MIN_EVIDENCE_SCORE
    round_no = int(state.get("round_no", 0))
    diagnostics = list(state.get("diagnostics", ()))
    diagnostics.append(f"enough={enough} top_score={top_score:.6f}")
    _timed(ctx, "assess", started)
    return {
        "enough": enough,
        "need_more": not enough and round_no < MAX_RETRIEVAL_ROUNDS,
        "diagnostics": diagnostics,
    }


def _route_after_assess(state: Mapping[str, Any]) -> str:
    if state.get("need_more"):
        return "retrieve"
    if state.get("evidence"):
        return "generate"
    return "refuse"


def _stream_text(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    writer = get_stream_writer()
    emitted = 0
    for chunk in ctx.model.stream(
        ModelPrompt(
            system=SYSTEM_PROMPT,
            question=state["query"],
            evidence=tuple(
                EvidenceView(handle=item.handle, chunk_id=str(item.chunk_id), text=item.text)
                for item in (
                    ctx.evidence_by_handle[ref["handle"]] for ref in state.get("evidence", ())
                )
            ),
        )
    ):
        if emitted + len(chunk) > MAX_STREAM_CHARS:
            raise StreamLimitExceeded("模型输出超过上限，拒绝继续（防止伪完成）")
        writer({"type": "token", "chars": len(chunk)})
        ctx.sink.persist(chunk)
        emitted += len(chunk)
    return {"answer_chars": emitted}


def _node_generate(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    _stream_text(state, config)
    _timed(ctx, "generate", started)
    return {"answer_text": ctx.sink.message.content}


def _node_refuse(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    ctx.sink.persist(REFUSAL_TEXT)
    _timed(ctx, "refuse", started)
    return {"answer_text": REFUSAL_TEXT, "answer_kind": "refused", "uncertainty_note": None}


def _node_validate_citations(
    state: Mapping[str, Any], config: RunnableConfig
) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    diagnostics = list(state.get("diagnostics", ()))
    evidence = tuple(ctx.evidence_by_handle.values())
    with closing(ep.connect(ctx.dsn)) as conn, conn.transaction():
        answer_text = ctx.sink.message.content
        check = validate_citations(
            conn,
            workspace_id=ctx.workspace_id,
            principal_id=ctx.principal_id,
            run_id=ctx.run.run_id,
            evidence=evidence,
            answer_text=answer_text,
        )
    answer_kind = "grounded"
    uncertainty_note: str | None = None
    if answer_text.strip() == REFUSAL_TEXT:
        answer_kind = "refused"
        uncertainty_note = None
    elif not check.citations:
        answer_kind = "insufficient_evidence"
        uncertainty_note = "引用未通过证据核验，未给出确定性结论。"
    elif check.rejections:
        answer_kind = "grounded_partial"
    diagnostics.append(
        f"citations_valid={len(check.citations)} rejected={len(check.rejections)}"
    )
    _timed(ctx, "validate_citations", started)
    return {
        "answer_text": ctx.sink.message.content,
        "answer_kind": answer_kind,
        "uncertainty_note": uncertainty_note,
        "citations": [
            {
                "chunk_id": str(item.chunk_id),
                "document_id": str(item.document_id),
                "index_version": item.index_version,
                "rank": item.rank,
                "quote_hash": item.quote_hash,
            }
            for item in check.citations
        ],
        "rejections": [dict(item) for item in check.rejections],
        "diagnostics": diagnostics,
    }


def _node_finalize(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _ctx(config)
    citations = tuple(
        ep.CitationSpec(
            chunk_id=uuid.UUID(item["chunk_id"]),
            document_id=uuid.UUID(item["document_id"]),
            index_version=int(item["index_version"]),
            rank=int(item["rank"]),
            quote_hash=item["quote_hash"],
        )
        for item in state.get("citations", ())
    )
    _hit(ctx, "before_finalize_commit")
    with closing(ep.connect(ctx.dsn)) as conn:
        conn.execute("BEGIN")
        try:
            result = ep.finalize(
                conn,
                run=ctx.run,
                message=ctx.sink.message,
                generation=ctx.generation,
                content=state.get("answer_text", ctx.sink.message.content),
                citations=citations,
                answer_kind=state.get("answer_kind", "grounded"),
                uncertainty_note=state.get("uncertainty_note"),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    _hit(ctx, "after_finalize_commit")
    _timed(ctx, "finalize", started)
    return {
        "status": "completed",
        "terminal_event_id": str(result.terminal_event.event_id),
        "replayed": result.replayed,
    }


def build_graph() -> Any:
    graph = StateGraph(RagState)
    graph.add_node("validate_scope", _node_validate_scope)
    graph.add_node("rewrite", _node_rewrite)
    graph.add_node("retrieve", _node_retrieve)
    graph.add_node("rerank", _node_rerank)
    graph.add_node("assess", _node_assess)
    graph.add_node("generate", _node_generate)
    graph.add_node("refuse", _node_refuse)
    graph.add_node("validate_citations", _node_validate_citations)
    graph.add_node("finalize", _node_finalize)
    graph.add_edge(START, "validate_scope")
    graph.add_edge("validate_scope", "rewrite")
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("retrieve", "rerank")
    graph.add_edge("rerank", "assess")
    graph.add_conditional_edges(
        "assess",
        _route_after_assess,
        {"retrieve": "retrieve", "generate": "generate", "refuse": "refuse"},
    )
    graph.add_edge("generate", "validate_citations")
    graph.add_edge("refuse", "validate_citations")
    graph.add_edge("validate_citations", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RagRunResult:
    run: ep.RunState
    assistant_message: ep.MessageState | None
    terminal: ep.RunEvent | None
    answer_text: str
    answer_kind: str | None
    citations: tuple[dict[str, Any], ...]
    rejections: tuple[dict[str, Any], ...]
    diagnostics: tuple[str, ...]
    refused: bool
    latency_ms: float
    stages: Mapping[str, float]
    status: str
    error_code: str | None
    generation: int
    persisted_tokens: int
    stream_chunks: int


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    run: ep.RunState
    message: ep.MessageState


def _prepare_run(
    dsn: str,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    conversation_id: uuid.UUID,
    query: str,
    generation: int = 1,
) -> _PreparedRun:
    with closing(ep.connect(dsn)) as conn:
        conn.execute("BEGIN")
        try:
            run, _user_message = ep.create_run(
                conn,
                workspace_id=workspace_id,
                principal_id=principal_id,
                conversation_id=conversation_id,
                query=query,
                generation=generation,
            )
            message = ep.ensure_assistant_message(
                conn,
                workspace_id=workspace_id,
                run_id=run.run_id,
                generation=generation,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return _PreparedRun(run=run, message=message)


def _load_result(
    dsn: str,
    *,
    run: ep.RunState,
    answer_kind: str | None,
    citations: Sequence[Mapping[str, Any]],
    rejections: Sequence[Mapping[str, Any]],
    diagnostics: Sequence[str],
    latency_ms: float,
    stages: Mapping[str, float],
    error_code: str | None,
    persisted_tokens: int,
    stream_chunks: int,
) -> RagRunResult:
    with closing(ep.connect(dsn)) as conn, conn.transaction():
        current_run = ep.read_run(conn, run.workspace_id, run.run_id)
        messages = ep.list_messages(conn, run.workspace_id, run.run_id)
        assistants = [item for item in messages if item.role == "assistant"]
        assistant = max(assistants, key=lambda item: item.generation) if assistants else None
        terminal = ep.read_terminal(conn, run.workspace_id, run.run_id)
        text = assistant.content if assistant is not None else ""
    return RagRunResult(
        run=current_run,
        assistant_message=assistant,
        terminal=terminal,
        answer_text=text,
        answer_kind=answer_kind if text else None,
        citations=tuple(dict(item) for item in citations),
        rejections=tuple(dict(item) for item in rejections),
        diagnostics=tuple(diagnostics),
        refused=text == REFUSAL_TEXT,
        latency_ms=latency_ms,
        stages=stages,
        status=current_run.status,
        error_code=error_code,
        generation=current_run.generation,
        persisted_tokens=persisted_tokens,
        stream_chunks=stream_chunks,
    )


def run_rag(
    dsn: str,
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    conversation_id: uuid.UUID,
    query: str,
    kb_ids: Sequence[uuid.UUID],
    model: ModelPort | None = None,
    sink_factory: Any | None = None,
    reranker: Reranker | None = None,
    barrier: Any | None = None,
    retry_on_model_crash: bool = True,
    graph: Any | None = None,
) -> RagRunResult:
    """跑一次 RAG Graph：检索→生成（token 先持久）→引用校验→终态一次事务提交。"""

    started = time.perf_counter()
    dsn_run = _prepare_run(
        dsn,
        workspace_id=workspace_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        query=query,
    )
    compiled = graph if graph is not None else build_graph()
    diagnostics: list[str] = []
    stages: dict[str, float] = {}
    answer_kind: str | None = None
    citations: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    error_code: str | None = None
    stream_chunks = 0
    persisted_tokens = 0
    current = dsn_run
    generation = 1
    while True:
        model_port = model if model is not None else DeterministicModel()
        sink = (
            sink_factory(dsn, run=current.run, message=current.message, generation=generation)
            if sink_factory is not None
            else TokenSink(dsn, run=current.run, message=current.message, generation=generation)
        )
        ctx = GraphContext(
            dsn=dsn,
            run=current.run,
            message=current.message,
            generation=generation,
            workspace_id=workspace_id,
            principal_id=principal_id,
            kb_ids=tuple(kb_ids),
            model=model_port,
            sink=sink,
            reranker=reranker,
            barrier=barrier,
        )
        try:
            final_state = compiled.invoke(
                {
                    "query": query,
                    "diagnostics": [],
                    "kb_ids": [str(item) for item in kb_ids],
                },
                config={"configurable": {"ctx": ctx}},
            )
            stream_chunks = 0
        except BaseException as exc:  # noqa: BLE001 - 注入路径需要分类处理
            diagnostics.extend(ctx.diagnostics)
            diagnostics.append(f"graph_exception:{type(exc).__name__}:{str(exc)[:300]}")
            stages.update(ctx.stage_timings)
            if isinstance(exc, ModelCrashError):
                error_code = "MODEL_CRASH"
                if retry_on_model_crash and generation < 2:
                    with closing(ep.connect(dsn)) as conn:
                        conn.execute("BEGIN")
                        try:
                            new_run, new_message = ep.bump_generation(
                                conn,
                                run=current.run,
                                expected_generation=generation,
                                reason="model_crash",
                            )
                            conn.commit()
                        except BaseException:
                            conn.rollback()
                            raise
                    diagnostics.append(f"generation_reset:{generation}->{new_run.generation}")
                    current = _PreparedRun(run=new_run, message=new_message)
                    generation = new_run.generation
                    continue
                error_code = "MODEL_CRASH"
                _finalize_failure(dsn, current.run, "MODEL_CRASH", "两次模型中断，终止", generation)
            return _load_result(
                dsn,
                run=current.run,
                answer_kind=None,
                citations=citations,
                rejections=rejections,
                diagnostics=diagnostics,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                stages=stages,
                error_code=error_code,
                persisted_tokens=sink.persisted,
                stream_chunks=stream_chunks,
            )
        diagnostics.extend(ctx.diagnostics)
        diagnostics.extend(str(item) for item in final_state.get("diagnostics", ()))
        stages.update(ctx.stage_timings)
        answer_kind = final_state.get("answer_kind")
        citations = [dict(item) for item in final_state.get("citations", ())]
        rejections = [dict(item) for item in final_state.get("rejections", ())]
        persisted_tokens = sink.persisted
        break
    return _load_result(
        dsn,
        run=current.run,
        answer_kind=answer_kind,
        citations=citations,
        rejections=rejections,
        diagnostics=diagnostics,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        stages=stages,
        error_code=None,
        persisted_tokens=persisted_tokens,
        stream_chunks=stream_chunks,
    )


def _finalize_failure(
    dsn: str,
    run: ep.RunState,
    error_code: str,
    detail: str,
    generation: int,
) -> None:
    with closing(ep.connect(dsn)) as conn:
        conn.execute("BEGIN")
        try:
            ep.finalize_failure(
                conn,
                run=run,
                error_code=error_code,
                detail=detail,
                generation=generation,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 质量评测：100 例（可答/无证据/跨空间/旧版本），引用准确率与延迟实测
# ---------------------------------------------------------------------------
HANDBOOK_TEXTS: Final = (
    "报销流程规定，员工应在费用发生后三十日内通过线上系统提交电子发票与审批单，"
    "财务在五个工作日内完成审核并打款。",
    "差旅住宿标准为一线城市每晚六百元，二线城市四百元，超出部分需部门负责人书面审批。",
    "采购申请需要预算编码，单笔超过五万元的采购必须经过三人比价并留存比价记录。",
)
FINANCE_TEXTS: Final = (
    "季度预算在每季度最后一个月二十日前提交，逾期视为放弃本季度新增预算。",
    "预算调整需要说明资金来源，涉及跨部门调剂的需财务负责人与业务负责人共同签字。",
)
HISTORY_TEXTS: Final = (
    "旧版报销流程要求员工提交纸质单据并由部门助理统一送交财务窗口，处理周期约十五个工作日。",
)
CROSS_TEXTS: Final = (
    "外部合作方结算需要提供合同编号与验收单，结算周期为验收通过后的十个工作日。",
)


def _new_conversation(dsn: str, *, workspace_id: uuid.UUID, principal_id: uuid.UUID) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    with closing(ep.connect(dsn)) as conn, conn.transaction():
        ep.ensure_conversation(
            conn,
            workspace_id=workspace_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
    return conversation_id


def seed_quality_corpus(dsn: str) -> QualityCorpus:
    """建三空间语料：A（可答 + 旧版本 + 空 KB）、B（跨空间同话题）、C（空）。"""

    with closing(ep.connect(dsn)) as conn:
        with conn.transaction():
            reset_corpus(conn)
            workspace_a = create_workspace(conn, slug="qa-a")
            workspace_b = create_workspace(conn, slug="qa-b")
            workspace_c = create_workspace(conn, slug="qa-c")
            owner_a = add_principal(conn, workspace_id=workspace_a, role="owner")
            member_a = add_principal(conn, workspace_id=workspace_a)
            outsider_a = add_principal(conn, workspace_id=workspace_a)
            owner_b = add_principal(conn, workspace_id=workspace_b, role="owner")
            owner_c = add_principal(conn, workspace_id=workspace_c, role="owner")
            kb_handbook = add_kb(conn, workspace_id=workspace_a, name="handbook")
            kb_finance = add_kb(conn, workspace_id=workspace_a, name="finance")
            kb_empty = add_kb(conn, workspace_id=workspace_a, name="empty-archive")
            kb_b = add_kb(conn, workspace_id=workspace_b, name="handbook")
            grant_read(conn, workspace_id=workspace_a, kb_id=kb_handbook, principal_id=owner_a)
            grant_read(conn, workspace_id=workspace_a, kb_id=kb_handbook, principal_id=member_a)
            grant_read(conn, workspace_id=workspace_a, kb_id=kb_finance, principal_id=owner_a)
            grant_read(conn, workspace_id=workspace_b, kb_id=kb_b, principal_id=owner_b)
            docs: list[SeededDocument] = []
            handbook = add_document(
                conn,
                workspace_id=workspace_a,
                kb_id=kb_handbook,
                texts=HANDBOOK_TEXTS,
            )
            docs.append(handbook)
            finance = add_document(
                conn, workspace_id=workspace_a, kb_id=kb_finance, texts=FINANCE_TEXTS
            )
            docs.append(finance)
            history = add_document(
                conn,
                workspace_id=workspace_a,
                kb_id=kb_handbook,
                texts=HISTORY_TEXTS,
                index_version=1,
                activate=False,
            )
            add_document(
                conn,
                workspace_id=workspace_a,
                kb_id=kb_handbook,
                texts=("归档说明：历史流程材料仅作留档，不作为现行依据。",),
                document_id=history.document_id,
                index_version=2,
            )
            docs.append(history)
            cross = add_document(conn, workspace_id=workspace_b, kb_id=kb_b, texts=CROSS_TEXTS)
            docs.append(cross)
        cases = _build_quality_cases(
            workspace_a=workspace_a,
            owner_a=owner_a,
            member_a=member_a,
            kb_handbook=kb_handbook,
            kb_finance=kb_finance,
            workspace_b=workspace_b,
            owner_b=owner_b,
            kb_b=kb_b,
            workspace_c=workspace_c,
            owner_c=owner_c,
            kb_empty=kb_empty,
            handbook=handbook,
            finance=finance,
            history=history,
            cross=cross,
        )
    return QualityCorpus(
        workspace_a=workspace_a,
        owner_a=owner_a,
        member_a=member_a,
        outsider_a=outsider_a,
        kb_handbook=kb_handbook,
        kb_finance=kb_finance,
        workspace_b=workspace_b,
        owner_b=owner_b,
        kb_b=kb_b,
        workspace_c=workspace_c,
        owner_c=owner_c,
        kb_empty=kb_empty,
        docs=tuple(docs),
        cases=cases,
    )


def _build_quality_cases(
    *,
    workspace_a: uuid.UUID,
    owner_a: uuid.UUID,
    member_a: uuid.UUID,
    kb_handbook: uuid.UUID,
    kb_finance: uuid.UUID,
    workspace_b: uuid.UUID,
    owner_b: uuid.UUID,
    kb_b: uuid.UUID,
    workspace_c: uuid.UUID,
    owner_c: uuid.UUID,
    kb_empty: uuid.UUID,
    handbook: SeededDocument,
    finance: SeededDocument,
    history: SeededDocument,
    cross: SeededDocument,
) -> tuple[QualityCase, ...]:
    cases: list[QualityCase] = []
    visible_chunks = (*handbook.chunks, *finance.chunks)
    answerable_pool = [
        "报销系统提交电子发票的时限是多少",
        "一线城市差旅住宿标准每晚多少元",
        "采购比价需要几人参与",
        "季度预算提交截止日是哪天",
        "预算跨部门调剂需要谁签字",
    ]
    for index in range(40):
        question = answerable_pool[index % len(answerable_pool)]
        chunk = _best_match_chunk(question, visible_chunks)
        cases.append(
            QualityCase(
                case_id=f"answerable-{index:03d}",
                kind="answerable",
                workspace_id=workspace_a,
                principal_id=owner_a,
                kb_ids=(kb_handbook, kb_finance),
                question=f"{question}（第{index // len(answerable_pool) + 1}次问询）",
                expected="grounded",
                gold_chunk_ids=(chunk.chunk_id,),
                forbidden_chunk_ids=(history.chunks[0].chunk_id,),
            )
        )
    for index in range(20):
        cases.append(
            QualityCase(
                case_id=f"no-evidence-{index:03d}",
                kind="no_evidence",
                workspace_id=workspace_a,
                principal_id=owner_a,
                kb_ids=(kb_empty,),
                question=f"火星殖民地第{index}期退税政策如何计算？",
                expected="refused",
            )
        )
    for index in range(20):
        cases.append(
            QualityCase(
                case_id=f"cross-space-{index:03d}",
                kind="cross_space",
                workspace_id=workspace_a,
                principal_id=owner_a,
                kb_ids=(kb_handbook, kb_finance),
                question=f"供应商对账与验收结算由谁负责（{index}）",
                expected="no_foreign_citation",
                forbidden_chunk_ids=(cross.chunks[0].chunk_id,),
            )
        )
    for index in range(20):
        cases.append(
            QualityCase(
                case_id=f"stale-version-{index:03d}",
                kind="stale_version",
                workspace_id=workspace_a,
                principal_id=owner_a,
                kb_ids=(kb_handbook,),
                question=f"纸质单据由谁统一送交财务窗口（{index}）",
                expected="no_stale_citation",
                forbidden_chunk_ids=(history.chunks[0].chunk_id,),
            )
        )
    return tuple(cases)


def _best_match_chunk(question: str, candidates: Sequence[SeededChunk]) -> SeededChunk:
    """合成语料的 gold 定义：与问题共享 token 最多的活动版本 chunk。

    真实模型下会由人工标注 gold；替身实验里用它把「检索+引用一致性」与
    「语义正确性」分开，避免把语料模板误差算进引用准确率。
    """

    terms = set(tokenize(question))
    return max(
        candidates,
        key=lambda item: (len(terms & set(tokenize(item.text))), -item.chunk_ordinal),
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(max(int(round(quantile * (len(ordered) - 1))), 0), len(ordered) - 1)
    return ordered[position]


def evaluate_quality(
    dsn: str,
    *,
    cases: int = 100,
    model: ModelPort | None = None,
) -> dict[str, Any]:
    """跑 100 例，实测引用准确率/伪引用/越权引用/延迟，并落 JSON 证据。"""

    corpus = seed_quality_corpus(dsn)
    selected = corpus.cases[:cases]
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    fabricated_citations = 0
    wrong_citations = 0
    forbidden_citations = 0
    unauthorized_citations = 0
    citations_total = 0
    correct_citations = 0
    behaviour_matches = 0
    completed_runs = 0
    refused = 0
    conversation = _new_conversation(
        dsn, workspace_id=corpus.workspace_a, principal_id=corpus.owner_a
    )
    for case in selected:
        outcome = run_rag(
            dsn,
            workspace_id=case.workspace_id,
            principal_id=case.principal_id,
            conversation_id=conversation,
            query=case.question,
            kb_ids=case.kb_ids,
            model=model,
        )
        latencies.append(outcome.latency_ms)
        cited = [uuid.UUID(item["chunk_id"]) for item in outcome.citations]
        citations_total += len(cited)
        allowed = set(case.gold_chunk_ids)
        forbidden = set(case.forbidden_chunk_ids)
        for chunk_id in cited:
            visible = _chunk_workspace(dsn, chunk_id)
            if visible is None or visible != case.workspace_id:
                unauthorized_citations += 1
                fabricated_citations += 1
                continue
            if chunk_id in allowed:
                correct_citations += 1
            else:
                wrong_citations += 1
                if chunk_id in forbidden:
                    forbidden_citations += 1
        if outcome.status == "completed":
            completed_runs += 1
        if outcome.refused:
            refused += 1
        cited_set = set(cited)
        matched = False
        if case.kind == "answerable":
            matched = outcome.answer_kind in ("grounded", "grounded_partial") and bool(
                cited_set & allowed
            )
        elif case.kind == "no_evidence":
            matched = outcome.refused and not cited
        else:
            matched = not (cited_set & forbidden)
        behaviour_matches += int(matched)
        results.append(
            {
                "case_id": case.case_id,
                "kind": case.kind,
                "status": outcome.status,
                "answer_kind": outcome.answer_kind,
                "refused": outcome.refused,
                "citations": len(cited),
                "false_citations": sum(1 for item in cited if item not in allowed),
                "diagnostics": list(outcome.diagnostics),
                "latency_ms": round(outcome.latency_ms, 3),
            }
        )
    by_kind: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = by_kind.setdefault(
            item["kind"],
            {"cases": 0, "completed": 0, "refused": 0, "citations": 0, "false_citations": 0},
        )
        bucket["cases"] += 1
        bucket["completed"] += int(item["status"] == "completed")
        bucket["refused"] += int(bool(item["refused"]))
        bucket["citations"] += int(item["citations"])
        bucket["false_citations"] += int(item["false_citations"])
    accuracy = (correct_citations / citations_total) if citations_total else 0.0
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "by_kind": by_kind,
        "schema": SCHEMA,
        "model_port": type(model).__name__ if model is not None else "DeterministicModel",
        "limitation": "真实 chat 模型链路 NOT_RUN；引用准确率为替身模型下的结构测量",
        "sample_count": len(selected),
        "behaviour_match_rate": round(behaviour_matches / len(selected), 4),
        "citation_accuracy": round(accuracy, 4),
        "citations_total": citations_total,
        "fabricated_citations": fabricated_citations,
        "wrong_citations": wrong_citations,
        "forbidden_citations": forbidden_citations,
        "unauthorized_citations": unauthorized_citations,
        "completed_runs": completed_runs,
        "refused_runs": refused,
        "latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "cases": results[:20],
    }
    return payload


def _chunk_workspace(dsn: str, chunk_id: uuid.UUID) -> uuid.UUID | None:
    with closing(ep.connect(dsn)) as conn, conn.transaction():
        row = conn.execute(
            f"SELECT workspace_id FROM {SCHEMA}.phase0_chunks WHERE chunk_id = %s",
            (chunk_id,),
        ).fetchone()
    return None if row is None else row["workspace_id"]


def write_evidence(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path if path is not None else EVIDENCE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "DEFAULT_TOP_K",
    "EMBEDDING_DIM",
    "EMBEDDING_PROFILE",
    "EVIDENCE_PATH",
    "Evidence",
    "FailingReranker",
    "FailingTokenSink",
    "ModelPort",
    "ModelPrompt",
    "RagProbeError",
    "RagRunResult",
    "Reranker",
    "SeededDocument",
    "TokenSink",
    "add_document",
    "add_kb",
    "add_principal",
    "build_graph",
    "create_workspace",
    "evaluate_quality",
    "grant_read",
    "parse_citation_handles",
    "reset_corpus",
    "retrieve",
    "retrieve_in_snapshot",
    "run_rag",
    "seed_quality_corpus",
    "validate_citations",
    "write_evidence",
]
