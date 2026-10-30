"""单 URL 受控导入：SSRF 防护、逐跳重定向、资源上限、幂等与跨区隔离。"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from pyknora.config import get_settings
from pyknora.ingest import url_fetch
from pyknora.ingest.url_fetch import UrlFetchError, derive_filename, normalize_url
from pyknora.storage import reset_storage

PASSWORD = "correct-horse-battery"

PAGE_HTML = (
    b"<html><body><h1>PyKnora</h1><p>" + b"knowledge base context " * 20 + b"</p></body></html>"
)


@contextlib.contextmanager
def _serve(routes: dict[str, tuple[int, dict[str, str], bytes]]) -> Iterator[str]:
    """起一个只在回环监听的临时 HTTP 服务，返回 base_url。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status_code, headers, body = routes.get(
                self.path, (404, {"Content-Type": "text/plain"}, b"not found")
            )
            self.send_response(status_code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """静默访问日志，避免污染测试输出。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path):
    """每个用例独立的本地对象存储目录，避免写入仓库 data/。"""

    reset_storage(tmp_path / "uploads")
    yield


@pytest.fixture
def allow_loopback(monkeypatch):
    """仅测试用：允许访问回环地址（生产默认禁止）。"""

    monkeypatch.setattr(get_settings(), "url_allow_private_network", True)
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


def _import(client, token: str, kb_id: str, url: str, title: str | None = None):
    payload: dict[str, str] = {"url": url}
    if title:
        payload["title"] = title
    return client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/from-url",
        json=payload,
        headers=_auth(token),
    )


# —— URL 规范化 ——


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://Example.COM/doc", "http://example.com/doc"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a?b=1#frag", "https://example.com/a?b=1"),
        ("  https://example.com/x  ", "https://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
    ],
)
def test_normalize_url_accepts_and_canonicalizes(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "invalid_url"),
        ("ftp://example.com/a.txt", "unsupported_scheme"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("gopher://example.com/", "unsupported_scheme"),
        ("http://user:pass@example.com/", "url_credentials_not_allowed"),
        ("http:///nohost", "invalid_url"),
        ("http://example.com:99999/", "invalid_url"),
        ("http://example.com/" + "a" * 3000, "invalid_url"),
    ],
)
def test_normalize_url_rejects_unsafe_forms(raw: str, code: str) -> None:
    with pytest.raises(UrlFetchError) as excinfo:
        normalize_url(raw)
    assert excinfo.value.code == code


def test_derive_filename_falls_back_to_content_type() -> None:
    assert derive_filename("https://example.com/manual.pdf", "text/html") == "manual.pdf"
    assert derive_filename("https://example.com/docs/", "text/html") == "docs.html"
    assert derive_filename("https://example.com/", "text/plain") == "page.txt"
    assert derive_filename("https://example.com/x", "text/markdown") == "x.md"


# —— 私网判定 ——


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.1.2.3",
        "172.16.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.10",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ],
)
def test_blocked_reason_blocks_non_public_addresses(ip: str) -> None:
    assert url_fetch._blocked_reason(ip) is not None


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_blocked_reason_allows_public_addresses(ip: str) -> None:
    assert url_fetch._blocked_reason(ip) is None


async def test_assert_public_target_is_fail_closed(monkeypatch) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.7"]

    monkeypatch.setattr(url_fetch, "_resolve_ips", fake_resolve)
    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.assert_public_target("mixed.example", 80)
    assert excinfo.value.code == "blocked_target"


# —— 抓取层（用 MockTransport 注入响应，不依赖外网）——


async def test_redirect_to_private_target_is_blocked(monkeypatch) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"] if host == "public.example" else ["169.254.169.254"]

    monkeypatch.setattr(url_fetch, "_resolve_ips", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "public.example"
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.fetch_url(
            "http://public.example/start", transport=httpx.MockTransport(handler)
        )
    assert excinfo.value.code == "blocked_target"


async def test_redirect_hop_limit_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://loop.example/next"})

    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.fetch_url(
            "https://loop.example/start",
            transport=httpx.MockTransport(handler),
            max_redirects=2,
            allow_private_network=True,
        )
    assert excinfo.value.code == "too_many_redirects"


async def test_response_size_limit_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 500)

    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.fetch_url(
            "https://big.example/data",
            transport=httpx.MockTransport(handler),
            max_bytes=100,
            allow_private_network=True,
        )
    assert excinfo.value.code == "response_too_large"
    assert excinfo.value.status_code == 413


async def test_content_type_allow_list_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04"
        )

    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.fetch_url(
            "https://bin.example/a.zip",
            transport=httpx.MockTransport(handler),
            allow_private_network=True,
        )
    assert excinfo.value.code == "unsupported_content_type"
    assert excinfo.value.status_code == 415


async def test_dns_failure_is_reported(monkeypatch) -> None:
    async def failing_resolve(host: str, port: int) -> list[str]:
        raise UrlFetchError("dns_resolution_failed", "域名解析失败")

    monkeypatch.setattr(url_fetch, "_resolve_ips", failing_resolve)
    with pytest.raises(UrlFetchError) as excinfo:
        await url_fetch.fetch_url("https://no-such-host.invalid/x")
    assert excinfo.value.code == "dns_resolution_failed"


# —— API 层 ——


async def test_api_rejects_private_target(client, unique_email) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    response = await _import(client, token, kb_id, "http://169.254.169.254/latest/meta-data/")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "blocked_target"


async def test_api_rejects_unsupported_scheme(client, unique_email) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    response = await _import(client, token, kb_id, "file:///etc/passwd")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unsupported_scheme"


async def test_api_imports_url_and_is_idempotent(client, unique_email, allow_loopback) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)

    routes = {
        "/doc": (200, {"Content-Type": "text/html; charset=utf-8"}, PAGE_HTML),
    }
    with _serve(routes) as base:
        first = await _import(client, token, kb_id, f"{base}/doc", title="从 URL 导入")
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["source_type"] == "url"
        assert body["source_url"] == f"{base}/doc"
        assert body["status"] == "ready"
        assert body["chunk_count"] >= 1
        assert body["filename"] == "doc.html"
        assert body["title"] == "从 URL 导入"

        second = await _import(client, token, kb_id, f"{base}/doc")
        assert second.status_code == 200, second.text
        assert second.json()["id"] == body["id"]


async def test_api_enforces_size_and_type_limits(
    client, unique_email, allow_loopback, monkeypatch
) -> None:
    token = await _register(client, unique_email)
    workspace_id = await _workspace(client, token)
    kb_id = await _kb(client, token, workspace_id)
    monkeypatch.setattr(get_settings(), "url_max_bytes", 64)

    routes = {
        "/big": (200, {"Content-Type": "text/plain"}, b"y" * 4096),
        "/zip": (200, {"Content-Type": "application/zip"}, b"PK\x03\x04"),
    }
    with _serve(routes) as base:
        too_big = await _import(client, token, kb_id, f"{base}/big")
        assert too_big.status_code == 413, too_big.text
        assert too_big.json()["error"]["code"] == "response_too_large"

        bad_type = await _import(client, token, kb_id, f"{base}/zip")
        assert bad_type.status_code == 415, bad_type.text
        assert bad_type.json()["error"]["code"] == "unsupported_content_type"


async def test_api_cross_workspace_import_is_denied(client, unique_email, allow_loopback) -> None:
    owner = await _register(client, unique_email)
    owner_workspace = await _workspace(client, owner)
    kb_id = await _kb(client, owner, owner_workspace)

    intruder = await _register(client, f"intruder-{unique_email}")
    await _workspace(client, intruder, name="别人的工作区")

    with _serve({"/doc": (200, {"Content-Type": "text/html"}, PAGE_HTML)}) as base:
        response = await _import(client, intruder, kb_id, f"{base}/doc")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
