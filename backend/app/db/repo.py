# -*- coding: utf-8 -*-
"""
仓储层（阶段4）：会话 / 消息 / 引用 / 异常事件的全部数据库读写。

约定：
  - 全部为 async 函数，接收 AsyncSession，由调用方管理事务边界
  - 引用只存定位键 + 120 字摘录，不重复存知识库全文（方案 5.1 存储策略）
  - 异常事件初始化：启动时若表为空则跑阶段3检测链落库；确定性事件 ID 保证幂等
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.permissions import PERMISSIONS, ROLES, ROLE_PERMISSIONS
from app.core.rules import AnomalyEvent, detect_all, load_readings, compute_intervals
from app.db.models import (AnomalyEventRow, AnomalyEvidenceRow, AuditLog, Conversation,
                            ConversationAuditFlag, KnowledgeDocument,
                            KnowledgeDocumentVersion, Message, MessageCitation, Permission,
                            PromptTemplate, PromptTemplateVersion, ReviewRecord, Role,
                            RolePermission, User, UserRole)

logger = logging.getLogger(__name__)


# ============================== 身份与权限（阶段5第一批） ==============================

async def seed_roles_permissions(db: AsyncSession) -> None:
    """把 app.core.permissions 的定义幂等同步进三张表（启动时调用）。"""
    perm_map: dict[str, Permission] = {}
    for code, (desc, scope_required) in PERMISSIONS.items():
        obj = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one_or_none()
        if obj is None:
            obj = Permission(code=code, description=desc, scope_required=scope_required)
            db.add(obj)
            await db.flush()
        else:  # 描述变更时更新（幂等）
            obj.description, obj.scope_required = desc, scope_required
        perm_map[code] = obj

    for code, (name, desc) in ROLES.items():
        role = (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
        if role is None:
            role = Role(code=code, name=name, description=desc)
            db.add(role)
            await db.flush()
        else:
            role.name, role.description = name, desc
        # 同步角色-权限关联（幂等：按当前 ROLE_PERMISSIONS 对齐）
        current = (await db.execute(
            select(RolePermission).where(RolePermission.role_id == role.id))).scalars().all()
        want_ids = {perm_map[c].id for c in ROLE_PERMISSIONS[code]}
        have_ids = {rp.permission_id for rp in current}
        for pid in want_ids - have_ids:
            db.add(RolePermission(role_id=role.id, permission_id=pid))
        for rp in current:
            if rp.permission_id not in want_ids:
                await db.delete(rp)
    await db.flush()


async def get_user_by_name(db: AsyncSession, username: str) -> User | None:
    return (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_user_role_codes(db: AsyncSession, user: User) -> list[str]:
    """用户的角色码列表（user_roles + roles join）。"""
    stmt = select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(
        UserRole.user_id == user.id)
    return [r for (r,) in (await db.execute(stmt)).all()]


async def list_role_permissions(db: AsyncSession) -> dict[str, list[str]]:
    """全部角色 → 权限码集合（启动缓存用；角色变更时重新加载）。"""
    stmt = (select(Role.code, Permission.code)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id))
    mapping: dict[str, list[str]] = {}
    for role_code, perm_code in (await db.execute(stmt)).all():
        mapping.setdefault(role_code, []).append(perm_code)
    return mapping


async def create_user(db: AsyncSession, username: str, password_hash: str,
                      role_code: str, display_name: str = "") -> User:
    """创建用户并绑定角色。"""
    user = User(username=username, password_hash=password_hash,
                display_name=display_name or username)
    db.add(user)
    await db.flush()
    role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


async def list_users(db: AsyncSession) -> list[dict[str, Any]]:
    """用户列表（含角色，不含任何密码信息）。"""
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    role_stmt = (select(UserRole.user_id, Role.code, Role.name)
                 .join(Role, Role.id == UserRole.role_id))
    role_map: dict[int, list[dict[str, str]]] = {}
    for uid, code, name in (await db.execute(role_stmt)).all():
        role_map.setdefault(uid, []).append({"code": code, "name": name})
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "is_active": u.is_active,
            "roles": role_map.get(u.id, []),
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


async def set_user_role(db: AsyncSession, user_id: int, role_code: str) -> bool:
    """替换用户角色（单角色模型，多角色表结构天然支持未来扩展）。"""
    user = await get_user(db, user_id)
    if user is None:
        return False
    role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one_or_none()
    if role is None:
        return False
    old = (await db.execute(select(UserRole).where(UserRole.user_id == user_id))).scalars().all()
    for ur in old:
        await db.delete(ur)
    db.add(UserRole(user_id=user_id, role_id=role.id))
    await db.flush()
    return True


async def write_audit_log(db: AsyncSession, action: str, user_id: int | None,
                          object_type: str = "", object_id: str = "",
                          result: str = "success", detail: dict | None = None,
                          business_ref: str = "") -> None:
    """
    写操作日志。detail 白名单化：只放操作摘要，调用方不得传入密码/Key/全文。
    独立短事务由调用方保证；写日志失败不阻断业务（捕获并记 logger）。
    """
    try:
        db.add(AuditLog(
            user_id=user_id, action=action,
            object_type=object_type, object_id=str(object_id)[:64],
            result=result, detail=detail or {}, business_ref=business_ref[:64]))
        await db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("写操作日志失败 action=%s", action)


# ============================== 会话与消息 ==============================

def new_session_id() -> str:
    """生成会话 ID：conv-<uuid4 十六进制>。"""
    return f"conv-{uuid.uuid4().hex[:12]}"


async def get_conversation(db: AsyncSession, session_id: str) -> Conversation | None:
    return (await db.execute(select(Conversation).where(Conversation.id == session_id))).scalar_one_or_none()


async def create_conversation(db: AsyncSession, title: str,
                              user_id: int | None = None) -> Conversation:
    conv = Conversation(id=new_session_id(), title=(title or "新会话")[:64], user_id=user_id)
    db.add(conv)
    await db.flush()
    return conv


async def list_conversations(db: AsyncSession, user_id: int | None = None) -> list[dict[str, Any]]:
    """会话列表（按 updated_at 倒序），含消息数与最近一次模式。

    阶段5：传入 user_id 时只返回该用户的会话（普通用户隔离）；
    user_id=None（如内部调用）不过滤。
    """
    stmt = (
        select(
            Conversation.id,
            Conversation.title,
            Conversation.created_at,
            Conversation.updated_at,
            func.count(Message.id).label("message_count"),
            func.max(Message.mode).label("last_mode"),
        )
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "session_id": r.id,
            "title": r.title,
            "mode": r.last_mode or "knowledge",
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "message_count": int(r.message_count or 0),
        }
        for r in rows
    ]


async def get_messages(db: AsyncSession, conversation_id: str) -> list[dict[str, Any]]:
    """指定会话的全部消息（含引用），供前端重新加载历史聊天记录。"""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    )
    msgs = (await db.execute(stmt)).scalars().all()

    if not msgs:
        return []

    msg_ids = [m.id for m in msgs]
    citas = (
        await db.execute(
            select(MessageCitation).where(MessageCitation.message_id.in_(msg_ids))
        )
    ).scalars().all()
    by_message: dict[int, list[dict[str, Any]]] = {}
    for c in citas:
        by_message.setdefault(c.message_id, []).append({
            "source_type": c.source_type,
            "source_file": c.source_file,
            "chunk_id": c.chunk_id,
            "excerpt": c.excerpt,
        })

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "mode": m.mode,
            "status": m.status,                       # 用户消息恒为 None
            "created_at": m.created_at.isoformat(),
            "guardrail_passed": m.guardrail_passed,
            "prompt_version_id": m.prompt_version_id,  # 阶段5第三批：回答用的提示词版本
            "citations": by_message.get(m.id, []),
        }
        for m in msgs
    ]


async def add_message(db: AsyncSession, conversation_id: str, role: str, content: str,
                      mode: str, status: str | None = None,
                      prompt_version_id: int | None = None,
                      anomaly_id: str | None = None) -> Message:
    """插入一条消息；assistant 消息以 status=generating 先行落库（方案 1.2 第 4 条）。

    prompt_version_id：本回答使用的提示词版本（阶段5第三批，审计可追溯）。
    anomaly_id：anomaly 模式回答关联的事件号（阶段5第四批，审计详情用）。
    """
    msg = Message(conversation_id=conversation_id, role=role, content=content,
                  mode=mode, status=status, prompt_version_id=prompt_version_id,
                  anomaly_id=anomaly_id)
    db.add(msg)
    await db.flush()
    # 会话 updated_at 是列表排序键，每条消息都刷新
    conv = await get_conversation(db, conversation_id)
    if conv:
        conv.updated_at = datetime.now(conv.created_at.tzinfo) if conv.created_at.tzinfo else datetime.now()
    return msg


async def finish_message(db: AsyncSession, message_id: int, status: str,
                         content: str | None = None,
                         guardrail_passed: bool | None = None,
                         error_info: str | None = None) -> None:
    """消息终态落库：complete / interrupted / failed。绝不无凭据写 complete。"""
    values: dict[str, Any] = {"status": status}
    if content is not None:
        values["content"] = content
    if guardrail_passed is not None:
        values["guardrail_passed"] = guardrail_passed
    if error_info is not None:
        values["error_info"] = error_info
    await db.execute(update(Message).where(Message.id == message_id).values(**values))


async def save_citations(db: AsyncSession, message_id: int, excerpt_len: int,
                         citations: list[dict[str, Any]]) -> None:
    """保存引用行：只存定位键 + 截断摘录。引用必须来自当次检索的真实返回。"""
    for c in citations:
        excerpt = (c.get("excerpt") or "")[:excerpt_len]
        db.add(MessageCitation(
            message_id=message_id,
            source_type=c["source_type"],
            source_file=c["source_file"][:256],
            chunk_id=c["chunk_id"][:128],
            excerpt=excerpt,
        ))
    await db.flush()


async def heal_generating_messages(db: AsyncSession, before: datetime) -> int:
    """
    启动自愈（方案 8.3）：进程崩溃会遗留 status=generating 的悬挂消息，
    启动时统一改标 failed——否则历史里会出现"永远在生成中"的僵尸。
    """
    result = await db.execute(
        update(Message)
        .where(Message.status == "generating", Message.created_at < before)
        .values(status="failed", error_info='{"code": "ORPHANED", "message": "服务重启导致生成中断"}')
    )
    return int(result.rowcount or 0)


async def load_history(db: AsyncSession, conversation_id: str,
                       rounds: int, char_budget: int) -> list[dict[str, str]]:
    """
    读取最近 N 轮历史作为 Prompt 上下文。

    规则（方案 〇 节澄清）：
      - "6 轮"= 6 组（用户+助手）消息对，不是 6 条消息
      - 只取 complete / interrupted 的助手消息（中断消息是真实的部分输出，可作上下文；
        failed / generating 的空壳回答进上下文只会污染）
      - 用户消息恒入选（它没有状态）
      - 总字符预算约束：超出则从最旧开始丢弃
    """
    # 先取最近 rounds*2+2 条原始行，再按"轮"裁剪
    stmt = (
        select(Message.role, Message.content, Message.status)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(rounds * 2 + 4)
    )
    rows = (await db.execute(stmt)).all()
    rows = list(reversed(rows))   # 时间正序

    # 过滤不合格的助手消息
    filtered = [
        {"role": r.role, "content": r.content}
        for r in rows
        if r.role == "user" or (r.role == "assistant" and r.status in ("complete", "interrupted"))
    ]

    # 从尾部按"轮"裁剪：倒序数 rounds 组（一组 = 连续的 user + assistant）
    kept: list[dict[str, str]] = []
    rounds_seen = 0
    for m in reversed(filtered):
        kept.append(m)
        if m["role"] == "assistant":
            rounds_seen += 1
        if rounds_seen >= rounds:
            break
    kept.reverse()

    # 字符预算：从最旧开始丢
    total = sum(len(m["content"]) for m in kept)
    while kept and total > char_budget:
        total -= len(kept[0]["content"])
        kept.pop(0)
    return kept


# ============================== 异常事件 ==============================

async def count_anomaly_events(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(AnomalyEventRow.id)))).scalar_one() or 0)


async def seed_anomaly_events(db: AsyncSession, cfg: Settings) -> int:
    """
    启动时初始化异常事件：对异常数据集跑阶段3检测链并落库。

    正常数据集（0 事件）也跑一遍但结果为空——保持"检测链两侧都验证过"的口径。
    事件 ID 确定性（AN-YYYYMMDD-NNNN），重复启动不重复插入。
    """
    if await count_anomaly_events(db) > 0:
        return 0

    events: list[AnomalyEvent] = []
    for dataset in (cfg.normal_dataset, cfg.anomaly_dataset):
        if not dataset.exists():
            logger.warning("数据集不存在，跳过：%s", dataset)
            continue
        readings = load_readings(dataset)
        intervals = compute_intervals(readings)
        events.extend(detect_all(cfg, intervals))

    for ev in events:
        row = AnomalyEventRow(
            id=ev.event_id,
            meter_id=ev.meter_id,
            building_id=ev.building_id or "",
            anomaly_type=ev.title,
            rule_code=ev.rule_code,
            severity=ev.severity,
            detected_at=ev.window_start,
            window_start=ev.window_start,
            window_end=ev.window_end,
            description=ev.summary,
            rule_result=ev.metrics,
            status="pending_analysis",   # 六态机起点（阶段5第四批）
            # 重建 AnomalyEvent 所需的补充字段
            extra={
                "event_type": ev.event_type,
                "suspects": ev.suspects,
                "evidence": ev.evidence,
                "suggestions": ev.suggestions,
                "need_review": ev.need_review,
            },
        )
        db.add(row)

        # ---- 证据行：指标逐条 + 原始数据快照 ----
        for k, v in ev.metrics.items():
            db.add(AnomalyEvidenceRow(
                anomaly_id=ev.event_id,
                evidence_type="rule_metric",
                evidence_content=f"{k}={v}",
                source_reference=f"rule:{ev.rule_code}",
            ))
        for k, v in ev.evidence.items():
            db.add(AnomalyEvidenceRow(
                anomaly_id=ev.event_id,
                evidence_type="reading" if ev.rule_code == "R04_RATIO_CHANGE" else "balance_calc",
                evidence_content=f"{k}: {v}",
                source_reference=f"energy_readings#{ev.meter_id}",
            ))

    await db.flush()
    logger.info("异常事件初始化完成：%s 条", len(events))
    return len(events)


async def list_anomaly_events(db: AsyncSession, status: str | None = None,
                              meter_id: str | None = None,
                              building_id: str | None = None,
                              anomaly_type: str | None = None,
                              date_from: str | None = None,
                              date_to: str | None = None) -> list[dict[str, Any]]:
    """异常事件列表（按 detected_at 升序，与阶段3 CLI 输出顺序一致）。

    阶段5第四批增加筛选：电表 / 楼栋 / 类型 / 时间范围（审计页用）。
    """
    stmt = select(AnomalyEventRow).order_by(AnomalyEventRow.detected_at, AnomalyEventRow.id)
    if status:
        stmt = stmt.where(AnomalyEventRow.status == status)
    if meter_id:
        stmt = stmt.where(AnomalyEventRow.meter_id == meter_id)
    if building_id:
        stmt = stmt.where(AnomalyEventRow.building_id == building_id)
    if anomaly_type:
        stmt = stmt.where(AnomalyEventRow.anomaly_type == anomaly_type)
    if date_from:
        stmt = stmt.where(AnomalyEventRow.detected_at >= date_from)
    if date_to:
        stmt = stmt.where(AnomalyEventRow.detected_at
                          <= (date_to + " 23:59:59" if len(date_to) == 10 else date_to))
    rows = (await db.execute(stmt)).scalars().all()

    counts_stmt = select(
        AnomalyEvidenceRow.anomaly_id, func.count(AnomalyEvidenceRow.id)
    ).group_by(AnomalyEvidenceRow.anomaly_id)
    counts = dict((await db.execute(counts_stmt)).all())

    return [
        {
            "anomaly_id": r.id,
            "meter_id": r.meter_id,
            "building_id": r.building_id,
            "anomaly_type": r.anomaly_type,
            "rule_code": r.rule_code,
            "severity": r.severity,
            "detected_at": r.detected_at.isoformat(),
            "status": r.status,
            "evidence_count": int(counts.get(r.id, 0)),
            "summary": r.description,
        }
        for r in rows
    ]


async def get_anomaly_event(db: AsyncSession, anomaly_id: str) -> AnomalyEventRow | None:
    return (await db.execute(
        select(AnomalyEventRow).where(AnomalyEventRow.id == anomaly_id))).scalar_one_or_none()


async def get_anomaly_evidence(db: AsyncSession, anomaly_id: str) -> list[AnomalyEvidenceRow]:
    stmt = (select(AnomalyEvidenceRow)
            .where(AnomalyEvidenceRow.anomaly_id == anomaly_id)
            .order_by(AnomalyEvidenceRow.id))
    return list((await db.execute(stmt)).scalars().all())


def row_to_event(row: AnomalyEventRow) -> AnomalyEvent:
    """把库表行重建为 AnomalyEvent 对象（AI 分析 Prompt 构建用）。"""
    extra = row.extra or {}
    return AnomalyEvent(
        event_id=row.id,
        rule_code=row.rule_code,
        event_type=extra.get("event_type", "METERING"),
        severity=row.severity,
        title=row.anomaly_type,
        building_id=row.building_id or "",
        meter_id=row.meter_id,
        window_start=row.window_start,
        window_end=row.window_end,
        summary=row.description,
        metrics=row.rule_result or {},
        suspects=extra.get("suspects", []),
        evidence=extra.get("evidence", {}),
        suggestions=extra.get("suggestions", []),
        need_review=bool(extra.get("need_review", True)),
    )


# ============================== 知识库管理（阶段5第二批） ==============================

async def count_kb_documents(db: AsyncSession) -> int:
    """知识库文档总数（判断种子是否已执行）。"""
    return (await db.execute(select(func.count(KnowledgeDocument.id)))).scalar_one()


async def get_kb_document(db: AsyncSession, doc_id: int) -> KnowledgeDocument | None:
    return (await db.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))).scalar_one_or_none()


async def list_kb_documents(db: AsyncSession,
                            include_deleted: bool = False) -> list[dict[str, Any]]:
    """文档管理列表（按创建时间倒序），含版本号、切片数、上传人。"""
    ver = KnowledgeDocumentVersion
    user = User
    stmt = (
        select(
            KnowledgeDocument.id,
            KnowledgeDocument.filename,
            KnowledgeDocument.doc_type,
            KnowledgeDocument.status,
            KnowledgeDocument.error_info,
            KnowledgeDocument.created_at,
            KnowledgeDocument.updated_at,
            KnowledgeDocument.file_path,
            ver.version_no,
            ver.chunk_count,
            ver.content_hash,
            user.username.label("uploaded_by_name"),
        )
        .outerjoin(ver, ver.id == KnowledgeDocument.current_version_id)
        .outerjoin(user, user.id == KnowledgeDocument.uploaded_by)
        .order_by(KnowledgeDocument.id.desc())
    )
    if not include_deleted:
        stmt = stmt.where(KnowledgeDocument.status != "deleted")
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


async def list_published_kb_documents(db: AsyncSession) -> list[dict[str, Any]]:
    """已发布文档清单（检索构建输入：id / filename / file_path / content_hash）。"""
    return await list_kb_documents_for_build(db, [])


async def list_kb_documents_for_build(
        db: AsyncSession, include_ids: list[int] | None = None,
        exclude_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """
    检索构建用的文档清单 = 已发布文档 + 显式包含 − 显式排除。

    ★ 为什么需要 include_ids：上传/重新处理时目标文档还是 processing 或 failed，
      若只按 status=published 筛选，影子重建会把这份文档排除在外——
      表现为"上传成功但检索不到、切片数为 0"。加上 include_ids 后，
      构建时把它一并算进去，构建成功才真正发布；失败则整体丢弃影子。
    ★ 为什么需要 exclude_ids：删除是"先重建再改状态"无法成立的场景——
      文档此刻仍是 published，重建会把它又带进新索引，删了等于没删。
      用排除集在不改状态的前提下先构建"已剔除"的影子，成功后再落 deleted。
    """
    ver = KnowledgeDocumentVersion
    stmt = (
        select(
            KnowledgeDocument.id,
            KnowledgeDocument.filename,
            KnowledgeDocument.file_path,
            ver.content_hash,
        )
        .outerjoin(ver, ver.id == KnowledgeDocument.current_version_id)
        .order_by(KnowledgeDocument.id)
    )
    if include_ids:
        stmt = stmt.where(
            or_(KnowledgeDocument.status == "published",
                KnowledgeDocument.id.in_(include_ids)))
    else:
        stmt = stmt.where(KnowledgeDocument.status == "published")
    if exclude_ids:
        stmt = stmt.where(KnowledgeDocument.id.not_in(exclude_ids))
    rows = (await db.execute(stmt)).all()
    return [dict(r._mapping) for r in rows]


async def max_kb_version_no(db: AsyncSession, doc_id: int) -> int | None:
    """该文档当前最大版本号（新上传文档返回 None）。"""
    return (await db.execute(
        select(func.max(KnowledgeDocumentVersion.version_no))
        .where(KnowledgeDocumentVersion.document_id == doc_id))).scalar_one_or_none()


async def set_kb_file_path(db: AsyncSession, doc_id: int, rel_path: str) -> None:
    await db.execute(
        update(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        .values(file_path=rel_path))


async def set_kb_status(db: AsyncSession, doc_id: int, status: str,
                        error_info: str = "") -> None:
    """文档状态流转（processing/published/failed/needs_ocr/deleted）。"""
    await db.execute(
        update(KnowledgeDocument).where(KnowledgeDocument.id == doc_id)
        .values(status=status, error_info=error_info))


async def set_kb_version_published(db: AsyncSession, doc_id: int,
                                   content_hash: str, chunk_count: int) -> None:
    """当前版本置 published 并回填切片数与内容哈希。"""
    ver = (
        await db.execute(
            select(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.id == (
                select(KnowledgeDocument.current_version_id)
                .where(KnowledgeDocument.id == doc_id).scalar_subquery())))
    ).scalar_one_or_none()
    if ver is not None:
        ver.status = "published"
        ver.chunk_count = chunk_count
        ver.content_hash = content_hash


async def update_version_chunk_counts(db: AsyncSession,
                                      by_filename: dict[str, int]) -> int:
    """构建后回填切片数（内建种子时记 0 的修正）。返回更新行数。"""
    updated = 0
    rows = (await db.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.filename,
               KnowledgeDocument.current_version_id)
        .where(KnowledgeDocument.status == "published"))).all()
    for row in rows:
        count = by_filename.get(row.filename)
        if count is not None and row.current_version_id is not None:
            await db.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == row.current_version_id)
                .values(chunk_count=count))
            updated += 1
    return updated


async def get_kb_document_detail(db: AsyncSession,
                                 doc_id: int) -> dict[str, Any] | None:
    """文档详情：基础信息 + 版本历史。"""
    doc = await get_kb_document(db, doc_id)
    if doc is None:
        return None
    versions = (await db.execute(
        select(KnowledgeDocumentVersion)
        .where(KnowledgeDocumentVersion.document_id == doc_id)
        .order_by(KnowledgeDocumentVersion.version_no))).scalars().all()
    return {
        "id": doc.id,
        "filename": doc.filename,
        "doc_type": doc.doc_type,
        "status": doc.status,
        "error_info": doc.error_info,
        "file_path": doc.file_path,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "versions": [
            {
                "id": v.id,
                "version_no": v.version_no,
                "chunk_count": v.chunk_count,
                "content_hash": v.content_hash,
                "status": v.status,
                "created_at": v.created_at,
            }
            for v in versions
        ],
    }


# ============================== 提示词模板（阶段5第三批） ==============================

async def seed_prompt_templates(db: AsyncSession) -> int:
    """
    种子写入默认提示词模板（幂等：已存在的 code 不动，绝不覆盖管理员改过的内容）。

    返回新建的模板数。每个新建模板同时生成 v1 版本并置为当前版本。
    """
    from app.core.default_prompts import DEFAULT_TEMPLATES

    created = 0
    for code, (name, desc, content) in DEFAULT_TEMPLATES.items():
        exists = (await db.execute(
            select(PromptTemplate).where(PromptTemplate.code == code))).scalar_one_or_none()
        if exists is not None:
            continue
        tpl = PromptTemplate(code=code, name=name, description=desc)
        db.add(tpl)
        await db.flush()
        ver = PromptTemplateVersion(
            template_id=tpl.id, version_no=1, content=content,
            change_note="初始版本（系统内置）", created_by=None)
        db.add(ver)
        await db.flush()
        tpl.current_version_id = ver.id
        created += 1
    return created


async def list_prompt_templates(db: AsyncSession) -> list[dict[str, Any]]:
    """模板列表（含当前版本号、内容长度、更新时间）。"""
    tpl, ver = PromptTemplate, PromptTemplateVersion
    rows = (await db.execute(
        select(tpl.id, tpl.code, tpl.name, tpl.description, tpl.updated_at,
               ver.version_no, ver.content, ver.created_at.label("version_created_at"))
        .outerjoin(ver, ver.id == tpl.current_version_id)
        .order_by(tpl.id))).all()
    return [
        {
            "id": r.id, "code": r.code, "name": r.name, "description": r.description,
            "version_no": r.version_no, "chars": len(r.content or ""),
            "updated_at": r.updated_at, "version_created_at": r.version_created_at,
        }
        for r in rows
    ]


async def get_prompt_template(db: AsyncSession, code: str) -> PromptTemplate | None:
    return (await db.execute(
        select(PromptTemplate).where(PromptTemplate.code == code))).scalar_one_or_none()


async def get_prompt_template_by_id(db: AsyncSession,
                                    template_id: int) -> PromptTemplate | None:
    return (await db.execute(
        select(PromptTemplate)
        .where(PromptTemplate.id == template_id))).scalar_one_or_none()


async def get_prompt_version(db: AsyncSession,
                             version_id: int) -> PromptTemplateVersion | None:
    return (await db.execute(
        select(PromptTemplateVersion)
        .where(PromptTemplateVersion.id == version_id))).scalar_one_or_none()


async def get_current_prompt(db: AsyncSession, code: str) -> dict[str, Any] | None:
    """当前生效版本（一次联表查询：模板 + 版本内容 + 版本号）。"""
    tpl, ver, user = PromptTemplate, PromptTemplateVersion, User
    row = (await db.execute(
        select(tpl.id.label("template_id"), tpl.code, tpl.name,
               ver.id.label("version_id"), ver.version_no, ver.content,
               ver.change_note, ver.created_at, user.username.label("created_by_name"))
        .join(ver, ver.id == tpl.current_version_id)
        .outerjoin(user, user.id == ver.created_by)
        .where(tpl.code == code))).first()
    return dict(row._mapping) if row else None


async def list_prompt_versions(db: AsyncSession, template_id: int,
                               limit: int = 50) -> list[dict[str, Any]]:
    """版本历史（新版本在前），标记当前生效版本。"""
    ver, user, tpl = PromptTemplateVersion, User, PromptTemplate
    current_id = (await db.execute(
        select(tpl.current_version_id).where(tpl.id == template_id))).scalar_one_or_none()
    rows = (await db.execute(
        select(ver.id, ver.version_no, ver.change_note, ver.created_at,
               ver.content, user.username.label("created_by_name"))
        .outerjoin(user, user.id == ver.created_by)
        .where(ver.template_id == template_id)
        .order_by(ver.version_no.desc())
        .limit(limit))).all()
    return [
        {
            "id": r.id, "version_no": r.version_no, "change_note": r.change_note,
            "created_by_name": r.created_by_name or "系统内置",
            "created_at": r.created_at, "chars": len(r.content or ""),
            "is_current": r.id == current_id,
        }
        for r in rows
    ]


async def create_prompt_version(db: AsyncSession, template_id: int, content: str,
                                change_note: str, created_by: int | None) -> PromptTemplateVersion:
    """插入新版本并移动 current 指针（编辑与恢复共用；版本号 = 现有最大 + 1）。"""
    max_no = (await db.execute(
        select(func.max(PromptTemplateVersion.version_no))
        .where(PromptTemplateVersion.template_id == template_id))).scalar_one_or_none()
    ver = PromptTemplateVersion(
        template_id=template_id, version_no=(max_no or 0) + 1,
        content=content, change_note=change_note[:256], created_by=created_by)
    db.add(ver)
    await db.flush()
    await db.execute(
        update(PromptTemplate).where(PromptTemplate.id == template_id)
        .values(current_version_id=ver.id))
    return ver


# ============================== 对话审计与异常复核（阶段5第四批） ==============================

# 7 类风险标记（需求口径）；API 层用它做白名单校验
FLAG_TYPES: dict[str, str] = {
    "missing_evidence": "缺证据",
    "fabricated_rule": "编造规则",
    "speculation_as_fact": "推测当结论",
    "suggest_modify_data": "建议改数",
    "suggest_skip_review": "建议绕复核",
    "suggest_control_device": "建议控设备",
    "conflict_with_rule": "与规则冲突",
}


async def list_audit_messages(db: AsyncSession, user_id: int | None = None,
                              date_from: str | None = None, date_to: str | None = None,
                              session_id: str | None = None, mode: str | None = None,
                              anomaly_id: str | None = None,
                              has_anomaly: bool | None = None,
                              has_flag: bool | None = None,
                              keyword: str | None = None,
                              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """
    对话审计列表：以 assistant 消息为一行（每条回答即一次审计单元）。

    六条件组合筛选：用户 / 时间范围 / 会话号 / 模式 / 是否涉异常 / 是否有风险标记。
    另加 keyword（命中问题或回答正文），便于按内容追查。
    """
    msg, conv, usr = Message, Conversation, User
    flag_count = (
        select(ConversationAuditFlag.message_id,
               func.count(ConversationAuditFlag.id).label("flag_count"))
        .group_by(ConversationAuditFlag.message_id)
        .subquery())
    ver = PromptTemplateVersion

    stmt = (
        select(msg.id, msg.conversation_id, msg.mode, msg.status, msg.content,
               msg.guardrail_passed, msg.prompt_version_id, msg.anomaly_id, msg.created_at,
               conv.title, usr.username, conv.user_id,
               func.coalesce(flag_count.c.flag_count, 0).label("flag_count"),
               ver.version_no.label("prompt_version_no"))
        .join(conv, conv.id == msg.conversation_id)
        .outerjoin(usr, usr.id == conv.user_id)
        .outerjoin(flag_count, flag_count.c.message_id == msg.id)
        .outerjoin(ver, ver.id == msg.prompt_version_id)
        .where(msg.role == "assistant")
    )
    if user_id:
        stmt = stmt.where(conv.user_id == user_id)
    if session_id:
        stmt = stmt.where(msg.conversation_id == session_id)
    if mode:
        stmt = stmt.where(msg.mode == mode)
    if anomaly_id:
        stmt = stmt.where(msg.anomaly_id == anomaly_id)
    if date_from:
        stmt = stmt.where(msg.created_at >= date_from)
    if date_to:
        # 传日期（YYYY-MM-DD）时按"当日含全天"处理，避免漏掉当天下午的记录
        stmt = stmt.where(msg.created_at <= (date_to + " 23:59:59"
                                            if len(date_to) == 10 else date_to))
    if has_anomaly is True:
        stmt = stmt.where(msg.anomaly_id.is_not(None))
    elif has_anomaly is False:
        stmt = stmt.where(msg.anomaly_id.is_(None))
    if has_flag is True:
        stmt = stmt.where(func.coalesce(flag_count.c.flag_count, 0) > 0)
    elif has_flag is False:
        stmt = stmt.where(func.coalesce(flag_count.c.flag_count, 0) == 0)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(msg.content.like(like), conv.title.like(like)))

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(
        stmt.order_by(msg.id.desc()).limit(limit).offset(offset))).all()

    return {
        "total": int(total),
        "items": [
            {
                "message_id": r.id,
                "session_id": r.conversation_id,
                "session_title": r.title,
                "username": r.username or "（未登录会话）",
                "user_id": r.user_id,
                "mode": r.mode,
                "status": r.status,
                "excerpt": (r.content or "")[:160],
                "guardrail_passed": r.guardrail_passed,
                "prompt_version_id": r.prompt_version_id,
                "prompt_version_no": r.prompt_version_no,
                "anomaly_id": r.anomaly_id,
                "flag_count": int(r.flag_count or 0),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


async def get_audit_message_detail(db: AsyncSession, message_id: int) -> dict[str, Any] | None:
    """消息审计详情：问答原文 + 引用 + 提示词版本 + 生成状态 + 关联异常 + 风险标记。"""
    msg = (await db.execute(
        select(Message).where(Message.id == message_id))).scalar_one_or_none()
    if msg is None:
        return None

    conv = await get_conversation(db, msg.conversation_id)
    usr = await get_user(db, conv.user_id) if conv and conv.user_id else None
    # 同一会话里该回答之前最近一条用户消息 = 这次的问题
    question = (await db.execute(
        select(Message.content)
        .where(Message.conversation_id == msg.conversation_id,
               Message.role == "user", Message.id < msg.id)
        .order_by(Message.id.desc()).limit(1))).scalar_one_or_none()

    citations = (await db.execute(
        select(MessageCitation).where(MessageCitation.message_id == message_id)
        .order_by(MessageCitation.id))).scalars().all()

    prompt_ver = None
    if msg.prompt_version_id:
        prompt_ver = (await db.execute(
            select(PromptTemplateVersion.version_no)
            .where(PromptTemplateVersion.id == msg.prompt_version_id))).scalar_one_or_none()

    anomaly = None
    if msg.anomaly_id:
        row = await get_anomaly_event(db, msg.anomaly_id)
        if row is not None:
            anomaly = {
                "anomaly_id": row.id, "meter_id": row.meter_id,
                "building_id": row.building_id, "anomaly_type": row.anomaly_type,
                "rule_code": row.rule_code, "severity": row.severity,
                "status": row.status, "detected_at": row.detected_at.isoformat(),
                "summary": row.description,
            }

    flags = await list_flags(db, message_id=message_id)

    return {
        "message_id": msg.id,
        "session_id": msg.conversation_id,
        "session_title": conv.title if conv else "",
        "username": (usr.username if usr else "（未登录会话）"),
        "mode": msg.mode,
        "status": msg.status,
        "question": question or "",
        "answer": msg.content or "",
        "error_info": msg.error_info,
        "guardrail_passed": msg.guardrail_passed,
        "prompt_version_id": msg.prompt_version_id,
        "prompt_version_no": prompt_ver,
        "anomaly": anomaly,
        "citations": [
            {"source_type": c.source_type, "source_file": c.source_file,
             "chunk_id": c.chunk_id, "excerpt": c.excerpt}
            for c in citations
        ],
        "flags": flags,
        "created_at": msg.created_at.isoformat(),
    }


async def list_flags(db: AsyncSession, message_id: int | None = None,
                     status: str | None = None,
                     limit: int = 100) -> list[dict[str, Any]]:
    """风险标记列表（可按消息 / 处理状态过滤）。"""
    usr = User
    stmt = (select(ConversationAuditFlag, usr.username)
            .outerjoin(usr, usr.id == ConversationAuditFlag.created_by)
            .order_by(ConversationAuditFlag.id.desc()).limit(limit))
    if message_id is not None:
        stmt = stmt.where(ConversationAuditFlag.message_id == message_id)
    if status:
        stmt = stmt.where(ConversationAuditFlag.status == status)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": f.id, "message_id": f.message_id,
            "flag_type": f.flag_type,
            "flag_type_name": FLAG_TYPES.get(f.flag_type, f.flag_type),
            "note": f.note, "status": f.status, "source": f.source,
            "created_by_name": uname or "系统",
            "created_at": f.created_at.isoformat(),
            "updated_at": f.updated_at.isoformat(),
        }
        for f, uname in rows
    ]


async def create_flag(db: AsyncSession, message_id: int, flag_type: str,
                      note: str, user_id: int | None) -> ConversationAuditFlag:
    flag = ConversationAuditFlag(message_id=message_id, flag_type=flag_type,
                                 note=note[:512], created_by=user_id)
    db.add(flag)
    await db.flush()
    return flag


async def update_flag(db: AsyncSession, flag_id: int, status: str,
                      note: str | None = None) -> bool:
    values: dict[str, Any] = {"status": status}
    if note is not None:
        values["note"] = note[:512]
    result = await db.execute(
        update(ConversationAuditFlag)
        .where(ConversationAuditFlag.id == flag_id).values(**values))
    return bool(result.rowcount)


async def list_review_records(db: AsyncSession, anomaly_id: str) -> list[dict[str, Any]]:
    """人工复核记录（只读）：本阶段正常情况下为空——签核权未授予任何角色。"""
    usr = User
    rows = (await db.execute(
        select(ReviewRecord, usr.username)
        .outerjoin(usr, usr.id == ReviewRecord.reviewed_by)
        .where(ReviewRecord.anomaly_id == anomaly_id)
        .order_by(ReviewRecord.reviewed_at.desc()))).all()
    return [
        {
            "id": r.id, "review_type": r.review_type, "conclusion": r.conclusion,
            "evidence_refs": r.evidence_refs or [], "status": r.status,
            "reviewed_by_name": uname or "系统",
            "reviewed_at": r.reviewed_at.isoformat(),
        }
        for r, uname in rows
    ]


async def mark_anomaly_analyzed(db: AsyncSession, anomaly_id: str) -> bool:
    """
    AI 分析完成后把事件从 pending_analysis 推进到 pending_review（系统内部动作）。

    ★ 只允许这一个方向的状态推进：不做 confirmed/dismissed/closed 的任何写入——
      那是业务签核权的范围，本阶段接口层面不开放。
    """
    result = await db.execute(
        update(AnomalyEventRow)
        .where(AnomalyEventRow.id == anomaly_id,
               AnomalyEventRow.status == "pending_analysis")
        .values(status="pending_review"))
    return bool(result.rowcount)


async def get_flag(db: AsyncSession, flag_id: int) -> ConversationAuditFlag | None:
    return (await db.execute(
        select(ConversationAuditFlag)
        .where(ConversationAuditFlag.id == flag_id))).scalar_one_or_none()


# ============================== 工作台概览（阶段5补全） ==============================

async def dashboard_stats(db: AsyncSession) -> dict[str, Any]:
    """工作台概览卡片 + 最近 10 条操作日志。

    口径说明：对话数与消息数按"当日 00:00 起"统计；待复核异常只统计
    pending_review（复核中是业务签核后才有的态，本阶段不会出现）。
    """
    from datetime import datetime as _dt

    today = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
    users_total = (await db.execute(select(func.count(User.id)))).scalar_one()
    users_active = (await db.execute(
        select(func.count(User.id)).where(User.is_active.is_(True)))).scalar_one()
    docs_published = (await db.execute(
        select(func.count(KnowledgeDocument.id))
        .where(KnowledgeDocument.status == "published"))).scalar_one()
    conv_today = (await db.execute(
        select(func.count(Conversation.id))
        .where(Conversation.created_at >= today))).scalar_one()
    msg_today = (await db.execute(
        select(func.count(Message.id))
        .where(Message.role == "assistant", Message.created_at >= today))).scalar_one()
    pending_review = (await db.execute(
        select(func.count(AnomalyEventRow.id))
        .where(AnomalyEventRow.status == "pending_review"))).scalar_one()
    flags_pending = (await db.execute(
        select(func.count(ConversationAuditFlag.id))
        .where(ConversationAuditFlag.status == "pending"))).scalar_one()

    logs = (await db.execute(
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.id.desc()).limit(10))).all()

    return {
        "users_total": users_total,
        "users_active": users_active,
        "docs_published": docs_published,
        "conversations_today": conv_today,
        "answers_today": msg_today,
        "anomalies_pending_review": pending_review,
        "flags_pending": flags_pending,
        "recent_logs": [
            {
                "id": a.id, "action": a.action, "username": uname or "系统",
                "object_type": a.object_type, "object_id": a.object_id,
                "result": a.result, "detail": a.detail or {},
                "created_at": a.created_at.isoformat(),
            }
            for a, uname in logs
        ],
    }


# ============================== 系统操作日志（阶段5第五批） ==============================

# 8 类 action 枚举（方案 7.3）＋实际代码里出现的写入动作，统一中文名便于页面展示
ACTION_LABELS: dict[str, str] = {
    "login": "登录",
    "logout": "退出",
    "kb_upload": "知识库上传",
    "kb_delete": "知识库删除",
    "kb_reindex": "知识库重建索引",
    "kb_reprocess": "知识库重新处理",
    "prompt_update": "提示词更新",
    "user_create": "创建用户",
    "user_update": "修改用户",
    "user_role_change": "角色变更",
    "flag_update": "风险标记",
    "anomaly_review": "异常复核",
}


async def list_audit_logs(db: AsyncSession, date_from: str | None = None,
                          date_to: str | None = None, action: str | None = None,
                          user_id: int | None = None, result: str | None = None,
                          keyword: str | None = None,
                          limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """操作日志查询：时间范围 / 类型 / 操作人 / 结果 / 关键词（对象号）。"""
    usr = User
    stmt = (select(AuditLog, usr.username).outerjoin(usr, usr.id == AuditLog.user_id))
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= date_from)
    if date_to:
        stmt = stmt.where(AuditLog.created_at
                          <= (date_to + " 23:59:59" if len(date_to) == 10 else date_to))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if result:
        stmt = stmt.where(AuditLog.result == result)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(AuditLog.object_id.like(like),
                              AuditLog.business_ref.like(like)))

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(
        stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset))).all()
    return {
        "total": int(total),
        "items": [
            {
                "id": a.id, "action": a.action,
                "action_name": ACTION_LABELS.get(a.action, a.action),
                "username": uname or "系统", "user_id": a.user_id,
                "object_type": a.object_type, "object_id": a.object_id,
                "result": a.result, "detail": a.detail or {},
                "business_ref": a.business_ref,
                "created_at": a.created_at.isoformat(),
            }
            for a, uname in rows
        ],
    }


async def purge_old_audit_logs(db: AsyncSession, retention_days: int) -> int:
    """按保留天数清理过期日志（默认 90 天，配置项可改）。返回删除条数。"""
    from datetime import datetime as _dt, timedelta as _td

    if retention_days <= 0:
        return 0
    cutoff = _dt.now() - _td(days=retention_days)
    result = await db.execute(
        delete(AuditLog).where(AuditLog.created_at < cutoff))
    return int(result.rowcount or 0)
