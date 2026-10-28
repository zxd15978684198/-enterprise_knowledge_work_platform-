"""P0-T10：DocReader Read/ReadStream 真实 wire 互通（generated stubs + 参考语义 shim）。

运行（必须注入 gRPC 工具链；``experiments/phase0`` 的 pyproject/uv.lock 属 P0-T02 写权）：

    uv run --project experiments/phase0 --with grpcio --with grpcio-tools \
        pytest -c experiments/phase0/pyproject.toml tests/phase0/test_docreader_wire.py -q

覆盖（Backlog P0-T10 逐项）：

* 从**真实 proto**（sha256 ``33d4135c…``）生成 stub，并证明描述符与 proto、
  与 WeKnora 自带 ``docreader_pb2.py`` 的序列化描述符一致；
* Read / ReadStream / ListEngines / PreprocessImage 四个 RPC 全部走真实 TCP gRPC；
* 解析失败**只落 ``error`` 字段、gRPC status 仍 OK**（Backlog 的 ``ParseResponse.error`` 在
  proto v1.0.0 里就是 ``ReadResponse.error`` 字段 5；Read 与 ReadStream 各有用例）；
* ReadStream 帧序（meta 恰一帧且在前）、图片顺序、``image_count``、EOF 正常终止；
* unary 单消息上限 vs ReadStream 分片的批量边界；发送期异常的 status 实测；
* 客户端取消/超时被服务端观察且不挂死；错误 token → UNAUTHENTICATED。

**不覆盖 / 不声称**：真实 DocReader 镜像（P0-T09：digest ``UNKNOWN``、许可 ``UNKNOWN``）
未运行，``docreader_probe/server.py`` 是按参考 wire 语义复刻的 shim。真实服务验证
记 ``NOT_RUN``，解除条件见 ``docreader_manifest.md`` §9。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from docreader_probe import client as wire_client
from docreader_probe import server as wire_server
from docreader_probe.generated import docreader_pb2 as pb
from docreader_probe.generated import docreader_pb2_grpc
from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor as FD

try:
    import grpc
except ModuleNotFoundError as exc:  # pragma: no cover - 缺依赖时给出可执行指引
    raise RuntimeError(
        "P0-T10 wire 测试缺少 grpcio；本任务不改 pyproject.toml/uv.lock（T02 写权）。"
        "请按任务卡临时注入：uv run --project experiments/phase0 --with grpcio "
        "--with grpcio-tools pytest -c experiments/phase0/pyproject.toml "
        "tests/phase0/test_docreader_wire.py -q"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_DIR = REPO_ROOT / "experiments" / "phase0" / "docreader_probe"
PROTO_COPY = PROBE_DIR / "proto" / "docreader.proto"
REF_PROTO = REPO_ROOT / "references" / "WeKnora" / "docreader" / "proto" / "docreader.proto"
REF_PB2 = REPO_ROOT / "references" / "WeKnora" / "docreader" / "proto" / "docreader_pb2.py"
PROVENANCE = PROBE_DIR / "generated" / "PROVENANCE.json"
EVIDENCE_PATH = PROBE_DIR / "wire_evidence.json"

#: P0-T09 ``docs/poc/evidence/docreader_manifest.md`` §1.1 锚点。
PROTO_SHA256 = "33d4135cfcd2ec95f8d160d6cefdd54be669167e8b8fb69ef81095d218a738d0"
PROTO_BYTES = 4689
SOURCE_COMMIT = "662abaf9f944e37689d04c42160034b2cdec6829"

FIXED_PORT = 5600
TOKEN = "t10-shared-secret"
MIB = 1024 * 1024
MESSAGE_CAP = 256 * 1024

_CASES: list[dict[str, Any]] = []
_EVIDENCE: dict[str, Any] = {"task": "P0-T10", "cases": _CASES, "secondary_servers": {}}


# ---------------------------------------------------------------------------
# 证据与计时
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _case_timing(request: pytest.FixtureRequest) -> Iterator[None]:
    """记录每例耗时（验收要求"每例耗时"）。"""
    started = time.perf_counter()
    outcome = "passed"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        _CASES.append(
            {
                "id": request.node.name,
                "outcome": outcome,
                "duration_s": round(time.perf_counter() - started, 4),
            }
        )


@pytest.fixture(scope="session", autouse=True)
def _evidence_writer() -> Iterator[None]:
    """把 proto sha、工具链版本、实际端口、每例耗时落到仓库内证据文件。"""
    _EVIDENCE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    yield
    _EVIDENCE["toolchain"] = {
        "python": sys.version.split()[0],
        "grpcio": grpc.__version__,
        "protobuf": importlib.metadata.version("protobuf"),
        "grpcio-tools": importlib.metadata.version("grpcio-tools"),
        "pytest": importlib.metadata.version("pytest"),
    }
    _EVIDENCE["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _EVIDENCE["real_docreader_service"] = "NOT_RUN (T09: image digest UNKNOWN, licenses UNKNOWN)"
    EVIDENCE_PATH.write_text(
        json.dumps(_EVIDENCE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 服务器 / 客户端 fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def server() -> Iterator[wire_server.ProbeServer]:
    probe = wire_server.ProbeServer(port=FIXED_PORT)
    assert probe.bound_port == FIXED_PORT, (
        f"127.0.0.1:{FIXED_PORT} 必须由 P0-T10 独占，实际绑定 {probe.bound_port}；"
        "端口被占用时先释放，不要改用临时端口掩盖环境事实。"
    )
    probe.start()
    _EVIDENCE["server"] = {"requested_port": FIXED_PORT, "bound_port": probe.bound_port}
    try:
        yield probe
    finally:
        probe.stop()


@pytest.fixture(scope="module")
def wire(server: wire_server.ProbeServer) -> Iterator[wire_client.DocReaderClient]:
    client = wire_client.DocReaderClient(server.target, timeout=5.0)
    client.wait_ready(5.0)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def sized_pair() -> Iterator[tuple[wire_server.ProbeServer, wire_client.DocReaderClient]]:
    probe = wire_server.ProbeServer(
        port=0, max_send_bytes=MESSAGE_CAP, max_receive_bytes=MESSAGE_CAP
    )
    assert probe.bound_port != 0, "临时端口绑定失败"
    probe.start()
    client = wire_client.DocReaderClient(
        probe.target, timeout=10.0, max_receive_bytes=MESSAGE_CAP
    )
    client.wait_ready(5.0)
    _EVIDENCE["secondary_servers"]["message_cap"] = {
        "bound_port": probe.bound_port,
        "cap_bytes": MESSAGE_CAP,
    }
    try:
        yield probe, client
    finally:
        client.close()
        probe.stop()


@pytest.fixture(scope="module")
def auth_pair() -> Iterator[tuple[wire_server.ProbeServer, wire_client.DocReaderClient]]:
    probe = wire_server.ProbeServer(port=0, token=TOKEN)
    assert probe.bound_port != 0, "临时端口绑定失败"
    probe.start()
    client = wire_client.DocReaderClient(probe.target, token=TOKEN, timeout=5.0)
    client.wait_ready(5.0)
    _EVIDENCE["secondary_servers"]["auth"] = {"bound_port": probe.bound_port}
    try:
        yield probe, client
    finally:
        client.close()
        probe.stop()


# ---------------------------------------------------------------------------
# 期望的 proto 形状（逐一对照 docreader.proto，不用 stub 自证）
# ---------------------------------------------------------------------------
OPT = FD.LABEL_OPTIONAL
REP = FD.LABEL_REPEATED
EXPECTED_MESSAGES: dict[str, list[tuple[int, str, int, int, str]]] = {
    "PreprocessImageRequest": [
        (1, "image", FD.TYPE_BYTES, OPT, ""),
    ],
    "PreprocessImageResponse": [
        (1, "image", FD.TYPE_BYTES, OPT, ""),
        (2, "fallback_needed", FD.TYPE_BOOL, OPT, ""),
        (3, "theta", FD.TYPE_FLOAT, OPT, ""),
        (4, "residual", FD.TYPE_FLOAT, OPT, ""),
        (5, "flip", FD.TYPE_BOOL, OPT, ""),
        (6, "n_boxes", FD.TYPE_INT32, OPT, ""),
        (7, "n_kept", FD.TYPE_INT32, OPT, ""),
        (8, "status", FD.TYPE_STRING, OPT, ""),
    ],
    "ReadConfig": [
        (1, "parser_engine", FD.TYPE_STRING, OPT, ""),
        (
            2,
            "parser_engine_overrides",
            FD.TYPE_MESSAGE,
            REP,
            "docreader.ReadConfig.ParserEngineOverridesEntry",
        ),
    ],
    "ReadRequest": [
        (1, "file_content", FD.TYPE_BYTES, OPT, ""),
        (2, "file_name", FD.TYPE_STRING, OPT, ""),
        (3, "file_type", FD.TYPE_STRING, OPT, ""),
        (4, "url", FD.TYPE_STRING, OPT, ""),
        (5, "title", FD.TYPE_STRING, OPT, ""),
        (6, "config", FD.TYPE_MESSAGE, OPT, "docreader.ReadConfig"),
        (7, "request_id", FD.TYPE_STRING, OPT, ""),
    ],
    "ImageRef": [
        (1, "filename", FD.TYPE_STRING, OPT, ""),
        (2, "original_ref", FD.TYPE_STRING, OPT, ""),
        (3, "mime_type", FD.TYPE_STRING, OPT, ""),
        (4, "storage_key", FD.TYPE_STRING, OPT, ""),
        (5, "image_data", FD.TYPE_BYTES, OPT, ""),
    ],
    "SourceBlock": [
        (1, "start", FD.TYPE_UINT32, OPT, ""),
        (2, "end", FD.TYPE_UINT32, OPT, ""),
        (3, "locator_json", FD.TYPE_STRING, OPT, ""),
    ],
    "ReadResponse": [
        (1, "markdown_content", FD.TYPE_STRING, OPT, ""),
        (2, "image_refs", FD.TYPE_MESSAGE, REP, "docreader.ImageRef"),
        (3, "image_dir_path", FD.TYPE_STRING, OPT, ""),
        (4, "metadata", FD.TYPE_MESSAGE, REP, "docreader.ReadResponse.MetadataEntry"),
        (5, "error", FD.TYPE_STRING, OPT, ""),
        (6, "source_blocks", FD.TYPE_MESSAGE, REP, "docreader.SourceBlock"),
    ],
    "ReadStreamMeta": [
        (1, "markdown_content", FD.TYPE_STRING, OPT, ""),
        (2, "image_dir_path", FD.TYPE_STRING, OPT, ""),
        (3, "metadata", FD.TYPE_MESSAGE, REP, "docreader.ReadStreamMeta.MetadataEntry"),
        (4, "error", FD.TYPE_STRING, OPT, ""),
        (5, "image_count", FD.TYPE_UINT32, OPT, ""),
        (6, "source_blocks", FD.TYPE_MESSAGE, REP, "docreader.SourceBlock"),
    ],
    "ReadStreamResponse": [
        (1, "meta", FD.TYPE_MESSAGE, OPT, "docreader.ReadStreamMeta"),
        (2, "image", FD.TYPE_MESSAGE, OPT, "docreader.ImageRef"),
    ],
    "ListEnginesRequest": [
        (
            1,
            "config_overrides",
            FD.TYPE_MESSAGE,
            REP,
            "docreader.ListEnginesRequest.ConfigOverridesEntry",
        ),
    ],
    "ParserEngineInfo": [
        (1, "name", FD.TYPE_STRING, OPT, ""),
        (2, "description", FD.TYPE_STRING, OPT, ""),
        (3, "file_types", FD.TYPE_STRING, REP, ""),
        (4, "available", FD.TYPE_BOOL, OPT, ""),
        (5, "unavailable_reason", FD.TYPE_STRING, OPT, ""),
    ],
    "ListEnginesResponse": [
        (1, "engines", FD.TYPE_MESSAGE, REP, "docreader.ParserEngineInfo"),
    ],
}

EXPECTED_METHODS: dict[str, tuple[str, str, bool, bool]] = {
    "Read": ("docreader.ReadRequest", "docreader.ReadResponse", False, False),
    "ReadStream": ("docreader.ReadRequest", "docreader.ReadStreamResponse", True, False),
    "ListEngines": (
        "docreader.ListEnginesRequest",
        "docreader.ListEnginesResponse",
        False,
        False,
    ),
    "PreprocessImage": (
        "docreader.PreprocessImageRequest",
        "docreader.PreprocessImageResponse",
        False,
        False,
    ),
}


def _committed_descriptor() -> descriptor_pb2.FileDescriptorProto:
    """把已生成 stub 的序列化描述符还原成 proto 视图。

    ``reserved_range`` 一类元数据只存在于 DescriptorProto 上；protobuf 7.x 的
    MessageDescriptor 不再直接暴露 ``reserved_range``，所以统一走 proto 视图。
    """
    descriptor = descriptor_pb2.FileDescriptorProto()
    descriptor.ParseFromString(pb.DESCRIPTOR.serialized_pb)
    return descriptor


def _reference_gencode_descriptor() -> descriptor_pb2.FileDescriptorProto:
    """从 WeKnora 自带 ``docreader_pb2.py`` 的 AddSerializedFile 字面量还原描述符。"""
    if not REF_PB2.is_file():
        raise AssertionError(f"缺少只读参考 {REF_PB2}")
    text = REF_PB2.read_text(encoding="utf-8")
    match = re.search(r"AddSerializedFile\(b'((?:[^'\\]|\\.)*)'\)", text, re.S)
    assert match is not None, "参考 docreader_pb2.py 中未找到 AddSerializedFile 字面量"
    payload = ast.literal_eval("b'" + match.group(1) + "'")
    descriptor = descriptor_pb2.FileDescriptorProto()
    descriptor.ParseFromString(payload)
    return descriptor


def _strip_derived_json_name(
    descriptor: descriptor_pb2.FileDescriptorProto,
) -> descriptor_pb2.FileDescriptorProto:
    """清掉派生 ``json_name`` 后再比较描述符。

    ``protoc --descriptor_set_out`` 总会写出每个字段的 ``json_name``，而 protobuf
    7.x 的 ``python_out`` 在序列化描述符里省略可推导的 json_name（实测差异只在
    这个派生字段）。除它之外的内容必须完全一致。
    """
    cloned = descriptor_pb2.FileDescriptorProto()
    cloned.CopyFrom(descriptor)

    def _walk(message: descriptor_pb2.DescriptorProto) -> None:
        for field in message.field:
            field.ClearField("json_name")
        for nested in message.nested_type:
            _walk(nested)

    for message in cloned.message_type:
        _walk(message)
    return cloned


def _png(tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag


# ---------------------------------------------------------------------------
# 00-02：proto 与生成物
# ---------------------------------------------------------------------------
def test_00_proto_copy_matches_t09_anchor() -> None:
    copy_bytes = PROTO_COPY.read_bytes()
    ref_bytes = REF_PROTO.read_bytes()
    digest = hashlib.sha256(copy_bytes).hexdigest()
    assert copy_bytes == ref_bytes, "副本被改动：必须与 references/WeKnora 逐字节一致"
    assert len(copy_bytes) == PROTO_BYTES
    assert digest == PROTO_SHA256, f"proto sha256 已漂移：{digest}"
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["proto_sha256"] == PROTO_SHA256
    assert provenance["source_commit"] == SOURCE_COMMIT
    _EVIDENCE["proto"] = {
        "path": "experiments/phase0/docreader_probe/proto/docreader.proto",
        "sha256": digest,
        "bytes": len(copy_bytes),
        "reference_commit": SOURCE_COMMIT,
        "source": "references/WeKnora/docreader/proto/docreader.proto",
    }


def test_01_generated_stubs_match_real_proto(tmp_path: Path) -> None:
    from grpc_tools import protoc

    descriptor_out = tmp_path / "docreader.desc"
    rc = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{PROTO_COPY.parent}",
            f"--descriptor_set_out={descriptor_out}",
            str(PROTO_COPY),
        ]
    )
    assert rc == 0, "grpc_tools.protoc 从真实 proto 重新生成描述符失败"
    file_set = descriptor_pb2.FileDescriptorSet()
    file_set.ParseFromString(descriptor_out.read_bytes())
    assert len(file_set.file) == 1
    fresh = file_set.file[0]

    committed = descriptor_pb2.FileDescriptorProto()
    committed.ParseFromString(pb.DESCRIPTOR.serialized_pb)
    assert _strip_derived_json_name(committed) == _strip_derived_json_name(fresh), (
        "committed stub 的 DESCRIPTOR 与从 proto 重新生成的结果不一致（已排除派生 json_name）"
    )
    assert committed.syntax == "proto3"
    assert committed.name == "docreader.proto"

    reference = _reference_gencode_descriptor()
    assert committed == reference, "与 WeKnora 自带 docreader_pb2 的描述符不一致"
    assert Path(docreader_pb2_grpc.__file__).is_relative_to(PROBE_DIR)

    _EVIDENCE["generated_stub"] = {
        "pb2": str(Path(pb.__file__).relative_to(REPO_ROOT)),
        "pb2_grpc": str(Path(docreader_pb2_grpc.__file__).relative_to(REPO_ROOT)),
        "descriptor_sha256": hashlib.sha256(pb.DESCRIPTOR.serialized_pb).hexdigest(),
        "regenerated_from_proto_equals_committed": True,
        "equals_weknora_gencode": True,
    }


def test_02_schema_matches_proto_field_table() -> None:
    assert pb.DESCRIPTOR.package == "docreader"
    assert set(pb.DESCRIPTOR.message_types_by_name) == set(EXPECTED_MESSAGES)

    for message_name, expected_fields in EXPECTED_MESSAGES.items():
        descriptor = pb.DESCRIPTOR.message_types_by_name[message_name]
        observed = [
            (
                field.number,
                field.name,
                field.type,
                FD.LABEL_REPEATED if field.is_repeated else FD.LABEL_OPTIONAL,
                field.message_type.full_name if field.message_type is not None else "",
            )
            for field in descriptor.fields
        ]
        assert observed == expected_fields, f"{message_name} 字段表与 proto 不一致"

    for message_name, field_name in (
        ("ReadConfig", "parser_engine_overrides"),
        ("ReadResponse", "metadata"),
        ("ReadStreamMeta", "metadata"),
        ("ListEnginesRequest", "config_overrides"),
    ):
        field = pb.DESCRIPTOR.message_types_by_name[message_name].fields_by_name[field_name]
        assert field.message_type.GetOptions().map_entry is True, (
            f"{message_name}.{field_name} 应是 map 字段"
        )

    read_config = next(
        message
        for message in _committed_descriptor().message_type
        if message.name == "ReadConfig"
    )
    assert [(item.start, item.end) for item in read_config.reserved_range] == [(3, 4)]
    assert list(read_config.reserved_name) == []

    stream_response = pb.DESCRIPTOR.message_types_by_name["ReadStreamResponse"]
    assert [oneof.name for oneof in stream_response.oneofs] == ["payload"]
    assert [field.name for field in stream_response.oneofs_by_name["payload"].fields] == [
        "meta",
        "image",
    ]

    service = pb.DESCRIPTOR.services_by_name["DocReader"]
    methods = {
        method.name: (
            method.input_type.full_name,
            method.output_type.full_name,
            method.server_streaming,
            method.client_streaming,
        )
        for method in service.methods
    }
    assert methods == EXPECTED_METHODS, "DocReader 服务方法与 proto 不一致"


# ---------------------------------------------------------------------------
# 03-05：unary Read
# ---------------------------------------------------------------------------
def test_03_unary_read_round_trip_fields(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    content = "# 标题\n\n正文段落，含中文与 emoji 😀。\n"
    block_start = content.index("正文段落")
    emoji_start = content.index("😀")
    request_id = "t10-unary-fields"
    images = [
        ("media/page_001.png", _png(b"pdf-page-1")),
        ("media/page_002.jpg", b"\xff\xd8\xff\xe0pdf-page-2"),
        ("media/page_003.webp", b"{not-base64"),
        ("media/page_004.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
    ]
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content=content,
            images=images,
            metadata={"engine": "builtin", "page_count": "2"},
            source_blocks=[
                (0, len("# 标题"), '{"type":"pdf","page":1,"bbox":[0.1,0.2,0.9,0.8]}'),
                (block_start, block_start + len("正文段落"), '{"type":"pdf","page":2}'),
                (emoji_start, emoji_start + 1, '{"type":"pdf","page":2,"kind":"emoji"}'),
            ],
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF-1.4 synthetic bytes for wire only",
        file_name="sample.pdf",
        file_type="pdf",
        title="示例文档",
        request_id=request_id,
        config=pb.ReadConfig(parser_engine="builtin", parser_engine_overrides={"ocr": "off"}),
    )
    response, call = wire.stub.Read.with_call(request, timeout=5.0)
    assert call.code() == grpc.StatusCode.OK
    assert response.error == ""
    assert response.markdown_content == content
    assert response.image_dir_path == ""

    assert [item.original_ref for item in response.image_refs] == [path for path, _ in images]
    assert [item.filename for item in response.image_refs] == [
        "page_001.png",
        "page_002.jpg",
        "page_003.webp",
        "page_004.svg",
    ]
    assert [item.mime_type for item in response.image_refs] == [
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/octet-stream",
    ]
    assert [item.image_data for item in response.image_refs] == [data for _, data in images]
    assert all(item.storage_key == "" for item in response.image_refs)

    assert response.metadata["engine"] == "builtin"
    assert response.metadata["page_count"] == "2"
    assert response.metadata["probe.request_id"] == request_id
    assert response.metadata["probe.file_name"] == "sample.pdf"
    assert response.metadata["probe.file_type"] == "pdf"
    assert response.metadata["probe.title"] == "示例文档"
    assert response.metadata["probe.file_bytes"] == str(len(request.file_content))
    assert response.metadata["probe.parser_engine"] == "builtin"
    assert response.metadata["probe.override.ocr"] == "off"

    blocks = list(response.source_blocks)
    assert [(block.start, block.end) for block in blocks] == [
        (0, len("# 标题")),
        (block_start, block_start + len("正文段落")),
        (emoji_start, emoji_start + 1),
    ]
    assert content[blocks[0].start : blocks[0].end] == "# 标题"
    assert content[blocks[1].start : blocks[1].end] == "正文段落"
    assert content[blocks[2].start : blocks[2].end] == "😀"
    assert blocks[0].locator_json == '{"type":"pdf","page":1,"bbox":[0.1,0.2,0.9,0.8]}'


def test_04_empty_parse_result_error_field_keeps_status_ok(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-empty-parse"
    server.register(request_id, wire_server.ScriptedDocument(content=""))
    request = pb.ReadRequest(
        file_content=b"",
        file_name="empty.pdf",
        file_type="pdf",
        request_id=request_id,
    )
    response, call = wire.stub.Read.with_call(request, timeout=5.0)
    assert call.code() == grpc.StatusCode.OK, "解析失败不得升级为 gRPC 错误（T09 §5.1）"
    assert response.error == "Failed to parse: empty.pdf"
    assert response.markdown_content == ""
    assert list(response.image_refs) == []
    _EVIDENCE["parse_error_field"] = {
        "unary_status": "OK",
        "unary_error": response.error,
        "mechanism": "ReadResponse.error（不是 gRPC status）",
    }


def test_05_unsupported_file_type_error_field_keeps_status_ok(
    wire: wire_client.DocReaderClient,
) -> None:
    request = pb.ReadRequest(
        file_content=b"plain text",
        file_name="notes.txt",
        file_type="txt",
        request_id="t10-unsupported-type",
    )
    response, call = wire.stub.Read.with_call(request, timeout=5.0)
    assert call.code() == grpc.StatusCode.OK, "解析异常必须折叠成 error 字段（main.py:257-261）"
    assert response.error == "Unsupported file type: txt"
    assert response.markdown_content == ""


# ---------------------------------------------------------------------------
# 06-09：ReadStream 帧序、错误路径、批量边界
# ---------------------------------------------------------------------------
def test_06_read_stream_frame_order_and_termination(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    content = "# PDF stream\n\n第一页\n\n第二页\n"
    request_id = "t10-stream-order"
    refs = [f"media/part_{index:03d}.png" for index in range(1, 5)]
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content=content,
            images=[(ref, _png(ref.encode())) for ref in refs],
            metadata={"engine": "builtin"},
            source_blocks=[(0, len("# PDF stream"), '{"type":"pdf","page":1}')],
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="stream.pdf", file_type="pdf", request_id=request_id
    )
    call = wire.stub.ReadStream(request, timeout=5.0)
    frames = list(call)
    assert call.code() == grpc.StatusCode.OK, "正常流必须以 OK 结束"
    assert [frame.WhichOneof("payload") for frame in frames] == [
        "meta",
        "image",
        "image",
        "image",
        "image",
    ]
    meta = frames[0].meta
    assert meta.error == ""
    assert meta.markdown_content == content
    assert meta.image_dir_path == ""
    assert meta.image_count == 4
    assert meta.metadata["engine"] == "builtin"
    assert meta.metadata["probe.request_id"] == request_id
    assert [(block.start, block.end) for block in meta.source_blocks] == [(0, len("# PDF stream"))]
    assert [frame.image.original_ref for frame in frames[1:]] == refs

    result = wire.read_stream(request, timeout=5.0)
    assert result.frames == 5
    assert result.complete is True
    assert result.publishable is True
    assert len(result.images) == 4
    assert result.duration_s >= 0
    _EVIDENCE["stream_frames"] = {
        "frames": result.frames,
        "images": len(result.images),
        "image_count": result.image_count,
        "status": "OK",
    }


def test_07_read_stream_parse_failure_only_meta_error(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-stream-parse-error"
    detail = "simulated parser failure: bad xref"
    server.register(request_id, wire_server.ScriptedDocument(parse_error=detail))
    request = pb.ReadRequest(
        file_content=b"%PDF broken",
        file_name="broken.pdf",
        file_type="pdf",
        request_id=request_id,
    )
    call = wire.stub.ReadStream(request, timeout=5.0)
    frames = list(call)
    assert call.code() == grpc.StatusCode.OK, "流解析失败不得升级为 gRPC 错误（T09 §5.1）"
    assert [frame.WhichOneof("payload") for frame in frames] == ["meta"]
    assert frames[0].meta.error == detail
    assert frames[0].meta.markdown_content == ""

    result = wire.read_stream(request, timeout=5.0)
    assert result.error == detail
    assert result.complete is False
    assert result.publishable is False
    _EVIDENCE["parse_error_field"]["stream_status"] = "OK"
    _EVIDENCE["parse_error_field"]["stream_frames"] = ["meta(error)"]
    _EVIDENCE["parse_error_field"]["publishable"] = False


def test_08_read_stream_send_phase_failure_is_not_ok(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-stream-send-failure"
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content="# partial\n",
            images=[(f"media/{index}.png", f"img-{index}".encode()) for index in range(3)],
            fail_after_images=2,
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="send-failure.pdf", file_type="pdf", request_id=request_id
    )
    with pytest.raises(wire_client.DocReaderStreamError) as excinfo:
        wire.read_stream(request, timeout=5.0)
    error = excinfo.value
    assert error.code == grpc.StatusCode.UNKNOWN, f"发送期异常应映射为 UNKNOWN，实际 {error.code}"
    assert error.meta is not None
    assert error.meta.image_count == 3
    assert len(error.images) == 2
    assert len(error.images) < error.meta.image_count
    assert error.reason == "rpc_interrupted"
    assert error.publishable is False
    assert "send-phase failure" in error.details

    raw_call = wire.stub.ReadStream(request, timeout=5.0)
    received: list[str] = []
    with pytest.raises(grpc.RpcError) as raw_error:
        for frame in raw_call:
            received.append(frame.WhichOneof("payload"))
    assert received == ["meta", "image", "image"]
    assert raw_error.value.code() == grpc.StatusCode.UNKNOWN
    _EVIDENCE["send_phase_failure"] = {
        "status": error.code.name,
        "details_contains_message": True,
        "frames_before_failure": error.frames,
        "images_before_failure": len(error.images),
        "image_count_declared": error.meta.image_count,
        "note": "T09 §9 中该 status 原为 UNKNOWN，此处为实测",
    }


def test_09_read_stream_bypasses_unary_message_cap(
    sized_pair: tuple[wire_server.ProbeServer, wire_client.DocReaderClient],
) -> None:
    probe, client = sized_pair
    request_id = "t10-size-cap"
    images = [
        (f"media/scan_{index:03d}.png", bytes([index]) * 150_000) for index in range(8)
    ]
    probe.register(
        request_id,
        wire_server.ScriptedDocument(
            content="# scan\n", images=images, metadata={"engine": "builtin"}
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="scan.pdf", file_type="pdf", request_id=request_id
    )
    with pytest.raises(grpc.RpcError) as unary_error:
        client.stub.Read(request, timeout=10.0)
    assert unary_error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED, (
        "unary 单条消息超过上限应被拒绝（proto:9-15 的动机）"
    )

    result = client.read_stream(request, timeout=10.0)
    assert result.complete is True
    assert len(result.images) == 8
    assert result.meta.image_count == 8
    assert max(len(image.image_data) for image in result.images) < MESSAGE_CAP
    streamed_bytes = sum(len(image.image_data) for image in result.images)
    assert streamed_bytes > MESSAGE_CAP
    _EVIDENCE["message_cap"] = {
        "cap_bytes": MESSAGE_CAP,
        "unary_read_status": "RESOURCE_EXHAUSTED",
        "streamed_images": len(result.images),
        "streamed_payload_bytes": streamed_bytes,
        "largest_frame_bytes": max(len(image.image_data) for image in result.images),
    }


# ---------------------------------------------------------------------------
# 10-11：取消与超时
# ---------------------------------------------------------------------------
def test_10_client_cancel_is_observed_by_server(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-cancel"
    gate_name = "t10_cancel_observed"
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content="# cancel\n",
            images=[("media/a.png", _png(b"cancel"))],
            gate=wire_server.Gate(event_name=gate_name, wait_s=10.0),
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="cancel.pdf", file_type="pdf", request_id=request_id
    )
    call = wire.stub.ReadStream(request, timeout=10.0)
    first = next(call)
    assert first.WhichOneof("payload") == "meta"
    server.wait_observation(f"{gate_name}.entered", timeout=5.0)

    cancel_started = time.perf_counter()
    call.cancel()
    observation = server.wait_observation(gate_name, timeout=5.0)
    assert observation["observed"] is True, "服务端没有观察到客户端取消"
    assert observation["is_active"] is False
    assert observation["waited_s"] < 5.0

    try:
        rest = list(call)
    except grpc.RpcError as exc:
        assert exc.code() == grpc.StatusCode.CANCELLED
    else:
        assert rest == []
        assert call.code() == grpc.StatusCode.CANCELLED
    elapsed = time.perf_counter() - cancel_started
    assert elapsed < 5.0, f"取消后客户端耗时 {elapsed:.3f}s，疑似挂死"

    follow = wire.read(
        pb.ReadRequest(
            file_content=b"%PDF",
            file_name="after-cancel.pdf",
            file_type="pdf",
            request_id="t10-after-cancel",
        )
    )
    assert follow.error == ""
    _EVIDENCE["cancellation"] = {
        "server_observed": True,
        "server_is_active": False,
        "server_waited_s": observation["waited_s"],
        "client_status": "CANCELLED",
        "client_elapsed_s": round(elapsed, 4),
    }


def test_11_deadline_exceeded_is_observed_by_server(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-deadline"
    gate_name = "t10_deadline_observed"
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content="# deadline\n",
            images=[("media/a.png", _png(b"deadline"))],
            gate=wire_server.Gate(event_name=gate_name, wait_s=10.0),
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="deadline.pdf", file_type="pdf", request_id=request_id
    )
    call = wire.stub.ReadStream(request, timeout=0.5)
    started = time.perf_counter()
    with pytest.raises(grpc.RpcError) as excinfo:
        list(call)
    elapsed = time.perf_counter() - started
    assert excinfo.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert elapsed < 5.0, f"超时后客户端耗时 {elapsed:.3f}s，疑似挂死"

    observation = server.wait_observation(gate_name, timeout=5.0)
    assert observation["observed"] is True, "服务端没有观察到 deadline"
    assert observation["is_active"] is False

    follow = wire.read(
        pb.ReadRequest(
            file_content=b"%PDF",
            file_name="after-deadline.pdf",
            file_type="pdf",
            request_id="t10-after-deadline",
        )
    )
    assert follow.error == ""
    _EVIDENCE["deadline"] = {
        "client_status": "DEADLINE_EXCEEDED",
        "timeout_s": 0.5,
        "server_observed": True,
        "client_elapsed_s": round(elapsed, 4),
    }


# ---------------------------------------------------------------------------
# 12-14：认证、ListEngines、PreprocessImage
# ---------------------------------------------------------------------------
def test_12_auth_token_required(
    auth_pair: tuple[wire_server.ProbeServer, wire_client.DocReaderClient],
) -> None:
    probe, good = auth_pair
    request = pb.ReadRequest(file_content=b"%PDF", file_name="auth.pdf", file_type="pdf")

    anonymous = wire_client.DocReaderClient(probe.target, timeout=5.0)
    try:
        with pytest.raises(grpc.RpcError) as unary_error:
            anonymous.read(request)
        assert unary_error.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert unary_error.value.details() == wire_server.AUTH_DETAIL
        with pytest.raises(grpc.RpcError) as stream_error:
            list(anonymous.stub.ReadStream(request, timeout=5.0))
        assert stream_error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        anonymous.close()

    wrong = wire_client.DocReaderClient(probe.target, token="wrong-token", timeout=5.0)
    try:
        with pytest.raises(grpc.RpcError) as wrong_error:
            wrong.read(request)
        assert wrong_error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        wrong.close()

    assert good.read(request).error == ""
    assert good.read_stream(request, timeout=5.0).complete is True
    _EVIDENCE["auth"] = {
        "missing_token_status": "UNAUTHENTICATED",
        "wrong_token_status": "UNAUTHENTICATED",
        "detail": wire_server.AUTH_DETAIL,
        "valid_token_works": True,
    }


def test_13_list_engines_round_trip(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    response = wire.list_engines(pb.ListEnginesRequest(config_overrides={"probe_mode": "t10"}))
    assert [engine.name for engine in response.engines] == ["builtin", "mineru"]
    builtin, mineru = response.engines
    assert builtin.description == "built-in parsers (shim fixture)"
    assert list(builtin.file_types) == ["pdf", "docx", "html"]
    assert builtin.available is True
    assert builtin.unavailable_reason == ""
    assert list(mineru.file_types) == ["pdf"]
    assert mineru.available is False
    assert mineru.unavailable_reason == "engine not configured in this probe"

    call = server.last_call("/docreader.DocReader/ListEngines")
    assert call["config_overrides"] == {"probe_mode": "t10"}
    assert call["peer"].startswith("ipv4:127.0.0.1:")
    _EVIDENCE["list_engines"] = {
        "engines": [engine.name for engine in response.engines],
        "overrides_over_wire": call["config_overrides"],
        "note": "引擎值来自 shim fixture，不代表真实注册表",
    }


def test_14_preprocess_image_branches(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    payload = _png(b"preprocess-input" * 2)
    response, call = wire.stub.PreprocessImage.with_call(
        pb.PreprocessImageRequest(image=payload), timeout=5.0
    )
    assert call.code() == grpc.StatusCode.OK
    assert response.fallback_needed is False
    assert response.image == b"\xff\xd8\xff\xe0" + payload
    assert response.status == "ok"
    assert response.theta == pytest.approx(1.25)
    assert response.residual == pytest.approx(0.5)
    assert response.flip is False
    assert response.n_boxes == 7
    assert response.n_kept == 6

    fallback, fallback_call = wire.stub.PreprocessImage.with_call(
        pb.PreprocessImageRequest(image=b""), timeout=5.0
    )
    assert fallback_call.code() == grpc.StatusCode.OK, "不可纠正不是错误（proto:18-23）"
    assert fallback.fallback_needed is True
    assert fallback.image == b""
    assert fallback.status in {"ok", "no_text", "low_quality"}
    _EVIDENCE["preprocess_image"] = {
        "ok_status": response.status,
        "fallback_status": fallback.status,
        "note": "数值与状态来自 shim fixture；真实服务的超时/超内存行为仍 UNKNOWN（T09 §9）",
    }


# ---------------------------------------------------------------------------
# 15-16：真 TCP 方法路径 / 缺 meta 的协议拒绝
# ---------------------------------------------------------------------------
def test_15_all_four_methods_over_real_tcp(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    assert isinstance(wire.channel, grpc.Channel)
    assert wire.stub.__class__.__module__ == "docreader_probe.generated.docreader_pb2_grpc"

    before = len(server.calls)
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="paths.pdf", file_type="pdf", request_id="t10-paths"
    )
    wire.read(request)
    list(wire.read_stream_raw(request, timeout=5.0))
    wire.list_engines(pb.ListEnginesRequest())
    wire.preprocess_image(pb.PreprocessImageRequest(image=_png(b"paths")))
    calls = server.calls[before:]
    assert [call["method"] for call in calls] == [
        "/docreader.DocReader/Read",
        "/docreader.DocReader/ReadStream",
        "/docreader.DocReader/ListEngines",
        "/docreader.DocReader/PreprocessImage",
    ]
    for call in calls:
        assert call["peer"].startswith("ipv4:127.0.0.1:"), call["peer"]
    _EVIDENCE["method_paths"] = [call["method"] for call in calls]
    _EVIDENCE["peer_prefixes"] = sorted({call["peer"].split(":")[0] for call in calls})


def test_16_stream_without_meta_is_protocol_error(
    server: wire_server.ProbeServer, wire: wire_client.DocReaderClient
) -> None:
    request_id = "t10-no-meta"
    server.register(
        request_id,
        wire_server.ScriptedDocument(
            content="# no meta\n",
            images=[("media/a.png", _png(b"a")), ("media/b.png", _png(b"b"))],
            omit_meta=True,
        ),
    )
    request = pb.ReadRequest(
        file_content=b"%PDF", file_name="no-meta.pdf", file_type="pdf", request_id=request_id
    )
    with pytest.raises(wire_client.DocReaderProtocolError) as excinfo:
        wire.read_stream(request, timeout=5.0)
    assert "image 帧先于 meta" in str(excinfo.value)

    raw = list(wire.stub.ReadStream(request, timeout=5.0))
    assert [frame.WhichOneof("payload") for frame in raw] == ["image", "image"]
    _EVIDENCE["missing_meta"] = {"client_error": "DocReaderProtocolError", "raw_frames": 2}
