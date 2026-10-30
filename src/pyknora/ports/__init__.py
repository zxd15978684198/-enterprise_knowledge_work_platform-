"""模型端口：把「embedding / chat」抽成可注入协议，便于替换真实供应商。"""

from __future__ import annotations

from pyknora.ports.models import (
    ChatMessage,
    ChatPort,
    EmbeddingPort,
    ExtractiveChat,
    HashingEmbedding,
    get_chat_port,
    get_embedding_port,
    reset_ports,
)

__all__ = [
    "ChatMessage",
    "ChatPort",
    "EmbeddingPort",
    "ExtractiveChat",
    "HashingEmbedding",
    "get_chat_port",
    "get_embedding_port",
    "reset_ports",
]
