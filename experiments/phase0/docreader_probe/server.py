"""P0-T10 的 DocReader **参考语义 shim**（不是被验证的服务本体）。

用途：在 ``127.0.0.1:5600`` 上用真实 gRPC（HTTP/2 + protobuf）跑通
``tests/phase0/test_docreader_wire.py``，验证 PyKnora 侧 generated stub 与
``client.py`` 的流完整性判定。

逐条复刻自只读参考 ``references/WeKnora@662abaf9``：

* ``docreader/main.py:233-236`` 解析结果为空 → ``ReadResponse.error``，gRPC status 仍 OK
* ``docreader/main.py:257-261`` 解析抛异常 → ``error=str(exc)``，status 仍 OK
* ``docreader/main.py:314``      流解析异常 → **首帧** ``meta.error`` 后正常结束
* ``docreader/main.py:321-326``  流解析结果为空 → 首帧 ``meta.error="Failed to parse: ..."``
* ``docreader/main.py:332-342``  正常时首帧 meta 携带 markdown / ``image_count`` / blocks
* ``docreader/main.py:344-354``  meta 之后一帧一张图，顺序与解析结果一致
* ``docreader/main.py:355-357``  发送期异常直接 ``raise``（Python gRPC 映射为 UNKNOWN）
* ``docreader/main.py:62-111``   图片内联 bytes、filename=basename、mime 按扩展名映射
* ``docreader/auth.py:110-198``  token 不匹配 ``abort(UNAUTHENTICATED, ...)``

**明确不声称**：这里的文档内容、引擎清单、预处理数值都是可控 fixture（文件内的
``shim fixture`` 字样），不代表 WeKnora 的解析质量或引擎可用性。真实镜像 digest/许可
``UNKNOWN``（P0-T09 §4.1/§4.3/§9），真实服务验证记 ``NOT_RUN``。
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from dataclasses import dataclass, field
from typing import Any

import grpc

from .generated import docreader_pb2 as pb
from .generated import docreader_pb2_grpc

MIB = 1024 * 1024
#: 与 docreader/config.py 的默认 gRPC 上限一致（单条消息 50 MiB）。
DEFAULT_MAX_BYTES = 50 * MIB
AUTH_DETAIL = "Invalid or missing authentication token"
_MIN_IMAGE_BYTES = 32

_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_JPEG_HEADER = b"\xff\xd8\xff\xe0"


@dataclass(frozen=True)
class Gate:
    """在 meta 帧之后挂起，等待 RPC 终止（客户端取消或 deadline 到期）。"""

    event_name: str
    wait_s: float = 10.0


@dataclass
class ScriptedDocument:
    """一次请求的服务端行为脚本；字段与 proto 响应面一一对应。"""

    content: str | None = None
    images: list[tuple[str, bytes | str]] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    source_blocks: list[tuple[int, int, str]] = field(default_factory=list)
    parse_error: str | None = None
    fail_after_images: int | None = None
    gate: Gate | None = None
    omit_meta: bool = False


def _to_valid_utf8_text(text: str) -> str:
    """复刻 ``main.py:39-43``：孤立代理项替换为 U+FFFD，避免序列化失败。"""
    return _SURROGATE_RE.sub("\ufffd", text)


def _source_desc(request: Any) -> str:
    """来源描述：优先 url，否则 file_name（``main.py:205`` / ``223``）。"""
    return request.url or request.file_name or "<unknown source>"


def _decode_image(value: bytes | str) -> bytes:
    """复刻 ``main.py:75-86``：bytes 原样用；str 按 base64 解码，失败退回原字节。"""
    if isinstance(value, bytes):
        return value
    try:
        return base64.b64decode(value.encode(), validate=True)
    except ValueError:
        return value.encode()


def _image_ref(ref_path: str, image_data: bytes) -> pb.ImageRef:
    """复刻 ``main.py:89-111``：filename=basename，mime 按扩展名，图片内联 bytes。"""
    filename = os.path.basename(ref_path) or "image.png"
    ext = os.path.splitext(filename)[1].lower()
    return pb.ImageRef(
        filename=filename,
        original_ref=ref_path,
        mime_type=_MIME_BY_EXT.get(ext, "application/octet-stream"),
        storage_key="",
        image_data=image_data,
    )


def _resolve_images(images: list[tuple[str, bytes | str]]) -> list[pb.ImageRef]:
    return [_image_ref(ref_path, _decode_image(data)) for ref_path, data in images]


def _source_blocks(blocks: list[tuple[int, int, str]]) -> list[pb.SourceBlock]:
    return [
        pb.SourceBlock(start=start, end=end, locator_json=locator)
        for start, end, locator in blocks
    ]


def _abort_unary_unary(request: Any, context: Any) -> None:
    context.abort(grpc.StatusCode.UNAUTHENTICATED, AUTH_DETAIL)


def _abort_unary_stream(request: Any, context: Any) -> Iterator[Any]:
    context.abort(grpc.StatusCode.UNAUTHENTICATED, AUTH_DETAIL)
    yield  # pragma: no cover - context.abort 已经抛异常


class _TokenAuthInterceptor(grpc.ServerInterceptor):
    """复刻 ``auth.py:156-198``：token 不匹配时原 handler 不被执行。"""

    def __init__(self, token: str) -> None:
        self._token = token

    def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        metadata = {
            item.key: item.value for item in handler_call_details.invocation_metadata or ()
        }
        supplied = metadata.get("authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[len("Bearer ") :]
        handler = continuation(handler_call_details)
        if supplied and hmac.compare_digest(supplied, self._token):
            return handler
        if handler is None:  # pragma: no cover - 只有未注册方法才会走到
            return None
        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(_abort_unary_unary)
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(_abort_unary_stream)
        if handler.stream_unary:  # pragma: no cover - DocReader 没有这类 RPC
            return grpc.stream_unary_rpc_method_handler(_abort_unary_unary)
        return grpc.stream_stream_rpc_method_handler(_abort_unary_stream)  # pragma: no cover


class ProbeServer:
    """真实 TCP gRPC 上的 DocReader shim；所有调用与观测都留档供断言。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 5600,
        token: str | None = None,
        max_send_bytes: int = DEFAULT_MAX_BYTES,
        max_receive_bytes: int = DEFAULT_MAX_BYTES,
        max_workers: int = 8,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.token = token
        self.max_send_bytes = max_send_bytes
        self.max_receive_bytes = max_receive_bytes
        self.scripts: dict[str, ScriptedDocument] = {}
        #: ListEngines 的返回值是 **shim fixture**，不是真实注册表快照。
        self.engines: list[dict[str, Any]] = [
            {
                "name": "builtin",
                "description": "built-in parsers (shim fixture)",
                "file_types": ["pdf", "docx", "html"],
                "available": True,
            },
            {
                "name": "mineru",
                "description": "MinerU engine (shim fixture)",
                "file_types": ["pdf"],
                "available": False,
                "unavailable_reason": "engine not configured in this probe",
            },
        ]
        self.calls: list[dict[str, Any]] = []
        self.observations: dict[str, dict[str, Any]] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        interceptors = (_TokenAuthInterceptor(token),) if token else ()
        self._server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="t10-docreader"),
            interceptors=interceptors,
            options=[
                ("grpc.max_send_message_length", max_send_bytes),
                ("grpc.max_receive_message_length", max_receive_bytes),
            ],
        )
        docreader_pb2_grpc.add_DocReaderServicer_to_server(_Servicer(self), self._server)
        self.bound_port = self._server.add_insecure_port(f"{host}:{port}")

    @property
    def target(self) -> str:
        return f"{self.host}:{self.bound_port}"

    def start(self) -> None:
        self._server.start()

    def stop(self, grace: float = 2.0) -> None:
        self._server.stop(grace).wait(timeout=grace + 5.0)

    # -- 脚本与默认行为 ----------------------------------------------------
    def register(self, request_id: str, document: ScriptedDocument) -> None:
        self.scripts[request_id] = document

    def script_for(self, request: Any) -> ScriptedDocument:
        if request.request_id and request.request_id in self.scripts:
            return self.scripts[request.request_id]
        file_type = request.file_type or os.path.splitext(request.file_name)[1][1:]
        if file_type == "pdf":
            return ScriptedDocument(
                content="# PDF sample\n\npage one text\n\npage two text\n",
                images=[
                    ("media/page_001.png", _PNG_HEADER + b"pdf-page-1"),
                    ("media/page_002.jpg", _JPEG_HEADER + b"pdf-page-2"),
                ],
                metadata={"engine": "builtin", "page_count": "2"},
                source_blocks=[(0, 12, '{"type":"pdf","page":1}')],
            )
        if file_type == "docx":
            return ScriptedDocument(
                content="# DOCX sample\n\nparagraph text\n",
                metadata={"engine": "docx2"},
            )
        # 复刻 main.py:257-261：解析异常被折叠成 error 字段，status 仍 OK
        return ScriptedDocument(parse_error=f"Unsupported file type: {file_type}")

    # -- 观测 --------------------------------------------------------------
    def record_call(self, method: str, request: Any, context: Any) -> None:
        entry: dict[str, Any] = {
            "method": method,
            "request_type": request.DESCRIPTOR.name,
            "peer": context.peer(),
            "metadata": dict(context.invocation_metadata()),
        }
        if request.DESCRIPTOR.name == "ReadRequest":
            config = request.config
            entry.update(
                {
                    "request_id": request.request_id,
                    "file_name": request.file_name,
                    "file_type": request.file_type,
                    "title": request.title,
                    "url": request.url,
                    "file_bytes": len(request.file_content),
                    "parser_engine": config.parser_engine,
                    "overrides": dict(config.parser_engine_overrides),
                }
            )
        elif request.DESCRIPTOR.name == "ListEnginesRequest":
            entry["config_overrides"] = dict(request.config_overrides)
        elif request.DESCRIPTOR.name == "PreprocessImageRequest":
            entry["image_bytes"] = len(request.image)
        with self._lock:
            self.calls.append(entry)
        self.notify("call_recorded")

    def notify(self, name: str) -> None:
        with self._lock:
            event = self._events.setdefault(name, threading.Event())
        event.set()

    def observe(self, name: str, **data: Any) -> None:
        with self._lock:
            self.observations[name] = dict(data)
            event = self._events.setdefault(name, threading.Event())
        event.set()

    def wait_observation(self, name: str, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            event = self._events.setdefault(name, threading.Event())
        if not event.wait(timeout):
            raise TimeoutError(f"未在 {timeout}s 内观察到 {name!r}")
        with self._lock:
            return dict(self.observations.get(name, {}))

    def last_call(self, method: str) -> dict[str, Any]:
        with self._lock:
            for entry in reversed(self.calls):
                if entry["method"] == method:
                    return dict(entry)
        raise AssertionError(f"没有记录到 {method} 调用")

    # -- 响应组装 ----------------------------------------------------------
    def metadata_for(self, script: ScriptedDocument, request: Any) -> dict[str, str]:
        """文档 metadata + 请求回显。

        回显键（``probe.*``）是本 shim 的观测手段，真实服务不会这样做；
        它们只用于断言请求字段确实跨过了 wire。
        """
        metadata = dict(script.metadata)
        config = request.config
        metadata.update(
            {
                "probe.request_id": request.request_id,
                "probe.file_name": request.file_name,
                "probe.file_type": request.file_type,
                "probe.title": request.title,
                "probe.file_bytes": str(len(request.file_content)),
                "probe.parser_engine": config.parser_engine,
            }
        )
        for key, value in config.parser_engine_overrides.items():
            metadata[f"probe.override.{key}"] = value
        return metadata

    def wait_for_termination(self, context: Any, gate: Gate) -> None:
        """在 gate 处挂起，直到 RPC 终止（客户端取消或 deadline 到期）。

        ``context.add_callback`` 在 RPC 结束时触发，既覆盖取消也覆盖超时，
        而且调用已结束时回调会立即执行，因此不会漏掉竞态。
        """
        self.observe(f"{gate.event_name}.entered")
        terminated = threading.Event()
        context.add_callback(terminated.set)
        started = time.perf_counter()
        observed = terminated.wait(gate.wait_s)
        self.observe(
            gate.event_name,
            observed=observed,
            is_active=context.is_active(),
            waited_s=round(time.perf_counter() - started, 4),
        )


class _Servicer(docreader_pb2_grpc.DocReaderServicer):
    """4 个 RPC 的 shim；所有行为都由 ``ProbeServer`` 的脚本与观测驱动。"""

    def __init__(self, owner: ProbeServer) -> None:
        self._owner = owner

    def Read(self, request: Any, context: Any) -> pb.ReadResponse:
        owner = self._owner
        owner.record_call("/docreader.DocReader/Read", request, context)
        script = owner.script_for(request)
        if script.parse_error is not None:
            return pb.ReadResponse(error=script.parse_error)
        if not script.content:
            return pb.ReadResponse(error=f"Failed to parse: {_source_desc(request)}")
        return pb.ReadResponse(
            markdown_content=_to_valid_utf8_text(script.content),
            image_refs=_resolve_images(script.images),
            image_dir_path="",
            metadata=owner.metadata_for(script, request),
            source_blocks=_source_blocks(script.source_blocks),
        )

    def ReadStream(self, request: Any, context: Any) -> Iterator[pb.ReadStreamResponse]:
        owner = self._owner
        owner.record_call("/docreader.DocReader/ReadStream", request, context)
        script = owner.script_for(request)
        if script.parse_error is not None:
            # main.py:314：解析期异常 → 首帧 meta.error，status 保持 OK
            yield pb.ReadStreamResponse(meta=pb.ReadStreamMeta(error=script.parse_error))
            return
        if not script.content:
            # main.py:321-326：空结果同样只在 meta.error 里表达
            yield pb.ReadStreamResponse(
                meta=pb.ReadStreamMeta(error=f"Failed to parse: {_source_desc(request)}")
            )
            return
        if not script.omit_meta:
            yield pb.ReadStreamResponse(
                meta=pb.ReadStreamMeta(
                    markdown_content=_to_valid_utf8_text(script.content),
                    image_dir_path="",
                    metadata=owner.metadata_for(script, request),
                    image_count=len(script.images),
                    source_blocks=_source_blocks(script.source_blocks),
                )
            )
        if script.gate is not None:
            owner.wait_for_termination(context, script.gate)
        for sent, (ref_path, data) in enumerate(script.images):
            if script.fail_after_images is not None and sent >= script.fail_after_images:
                # main.py:355-357：发送期异常直接 raise，由 gRPC 映射为非 OK status
                raise RuntimeError(
                    f"simulated send-phase failure after {sent} image frames"
                    " (mirrors docreader/main.py:355-357)"
                )
            yield pb.ReadStreamResponse(image=_image_ref(ref_path, _decode_image(data)))

    def ListEngines(self, request: Any, context: Any) -> pb.ListEnginesResponse:
        owner = self._owner
        owner.record_call("/docreader.DocReader/ListEngines", request, context)
        return pb.ListEnginesResponse(
            engines=[pb.ParserEngineInfo(**engine) for engine in owner.engines]
        )

    def PreprocessImage(self, request: Any, context: Any) -> pb.PreprocessImageResponse:
        owner = self._owner
        owner.record_call("/docreader.DocReader/PreprocessImage", request, context)
        image = request.image
        if len(image) < _MIN_IMAGE_BYTES:
            # proto:18-23：不可纠正的图片不是错误，只用 status 描述
            return pb.PreprocessImageResponse(image=b"", fallback_needed=True, status="no_text")
        return pb.PreprocessImageResponse(
            image=_JPEG_HEADER + image,
            fallback_needed=False,
            theta=1.25,
            residual=0.5,
            flip=False,
            n_boxes=7,
            n_kept=6,
            status="ok",
        )
