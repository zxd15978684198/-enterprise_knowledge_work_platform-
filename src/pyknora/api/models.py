"""模型配置 API（Phase 1 最小版本）：增删改查 + 连接测试。

设计约束：
- 只保存「凭据引用」（环境变量名），不接收也不返回明文密钥；
- 连接测试只探测**当前进程真实装配的适配器**，并如实报告配置与运行时是否一致；
  Phase 1 只有 ``local`` 替身适配器，登记其它 provider 会拿到 ``ok=false`` 与原因，
  而不是伪造成功。
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select, update

from pyknora.deps import CurrentUser, SessionDep, load_workspace_context
from pyknora.errors import ApiError
from pyknora.models import ModelConfig
from pyknora.ports import get_chat_port, get_embedding_port
from pyknora.schemas import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelTestResult,
)

router = APIRouter(tags=["models"])

#: 已装配的本地替身 provider 名称（真实供应商适配器尚未接入）
_LOCAL_PROVIDERS = {"local"}


async def _load_model(
    session: SessionDep, user: CurrentUser, model_id: uuid.UUID, workspace_id: uuid.UUID
) -> ModelConfig:
    """按工作区成员身份加载模型配置；跨工作区一律 404，避免枚举。"""

    await load_workspace_context(session, user, workspace_id)
    config = await session.get(ModelConfig, model_id)
    if config is None or config.workspace_id != workspace_id:
        raise ApiError("not_found", "模型配置不存在", status_code=status.HTTP_404_NOT_FOUND)
    return config


async def _clear_other_defaults(
    session: SessionDep, workspace_id: uuid.UUID, kind: str, keep_id: uuid.UUID
) -> None:
    """同一工作区同一 kind 只能有一个默认模型（DB 有部分唯一索引兜底）。"""

    await session.execute(
        update(ModelConfig)
        .where(
            ModelConfig.workspace_id == workspace_id,
            ModelConfig.kind == kind,
            ModelConfig.id != keep_id,
            ModelConfig.is_default.is_(True),
        )
        .values(is_default=False)
    )


@router.post(
    "/models",
    response_model=ModelConfigOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记模型配置",
)
async def create_model(
    payload: ModelConfigCreate, session: SessionDep, user: CurrentUser
) -> ModelConfig:
    ctx = await load_workspace_context(session, user, payload.workspace_id)

    exists = (
        await session.execute(
            select(ModelConfig.id).where(
                ModelConfig.workspace_id == payload.workspace_id,
                ModelConfig.kind == payload.kind,
                ModelConfig.name == payload.name,
            )
        )
    ).first()
    if exists is not None:
        raise ApiError("conflict", "同名模型配置已存在", status_code=status.HTTP_409_CONFLICT)

    if payload.is_default:
        await session.execute(
            update(ModelConfig)
            .where(
                ModelConfig.workspace_id == payload.workspace_id,
                ModelConfig.kind == payload.kind,
                ModelConfig.is_default.is_(True),
            )
            .values(is_default=False)
        )

    config = ModelConfig(
        workspace_id=payload.workspace_id,
        name=payload.name,
        kind=payload.kind,
        provider=payload.provider,
        model_name=payload.model_name,
        base_url=payload.base_url,
        credential_ref=payload.credential_ref,
        is_default=payload.is_default,
        created_by=ctx.membership.user_id,
    )
    session.add(config)
    await session.flush()
    await session.commit()
    await session.refresh(config)
    return config


@router.get("/models", response_model=list[ModelConfigOut], summary="模型配置列表")
async def list_models(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    kind: str | None = Query(default=None, pattern="^(chat|embedding)$"),
) -> list[ModelConfig]:
    await load_workspace_context(session, user, workspace_id)
    stmt = select(ModelConfig).where(ModelConfig.workspace_id == workspace_id)
    if kind is not None:
        stmt = stmt.where(ModelConfig.kind == kind)
    stmt = stmt.order_by(ModelConfig.kind, ModelConfig.name)
    return list((await session.execute(stmt)).scalars())


@router.patch("/models/{model_id}", response_model=ModelConfigOut, summary="更新模型配置")
async def update_model(
    model_id: uuid.UUID,
    payload: ModelConfigUpdate,
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ModelConfig:
    config = await _load_model(session, user, model_id, workspace_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(config, field, value)
    if data.get("is_default"):
        await _clear_other_defaults(session, config.workspace_id, config.kind, config.id)
    await session.commit()
    await session.refresh(config)
    return config


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型配置",
)
async def delete_model(
    model_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> Response:
    config = await _load_model(session, user, model_id, workspace_id)
    await session.delete(config)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/models/{model_id}/test",
    response_model=ModelTestResult,
    summary="连接测试：探测当前装配的适配器",
)
async def test_model(
    model_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> ModelTestResult:
    config = await _load_model(session, user, model_id, workspace_id)
    started = time.perf_counter()

    if config.kind == "embedding":
        port = get_embedding_port()
        adapter_name = getattr(port, "name", type(port).__name__)
        if config.provider in _LOCAL_PROVIDERS:
            port.embed(["pyknora-connection-test"])
            ok = True
            detail = "本地替身适配器可用；生产环境需接入真实 embedding 服务"
        else:
            ok = False
            detail = f"provider={config.provider} 的适配器尚未接入（Phase 1 仅内置 local）"
    else:
        port = get_chat_port()
        adapter_name = getattr(port, "name", type(port).__name__)
        if config.provider in _LOCAL_PROVIDERS:
            async for _ in port.stream(question="ping", contexts=()):
                break
            ok = True
            detail = "本地替身适配器可用；生产环境需接入真实 chat 服务"
        else:
            ok = False
            detail = f"provider={config.provider} 的适配器尚未接入（Phase 1 仅内置 local）"

    matches_runtime = config.provider in _LOCAL_PROVIDERS
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return ModelTestResult(
        ok=ok,
        kind=config.kind,
        provider=config.provider,
        adapter=str(adapter_name),
        matches_runtime=matches_runtime,
        latency_ms=latency_ms,
        detail=detail,
    )
