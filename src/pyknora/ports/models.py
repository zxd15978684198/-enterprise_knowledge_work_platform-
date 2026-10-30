"""模型端口实现。

本文件提供**两个可注入端口**：

``EmbeddingPort``
    文本 → 定长向量。W20 默认实现是 :class:`HashingEmbedding`：字符 n-gram 哈希投影，
    确定性、无外部依赖、可离线跑测试。它**不是语义模型**——同义改写不会靠近，
    只按字面 n-gram 重叠聚簇；真实 embedding（OpenAI / BGE / 本地推理）接入时实现
    同一协议、把 ``PYKNORA_EMBEDDING_PROVIDER`` 切过去即可，检索层不用改。

``ChatPort``
    问题 + 检索片段 → 增量文本流。W20 默认实现是 :class:`ExtractiveChat`：从片段里
    摘录与问题字面重叠最多的句子，按引用编号拼装回答。它是**抽取式替身**，用于验证
    流式、引用与「证据不足」三条链路，不代表生成质量。

真实模型未接入是本轮明确登记的 ``NOT_RUN`` 项（见 ``docs/phase1/wave-w20-record.md``）。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol, runtime_checkable

from pyknora.config import get_settings

__all__ = [
    "ChatMessage",
    "ChatPort",
    "EmbeddingPort",
    "ExtractiveChat",
    "HashingEmbedding",
    "RetrievedContext",
    "get_chat_port",
    "get_embedding_port",
    "reset_ports",
]

_MAX_CONTEXT_CHARS = 1200
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")


@dataclass(slots=True, frozen=True)
class ChatMessage:
    """一条对话消息（只保留生成所需字段）。"""

    role: str
    content: str


@dataclass(slots=True, frozen=True)
class RetrievedContext:
    """送进模型的一段检索证据，``index`` 是回答里引用的编号（从 1 开始）。"""

    index: int
    title: str
    ordinal: int
    content: str

    def prompt_block(self) -> str:
        text = self.content[:_MAX_CONTEXT_CHARS]
        return f"[{self.index}] 来源：{self.title}（片段 {self.ordinal}）\n{text}"


@runtime_checkable
class EmbeddingPort(Protocol):
    """文本向量化端口。"""

    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化；返回顺序与入参一致。"""


@runtime_checkable
class ChatPort(Protocol):
    """对话生成端口：只暴露增量文本流。"""

    name: str

    def stream(
        self,
        *,
        question: str,
        contexts: Sequence[RetrievedContext],
        history: Sequence[ChatMessage] = (),
    ) -> AsyncIterator[str]:
        """按块产出回答文本。"""


class HashingEmbedding:
    """字符 n-gram 哈希投影（确定、离线、非语义）。

    算法：对每条文本取 1–3 元字符 n-gram，用 blake2b 派生「桶下标 + 符号位」，
    累加后做 L2 归一化。同字面重叠越多，余弦相似度越高；因此它只能验证检索
    与引用链路，不承担语义召回。
    """

    name = "hashing-local"

    def __init__(self, dim: int = 1024) -> None:
        if dim <= 0:
            raise ValueError("embedding 维度必须为正整数")
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        normalized = text.strip()
        if not normalized:
            return vector
        for ngram in self._ngrams(normalized):
            digest = hashlib.blake2b(ngram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    @staticmethod
    def _ngrams(text: str) -> list[str]:
        compact = "".join(text.split())
        grams: list[str] = []
        for size in (1, 2, 3):
            if len(compact) < size:
                continue
            grams.extend(compact[i : i + size] for i in range(len(compact) - size + 1))
        return grams


class ExtractiveChat:
    """抽取式回答替身：按字面重叠挑句子，带引用编号输出。

    输出结构固定为「一句总述 + 每段证据一句摘录 + 一句收尾」，便于断言流式分块与
    引用是否对齐。真实生成模型接入后此实现仅用于测试。
    """

    name = "extractive-local"

    def __init__(self, max_sentences: int = 3) -> None:
        self.max_sentences = max_sentences

    async def stream(
        self,
        *,
        question: str,
        contexts: Sequence[RetrievedContext],
        history: Sequence[ChatMessage] = (),
    ) -> AsyncIterator[str]:
        del history  # 抽取式替身不利用多轮历史，保留参数以对齐协议
        if not contexts:
            yield "未检索到可用证据。"
            return

        picked = self._pick_sentences(question, contexts)
        yield f"根据检索到的 {len(picked)} 段证据，回答如下：\n"
        for index, sentence in picked:
            yield f"- {sentence} [{index}]\n"
        yield "以上内容均来自上方引用的文档片段。"

    def _pick_sentences(
        self, question: str, contexts: Sequence[RetrievedContext]
    ) -> list[tuple[int, str]]:
        tokens = {token for token in re.split(r"[\s，。！？、；：,.!?;:]+", question) if token}
        scored: list[tuple[int, str, int]] = []
        for context in contexts:
            for sentence in _SENTENCE_SPLIT.split(context.content):
                cleaned = sentence.strip()
                if not cleaned:
                    continue
                overlap = sum(1 for token in tokens if token and token in cleaned)
                scored.append((overlap, cleaned, context.index))
        scored.sort(key=lambda item: (-item[0], len(item[1])))
        picked: list[tuple[int, str]] = []
        for overlap, sentence, index in scored:
            if len(picked) >= self.max_sentences:
                break
            text = sentence if len(sentence) <= 160 else f"{sentence[:160]}…"
            picked.append((index, text))
            if overlap == 0 and picked:
                # 字面完全无重叠时只保留一条，避免拼出无关内容
                break
        return picked or [(contexts[0].index, contexts[0].content[:160])]


@lru_cache(maxsize=1)
def get_embedding_port() -> EmbeddingPort:
    """按配置返回 embedding 端口（当前只实现本地哈希实现）。"""

    settings = get_settings()
    provider = settings.embedding_provider.lower()
    if provider in {"local", "hashing"}:
        return HashingEmbedding(dim=settings.embedding_dim)
    raise RuntimeError(
        f"未实现的 embedding provider={settings.embedding_provider!r}；"
        "当前仅支持 local（真实模型适配器尚未接入，见 W20 记录 NOT_RUN 项）"
    )


@lru_cache(maxsize=1)
def get_chat_port() -> ChatPort:
    """按配置返回 chat 端口（当前只实现抽取式替身）。"""

    settings = get_settings()
    provider = settings.chat_provider.lower()
    if provider in {"local", "extractive"}:
        return ExtractiveChat()
    raise RuntimeError(
        f"未实现的 chat provider={settings.chat_provider!r}；"
        "当前仅支持 local（真实模型适配器尚未接入，见 W20 记录 NOT_RUN 项）"
    )


def reset_ports() -> None:
    """清空端口缓存（测试切换配置时使用）。"""

    get_embedding_port.cache_clear()
    get_chat_port.cache_clear()
