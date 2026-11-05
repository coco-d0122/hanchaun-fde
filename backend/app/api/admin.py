# -*- coding: utf-8 -*-
"""管理后台页面路由（阶段5第二批起：Jinja2 服务端渲染 + fetch 局部交互）。

页面路由与 API 路由的失败语义不同：
  - API：401/403 JSON（前端按提示处理）
  - 页面：未登录 → 302 跳登录页（带 next 回跳）；无权限 → 403 中文提示页

为什么不用 Depends(require_permission)：那个依赖抛 AppError，会被全局异常
处理器转成 JSON——浏览器直接打地址栏访问时看到一坨 JSON 体验很差。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.deps import get_current_user_optional, get_permission_cache
from app.db import repo
from app.db.models import User
from app.db.session import session_factory

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
# 独立环境仅用于渲染 403 提示页（不继承 base，避免依赖 nav/username 变量）
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)),
                   autoescape=select_autoescape(["html"]))

router = APIRouter(prefix="/admin", tags=["admin-pages"])

_FORBIDDEN_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>403 · 无访问权限</title>
<style>
 body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;
      color:#1f2328;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .box{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:32px 40px;text-align:center}
 h1{font-size:18px;margin:0 0 10px}
 p{color:#57606a;font-size:13.5px;line-height:1.8;margin:0 0 18px}
 a{color:#2563eb;text-decoration:none;font-size:13.5px}
</style></head><body><div class="box">
<h1>无访问权限</h1>
<p>当前账号「{{ username }}」缺少权限码 <b>{{ perm }}</b>。<br>如需访问请联系系统管理员调整角色。</p>
<a href="/">← 返回聊天</a>
</div></body></html>"""


async def _permission_codes(request: Request, user: User) -> list[str]:
    """当前用户的权限码合集（优先走启动缓存，缺失时实时查库）。"""
    mapping = get_permission_cache(request)
    if not mapping:
        async with session_factory() as db:
            async with db.begin():
                mapping = await repo.list_role_permissions(db)
    return sorted({c for r in user.roles for c in mapping.get(r.code, [])})


def _forbidden(user: User, perm: str) -> HTMLResponse:
    return HTMLResponse(
        _env.from_string(_FORBIDDEN_TPL).render(username=user.username, perm=perm),
        status_code=403)


@router.get("/kb", response_class=HTMLResponse)
async def kb_page(request: Request,
                  user: User | None = Depends(get_current_user_optional)):
    """能源管理知识库管理页（阶段5第二批）。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/kb", status_code=302)
    if "kb:read" not in await _permission_codes(request, user):
        return _forbidden(user, "kb:read")

    from app.core.config import get_settings
    from app.services.kb_service import DOC_TYPES

    request.state.user = user
    return _templates.TemplateResponse(request, "kb.html", {
        "username": user.username,
        "nav": "kb",
        "doc_types": DOC_TYPES,
        "doc_types_json": json.dumps({code: name for code, name in DOC_TYPES},
                                     ensure_ascii=False),
        "max_mb": get_settings().kb_upload_max_mb,
    })


@router.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request,
                       user: User | None = Depends(get_current_user_optional)):
    """AI 提示词管理页（阶段5第三批）：编辑/版本历史/恢复。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/prompts", status_code=302)
    if "prompt:read" not in await _permission_codes(request, user):
        return _forbidden(user, "prompt:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "prompts.html", {
        "username": user.username,
        "nav": "prompts",
        "template_codes": [("qa", "知识库问答模板"), ("anomaly", "异常分析模板")],
    })
