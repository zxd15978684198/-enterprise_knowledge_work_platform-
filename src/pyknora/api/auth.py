"""认证：注册、登录、当前用户。"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from pyknora.config import get_settings
from pyknora.deps import CurrentUser, SessionDep
from pyknora.errors import ApiError
from pyknora.models import Membership, User, Workspace
from pyknora.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from pyknora.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")
    return slug or "workspace"


async def _unique_slug(session: SessionDep, base: str) -> str:
    candidate = base
    for suffix in range(0, 100):
        exists = await session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.slug == candidate)
        )
        if not exists:
            return candidate
        candidate = f"{base}-{suffix + 2}"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _token_payload(user: User) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=settings.jwt_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册并创建默认工作区",
)
async def register(payload: RegisterRequest, session: SessionDep) -> TokenResponse:
    email = payload.email.lower()
    existing = await session.scalar(select(User.id).where(func.lower(User.email) == email))
    if existing is not None:
        raise ApiError("email_taken", "该邮箱已注册", status_code=status.HTTP_409_CONFLICT)

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name or email.split("@", 1)[0],
    )
    session.add(user)
    await session.flush()

    display = user.display_name or "我的工作区"
    workspace = Workspace(
        name=f"{display} 的工作区",
        slug=await _unique_slug(session, _slugify(display)),
        owner_id=user.id,
    )
    session.add(workspace)
    await session.flush()
    session.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    await session.commit()
    return _token_payload(user)


@router.post("/login", response_model=TokenResponse, summary="登录")
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    email = payload.email.lower()
    user = await session.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise ApiError(
            "invalid_credentials",
            "邮箱或密码不正确",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not user.is_active:
        raise ApiError("account_disabled", "账号已停用", status_code=status.HTTP_403_FORBIDDEN)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        await session.commit()
    return _token_payload(user)


@router.get("/me", response_model=UserOut, summary="当前用户")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
