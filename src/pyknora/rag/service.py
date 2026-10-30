"""RAG 服务。

一次问答回合的完整链路：

1. 落用户消息（``role='user'``）；
2. 混合检索（:func:`pyknora.retrieval.service.search`）；
3. 证据判定：命中为空（或最高余弦相似度低于 ``rag_min_similarity``）→ 直接落
   ``status='insufficient_evidence'`` 的助手消息，不调用模型，不编造答案；
4. 组装上下文（片段 + 编号）与历史消息，走 ``ChatPort`` 流式生成；
5. 生成完成后把助手消息、结构化引用与 trace 一并落库。

为什么把「落库」放在生成器内部：SSE 响应的生成器在请求处理函数返回之后才执行，
请求级 session 此时已关闭，因此流式路径自己开 ``session_scope``。这样客户端中途
断开时，助手消息会落到 ``status='failed'``，不会留下永远 ``streaming`` 的脏行。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from pyknora.config import Settings, get_settings
from pyknora.db import session_scope
from pyknora.models import Conversation, Message
from pyknora.ports.models import (
    ChatMessage,
    RetrievedContext,
    get_chat_port,
    get_embedding_port,
)
from pyknora.retrieval.service import Hit, search

logger = logging.getLogger(__name__)

__all__ = ["PreparedTurn", "answer_stream", "prepare_turn", "run_turn"]

_INSUFFICIENT_EVIDENCE = "未在所检索的知识库中找到可用证据，无法回答该问题。"
_MAX_USER_CHARS = 8000


@dataclass(slots=True)
class PreparedTurn:
    """一次回合的检索结果与已落库的用户消息。"""

    conversation_id: uuid.UUID
    workspace_id: uuid.UUID
    user_message_id: uuid.UUID
    question: str
    hits: list[Hit]
    citations: list[dict[str, object]] = field(default_factory=list)
    history: list[ChatMessage] = field(default_factory=list)
    sufficient: bool = True
    trace: dict[str, object] = field(default_factory=dict)


def _sse(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def _load_history(session, conversation_id: uuid.UUID, limit: int) -> list[ChatMessage]:  # type: ignore[no-untyped-def]
    if limit <= 0:
        return []
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    return [
        ChatMessage(role=row.role, content=row.content)
        for row in rows
        if row.role in {"user", "assistant"}
    ]


async def prepare_turn(
    *,
    conversation: Conversation,
    question: str,
    knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    settings: Settings | None = None,
) -> PreparedTurn:
    """落用户消息并完成检索；不生成回答。"""

    settings = settings or get_settings()
    cleaned = question.strip()
    if not cleaned:
        raise ValueError("问题不能为空")
    if len(cleaned) > _MAX_USER_CHARS:
        raise ValueError(f"问题过长（上限 {_MAX_USER_CHARS} 字符）")

    embedder = get_embedding_port()
    async with session_scope() as session:
        user_message = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            role="user",
            content=cleaned,
            status="completed",
        )
        session.add(user_message)
        await session.flush()
        user_message_id = user_message.id
        await session.commit()

        hits = await search(
            session,
            workspace_id=conversation.workspace_id,
            query=cleaned,
            knowledge_base_ids=knowledge_base_ids,
        )
        history = await _load_history(session, conversation.id, settings.rag_history_turns)

    citations = [
        hit.citation(index)
        for index, hit in enumerate(hits[: settings.rag_max_context_chunks], start=1)
    ]
    threshold = settings.rag_min_similarity
    best_vector = max((hit.vector_score or 0.0 for hit in hits), default=0.0)
    sufficient = bool(hits) and (threshold <= 0.0 or best_vector >= threshold)
    trace: dict[str, object] = {
        "embedding_model": embedder.name,
        "embedding_dim": embedder.dim,
        "candidate_hits": len(hits),
        "best_vector_score": round(best_vector, 6),
        "min_similarity": threshold,
        "sufficient": sufficient,
        "citation_count": len(citations),
    }
    return PreparedTurn(
        conversation_id=conversation.id,
        workspace_id=conversation.workspace_id,
        user_message_id=user_message_id,
        question=cleaned,
        hits=hits,
        citations=citations,
        history=history,
        sufficient=sufficient,
        trace=trace,
    )


async def answer_stream(prepared: PreparedTurn) -> AsyncIterator[str]:
    """把一次回合渲染成 SSE 文本流，并在结束时落库助手消息。"""

    yield _sse(
        "retrieval",
        {
            "conversation_id": str(prepared.conversation_id),
            "user_message_id": str(prepared.user_message_id),
            "citations": prepared.citations,
            "trace": prepared.trace,
        },
    )

    if not prepared.sufficient:
        async with session_scope() as session:
            assistant = Message(
                workspace_id=prepared.workspace_id,
                conversation_id=prepared.conversation_id,
                role="assistant",
                content=_INSUFFICIENT_EVIDENCE,
                status="insufficient_evidence",
                citations=[],
                trace=prepared.trace,
            )
            session.add(assistant)
            await session.flush()
            assistant_id = assistant.id
            await session.commit()
        yield _sse(
            "message.completed",
            {
                "message_id": str(assistant_id),
                "status": "insufficient_evidence",
                "content": _INSUFFICIENT_EVIDENCE,
                "citations": [],
            },
        )
        return

    contexts = [
        RetrievedContext(
            index=index,
            title=hit.document_title,
            ordinal=hit.ordinal,
            content=hit.content,
        )
        for index, hit in enumerate(prepared.hits[: len(prepared.citations)], start=1)
    ]
    chat = get_chat_port()
    chunks: list[str] = []
    failure: str | None = None
    try:
        async for delta in chat.stream(
            question=prepared.question,
            contexts=contexts,
            history=prepared.history,
        ):
            chunks.append(delta)
            yield _sse("message.delta", {"text": delta})
    except Exception as exc:  # 生成器内部异常也要落库，避免消息悬空
        failure = f"{type(exc).__name__}: {exc}"
        logger.warning("生成失败 conversation_id=%s error=%s", prepared.conversation_id, failure)

    content = "".join(chunks)
    status = "failed" if failure else "completed"
    trace = dict(prepared.trace)
    trace["chat_model"] = chat.name
    if failure:
        trace["error"] = failure[:500]

    async with session_scope() as session:
        assistant = Message(
            workspace_id=prepared.workspace_id,
            conversation_id=prepared.conversation_id,
            role="assistant",
            content=content,
            status=status,
            citations=prepared.citations if status == "completed" else [],
            trace=trace,
        )
        session.add(assistant)
        await session.flush()
        assistant_id = assistant.id
        await session.commit()

    if failure:
        yield _sse("error", {"code": "generation_failed", "message": failure[:500]})
    yield _sse(
        "message.completed",
        {
            "message_id": str(assistant_id),
            "status": status,
            "content": content,
            "citations": prepared.citations if status == "completed" else [],
        },
    )


async def run_turn(
    *,
    conversation: Conversation,
    question: str,
    knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    settings: Settings | None = None,
) -> tuple[str, dict[str, object]]:
    """非流式回合：返回 (回答文本, 元信息)；供 ``POST /messages`` 使用。"""

    prepared = await prepare_turn(
        conversation=conversation,
        question=question,
        knowledge_base_ids=knowledge_base_ids,
        settings=settings,
    )
    events: list[tuple[str, dict[str, object]]] = []
    async for raw in answer_stream(prepared):
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            lines = block.splitlines()
            event = lines[0].removeprefix("event: ").strip()
            data = json.loads(lines[1].removeprefix("data: "))
            events.append((event, data))
    completed = next(
        (data for name, data in events if name == "message.completed"),
        {"content": "", "status": "failed"},
    )
    trace = dict(prepared.trace)
    trace["chat_model"] = get_chat_port().name
    return str(completed.get("content", "")), {
        "message_id": completed.get("message_id"),
        "status": completed.get("status"),
        "citations": completed.get("citations", []),
        "trace": trace,
    }
