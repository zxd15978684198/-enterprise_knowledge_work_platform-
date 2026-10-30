"""知识库与文档入库：CRUD、解析分块、幂等、重处理、跨工作区隔离。"""

from __future__ import annotations

import pytest

from pyknora.storage import reset_storage

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path):
    """每个用例独立的本地对象存储目录，避免写入仓库 data/。"""

    reset_storage(tmp_path / "uploads")
    yield


async def _register(client, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": email.split("@")[0]},
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _workspace(client, token: str, name: str = "知识中心") -> str:
    response = await client.post("/api/v1/workspaces", json={"name": name}, headers=_auth(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _kb(client, token: str, workspace_id: str, name: str = "产品手册") -> str:
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": name, "description": "测试库"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(
    client, token: str, kb_id: str, filename: str, data: bytes, title: str | None = None
):
    files = {"file": (filename, data, "application/octet-stream")}
    form = {"title": title} if title else None
    return await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files=files,
        data=form,
        headers=_auth(token),
    )


async def test_create_and_list_knowledge_bases(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": "产品手册", "description": "第一批"},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "产品手册"
    assert body["document_count"] == 0
    assert body["chunk_count"] == 0

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases", headers=_auth(token)
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    duplicated = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": "产品手册"},
        headers=_auth(token),
    )
    assert duplicated.status_code == 409
    assert duplicated.json()["error"]["code"] == "name_taken"


async def test_upload_text_document_ingests_chunks(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    text = "\n\n".join(
        f"第 {i} 段：这是用于验证分块与入库流程的测试内容。" * 6 for i in range(1, 9)
    )
    response = await _upload(
        client, token, kb_id, "guide.txt", text.encode("utf-8"), title="使用指南"
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready", body
    assert body["error"] is None
    assert body["title"] == "使用指南"
    assert body["chunk_count"] >= 1
    assert body["mime_type"].startswith("text/plain")

    chunks = await client.get(f"/api/v1/documents/{body['id']}/chunks", headers=_auth(token))
    assert chunks.status_code == 200
    page = chunks.json()
    assert page["total"] == body["chunk_count"]
    assert page["items"][0]["ordinal"] == 0
    assert "分块与入库流程" in page["items"][0]["content"]

    kb_detail = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(token))
    assert kb_detail.json()["document_count"] == 1
    assert kb_detail.json()["chunk_count"] == body["chunk_count"]


async def test_markdown_is_ingested_as_text_without_loss(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    # 当前实现把 .md 当纯文本摄取（不做 Markdown 渲染），内容必须完整保留。
    markdown = "# 标题\n\n**加粗** 与 [链接](https://example.com) 都要保留。\n\n- 列表项"
    response = await _upload(client, token, kb_id, "note.md", markdown.encode("utf-8"))

    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    chunks = await client.get(f"/api/v1/documents/{document_id}/chunks", headers=_auth(token))
    content = " ".join(item["content"] for item in chunks.json()["items"])
    assert "标题" in content
    assert "加粗" in content
    assert "列表项" in content


async def test_upload_is_idempotent_by_checksum(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)
    payload = "同样的内容不该被重复入库。".encode()

    first = await _upload(client, token, kb_id, "same.txt", payload)
    second = await _upload(client, token, kb_id, "same-again.txt", payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["chunk_count"] == first.json()["chunk_count"]


async def test_upload_rejects_unsupported_format(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    response = await _upload(client, token, kb_id, "archive.zip", b"PK\x03\x04")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_format"


async def test_upload_rejects_oversized_file(client, unique_email: str, monkeypatch) -> None:
    from pyknora.config import get_settings

    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_bytes", 16, raising=False)
    response = await _upload(client, token, kb_id, "big.txt", b"x" * 64)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


async def test_reprocess_bumps_version_and_replaces_chunks(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)
    document_id = (await _upload(client, token, kb_id, "v1.txt", "第一版内容".encode())).json()[
        "id"
    ]

    before = await client.get(f"/api/v1/documents/{document_id}/chunks", headers=_auth(token))
    first_chunk_ids = [item["id"] for item in before.json()["items"]]

    response = await client.post(f"/api/v1/documents/{document_id}/reprocess", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["status"] == "ready"

    after = await client.get(f"/api/v1/documents/{document_id}/chunks", headers=_auth(token))
    second_chunk_ids = [item["id"] for item in after.json()["items"]]
    assert second_chunk_ids and set(second_chunk_ids).isdisjoint(first_chunk_ids)


async def test_delete_document_removes_chunks_and_hides_document(client, unique_email: str) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)
    document_id = (await _upload(client, token, kb_id, "gone.txt", "待删除文档".encode())).json()[
        "id"
    ]

    deleted = await client.delete(f"/api/v1/documents/{document_id}", headers=_auth(token))
    assert deleted.status_code == 204

    detail = await client.get(f"/api/v1/documents/{document_id}", headers=_auth(token))
    assert detail.status_code == 404
    chunks = await client.get(f"/api/v1/documents/{document_id}/chunks", headers=_auth(token))
    assert chunks.status_code == 404
    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=_auth(token))
    assert listed.json() == []


async def test_cross_workspace_access_is_denied(client, unique_email: str) -> None:
    owner = await _register(client, unique_email)
    workspace_id = await _workspace(client, owner)
    kb_id = await _kb(client, owner, workspace_id)
    document_id = (await _upload(client, owner, kb_id, "secret.txt", "机密内容".encode())).json()[
        "id"
    ]

    intruder = await _register(client, f"other-{unique_email}")

    assert (
        await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=_auth(intruder))
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/documents/{document_id}", headers=_auth(intruder))
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/workspaces/{workspace_id}/knowledge-bases", headers=_auth(intruder)
        )
    ).status_code == 404
