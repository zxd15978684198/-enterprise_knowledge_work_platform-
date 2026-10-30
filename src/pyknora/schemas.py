"""请求/响应 DTO。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")


class MemberOut(ORMModel):
    user_id: uuid.UUID
    role: str
    email: EmailStr
    display_name: str


class WorkspaceOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    owner_id: uuid.UUID
    created_at: datetime


class WorkspaceDetail(WorkspaceOut):
    role: str
    members: list[MemberOut]


# —— 知识库与文档（W19）——


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    document_count: int = 0
    chunk_count: int = 0


class DocumentOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source_type: str
    source_url: str | None
    filename: str
    mime_type: str
    size_bytes: int
    checksum: str
    status: str
    error: str | None
    version: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ChunkOut(ORMModel):
    id: uuid.UUID
    document_id: uuid.UUID
    ordinal: int
    content: str
    char_count: int
    page: int | None


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int
    limit: int
    offset: int


# —— 检索（W20）——


class RetrievalRequest(BaseModel):
    workspace_id: uuid.UUID
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[uuid.UUID] | None = None
    top_k: int | None = Field(default=None, ge=1, le=50)


class CitationOut(BaseModel):
    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    ordinal: int
    snippet: str
    score: float


class RetrievalHitOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_title: str
    ordinal: int
    snippet: str
    vector_score: float | None
    lexical_score: float | None
    fused_score: float
    vector_rank: int | None
    lexical_rank: int | None


class RetrievalResponse(BaseModel):
    query: str
    total: int
    mode: str = "hybrid-rrf"
    hits: list[RetrievalHitOut]


# —— 会话与问答（W20）——


class ConversationCreate(BaseModel):
    workspace_id: uuid.UUID
    title: str = Field(default="", max_length=200)


class MessageOut(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    status: str
    citations: list[dict[str, object]] = Field(default_factory=list)
    trace: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class ConversationOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    knowledge_base_ids: list[uuid.UUID] | None = None


class AskResponse(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    status: str
    content: str
    citations: list[dict[str, object]] = Field(default_factory=list)
    trace: dict[str, object] = Field(default_factory=dict)


class ModelConfigCreate(BaseModel):
    """登记一个模型配置。"""

    workspace_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(chat|embedding)$")
    provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=2048)
    credential_ref: str | None = Field(default=None, max_length=128)
    is_default: bool = False


class ModelConfigUpdate(BaseModel):
    """局部更新模型配置。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=2048)
    credential_ref: str | None = Field(default=None, max_length=128)
    is_default: bool | None = None


class ModelConfigOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: str
    provider: str
    model_name: str
    base_url: str | None
    credential_ref: str | None
    is_default: bool
    created_at: datetime
    updated_at: datetime


class ModelTestResult(BaseModel):
    """连接测试结果；``ok=false`` 时 ``detail`` 说明原因，不返回任何凭据。"""

    ok: bool
    kind: str
    provider: str
    adapter: str | None = None
    matches_runtime: bool
    latency_ms: float | None = None
    detail: str = ""


class UrlImportIn(BaseModel):
    """单 URL 导入请求（URL 属不可信输入，由服务端做 SSRF 与资源校验）。"""

    url: str = Field(min_length=1, max_length=2048, description="http/https 地址")
    title: str | None = Field(default=None, max_length=300, description="可选标题")
