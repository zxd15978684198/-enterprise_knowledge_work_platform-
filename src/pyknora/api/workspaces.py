"""工作区：列表、创建、详情。"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from pyknora.deps import CurrentUser, SessionDep, WorkspaceDep
from pyknora.errors import ApiError
from pyknora.models import Membership, User, Workspace
from pyknora.schemas import (
    MemberOut,
    WorkspaceCreate,
    WorkspaceDetail,
    WorkspaceOut,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

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


@router.get("", response_model=list[WorkspaceOut], summary="我参与的工作区")
async def list_workspaces(user: CurrentUser, session: SessionDep) -> list[WorkspaceOut]:
    stmt = (
        select(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.created_at)
    )
    rows = (await session.scalars(stmt)).all()
    return [WorkspaceOut.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=WorkspaceDetail,
    status_code=status.HTTP_201_CREATED,
    summary="创建工作区",
)
async def create_workspace(
    payload: WorkspaceCreate, user: CurrentUser, session: SessionDep
) -> WorkspaceDetail:
    slug = payload.slug or _slugify(payload.name)
    if await session.scalar(
        select(func.count()).select_from(Workspace).where(Workspace.slug == slug)
    ):
        if payload.slug:
            raise ApiError("slug_taken", "该 slug 已被占用", status_code=status.HTTP_409_CONFLICT)
        slug = await _unique_slug(session, slug)

    workspace = Workspace(name=payload.name, slug=slug, owner_id=user.id)
    session.add(workspace)
    await session.flush()
    membership = Membership(workspace_id=workspace.id, user_id=user.id, role="owner")
    session.add(membership)
    await session.commit()

    return WorkspaceDetail(
        **WorkspaceOut.model_validate(workspace).model_dump(),
        role=membership.role,
        members=[
            MemberOut(
                user_id=user.id,
                role="owner",
                email=user.email,
                display_name=user.display_name,
            )
        ],
    )


@router.get("/{workspace_id}", response_model=WorkspaceDetail, summary="工作区详情")
async def get_workspace(ctx: WorkspaceDep, session: SessionDep) -> WorkspaceDetail:
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == ctx.workspace_id)
        .order_by(Membership.created_at)
    )
    members = [
        MemberOut(
            user_id=membership.user_id,
            role=membership.role,
            email=member.email,
            display_name=member.display_name,
        )
        for membership, member in (await session.execute(stmt)).all()
    ]
    return WorkspaceDetail(
        **WorkspaceOut.model_validate(ctx.workspace).model_dump(),
        role=ctx.role,
        members=members,
    )
