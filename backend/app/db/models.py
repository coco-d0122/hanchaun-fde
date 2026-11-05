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


# ==============================================================================
# 阶段5第一批：身份与权限（5 张表）
# 关键立场：权限码 / 数据范围(scope 列) / 业务签核权三维度分离。
# anomaly:review:submit 首期不授予任何角色——"系统管理员 ≠ 电气安全主管"。
# ==============================================================================

class User(Base):
    """用户表 users。密码只存 bcrypt 哈希（$2b$ 前缀），绝不存明文。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))     # bcrypt
    display_name: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", lazy="selectin", viewonly=True)


class Role(Base):
    """角色表 roles：角色 = 权限码集合。扩展六业务角色只加数据不改代码。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)   # user / admin / 未来业务角色
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")

    permissions: Mapped[list["Permission"]] = relationship(
        secondary="role_permissions", lazy="selectin", viewonly=True)


class Permission(Base):
    """权限码字典 permissions。scope_required 标记该权限需叠加数据范围判断。"""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)   # 形如 kb:write
    description: Mapped[str] = mapped_column(String(128), default="")
    scope_required: Mapped[bool] = mapped_column(Boolean, default=False)


class UserRole(Base):
    """用户-角色关联 user_roles。

    scope_type / scope_values 为**数据范围第二维度**的预留列（3.2 节）：
    本阶段恒为 all 且接口层不读取——但列已在库里，未来按楼栋/租户
    授权时只改权限中间件的过滤逻辑，不改表。
    """

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), default="all")   # all / buildings / tenants
    scope_values: Mapped[list] = mapped_column(JSON, default=list)       # 如 ["B1","B2"]


class RolePermission(Base):
    """角色-权限关联 role_permissions。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class AuditLog(Base):
    """系统操作日志 audit_logs（P0）。

    detail 白名单化：只存操作摘要（用户名/角色/对象编号/版本号），
    绝不含密码、API Key、提示词全文等敏感信息。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_time", "created_at", "action"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))    # login/logout/user_create/...
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(16), default="success")   # success / failure
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    business_ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KnowledgeDocument(Base):
    """知识库文档主档 knowledge_documents（阶段5第二批）。

    status 状态机：
      processing → published（正常）
                ↘ failed（解析/向量化失败，可重新处理）
                ↘ needs_ocr（PDF 无文本层，需 OCR 后重新上传）
      published → deleted（软删：检索内容已移除，行保留供审计）
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(128))                # 展示文件名
    doc_type: Mapped[str] = mapped_column(String(32), default="other")  # 6 类枚举
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    file_path: Mapped[str] = mapped_column(String(256))               # 原始文件相对 backend 的路径
    error_info: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # FK users.id（内建文档为 NULL）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["KnowledgeDocumentVersion"]] = relationship(
        back_populates="document", order_by="KnowledgeDocumentVersion.version_no")


class KnowledgeDocumentVersion(Base):
    """知识库文档版本 knowledge_document_versions（阶段5第二批）。

    每次上传/重处理生成新版本；同一文档版本号递增。
    content_hash 用于幂等：内容未变时向量缓存可直接复用。
    """

    __tablename__ = "knowledge_document_versions"
    __table_args__ = (Index("ix_kdv_doc_version", "document_id", "version_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(Integer,
                                             ForeignKey("knowledge_documents.id"))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(32))             # 文件字节 SHA256[:16]
    status: Mapped[str] = mapped_column(String(16), default="processing")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="versions")


class PromptTemplate(Base):
    """提示词模板主档 prompt_templates（阶段5第三批）。

    code 区分用途（qa / anomaly），current_version_id 指向当前生效版本。
    指针式设计的意义：改提示词只插入新版本 + 移动指针，历史版本永不修改，
    任何一条历史回答都能回答"当时用的是哪版提示词"。
    """

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)   # qa / anomaly
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["PromptTemplateVersion"]] = relationship(
        back_populates="template", order_by="PromptTemplateVersion.version_no")


class PromptTemplateVersion(Base):
    """提示词版本 prompt_template_versions（阶段5第三批）。

    版本只增不改：编辑=插入新版本并移动指针；恢复历史=以旧内容生成**新版本号**。
    """

    __tablename__ = "prompt_template_versions"
    __table_args__ = (Index("ix_ptv_tpl_version", "template_id", "version_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("prompt_templates.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    change_note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)   # FK users.id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    template: Mapped[PromptTemplate] = relationship(back_populates="versions")


class Conversation(Base):
    """会话表 conversations。阶段5第一批：增加 user_id 归属（用户隔离）。"""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conv_user", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)          # conv-<uuid>
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
    # 阶段5第三批：本回答使用的提示词版本（审计可回答"当时用的哪版"）
    prompt_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
