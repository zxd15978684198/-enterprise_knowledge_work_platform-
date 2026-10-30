"""FastAPI 依赖：会话、当前用户、工作区上下文。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pyknora.db import get_session
from pyknora.errors import ApiError
from pyknora.models import Membership, User, Workspace
from pyknora.security import TokenError, decode_access_token

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """从 ``Authorization: Bearer <jwt>`` 解析当前用户。"""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(
            "unauthenticated", "缺少 Bearer 令牌", status_code=status.HTTP_401_UNAUTHORIZED
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise ApiError("invalid_token", str(exc), status_code=status.HTTP_401_UNAUTHORIZED) from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise ApiError(
            "invalid_token", "令牌主体非法", status_code=status.HTTP_401_UNAUTHORIZED
        ) from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise ApiError(
            "unauthenticated", "用户不存在或已停用", status_code=status.HTTP_401_UNAUTHORIZED
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@dataclass(slots=True)
class WorkspaceContext:
    """工作区访问上下文：调用方在该工作区的成员身份。"""

    workspace: Workspace
    membership: Membership

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def role(self) -> str:
        return self.membership.role

    def require_owner(self) -> None:
        if self.role != "owner":
            raise ApiError(
                "forbidden", "仅工作区 Owner 可执行该操作", status_code=status.HTTP_403_FORBIDDEN
            )


async def get_workspace_context(
    workspace_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> WorkspaceContext:
    """校验成员身份；非成员与不存在同返回 404，避免泄漏工作区是否存在。"""

    stmt = (
        select(Workspace, Membership)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
            Membership.user_id == user.id,
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise ApiError("not_found", "工作区不存在", status_code=status.HTTP_404_NOT_FOUND)
    workspace, membership = row
    return WorkspaceContext(workspace=workspace, membership=membership)


WorkspaceDep = Annotated[WorkspaceContext, Depends(get_workspace_context)]
