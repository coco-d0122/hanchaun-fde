# -*- coding: utf-8 -*-
"""
对话审计 API（阶段5第四批）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET   | /api/audit/conversations                 | audit:conversation:read |
| GET   | /api/audit/conversations/{message_id}    | audit:conversation:read |
| GET   | /api/audit/flags                         | audit:conversation:read |
| POST  | /api/audit/flags                         | audit:flag:write |
| PATCH | /api/audit/flags/{flag_id}               | audit:flag:write |

审计单元 = 一条 assistant 消息（含问题、回答、引用、提示词版本、生成状态、关联异常）。
本阶段管理员可见全库；scope 列就位后按数据范围收紧（方案 7.1 防越界）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, require_csrf_header, require_permission
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError
from app.services import audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])

_Reader = Depends(require_permission("audit:conversation:read"))
_Writer = Depends(require_permission("audit:flag:write"))


class FlagCreate(BaseModel):
    message_id: int = Field(..., ge=1)
    flag_type: str = Field(..., description="7 类之一，见 GET /api/audit/flag-types")
    note: str = Field("", max_length=500)


class FlagUpdate(BaseModel):
    status: Literal["pending", "processed", "dismissed"]
    note: str | None = Field(None, max_length=500)


@router.get("/flag-types")
async def flag_types(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
) -> dict:
    """风险标记类型字典（前端下拉用，避免前端硬编码）。"""
    return {"items": [{"code": k, "name": v} for k, v in repo.FLAG_TYPES.items()]}


@router.get("/conversations")
async def list_conversations(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
    user_id: int | None = Query(None, description="按用户过滤"),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    session_id: str | None = Query(None, max_length=36),
    mode: Literal["knowledge", "anomaly"] | None = None,
    anomaly_id: str | None = Query(None, max_length=32, description="按关联事件号过滤"),
    has_anomaly: bool | None = Query(None, description="是否涉及异常事件"),
    has_flag: bool | None = Query(None, description="是否有风险标记"),
    keyword: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """对话审计列表：六条件组合筛选（用户/时间/会话/模式/涉异常/有标记）+ 关键词。"""
    async with session_factory() as db:
        async with db.begin():
            return await repo.list_audit_messages(
                db, user_id=user_id, date_from=date_from, date_to=date_to,
                session_id=session_id, mode=mode, anomaly_id=anomaly_id,
                has_anomaly=has_anomaly,
                has_flag=has_flag, keyword=keyword, limit=limit, offset=offset)


@router.get("/conversations/{message_id}")
async def get_message_detail(
    message_id: int = Path(ge=1),
    user: Annotated[User, Depends(get_current_user)] = None,   # noqa: RUF013
    _perm: None = _Reader,
) -> dict:
    """消息详情：问答原文 + 引用 + 提示词版本 + 生成状态（含 interrupted/failed）+ 关联异常。"""
    async with session_factory() as db:
        async with db.begin():
            detail = await repo.get_audit_message_detail(db, message_id)
    if detail is None:
        raise AppError("MESSAGE_NOT_FOUND", "消息不存在或已被删除", 404)
    return detail


@router.get("/flags")
async def list_flags(
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Reader,
    message_id: int | None = Query(None, ge=1),
    status: Literal["pending", "processed", "dismissed"] | None = None,
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    """风险标记列表（可按消息或处理状态过滤）。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_flags(db, message_id=message_id,
                                          status=status, limit=limit)
    return {"total": len(items), "items": items}


@router.post("/flags", dependencies=[Depends(require_csrf_header)])
async def create_flag(
    body: FlagCreate,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Writer,
) -> dict:
    """添加风险标记（7 类 + 备注）；同时写 audit_logs（action=flag_update）。"""
    if body.flag_type not in repo.FLAG_TYPES:
        raise AppError("FLAG_TYPE_INVALID",
                       f"未知的风险标记类型：{body.flag_type}", 400)
    return await audit_service.create_flag(body.message_id, body.flag_type,
                                           body.note, user.id)


@router.patch("/flags/{flag_id}", dependencies=[Depends(require_csrf_header)])
async def update_flag(
    flag_id: int,
    body: FlagUpdate,
    user: Annotated[User, Depends(get_current_user)],
    _perm: None = _Writer,
) -> dict:
    """更新标记处理状态（待处理/已处理/已驳回）与备注。"""
    return await audit_service.update_flag(flag_id, body.status, body.note, user.id)
