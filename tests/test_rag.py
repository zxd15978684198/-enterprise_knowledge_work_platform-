"""RAG 问答测试：非流式、SSE 流式、证据不足、事件回放、租户隔离。"""

from __future__ import annotations

import json
import uuid
from typing import Any

DOC_TEXT = (
    "故障分级标准：一级故障指核心交易链路不可用，要求十五分钟内响应。"
    "二级故障指部分功能降级，要求一小时内响应。"
    "所有故障必须在处理完成后二十四小时内提交复盘报告。"
)


async def _bootstrap(client, unique_email: str, *, with_document: bool = True) -> dict[str, Any]:
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": "sup3r-secret", "display_name": "问答用户"},
    )
    assert register.status_code == 201, register.text
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

    workspace = await client.post(
        "/api/v1/workspaces", json={"name": f"问答工作区-{uuid.uuid4().hex[:6]}"}, headers=headers
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    kb = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": "应急手册", "description": ""},
        headers=headers,
    )
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]

    if with_document:
        upload = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files={"file": ("incident.md", DOC_TEXT.encode("utf-8"), "text/markdown")},
            data={"title": "故障分级与响应"},
            headers=headers,
        )
        assert upload.status_code == 201, upload.text
        assert upload.json()["status"] == "ready"

    conversation = await client.post(
        "/api/v1/conversations",
        json={"workspace_id": workspace_id, "title": "故障问答"},
        headers=headers,
    )
    assert conversation.status_code == 201, conversation.text

    return {
        "headers": headers,
        "workspace_id": workspace_id,
        "kb_id": kb_id,
        "conversation_id": conversation.json()["id"],
    }


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event = lines[0].removeprefix("event: ").strip()
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))
    return events


async def test_ask_returns_answer_with_citations(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email)
    response = await client.post(
        f"/api/v1/conversations/{ctx['conversation_id']}/messages",
        json={"question": "一级故障要求多久响应", "knowledge_base_ids": [ctx["kb_id"]]},
        headers=ctx["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["content"]
    assert body["citations"], "回答必须带结构化引用"
    citation = body["citations"][0]
    assert citation["document_title"] == "故障分级与响应"
    assert citation["index"] == 1
    assert "十五分钟" in citation["snippet"] or "故障" in citation["snippet"]
    assert body["trace"]["embedding_model"] == "hashing-local"
    assert body["trace"]["chat_model"] or body["trace"]["sufficient"] is True
    assert body["message_id"]


async def test_ask_without_evidence_marks_insufficient(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email, with_document=False)
    response = await client.post(
        f"/api/v1/conversations/{ctx['conversation_id']}/messages",
        json={"question": "一级故障要求多久响应"},
        headers=ctx["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "insufficient_evidence"
    assert body["citations"] == []
    assert "未在" in body["content"]


async def test_stream_emits_ordered_events_and_persists(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email)
    response = await client.post(
        f"/api/v1/conversations/{ctx['conversation_id']}/messages/stream",
        json={"question": "复盘报告什么时候提交", "knowledge_base_ids": [ctx["kb_id"]]},
        headers=ctx["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "retrieval"
    assert "message.delta" in names
    assert names[-1] == "message.completed"

    retrieval = events[0][1]
    assert retrieval["citations"]
    deltas = [data["text"] for name, data in events if name == "message.delta"]
    assert len(deltas) >= 2, "流式应至少分两块下发"
    completed = events[-1][1]
    assert completed["status"] == "completed"
    assert completed["citations"]

    detail = await client.get(
        f"/api/v1/conversations/{ctx['conversation_id']}", headers=ctx["headers"]
    )
    assert detail.status_code == 200, detail.text
    messages = detail.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "completed"
    assert messages[1]["citations"]


async def test_events_endpoint_replays_persisted_messages(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email)
    await client.post(
        f"/api/v1/conversations/{ctx['conversation_id']}/messages",
        json={"question": "二级故障响应时限"},
        headers=ctx["headers"],
    )
    replay = await client.get(
        f"/api/v1/conversations/{ctx['conversation_id']}/events", headers=ctx["headers"]
    )
    assert replay.status_code == 200
    events = _parse_sse(replay.text)
    names = [name for name, _ in events]
    assert names.count("message.snapshot") == 2
    assert names[-1] == "replay.completed"
    roles = [data["role"] for name, data in events if name == "message.snapshot"]
    assert roles == ["user", "assistant"]


async def test_conversation_isolated_across_workspaces(client, unique_email: str) -> None:
    ctx_a = await _bootstrap(client, unique_email)
    ctx_b = await _bootstrap(client, f"other-{unique_email}")
    response = await client.get(
        f"/api/v1/conversations/{ctx_a['conversation_id']}", headers=ctx_b["headers"]
    )
    assert response.status_code == 404
