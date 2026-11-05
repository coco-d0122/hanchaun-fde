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

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
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
    # 阶段5第四批：anomaly 模式回答关联的事件号（审计详情"关联异常"用）
    anomaly_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
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


# ==============================================================================
# 阶段5第四批：对话审计与异常复核（2 张表 + messages.anomaly_id）
# ==============================================================================

class ConversationAuditFlag(Base):
    """对话风险标记 conversation_audit_flags。

    7 类 flag_type 对应需求（缺证据/编造规则/推测当结论/建议改数/建议绕复核/
    建议控设备/与规则冲突）。本阶段唯一来源是人工标记；source 预留给 P2 自动规则。
    标记本身也是写操作 → 同时写 audit_logs（action=flag_update）。
    """

    __tablename__ = "conversation_audit_flags"
    __table_args__ = (Index("ix_caf_message", "message_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    flag_type: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/processed/dismissed
    source: Mapped[str] = mapped_column(String(8), default="manual")    # manual / auto（P2 预留）
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReviewRecord(Base):
    """人工复核记录 review_records（本阶段只读）。

    ★ 表结构即为最终结构，但**写入接口不注册**：
      `anomaly:review:submit` 签核权首期不授予任何角色（含 admin），
      "系统管理员 ≠ 电气安全主管"。页面上的提交按钮渲染为禁用态。
    """

    __tablename__ = "review_records"
    __table_args__ = (Index("ix_review_anomaly", "anomaly_id", "reviewed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    anomaly_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("anomaly_events.id", ondelete="CASCADE"), index=True)
    review_type: Mapped[str] = mapped_column(String(32), default="final")  # final / spot_check
    conclusion: Mapped[str] = mapped_column(Text, default="")             # 复核结论（业务填写）
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)        # 引用的证据定位键
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="submitted")


# ==============================================================================
# 阶段6：Agent 任务账本与工具调用明细（模型无写权，只由执行器写入）
# ==============================================================================

class AgentTask(Base):
    """Agent 任务账本 agent_tasks。

    status: running / completed / budget_exceeded / failed
    状态字段只由工具执行器在"工具成功执行后"写入——模型没有任何写状态的通道
    （它只能发起 tool_call，状态变更逻辑在工具实现内部），这是门禁的依据。
    """

    __tablename__ = "agent_tasks"

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)   # TASK-<uuid12>
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(16), default="anomaly_analysis")
    current_step: Mapped[str] = mapped_column(String(32), default="init")
    data_queried: Mapped[bool] = mapped_column(Boolean, default=False)
    config_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_status: Mapped[str] = mapped_column(String(16), default="not_run")
    anomaly_status: Mapped[str] = mapped_column(String(16), default="not_run")
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    review_status: Mapped[str] = mapped_column(String(16), default="none")
    report_status: Mapped[str] = mapped_column(String(16), default="none")
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    result_digest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentToolCall(Base):
    """工具调用明细 agent_tool_calls（失败可定位：卡在哪一步、哪个工具、什么错误）。"""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (Index("ix_atc_task_seq", "task_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_tasks.task_id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    tool_name: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(512), default="")
    output_digest: Mapped[str] = mapped_column(String(1024), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ==============================================================================
# 阶段7：Harness 执行日志 + 报告草稿确认（人工确认机制）
# ==============================================================================

class AgentExecutionLog(Base):
    """Harness 执行日志 agent_execution_logs。

    一行 = 一次「模型决策」或「工具调用」：
      - kind=decision  ：记录本轮选定了哪些工具（只记决策结果，**不记隐藏推理过程**）
      - kind=tool      ：记录参数、耗时、状态、结果摘要、错误码

    input_parameters 是白名单化后的结构化参数（Pydantic 校验通过才落库），
    不含 API Key、密码、提示词全文，也不含模型内部推理。
    """

    __tablename__ = "agent_execution_logs"
    __table_args__ = (
        Index("ix_ael_task_round", "task_id", "decision_round", "seq"),
        Index("ix_ael_user_time", "user_id", "created_at"),
        Index("ix_ael_tool_time", "tool_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_round: Mapped[int] = mapped_column(Integer, default=1)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="tool")  # decision / tool
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    input_parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    execution_status: Mapped[str] = mapped_column(String(16), default="success")
    # success / failed / timeout / cancelled / rejected / recorded
    result_summary: Mapped[str] = mapped_column(String(1024), default="")
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentReportDraft(Base):
    """报告草稿确认账本 agent_report_drafts（人机协同的"暂停点"）。

    状态机：pending → confirmed / cancelled / expired
    confirmed 时写 review_request_id；idempotency_key 唯一，防重复确认重复建单。
    force_review=true 表示存在强制复核风险（版本冲突/数据缺失/电气安全风险），
    即使用户点"取消"，后端也必须独立保留风险记录。
    """

    __tablename__ = "agent_report_drafts"
    __table_args__ = (Index("ix_ard_task", "task_id", "created_at"),)

    draft_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    report_version: Mapped[int] = mapped_column(Integer, default=1)
    evidence_hash: Mapped[str] = mapped_column(String(64), default="")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    force_review: Mapped[bool] = mapped_column(Boolean, default=False)
    force_reason: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                        unique=True)
    review_request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
