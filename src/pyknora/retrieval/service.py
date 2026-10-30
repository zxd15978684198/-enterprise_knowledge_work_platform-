"""混合检索服务。

链路：并行两路召回 → 名次融合（RRF）→ 截断。

* **向量路**：pgvector 余弦距离 ``embedding <=> query``，HNSW 索引，带
  ``workspace_id`` 过滤；只考虑 ``status='ready'`` 且未删除文档的分块；
* **词法路**：中文没有内置分词器，这里把问题切成**字面词项**（标点切分 + 去停用
  词 + 长度 >= 2），用 ``ILIKE`` 命中项数排序，`pg_trgm` 的 trigram 相似度做次级
  排序（GIN 索引支撑 LIKE 前缀匹配）。它不是 BM25，只是可用的字面召回；
* **融合**：Reciprocal Rank Fusion，``score = Σ 1 / (k + rank)``，k 默认 60。
  两路分数不可比（余弦相似度 vs 命中数），因此只在名次上融合。

租户隔离在这里也做了一层保险：所有查询强制带 ``workspace_id``，即使调用方已经
在 API 层校验过权限，也不会因为漏传参数而跨工作区取数。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyknora.config import get_settings
from pyknora.models import Chunk, Document
from pyknora.ports.models import get_embedding_port

__all__ = ["Hit", "extract_terms", "search", "search_lexical", "search_vector"]

_SNIPPET_CHARS = 240
_STOPWORDS = frozenset(
    {
        "什么",
        "怎么",
        "如何",
        "为何",
        "哪些",
        "哪个",
        "多少",
        "是否",
        "可以",
        "需要",
        "请",
        "的",
        "了",
        "吗",
        "呢",
        "和",
        "与",
        "是",
        "在",
        "有",
        "what",
        "how",
        "why",
        "which",
        "the",
        "and",
        "is",
        "are",
        "of",
        "to",
    }
)
_BIGRAM_THRESHOLD = 4  #: 超过这个长度的中文短语退化成二字组召回
_TERM_SPLIT = re.compile(r"[\s，。！？、；：,.!?;:\"'（）()\[\]【】<>《》/\\|]+")
#: 疑问/语气成分：出现在词项首尾时剥掉，否则「备份策略是什么」这类整段只会命中字面
_AFFIXES = (
    "什么时候",
    "是什么原因",
    "有哪些",
    "是什么",
    "为什么",
    "怎么样",
    "多久",
    "如何",
    "怎么",
    "是否",
    "什么",
    "吗",
    "呢",
    "的",
    "了",
)


@dataclass(slots=True, frozen=True)
class Hit:
    """一条检索命中（融合后）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_title: str
    ordinal: int
    content: str
    snippet: str
    vector_score: float | None
    lexical_score: float | None
    fused_score: float
    vector_rank: int | None = None
    lexical_rank: int | None = None

    def citation(self, index: int) -> dict[str, object]:
        """转成对外的引用结构（编号从 1 开始）。"""

        return {
            "index": index,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "document_title": self.document_title,
            "ordinal": self.ordinal,
            "snippet": self.snippet,
            "score": round(self.fused_score, 6),
        }


@dataclass(slots=True)
class _Candidate:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_title: str
    ordinal: int
    content: str
    score: float


def _trim_affixes(term: str) -> str:
    """反复剥掉首尾的疑问词/语气词（中文没有分词器，只能做这种轻量清洗）。"""

    changed = True
    while changed and len(term) > 1:
        changed = False
        for affix in _AFFIXES:
            if term.startswith(affix) and len(term) - len(affix) >= 2:
                term = term[len(affix) :]
                changed = True
                break
            if term.endswith(affix) and len(term) - len(affix) >= 2:
                term = term[: -len(affix)]
                changed = True
                break
    return term


def extract_terms(query: str, *, min_length: int = 2, limit: int = 12) -> list[str]:
    """把问题切成用于字面召回的词项（去停用词、剥疑问词、去重、保序）。"""

    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> bool:
        if len(term) < min_length or term.lower() in _STOPWORDS or term in seen:
            return False
        seen.add(term)
        terms.append(term)
        return len(terms) >= limit

    for raw in _TERM_SPLIT.split(query):
        term = _trim_affixes(raw.strip())
        if len(term) <= _BIGRAM_THRESHOLD:
            if _add(term):
                break
            continue
        # 长中文短语没有分词器可切，整段 ILIKE 几乎不可能命中；退化成相邻二字组兜底。
        # 副作用是召回会放宽（同音/同字片段也算命中），ranking 时按命中词项数计分。
        for index in range(len(term) - 1):
            if _add(term[index : index + 2]):
                return terms
    return terms


def _base_filters(workspace_id: uuid.UUID, knowledge_base_ids: Sequence[uuid.UUID] | None):
    filters = [
        Chunk.workspace_id == workspace_id,
        Document.deleted_at.is_(None),
        Document.status == "ready",
        Document.knowledge_base_id.in_(knowledge_base_ids) if knowledge_base_ids else None,
    ]
    return [item for item in filters if item is not None]


async def search_vector(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    limit: int,
    knowledge_base_ids: Sequence[uuid.UUID] | None = None,
) -> list[_Candidate]:
    """向量路召回：按余弦距离升序。"""

    embedder = get_embedding_port()
    query_vector = embedder.embed([query])[0]
    distance = Chunk.embedding.cosine_distance(query_vector)
    # 向量路必须设相似度下限，否则「最近邻」永远能返回一批与问题无关的分块，
    # 既污染融合结果，也会让「证据不足」判定永远不会触发。
    min_similarity = get_settings().retrieval_min_similarity
    max_distance = 1.0 - min_similarity
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.knowledge_base_id,
            Document.title,
            Chunk.ordinal,
            Chunk.content,
            distance.label("distance"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            *_base_filters(workspace_id, knowledge_base_ids),
            Chunk.embedding.isnot(None),
            distance <= max_distance,
        )
        .order_by(distance)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        _Candidate(
            chunk_id=row.id,
            document_id=row.document_id,
            knowledge_base_id=row.knowledge_base_id,
            document_title=row.title,
            ordinal=row.ordinal,
            content=row.content,
            score=1.0 - float(row.distance),
        )
        for row in rows
    ]


async def search_lexical(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    limit: int,
    knowledge_base_ids: Sequence[uuid.UUID] | None = None,
) -> list[_Candidate]:
    """词法路召回：命中词项数优先，trigram 相似度次级排序。"""

    terms = extract_terms(query)
    if not terms:
        return []
    hit_expression = sum(
        (case((Chunk.content.ilike(f"%{term}%"), 1), else_=0) for term in terms),
        start=literal(0),
    )
    similarity = func.similarity(Chunk.content, query)
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Document.knowledge_base_id,
            Document.title,
            Chunk.ordinal,
            Chunk.content,
            hit_expression.label("hits"),
            similarity.label("similarity"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            *_base_filters(workspace_id, knowledge_base_ids),
            or_(*[Chunk.content.ilike(f"%{term}%") for term in terms]),
        )
        .order_by(hit_expression.desc(), similarity.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        _Candidate(
            chunk_id=row.id,
            document_id=row.document_id,
            knowledge_base_id=row.knowledge_base_id,
            document_title=row.title,
            ordinal=row.ordinal,
            content=row.content,
            score=float(row.hits) + float(row.similarity or 0.0),
        )
        for row in rows
    ]


def _snippet(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SNIPPET_CHARS:
        return collapsed
    return f"{collapsed[:_SNIPPET_CHARS]}…"


def fuse(
    *,
    vector_candidates: Iterable[_Candidate],
    lexical_candidates: Iterable[_Candidate],
    rrf_k: int,
    top_k: int,
) -> list[Hit]:
    """RRF 融合两路结果；同分时按向量名次优先。"""

    vector_list = list(vector_candidates)
    lexical_list = list(lexical_candidates)
    vector_rank = {candidate.chunk_id: rank for rank, candidate in enumerate(vector_list, start=1)}
    lexical_rank = {
        candidate.chunk_id: rank for rank, candidate in enumerate(lexical_list, start=1)
    }
    by_id: dict[uuid.UUID, _Candidate] = {}
    for candidate in [*vector_list, *lexical_list]:
        by_id.setdefault(candidate.chunk_id, candidate)

    scored: list[tuple[float, uuid.UUID]] = []
    for chunk_id in by_id:
        score = 0.0
        if chunk_id in vector_rank:
            score += 1.0 / (rrf_k + vector_rank[chunk_id])
        if chunk_id in lexical_rank:
            score += 1.0 / (rrf_k + lexical_rank[chunk_id])
        scored.append((score, chunk_id))
    scored.sort(key=lambda item: (-item[0], vector_rank.get(item[1], 1 << 30)))

    hits: list[Hit] = []
    for score, chunk_id in scored[:top_k]:
        candidate = by_id[chunk_id]
        hits.append(
            Hit(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                knowledge_base_id=candidate.knowledge_base_id,
                document_title=candidate.document_title,
                ordinal=candidate.ordinal,
                content=candidate.content,
                snippet=_snippet(candidate.content),
                vector_score=(
                    vector_list[vector_rank[chunk_id] - 1].score
                    if chunk_id in vector_rank
                    else None
                ),
                lexical_score=(
                    lexical_list[lexical_rank[chunk_id] - 1].score
                    if chunk_id in lexical_rank
                    else None
                ),
                fused_score=score,
                vector_rank=vector_rank.get(chunk_id),
                lexical_rank=lexical_rank.get(chunk_id),
            )
        )
    return hits


async def search(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    top_k: int | None = None,
    candidate_k: int | None = None,
    rrf_k: int | None = None,
) -> list[Hit]:
    """执行一次混合检索；两路候选各取 ``candidate_k``，融合后取 ``top_k``。"""

    settings = get_settings()
    effective_top_k = top_k or settings.retrieval_top_k
    effective_candidate_k = max(candidate_k or settings.retrieval_candidate_k, effective_top_k)
    effective_rrf_k = rrf_k or settings.rrf_k

    vector_candidates = await search_vector(
        session,
        workspace_id=workspace_id,
        query=query,
        limit=effective_candidate_k,
        knowledge_base_ids=knowledge_base_ids,
    )
    lexical_candidates = await search_lexical(
        session,
        workspace_id=workspace_id,
        query=query,
        limit=effective_candidate_k,
        knowledge_base_ids=knowledge_base_ids,
    )
    return fuse(
        vector_candidates=vector_candidates,
        lexical_candidates=lexical_candidates,
        rrf_k=effective_rrf_k,
        top_k=effective_top_k,
    )
