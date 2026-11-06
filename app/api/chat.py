# -*- coding: utf-8 -*-
"""
POST /api/chat —— SSE 流式聊天接口（阶段4方案第三节）

建流之前的错误（参数/会话/事件/KB 状态）→ HTTP 状态码 + 统一 JSON 错误体；
建流之后的错误 → SSE error 事件（见 chat_service）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import require_csrf_header, require_login
from app.core import token_budget
from app.core.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import anomaly_not_found, anomaly_not_specified, kb_not_ready, session_not_found
from app.schemas.chat import ChatRequest
from app.services.chat_service import chat_stream, resolve_anomaly_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", dependencies=[Depends(require_csrf_header)])
async def post_chat(req: ChatRequest, request: Request,
                    user: Annotated[User, Depends(require_login)]) -> StreamingResponse:
    cfg = get_settings()

    # ---- 建流前校验 0：LLM 日预算（阶段11，超限 429 中文提示）----
    await token_budget.enforce_daily_budget(user.id, cfg)

    # ---- 建流前校验 1：知识库就绪 ----
    retriever = getattr(request.app.state, "retriever", None)
    if retriever is None or not retriever.chunks:
        raise kb_not_ready()

    # ---- 建流前校验 2：会话存在性（且必须是本人的会话，防横向越权）----
    if req.session_id:
        async with session_factory() as db:
            conv = await repo.get_conversation(db, req.session_id)
        if conv is None:
            raise session_not_found(req.session_id)
        if conv.user_id is not None and conv.user_id != user.id:
            # 别人的会话：按"不存在"处理，不泄露存在性
            raise session_not_found(req.session_id)

    # ---- 建流前校验 3：anomaly 模式的事件定位（E1 扩展）----
    if req.mode == "anomaly":
        async with session_factory() as db:
            if req.anomaly_id:
                row = await repo.get_anomaly_event(db, req.anomaly_id)
                if row is None:
                    raise anomaly_not_found(req.anomaly_id)
            else:
                # 无显式 anomaly_id：从问题中正则提取事件号 / 电表号
                rows = await repo.list_anomaly_events(db)
                resolved = resolve_anomaly_id(req.question, rows)
                if resolved is None:
                    raise anomaly_not_specified()
                req.anomaly_id = resolved   # 回填，流内直接使用

    # ---- 建流：SSE（no-cache 禁用代理缓冲，保证 token 逐段到达）----
    return StreamingResponse(
        chat_stream(req, request, retriever, cfg, user_id=user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # Nginx 前置时也禁用缓冲
        },
    )
