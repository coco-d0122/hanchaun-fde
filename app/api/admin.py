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


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request,
                         user: User | None = Depends(get_current_user_optional)):
    """工作台（阶段5补全）：概览卡片 + 最近 10 条操作日志。

    门禁用 user:read：工作台是管理员视角的总览，普通用户没有该权限码。
    """
    if user is None:
        return RedirectResponse("/login?next=/admin/dashboard", status_code=302)
    if "user:read" not in await _permission_codes(request, user):
        return _forbidden(user, "user:read")

    async with session_factory() as db:
        async with db.begin():
            stats = await repo.dashboard_stats(db)
    # detail 是 JSON 列：转成可读文本再交给模板（Jinja 直接渲染 dict 会变成单引号 repr）
    for row in stats["recent_logs"]:
        row["detail"] = json.dumps(row["detail"], ensure_ascii=False)

    request.state.user = user
    return _templates.TemplateResponse(request, "dashboard.html", {
        "username": user.username, "nav": "dashboard", "stats": stats,
    })


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request,
                     user: User | None = Depends(get_current_user_optional)):
    """用户管理页（阶段5补全）：列表 / 建号 / 改角色 / 禁用启用。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/users", status_code=302)
    if "user:read" not in await _permission_codes(request, user):
        return _forbidden(user, "user:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "users.html", {
        "username": user.username, "nav": "users",
    })


def _forbidden(user: User, perm: str) -> HTMLResponse:
    return HTMLResponse(
        _env.from_string(_FORBIDDEN_TPL).render(username=user.username, perm=perm),
        status_code=403)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request,
                    user: User | None = Depends(get_current_user_optional)):
    """系统操作日志页（阶段5第五批）：只读，按时间/类型/操作人/结果/对象号筛选。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/logs", status_code=302)
    if "log:read" not in await _permission_codes(request, user):
        return _forbidden(user, "log:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "logs.html", {
        "username": user.username, "nav": "logs",
    })


@router.get("/agent-tasks", response_class=HTMLResponse)
async def agent_tasks_page(request: Request,
                           user: User | None = Depends(get_current_user_optional)):
    """Agent 任务账本页（阶段6）：状态机快照 + 工具调用轨迹，用于排查执行过程。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/agent-tasks", status_code=302)
    if "audit:conversation:read" not in await _permission_codes(request, user):
        return _forbidden(user, "audit:conversation:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "agent_tasks.html", {
        "username": user.username, "nav": "agent_tasks",
    })


@router.get("/agent-logs", response_class=HTMLResponse)
async def agent_logs_page(request: Request,
                          user: User | None = Depends(get_current_user_optional)):
    """Harness 执行日志页（阶段7）：每轮决策与每次工具调用可追溯。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/agent-logs", status_code=302)
    if "audit:conversation:read" not in await _permission_codes(request, user):
        return _forbidden(user, "audit:conversation:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "agent_logs.html", {
        "username": user.username, "nav": "agent_logs",
    })


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


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request,
                     user: User | None = Depends(get_current_user_optional)):
    """AI 对话审计页（阶段5第四批）：六条件筛选 + 消息详情 + 风险标记。"""
    if user is None:
        return RedirectResponse("/login?next=/admin/audit", status_code=302)
    if "audit:conversation:read" not in await _permission_codes(request, user):
        return _forbidden(user, "audit:conversation:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "audit.html", {
        "username": user.username, "nav": "audit",
    })


@router.get("/anomalies", response_class=HTMLResponse)
async def anomalies_page(request: Request,
                         user: User | None = Depends(get_current_user_optional)):
    """能耗异常事件审计页（阶段5第四批）：规则结果与 AI 草稿分离展示，复核只读。

    ★ 为什么除 anomaly:read 还要 audit:conversation:read：
      按方案页面清单，本页是 admin 专属的"审计视图"（含 AI 分析草稿、复核记录）。
      而 anomaly:read 权限矩阵里 user 也有（聊天页要能看事件），单靠它会让普通用户
      进入后台审计页。用"审计视角"权限做闸门，语义比新造一个页面权限更准确。
    """
    if user is None:
        return RedirectResponse("/login?next=/admin/anomalies", status_code=302)
    perms = await _permission_codes(request, user)
    if "anomaly:read" not in perms or "audit:conversation:read" not in perms:
        return _forbidden(user, "audit:conversation:read")

    request.state.user = user
    return _templates.TemplateResponse(request, "anomalies.html", {
        "username": user.username, "nav": "anomalies",
    })
