# -*- coding: utf-8 -*-
"""
历史会话接口（阶段4方案 3.2 / 3.3）
"""

from __future__ import annotations

from fastapi import APIRouter, Path

from app.db import repo
from app.db.session import session_factory
from app.errors import session_not_found
from app.schemas.chat import SessionListResponse, SessionMessagesResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=SessionListResponse)
async def get_sessions() -> SessionListResponse:
    """会话列表，按最近更新时间倒序。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_conversations(db)
    return SessionListResponse(items=items, total=len(items))


@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(session_id: str = Path(max_length=36)) -> SessionMessagesResponse:
    """指定会话的历史消息（含引用），支持前端重新加载历史聊天记录。"""
    async with session_factory() as db:
        async with db.begin():
            conv = await repo.get_conversation(db, session_id)
            if conv is None:
                raise session_not_found(session_id)
            items = await repo.get_messages(db, session_id)
    return SessionMessagesResponse(session_id=session_id, items=items)
