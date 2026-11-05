# -*- coding: utf-8 -*-
"""
身份认证接口（阶段5第一批，方案 2.1）

POST /api/auth/login   登录：校验 bcrypt → 签发 JWT → HttpOnly Cookie
POST /api/auth/logout  退出：清除 Cookie
GET  /api/auth/me      当前用户信息（用户名/角色/权限码列表）
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, require_csrf_header
from app.core.security import COOKIE_NAME, JWT_EXPIRE_HOURS, issue_token, verify_password
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError, param_invalid
from app.api.deps import get_permission_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login", dependencies=[Depends(require_csrf_header)])
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    """登录。失败统一返回 401 中文提示（不区分"用户不存在"与"密码错误"，防枚举）。"""
    async with session_factory() as db:
        async with db.begin():
            user = await repo.get_user_by_name(db, body.username.strip())
            # 审计登录失败（不含密码任何信息）
            if user is None or not verify_password(body.password, user.password_hash) \
                    or not user.is_active:
                await repo.write_audit_log(
                    db, "login_failure", None,
                    object_type="user", object_id=body.username.strip(),
                    result="failure", detail={"reason": "用户名或密码错误或账号禁用"})
                raise AppError("UNAUTHORIZED", "用户名或密码错误，或账号已被禁用", 401)

            role_codes = await repo.get_user_role_codes(db, user)
            await repo.write_audit_log(
                db, "login", user.id,
                object_type="user", object_id=str(user.id),
                detail={"username": user.username, "roles": role_codes})

    # 角色主角色 = 第一个（单角色模型）
    primary_role = role_codes[0] if role_codes else "user"
    token, expires = issue_token(user.id, user.username, primary_role)

    # HttpOnly + SameSite=Strict：JS 读不到令牌，跨站不携带（XSS/CSRF 双防）
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_EXPIRE_HOURS * 3600,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {
        "user": {"id": user.id, "username": user.username,
                 "display_name": user.display_name, "roles": role_codes},
        "expires_at": expires.isoformat(),
    }


@router.post("/logout", dependencies=[Depends(require_csrf_header)])
async def logout(request: Request, response: Response) -> dict:
    """退出：清除 Cookie。已登录才写审计；未登录也允许清（幂等）。"""
    user: User | None = None
    try:
        user = await get_current_user(request)
    except AppError:
        pass
    async with session_factory() as db:
        async with db.begin():
            await repo.write_audit_log(
                db, "logout", user.id if user else None,
                object_type="user", object_id=str(user.id) if user else "")
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, user: Annotated[User, Depends(get_current_user)]) -> dict:
    """当前用户信息：用户名、角色、权限码列表（前端按权限渲染入口）。"""
    async with session_factory() as db:
        async with db.begin():
            role_codes = await repo.get_user_role_codes(db, user)
    cache = get_permission_cache(request)
    perms = sorted({p for r in role_codes for p in cache.get(r, [])})
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "roles": role_codes,
        "permissions": perms,
    }
