"""检索 API：混合检索（向量 + 词法 + RRF）。"""

from __future__ import annotations

from fastapi import APIRouter, status

from pyknora.deps import CurrentUser, SessionDep, load_workspace_context
from pyknora.errors import ApiError
from pyknora.retrieval.service import search
from pyknora.schemas import RetrievalHitOut, RetrievalRequest, RetrievalResponse

router = APIRouter(tags=["retrieval"])

_MAX_QUERY_CHARS = 2000


@router.post(
    "/retrieval/search",
    response_model=RetrievalResponse,
    summary="混合检索：向量召回 + 词法召回 + RRF 融合",
)
async def retrieval_search(
    payload: RetrievalRequest,
    session: SessionDep,
    user: CurrentUser,
) -> RetrievalResponse:
    query = payload.query.strip()
    if not query:
        raise ApiError("invalid_request", "查询不能为空", status_code=status.HTTP_400_BAD_REQUEST)
    if len(query) > _MAX_QUERY_CHARS:
        raise ApiError("invalid_request", "查询过长", status_code=status.HTTP_400_BAD_REQUEST)

    await load_workspace_context(session, user, payload.workspace_id)

    hits = await search(
        session,
        workspace_id=payload.workspace_id,
        query=query,
        knowledge_base_ids=payload.knowledge_base_ids or None,
        top_k=payload.top_k,
    )
    return RetrievalResponse(
        query=query,
        total=len(hits),
        hits=[
            RetrievalHitOut(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                knowledge_base_id=hit.knowledge_base_id,
                document_title=hit.document_title,
                ordinal=hit.ordinal,
                snippet=hit.snippet,
                vector_score=hit.vector_score,
                lexical_score=hit.lexical_score,
                fused_score=hit.fused_score,
                vector_rank=hit.vector_rank,
                lexical_rank=hit.lexical_rank,
            )
            for hit in hits
        ],
    )
