"""API 路由包。"""

from __future__ import annotations

from fastapi import APIRouter

from pyknora.api import auth, health, workspaces

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(workspaces.router)

__all__ = ["api_router", "auth", "health", "workspaces"]
