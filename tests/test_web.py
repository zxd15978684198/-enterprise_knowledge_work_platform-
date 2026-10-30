"""静态前端挂载（W22）：``/app`` 提供纯静态页面，``/`` 重定向过去。

前端刻意不引入 Node 构建链：部署形态下就是一个目录，缺文件时也不该让 API 起不来。
"""

from __future__ import annotations

from typing import Any

import pytest


async def test_app_index_served(client: Any) -> None:
    response = await client.get("/app/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "PyKnora" in response.text
    assert "./app.js" in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/app/app.js", "javascript"),
        ("/app/styles.css", "text/css"),
    ],
)
async def test_app_assets_served(client: Any, path: str, content_type: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.text.strip(), f"{path} 不应为空"


async def test_root_redirects_to_app(client: Any) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in {307, 308}
    assert response.headers["location"] == "/app/"


async def test_openapi_still_available(client: Any) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    # 静态挂载不能遮住 API 路由。
    assert "/api/v1/retrieval/search" in paths
    assert "/api/v1/conversations/{conversation_id}/messages" in paths
