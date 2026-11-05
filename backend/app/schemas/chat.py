# -*- coding: utf-8 -*-
"""
Pydantic 请求/响应模型（阶段4）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。

    - session_id 可空：为空时由服务端创建并经 SSE session 事件返回
    - question：1–500 字
    - mode：knowledge / anomaly，缺省 knowledge
    - anomaly_id：扩展字段 E1，anomaly 模式的事件定位
    """

    session_id: str | None = Field(default=None, max_length=36)
    question: str = Field(min_length=1, max_length=500)
    mode: Literal["knowledge", "anomaly"] = "knowledge"
    anomaly_id: str | None = Field(default=None, max_length=32)


class SessionSummary(BaseModel):
    session_id: str
    title: str
    mode: str
    created_at: str
    updated_at: str
    message_count: int


class SessionListResponse(BaseModel):
    items: list[SessionSummary]
    total: int


class Citation(BaseModel):
    source_type: Literal["knowledge", "anomaly_data"]
    source_file: str
    chunk_id: str
    excerpt: str


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    mode: str
    status: str | None = None
    created_at: str
    guardrail_passed: bool | None = None
    citations: list[Citation] = []


class SessionMessagesResponse(BaseModel):
    session_id: str
    items: list[MessageItem]
