"""PyKnora 侧 DocReader 客户端（P0-T10 实验代码，不是产品实现）。

把 ``docs/tdd/06_Knowledge_Ingestion.md`` 与 P0-T09 ``docreader_manifest.md``
的流完整性要求落成可执行判定：

* meta 必须**恰好一帧**且是**第一帧**（``docreader.proto:95-112``）；
* ``meta.error`` 非空即失败，不产生可发布结果（T09 §5.1 / D-07）；
* 只有 EOF 才是正常终止；``image_count > 0`` 时实收图片数必须 ≥ 它（T09 D-04）；
* RPC 中断（取消/超时/发送期异常）必须暴露 gRPC status 与已收部分，
  由调用方判定 ``PARSE_INCOMPLETE``，不得当作成功。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

import grpc

from .generated import docreader_pb2 as pb
from .generated import docreader_pb2_grpc


class DocReaderProtocolError(RuntimeError):
    """帧序/帧数违反 proto 契约（客户端必须拒绝的响应）。"""


class DocReaderStreamError(RuntimeError):
    """RPC 未正常完成：保留 status、已收帧与已收图片，绝不静默降级。"""

    def __init__(
        self,
        *,
        code: grpc.StatusCode | None,
        details: str,
        meta: pb.ReadStreamMeta | None,
        images: list[pb.ImageRef],
        frames: int,
        duration_s: float,
        reason: str,
    ) -> None:
        super().__init__(
            f"{reason}: code={code} details={details!r} "
            f"frames={frames} images={len(images)} duration={duration_s:.3f}s"
        )
        self.code = code
        self.details = details
        self.meta = meta
        self.images = tuple(images)
        self.frames = frames
        self.duration_s = duration_s
        self.reason = reason

    @property
    def publishable(self) -> bool:
        """中断的流一律不可发布（T09 D-04：缺帧不得落库）。"""
        return False


@dataclass(frozen=True)
class StreamResult:
    """正常 EOF 结束的 ReadStream 结果。"""

    meta: pb.ReadStreamMeta
    images: tuple[pb.ImageRef, ...]
    frames: int
    duration_s: float

    @property
    def error(self) -> str:
        return self.meta.error

    @property
    def image_count(self) -> int:
        return self.meta.image_count

    @property
    def complete(self) -> bool:
        """EOF 已发生；再要求 error 为空且图片数不少于声明的 image_count。"""
        if self.meta.error:
            return False
        return not (self.meta.image_count > 0 and len(self.images) < self.meta.image_count)

    @property
    def publishable(self) -> bool:
        return self.complete


class DocReaderClient:
    """真实 TCP gRPC 上的最小客户端；默认 50 MiB 与 docreader/config.py 一致。"""

    def __init__(
        self,
        target: str,
        *,
        token: str | None = None,
        timeout: float = 5.0,
        max_receive_bytes: int | None = None,
    ) -> None:
        options: list[tuple[str, int]] = []
        if max_receive_bytes is not None:
            options.append(("grpc.max_receive_message_length", max_receive_bytes))
        self._channel = grpc.insecure_channel(target, options=options or None)
        self._stub = docreader_pb2_grpc.DocReaderStub(self._channel)
        self.target = target
        self.timeout = timeout
        self._token = token

    @property
    def stub(self) -> docreader_pb2_grpc.DocReaderStub:
        return self._stub

    @property
    def channel(self) -> grpc.Channel:
        return self._channel

    def wait_ready(self, timeout: float | None = None) -> None:
        grpc.channel_ready_future(self._channel).result(
            timeout=self.timeout if timeout is None else timeout
        )

    def close(self) -> None:
        self._channel.close()

    def _metadata(self) -> tuple[tuple[str, str], ...] | None:
        if not self._token:
            return None
        return (("authorization", f"Bearer {self._token}"),)

    def _rpc_timeout(self, timeout: float | None) -> float:
        return self.timeout if timeout is None else timeout

    def read(self, request: pb.ReadRequest, *, timeout: float | None = None) -> pb.ReadResponse:
        return self._stub.Read(
            request, timeout=self._rpc_timeout(timeout), metadata=self._metadata()
        )

    def read_stream_raw(
        self, request: pb.ReadRequest, *, timeout: float | None = None
    ) -> Iterator[pb.ReadStreamResponse]:
        yield from self._stub.ReadStream(
            request, timeout=self._rpc_timeout(timeout), metadata=self._metadata()
        )

    def read_stream(
        self, request: pb.ReadRequest, *, timeout: float | None = None
    ) -> StreamResult:
        """读取整个流并执行帧序/帧数校验；任何偏离都抛异常而非返回半成品。"""
        started = time.perf_counter()
        frames = 0
        meta: pb.ReadStreamMeta | None = None
        images: list[pb.ImageRef] = []
        try:
            for frame in self.read_stream_raw(request, timeout=timeout):
                frames += 1
                which = frame.WhichOneof("payload")
                if which == "meta":
                    if meta is not None:
                        raise DocReaderProtocolError(
                            f"meta 帧出现两次（第 {frames} 帧）；proto:95-96 要求恰一帧"
                        )
                    if frames != 1:
                        raise DocReaderProtocolError(f"meta 不是首帧（出现在第 {frames} 帧）")
                    meta = frame.meta
                elif which == "image":
                    if meta is None:
                        raise DocReaderProtocolError(
                            f"image 帧先于 meta（第 {frames} 帧）；无法确认文档长度"
                        )
                    images.append(frame.image)
                else:
                    raise DocReaderProtocolError(f"第 {frames} 帧 payload 为空")
        except grpc.RpcError as exc:
            raise DocReaderStreamError(
                code=exc.code(),
                details=exc.details() or "",
                meta=meta,
                images=images,
                frames=frames,
                duration_s=time.perf_counter() - started,
                reason="rpc_interrupted",
            ) from exc
        duration = time.perf_counter() - started
        if meta is None:
            raise DocReaderStreamError(
                code=None,
                details="stream ended without meta frame",
                meta=None,
                images=images,
                frames=frames,
                duration_s=duration,
                reason="missing_meta",
            )
        return StreamResult(meta=meta, images=tuple(images), frames=frames, duration_s=duration)

    def list_engines(
        self, request: pb.ListEnginesRequest, *, timeout: float | None = None
    ) -> pb.ListEnginesResponse:
        return self._stub.ListEngines(
            request, timeout=self._rpc_timeout(timeout), metadata=self._metadata()
        )

    def preprocess_image(
        self, request: pb.PreprocessImageRequest, *, timeout: float | None = None
    ) -> pb.PreprocessImageResponse:
        return self._stub.PreprocessImage(
            request, timeout=self._rpc_timeout(timeout), metadata=self._metadata()
        )
