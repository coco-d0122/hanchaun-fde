# -*- coding: utf-8 -*-
"""
用户管理接口（阶段5第一批，方案 2.2，全部 admin）

GET   /api/users        用户列表（user:read）
POST  /api/users        创建用户（user:write）
PATCH /api/users/{id}   修改角色 / 禁用启用 / 重置密码（user:write）

写操作一律：CSRF 头校验 + 成功后写 audit_logs（白名单摘要）+ 刷新权限缓存。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import require_csrf_header, require_permission
from app.core.security import hash_password
from app.db import repo
from app.db.models import Role, User, UserRole
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_\-\.]+$")
    password: str = Field(min_length=8, max_length=128)
    role: Literal["user", "admin"] = "user"
    display_name: str = Field(default="", max_length=64)


class UserPatch(BaseModel):
    """三类变更可单独提交；全部为可选字段。"""
    role: Literal["user", "admin"] | None = None
    is_active: bool | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


@router.get("", dependencies=[Depends(require_permission("user:read"))])
async def get_users() -> dict:
    """用户列表（不含任何密码信息）。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_users(db)
    return {"items": items, "total": len(items)}


@router.post("", dependencies=[Depends(require_permission("user:write")),
                               Depends(require_csrf_header)])
async def create_user(body: UserCreate, request: Request) -> dict:
    """创建用户：密码入库前即哈希（$2b$ 前缀），任何日志不留明文。"""
    async with session_factory() as db:
        async with db.begin():
            if await repo.get_user_by_name(db, body.username) is not None:
                raise AppError("USER_EXISTS", "用户名已存在", 409, {"username": body.username})
            user = await repo.create_user(
                db, body.username, hash_password(body.password),
                body.role, body.display_name)
            await repo.write_audit_log(
                db, "user_create", user.id,
                object_type="user", object_id=str(user.id),
                detail={"username": user.username, "role": body.role})
    _refresh_permission_cache(request)
    return {"id": user.id, "username": user.username, "role": body.role}


@router.patch("/{user_id}", dependencies=[Depends(require_permission("user:write")),
                                          Depends(require_csrf_header)])
async def patch_user(user_id: int, body: UserPatch, request: Request) -> dict:
    """修改角色 / 禁用启用 / 重置密码。禁止把最后一个管理员降级或禁用。"""
    async with session_factory() as db:
        async with db.begin():
            user = await repo.get_user(db, user_id)
            if user is None:
                raise AppError("USER_NOT_FOUND", "用户不存在", 404, {"user_id": user_id})

            changed: dict = {}
            if body.role is not None:
                # 保护：不允许把最后一个活跃管理员降级（否则系统失去管理入口）
                role_codes = await repo.get_user_role_codes(db, user)
                if "admin" in role_codes and body.role != "admin":
                    admins = (await db.execute(
                        select(User.id)
                        .join(UserRole, UserRole.user_id == User.id)
                        .join(Role, Role.id == UserRole.role_id)
                        .where(Role.code == "admin", User.is_active == True)  # noqa: E712
                    )).scalars().all()
                    if len(admins) <= 1:
                        raise AppError("LAST_ADMIN", "不能降级最后一个管理员", 400)
                await repo.set_user_role(db, user_id, body.role)
                changed["role"] = body.role

            if body.is_active is not None:
                user.is_active = body.is_active
                changed["is_active"] = body.is_active

            if body.new_password is not None:
                user.password_hash = hash_password(body.new_password)
                changed["password_reset"] = True   # 日志只记"重置过"，绝不记密码

            await db.flush()
            await repo.write_audit_log(
                db, "user_update", user_id,
                object_type="user", object_id=str(user_id),
                detail={"changed": changed, "username": user.username})

    _refresh_permission_cache(request)
    return {"id": user_id, "changed": {k: v for k, v in {
        "role": body.role, "is_active": body.is_active,
        "password_reset": body.new_password is not None}.items() if v is not None}}


def _refresh_permission_cache(request: Request) -> None:
    """角色变更后异步刷新缓存（从库重载角色→权限映射，不阻塞当前响应）。"""
    async def _reload() -> None:
        try:
            async with session_factory() as db:
                async with db.begin():
                    request.app.state.permission_cache = await repo.list_role_permissions(db)
        except Exception:  # noqa: BLE001
            logger.exception("刷新权限缓存失败（下次请求将退化为实时查库）")

    try:
        asyncio.get_running_loop().create_task(_reload())
    except RuntimeError:  # pragma: no cover
        pass
