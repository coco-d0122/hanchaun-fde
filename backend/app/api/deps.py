# -*- coding: utf-8 -*-
"""
认证与权限依赖（阶段5第一批）

用法（FastAPI Depends）：
    @router.get("/api/users", dependencies=[Depends(require_permission("user:read"))])

职责链：
  Cookie(hc_token) → decode_token → users 表实时核对（禁用立即生效）
  → 角色权限码集合（app.state.permission_cache）→ 401 / 403（统一中文错误）

CSRF（方案 3.3 简化双提交）：写操作校验 X-Requested-With: XMLHttpRequest
自定义头——同站表单/跨站表单都无法伪造该头，浏览器表单提交不带它。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from app.core.security import COOKIE_NAME, decode_token
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError


def _unauthorized() -> AppError:
    return AppError("UNAUTHORIZED", "登录已失效，请重新登录", 401)


def _forbidden() -> AppError:
    return AppError("FORBIDDEN", "当前角色无权执行此操作", 403)


def get_permission_cache(request: Request) -> dict[str, list[str]]:
    """角色→权限码集合缓存（启动时加载；用户管理变更后刷新）。"""
    cache = getattr(request.app.state, "permission_cache", None)
    return cache if isinstance(cache, dict) else {}


async def get_current_user(request: Request) -> User:
    """
    从 HttpOnly Cookie 解 JWT 并实时核对用户状态。

    为什么每次都查库而不是信任 token：禁用账号必须立即生效；
    权限码不进 JWT payload（改角色后立即生效，避免旧 token 携带过期权限）。
    """
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        raise _unauthorized()
    payload = decode_token(token)
    if not payload:
        raise _unauthorized()
    try:
        user_id = int(payload.get("sub", "0"))
    except (TypeError, ValueError):
        raise _unauthorized() from None

    async with session_factory() as db:
        async with db.begin():
            user = await repo.get_user(db, user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


async def get_current_user_optional(request: Request) -> User | None:
    """同 get_current_user，但未登录返回 None（页面路由跳转用，不抛 401）。"""
    try:
        return await get_current_user(request)
    except AppError:
        return None


def require_login(user: Annotated[User, Depends(get_current_user)]) -> User:
    """登录即可（chat:use 级别），不校验具体权限码。"""
    return user


def require_permission(code: str):
    """权限工厂：校验当前用户任一角色拥有指定权限码。"""

    async def _checker(
        user: Annotated[User, Depends(get_current_user)],
        request: Request,
    ) -> User:
        cache = get_permission_cache(request)
        if not cache:
            # 缓存缺失时退化为实时查库（防御性，正常不会发生）
            async with session_factory() as db:
                async with db.begin():
                    role_codes = await repo.get_user_role_codes(db, user)
                    mapping = await repo.list_role_permissions(db)
            role_codes = [r for r in role_codes if r]
        else:
            role_codes = [r.code for r in user.roles]

        for role_code in role_codes:
            if code in cache.get(role_code, []):
                return user
        raise _forbidden()

    return _checker


async def require_csrf_header(
    x_requested_with: Annotated[str | None, Header()] = None,
) -> None:
    """写操作的 CSRF 防线：要求自定义头 X-Requested-With: XMLHttpRequest。

    浏览器原生表单无法携带自定义头；跨站请求即使带 Cookie 也过不了这关。
    阶段5管理页的 Jinja2 表单将统一改走 fetch + 该头。
    """
    if (x_requested_with or "") != "XMLHttpRequest":
        raise AppError("CSRF_REJECTED", "请求缺少有效的 CSRF 校验头", 403)
