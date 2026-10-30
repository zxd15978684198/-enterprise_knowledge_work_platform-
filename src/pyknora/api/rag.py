"""会话与 RAG 问答 API（含 SSE 流式与事件回放）。"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from pyknora.deps import CurrentUser, SessionDep, load_workspace_context
from pyknora.errors import ApiError
from pyknora.models import Conversation, Message
from pyknora.rag.service import answer_stream, prepare_turn, run_turn
from pyknora.schemas import (
    AskRequest,
    AskResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    MessageOut,
)

router = APIRouter(tags=["conversations"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _load_conversation(
    session: SessionDep, user: CurrentUser, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise ApiError("not_found", "会话不存在", status_code=status.HTTP_404_NOT_FOUND)
    await load_workspace_context(session, user, conversation.workspace_id)
    return conversation


async def _message_count(session: SessionDep, conversation_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )
    return int(total or 0)


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
async def create_conversation(
    payload: ConversationCreate,
    session: SessionDep,
    user: CurrentUser,
) -> ConversationOut:
    await load_workspace_context(session, user, payload.workspace_id)
    conversation = Conversation(
        workspace_id=payload.workspace_id,
        title=payload.title.strip(),
        created_by=user.id,
    )
    session.add(conversation)
    await session.flush()
    await session.commit()
    return ConversationOut(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        created_by=conversation.created_by,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=0,
    )


@router.get("/conversations", response_model=list[ConversationOut], summary="会话列表")
async def list_conversations(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ConversationOut]:
    await load_workspace_context(session, user, workspace_id)
    stmt = (
        select(Conversation)
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    conversations = list((await session.scalars(stmt)).all())
    result: list[ConversationOut] = []
    for conversation in conversations:
        result.append(
            ConversationOut(
                id=conversation.id,
                workspace_id=conversation.workspace_id,
                title=conversation.title,
                created_by=conversation.created_by,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                message_count=await _message_count(session, conversation.id),
            )
        )
    return result


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="会话详情（含消息）",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ConversationDetail:
    conversation = await _load_conversation(session, user, conversation_id)
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    messages = list((await session.scalars(stmt)).all())
    return ConversationDetail(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        created_by=conversation.created_by,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=len(messages),
        messages=[MessageOut.model_validate(message) for message in messages],
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AskResponse,
    summary="提一个问题（非流式，等待完整回答）",
)
async def post_message(
    conversation_id: uuid.UUID,
    payload: AskRequest,
    session: SessionDep,
    user: CurrentUser,
) -> AskResponse:
    conversation = await _load_conversation(session, user, conversation_id)
    try:
        content, meta = await run_turn(
            conversation=conversation,
            question=payload.question,
            knowledge_base_ids=payload.knowledge_base_ids or None,
        )
    except ValueError as exc:
        raise ApiError(
            "invalid_request", str(exc), status_code=status.HTTP_400_BAD_REQUEST
        ) from exc
    return AskResponse(
        conversation_id=conversation.id,
        message_id=meta.get("message_id"),  # type: ignore[arg-type]
        status=str(meta.get("status")),
        content=content,
        citations=meta.get("citations", []),  # type: ignore[arg-type]
        trace=meta.get("trace", {}),  # type: ignore[arg-type]
    )


@router.post(
    "/conversations/{conversation_id}/messages/stream",
    summary="提一个问题（SSE 流式：retrieval → message.delta → message.completed）",
)
async def post_message_stream(
    conversation_id: uuid.UUID,
    payload: AskRequest,
    session: SessionDep,
    user: CurrentUser,
) -> StreamingResponse:
    conversation = await _load_conversation(session, user, conversation_id)
    try:
        prepared = await prepare_turn(
            conversation=conversation,
            question=payload.question,
            knowledge_base_ids=payload.knowledge_base_ids or None,
        )
    except ValueError as exc:
        raise ApiError(
            "invalid_request", str(exc), status_code=status.HTTP_400_BAD_REQUEST
        ) from exc
    return StreamingResponse(
        answer_stream(prepared),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get(
    "/conversations/{conversation_id}/events",
    summary="事件回放（SSE）：按时间重放会话已落库消息",
)
async def get_events(
    conversation_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> StreamingResponse:
    conversation = await _load_conversation(session, user, conversation_id)
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    messages = list((await session.scalars(stmt)).all())

    async def _replay():  # type: ignore[no-untyped-def]
        for message in messages:
            data = {
                "message_id": str(message.id),
                "role": message.role,
                "status": message.status,
                "content": message.content,
                "citations": message.citations,
                "created_at": message.created_at.isoformat(),
            }
            yield f"event: message.snapshot\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        yield "event: replay.completed\ndata: {}\n\n"

    return StreamingResponse(_replay(), media_type="text/event-stream", headers=_SSE_HEADERS)
