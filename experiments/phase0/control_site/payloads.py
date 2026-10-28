"""控制站用的确定性响应体与测试常量（全部原创合成内容，无第三方素材）。

设计要点：

* 页面与二进制载荷都是**固定字节序列**：同一版代码产生同一字节，因此
  ``experiments/phase0/fixtures/manifest.json`` 可以登记 sha256，并由测试逐字节校验
  （manifest 与真实响应的绑定是被断言出来的，而不是写文档时手抄的）。
* 二进制载荷由 SHA-256 计数器模式生成，无随机数、无网络、无第三方字体/图片。
* ``TEST_USER`` / ``TEST_PASSWORD`` 是**合成测试常量，不是真实凭据**，
  只用于验证「没有会话就不放行」和人工登录流程，任何生产环境都不得复用。
"""

from __future__ import annotations

import hashlib

TEST_USER = "phase0-manual-user"
TEST_PASSWORD = "phase0-manual-pass"  # 合成测试常量，非秘密；不得用于任何真实环境
SESSION_COOKIE = "phase0_manual_session"

#: 载荷版本号：任何页面字节变动都必须同步改这里与 ``fixtures/manifest.json``。
PAYLOAD_ID = "control-site/v1"

BINARY_SEED = b"pyknora-phase0-control-site/binary-v1"
BINARY_SIZE = 4096

INDEX_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>PyKnora Phase 0 控制站</title></head>
<body>
<h1>PyKnora Phase 0 control-site</h1>
<p>本机回环测试站：只做计数器/页面/故障靶场，不写任何业务表。</p>
<ul>
  <li><a href="/static/page.html">静态页</a></li>
  <li><a href="/static/js.html">JS 页（会 POST /js/beacon）</a></li>
  <li><a href="/static/iframe.html">iframe 页</a></li>
  <li><a href="/redirect/301">301 跳转</a></li>
  <li><a href="/redirect/302">302 跳转</a></li>
  <li><a href="/download/html">HTML 下载</a></li>
  <li><a href="/download/binary">二进制下载</a></li>
  <li><a href="/login">人工登录页</a></li>
  <li><a href="/healthz">/healthz</a></li>
</ul>
</body>
</html>
"""

STATIC_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站静态页 CS-07</title></head>
<body>
<h1>control-site / static</h1>
<p data-vector="CS-07">静态页向量 CS-07：纯 HTML，无脚本、无外部资源。</p>
<ul>
  <li><a href="/static/js.html">JS 页</a></li>
  <li><a href="/static/iframe.html">iframe 页</a></li>
  <li><a href="/download/html">HTML 下载</a></li>
  <li><a href="/download/binary">二进制下载</a></li>
</ul>
</body>
</html>
"""

JS_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站 JS 页 CS-08</title></head>
<body>
<h1>control-site / js</h1>
<p data-vector="CS-08">JS 页向量 CS-08：页面执行脚本并 POST /js/beacon，
浏览器通道（T18/T29）可用 document.title 断言脚本确实执行。</p>
<p id="beacon-state">pending</p>
<script>
fetch('/js/beacon', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({tag: 'js-page', vector: 'CS-08'})
}).then(function (r) { return r.json(); }).then(function (data) {
  document.title = 'beacon:' + data.count;
  document.getElementById('beacon-state').textContent = 'beacon:' + data.count;
}).catch(function (err) {
  document.title = 'beacon-error';
  document.getElementById('beacon-state').textContent = 'beacon-error:' + err;
});
</script>
</body>
</html>
"""

IFRAME_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站 iframe 页 CS-09</title></head>
<body>
<h1>control-site / iframe</h1>
<p data-vector="CS-09">iframe 页向量 CS-09：内嵌 /static/frame.html，
用于逐帧策略（iframe 隔离）验证，不加载任何站外来源。</p>
<iframe name="phase0-frame" src="/static/frame.html" width="320" height="160"></iframe>
</body>
</html>
"""

FRAME_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站 frame 子页</title></head>
<body>
<p data-vector="CS-09">frame 子页：仅回环来源，无脚本。</p>
</body>
</html>
"""

LOGIN_PAGE_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站人工登录页 CS-16</title></head>
<body>
<h1>需要人工登录</h1>
<p data-vector="CS-16">本页只用于人工参与场景：自动化不得猜测/绕过登录。
表单字段为合成测试账号，不含任何真实凭据。</p>
<p data-test-account="synthetic">人工测试账号：{TEST_USER} / {TEST_PASSWORD}
（合成常量，非真实凭据，仅存在于本控制站内存中）</p>
<form method="post" action="/login">
  <label>账号 <input name="username" autocomplete="off"></label>
  <label>口令 <input name="password" type="password" autocomplete="off"></label>
  <button type="submit">登录</button>
</form>
<p><a href="/protected">受保护资源</a></p>
</body>
</html>
"""

DOWNLOAD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>控制站 HTML 下载 CS-13</title></head>
<body>
<h1>control-site download (html)</h1>
<p data-vector="CS-13">HTML 附件向量 CS-13：固定字节，sha256 登记在 manifest.json。</p>
</body>
</html>
"""


def build_binary_payload(size: int = BINARY_SIZE, seed: bytes = BINARY_SEED) -> bytes:
    """用 SHA-256 计数器模式生成确定性二进制载荷（无随机、无外部依赖）。"""
    if size <= 0:
        raise ValueError("size 必须为正整数")
    if not seed:
        raise ValueError("seed 不得为空")
    chunks: list[bytes] = []
    total = 0
    counter = 0
    while total < size:
        chunk = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        chunks.append(chunk)
        total += len(chunk)
        counter += 1
    return b"".join(chunks)[:size]


BINARY_PAYLOAD = build_binary_payload()


def sha256_hex(payload: bytes) -> str:
    """返回载荷的 sha256 十六进制小写摘要（manifest 与测试共用同一算法）。"""
    return hashlib.sha256(payload).hexdigest()


def static_payloads() -> dict[str, bytes]:
    """返回需要登记 sha256 的固定载荷：路由 -> 字节（manifest 校验用）。"""
    return {
        "/": INDEX_PAGE_HTML.encode("utf-8"),
        "/static/page.html": STATIC_PAGE_HTML.encode("utf-8"),
        "/static/js.html": JS_PAGE_HTML.encode("utf-8"),
        "/static/iframe.html": IFRAME_PAGE_HTML.encode("utf-8"),
        "/static/frame.html": FRAME_PAGE_HTML.encode("utf-8"),
        "/login": LOGIN_PAGE_HTML.encode("utf-8"),
        "/download/html": DOWNLOAD_HTML.encode("utf-8"),
        "/download/binary": BINARY_PAYLOAD,
    }
