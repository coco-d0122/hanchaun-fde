# -*- coding: utf-8 -*-
"""
SQLAlchemy 2.x 声明式模型（阶段4方案第五节五表设计）

设计要点：
  - 全部主键自增/显式，时间字段用 DateTime（SQLite 存 ISO8601 字符串，PG 迁移友好）
  - JSON 字段走 sa.JSON（SQLite 存 TEXT，PG 可平滑换 JSONB）
  - String 显式长度，不使用 SQLite 方言特性
  - 级联删除：会话删除 → 级联消息、引用（本阶段无删除接口，约束先行）
  - anomaly_events.extra 存"重建分析用事件对象"所需的补充字段
    （event_type/suspects/evidence/suggestions/need_review），
    与方案 3.5 节详情接口的字段一一对应
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """统一 UTC 时间戳（ISO8601 UTC 字符串，PG 迁移友好）。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    """会话表 conversations。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)          # conv-<uuid>
    title: Mapped[str] = mapped_column(String(64), default="新会话")        # 首条用户消息前 30 字
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow,
                                                 index=True)                # 列表排序键

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(Base):
    """消息表 messages（status 扩展 E2：generating/complete/interrupted/failed）。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conv_id", "conversation_id", "id"),   # 取最近 N 条的最高频查询
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))                # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="knowledge")   # knowledge / anomaly
    # 用户消息恒为 NULL；assistant：generating / complete / interrupted / failed
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error_info: Mapped[str | None] = mapped_column(Text, nullable=True)  # failed 时的错误 JSON
    # 仅 anomaly 模式助手消息：数值溯源结果
    guardrail_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    citations: Mapped[list["MessageCitation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan")


class MessageCitation(Base):
    """消息引用表 message_citations（source_type 扩展 E3：knowledge / anomaly_data）。

    存储策略：只存定位键 + 短摘录（前 120 字），不存 500 字全文。
    知识库全文在 FAISS 缓存（chunks.json）里，chunk_id 可精确回查。
    """

    __tablename__ = "message_citations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(16))         # knowledge / anomaly_data
    source_file: Mapped[str] = mapped_column(String(256))        # 文件名 / 证据来源标识
    chunk_id: Mapped[str] = mapped_column(String(128))           # 知识库切片 ID / 证据定位键
    excerpt: Mapped[str] = mapped_column(String(160))            # 截断摘录

    message: Mapped[Message] = relationship(back_populates="citations")


class AnomalyEventRow(Base):
    """异常事件表 anomaly_events。

    本阶段 status 只由人工在数据库层面维护，不做修改接口——
    "不自动生成最终结论"的边界保持。
    """

    __tablename__ = "anomaly_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)   # AN-YYYYMMDD-NNNN
    meter_id: Mapped[str] = mapped_column(String(32), index=True)
    building_id: Mapped[str] = mapped_column(String(16), default="")
    anomaly_type: Mapped[str] = mapped_column(String(64))           # 中文类型名（title）
    rule_code: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(8))                # high / medium / low
    detected_at: Mapped[datetime] = mapped_column(DateTime)          # 事件窗口起点
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(Text)                   # 人可读摘要（summary）
    rule_result: Mapped[dict] = mapped_column(JSON)                  # 指标字典（metrics）
    status: Mapped[str] = mapped_column(String(16), default="pending_review")
    # 重建 AnomalyEvent 对象所需的补充字段（AI 分析时用），JSON 序列化存储
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    evidence_rows: Mapped[list["AnomalyEvidenceRow"]] = relationship(
        back_populates="event", cascade="all, delete-orphan")


class AnomalyEvidenceRow(Base):
    """异常证据表 anomaly_evidence。证据原文由规则引擎产出，不经过 LLM。"""

    __tablename__ = "anomaly_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    anomaly_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("anomaly_events.id", ondelete="CASCADE"), index=True)
    evidence_type: Mapped[str] = mapped_column(String(32))       # rule_metric / reading / balance_calc
    evidence_content: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str] = mapped_column(String(128))   # 数据定位（表#行）

    event: Mapped[AnomalyEventRow] = relationship(back_populates="evidence_rows")
