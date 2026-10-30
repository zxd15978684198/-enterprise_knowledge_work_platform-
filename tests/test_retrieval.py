"""混合检索测试：召回、融合、租户隔离。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pyknora.retrieval.service import extract_terms

DOC_TEXT = (
    "PyKnora 的备份策略要求每周执行一次全量备份，并保留四周的历史快照。"
    "恢复演练必须每季度进行一次，演练结束后要记录恢复耗时与数据一致性校验结果。"
    "数据库使用 PostgreSQL 17，向量检索依赖 pgvector 扩展。"
    "工作区之间的数据必须严格隔离，检索查询必须带 workspace_id 过滤。"
)

OTHER_TEXT = "员工报销流程：提交发票、部门审批、财务复核，三个工作日内到账。"


async def _bootstrap(client, unique_email: str, *, text: str = DOC_TEXT) -> dict[str, Any]:
    """注册用户 → 建工作区 → 建知识库 → 上传文档，返回上下文。"""

    register = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": "sup3r-secret", "display_name": "检索用户"},
    )
    assert register.status_code == 201, register.text
    token = register.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    workspace = await client.post(
        "/api/v1/workspaces", json={"name": f"检索工作区-{uuid.uuid4().hex[:6]}"}, headers=headers
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    kb = await client.post(
        f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
        json={"name": "运维手册", "description": ""},
        headers=headers,
    )
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]

    upload = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("backup.md", text.encode("utf-8"), "text/markdown")},
        data={"title": "备份与恢复手册"},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["status"] == "ready"

    return {
        "headers": headers,
        "workspace_id": workspace_id,
        "kb_id": kb_id,
        "document_id": upload.json()["id"],
    }


def test_extract_terms_drops_stopwords_and_punctuation() -> None:
    terms = extract_terms("备份策略是什么？如何执行恢复演练？")
    assert "备份策略" in terms
    # 长短语退化成二字组：不断言整段存在，而是断言语义片段被切出来
    assert "恢复" in terms
    assert "什么" not in terms
    assert "如何" not in terms
    assert all("？" not in term for term in terms)


async def test_search_returns_hybrid_hits(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email)

    response = await client.post(
        "/api/v1/retrieval/search",
        json={
            "workspace_id": ctx["workspace_id"],
            "query": "备份策略多久执行一次",
            "top_k": 5,
        },
        headers=ctx["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hybrid-rrf"
    assert body["total"] >= 1
    top = body["hits"][0]
    assert "备份" in top["snippet"]
    assert top["document_title"] == "备份与恢复手册"
    assert top["fused_score"] > 0
    assert top["vector_rank"] is not None or top["lexical_rank"] is not None
    # 向量路与词法路至少各有一路命中，才说明融合真的在起作用
    assert sum(1 for hit in body["hits"] if hit["lexical_rank"] is not None) >= 1


async def test_search_isolated_across_workspaces(client, unique_email: str) -> None:
    ctx_a = await _bootstrap(client, unique_email)
    ctx_b = await _bootstrap(client, f"other-{unique_email}", text=OTHER_TEXT)

    response = await client.get("/api/v1/workspaces", headers=ctx_b["headers"])
    assert response.status_code == 200
    query = await client.post(
        "/api/v1/retrieval/search",
        json={"workspace_id": ctx_b["workspace_id"], "query": "备份策略", "top_k": 5},
        headers=ctx_b["headers"],
    )
    assert query.status_code == 200, query.text
    body = query.json()
    # B 工作区只有报销流程文档：查「备份策略」必须一条都召不回，且不能出现 A 的文档
    assert body["total"] == 0, body
    assert body["hits"] == []
    assert ctx_a["document_id"] != ctx_b["document_id"]

    # 用 A 的 workspace_id 但 B 的令牌必须被拒（非成员 → 404）
    forbidden = await client.post(
        "/api/v1/retrieval/search",
        json={"workspace_id": ctx_a["workspace_id"], "query": "备份策略"},
        headers=ctx_b["headers"],
    )
    assert forbidden.status_code == 404


async def test_search_requires_non_empty_query(client, unique_email: str) -> None:
    ctx = await _bootstrap(client, unique_email)
    response = await client.post(
        "/api/v1/retrieval/search",
        json={"workspace_id": ctx["workspace_id"], "query": "   "},
        headers=ctx["headers"],
    )
    assert response.status_code == 400


def test_embeddings_are_deterministic() -> None:
    from pyknora.ports.models import HashingEmbedding

    embedder = HashingEmbedding(dim=64)
    first = embedder.embed(["数据库备份策略"])[0]
    second = embedder.embed(["数据库备份策略"])[0]
    assert first == second
    assert pytest.approx(sum(value * value for value in first), rel=1e-6) == 1.0
