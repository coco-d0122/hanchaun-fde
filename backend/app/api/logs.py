# -*- coding: utf-8 -*-
"""
系统操作日志 API（阶段5第五批）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | /api/logs          | log:read |
| GET | /api/logs/actions  | log:read |

只读、不可改不可删（清理只由启动时的保留策略执行）。
detail 白名单化：日志写入器只接受预定义键，密码 / API Key / 提示词全文进不来。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user, require_permission
from app.core.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import session_factory

router = APIRouter(prefix="/api/logs", tags=["logs"])

_Reader = Depends(require_permission("log:read"))


@router.get("")
async def list_logs(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    action: str | None = Query(None, max_length=64),
    user_id: int | None = Query(None, ge=1),
    result: Literal["success", "failure"] | None = None,
    keyword: str | None = Query(None, max_length=64, description="对象号模糊匹配"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """操作日志列表：时间/类型/操作人/结果/关键词组合筛选。"""
    async with session_factory() as db:
        async with db.begin():
            data = await repo.list_audit_logs(
                db, date_from=date_from, date_to=date_to, action=action,
                user_id=user_id, result=result, keyword=keyword,
                limit=limit, offset=offset)
    data["retention_days"] = get_settings().log_retention_days
    return data


@router.get("/actions")
async def list_actions(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """可筛选的操作类型字典（中文名，前端下拉用）。"""
    return {"items": [{"code": k, "name": v} for k, v in repo.ACTION_LABELS.items()]}
