# -*- coding: utf-8 -*-
"""
历史会话接口（阶段4方案 3.2 / 3.3）
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import require_login
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import session_not_found
from app.schemas.chat import SessionListResponse, SessionMessagesResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=SessionListResponse)
async def get_sessions(user: Annotated[User, Depends(require_login)]) -> SessionListResponse:
    """会话列表（按最近更新时间倒序）。阶段5：只返回当前用户自己的会话。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_conversations(db, user_id=user.id)
    return SessionListResponse(items=items, total=len(items))


@router.get("/{session_id}/messages", response_model=SessionMessagesResponse)
async def get_session_messages(
    session_id: str = Path(max_length=36),
    user: Annotated[User, Depends(require_login)] = None,
) -> SessionMessagesResponse:
    """指定会话的历史消息（含引用）。会话不存在或不属于当前用户 → 404（不泄露存在性）。"""
    async with session_factory() as db:
        async with db.begin():
            conv = await repo.get_conversation(db, session_id)
            if conv is None or (conv.user_id is not None and conv.user_id != user.id):
                raise session_not_found(session_id)
            items = await repo.get_messages(db, session_id)
    return SessionMessagesResponse(session_id=session_id, items=items)
