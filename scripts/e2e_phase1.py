#!/usr/bin/env python
"""Phase 1（P0 MVP）端到端冒烟：只靠 HTTP，不碰数据库内部。

覆盖链路：健康检查 → 注册/登录 → 工作区 → 知识库 → 上传入库 → 检索 → 会话提问与引用 → 静态前端。

用法::

    uv run python scripts/e2e_phase1.py                       # 默认 http://127.0.0.1:8000
    uv run python scripts/e2e_phase1.py --base-url http://127.0.0.1:8017
    uv run python scripts/e2e_phase1.py --queue       # 队列形态：轮询到 ready
    uv run python scripts/e2e_phase1.py --url https://example.com   # 额外一次 URL 导入

退出码 0 = 全部通过；1 = 有断言失败（失败项打印在 JSON 的 ``failures``）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

DOC_TEXT = (
    "# RG-01 访问控制\n\n"
    "RG-01 要求：所有业务资源必须携带 workspace_id，并在后端校验主体、工作区与资源权限。\n"
    "任何检索、引用、对象下载、任务恢复与工具调用都必须在服务端重新校验作用域，"
    "不得信任客户端提交的工作区标识。\n"
    "对于跨工作区访问，系统必须返回 404 而不是泄露资源是否存在。\n"
)


def _step(results: list[dict[str, object]], name: str, ok: bool, detail: object = None) -> bool:
    results.append({"step": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail is not None else ""))
    return ok


def run(base_url: str, *, queue: bool, url_import: str | None, timeout: float) -> int:
    results: list[dict[str, object]] = []
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct-horse-battery"

    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        health = client.get("/health")
        checks = health.json().get("checks", {}) if health.status_code == 200 else {}
        _step(
            results,
            "health",
            health.status_code == 200 and checks.get("database", {}).get("status") == "ok",
            checks,
        )

        page = client.get("/app/")
        _step(
            results,
            "frontend",
            page.status_code == 200 and "PyKnora" in page.text,
            page.status_code,
        )

        register = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "e2e"},
        )
        if not _step(results, "register", register.status_code == 201, register.status_code):
            return 1
        headers = {"Authorization": f"Bearer {register.json()['access_token']}"}

        workspace = client.post("/api/v1/workspaces", json={"name": "E2E 工作区"}, headers=headers)
        if not _step(results, "workspace", workspace.status_code == 201, workspace.status_code):
            return 1
        workspace_id = workspace.json()["id"]

        kb = client.post(
            f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
            json={"name": "E2E 知识库"},
            headers=headers,
        )
        if not _step(results, "knowledge-base", kb.status_code == 201, kb.status_code):
            return 1
        kb_id = kb.json()["id"]

        upload = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files={"file": ("rg01.md", DOC_TEXT.encode("utf-8"), "text/markdown")},
            headers=headers,
        )
        document = upload.json() if upload.status_code == 201 else {}
        _step(results, "upload", upload.status_code == 201, {"status": document.get("status")})

        document_id = document.get("id")
        if queue:
            deadline = time.monotonic() + timeout
            status = document.get("status")
            while document_id and status != "ready" and time.monotonic() < deadline:
                time.sleep(1.0)
                current = client.get(f"/api/v1/documents/{document_id}", headers=headers)
                status = current.json().get("status") if current.status_code == 200 else status
            _step(results, "ingest-queued", status == "ready", status)
        else:
            _step(
                results, "ingest-inline", document.get("status") == "ready", document.get("status")
            )

        search = client.post(
            "/api/v1/retrieval/search",
            json={"workspace_id": workspace_id, "query": "RG-01 访问控制 要求", "top_k": 5},
            headers=headers,
        )
        hits = search.json().get("hits", []) if search.status_code == 200 else []
        _step(
            results,
            "retrieval",
            search.status_code == 200 and len(hits) > 0,
            {
                "total": len(hits),
                "mode": search.json().get("mode") if search.status_code == 200 else None,
            },
        )

        conversation = client.post(
            "/api/v1/conversations",
            json={"workspace_id": workspace_id, "title": "E2E"},
            headers=headers,
        )
        if not _step(
            results, "conversation", conversation.status_code == 201, conversation.status_code
        ):
            return 1
        conversation_id = conversation.json()["id"]

        answer = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"question": "RG-01 要求所有业务资源必须做什么？", "knowledge_base_ids": [kb_id]},
            headers=headers,
        )
        body = answer.json() if answer.status_code == 200 else {}
        citations = body.get("citations") or []
        _step(
            results,
            "answer",
            answer.status_code == 200 and body.get("status") == "completed" and len(citations) > 0,
            {
                "status": body.get("status"),
                "citations": len(citations),
                "model": (body.get("trace") or {}).get("chat_model"),
            },
        )

        if url_import:
            imported = client.post(
                f"/api/v1/knowledge-bases/{kb_id}/documents/from-url",
                json={"url": url_import},
                headers=headers,
            )
            _step(results, "url-import", imported.status_code in {200, 201}, imported.status_code)

    failures = [item for item in results if not item["ok"]]
    print(
        json.dumps(
            {"base_url": base_url, "queue": queue, "steps": len(results), "failures": failures},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PyKnora Phase 1 端到端冒烟")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--queue", action="store_true", help="队列形态：轮询文档状态直到 ready")
    parser.add_argument(
        "--url", default=None, help="额外验证一次 URL 导入（需 BASE_URL 允许该目标）"
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    return run(args.base_url, queue=args.queue, url_import=args.url, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
