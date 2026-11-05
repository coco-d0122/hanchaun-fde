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

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.permissions import PERMISSIONS, ROLES, ROLE_PERMISSIONS
from app.core.rules import AnomalyEvent, detect_all, load_readings, compute_intervals
from app.db.models import (AnomalyEventRow, AnomalyEvidenceRow, AuditLog, Conversation,
                            KnowledgeDocument, KnowledgeDocumentVersion,
                            Message, MessageCitation, Permission, Role, RolePermission,
                            User, UserRole)

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
            "citations": by_message.get(m.id, []),
        }
        for m in msgs
    ]


async def add_message(db: AsyncSession, conversation_id: str, role: str, content: str,
                      mode: str, status: str | None = None) -> Message:
    """插入一条消息；assistant 消息以 status=generating 先行落库（方案 1.2 第 4 条）。"""
    msg = Message(conversation_id=conversation_id, role=role, content=content,
                  mode=mode, status=status)
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
            status="pending_review",
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


async def list_anomaly_events(db: AsyncSession, status: str | None = None) -> list[dict[str, Any]]:
    """异常事件列表（按 detected_at 升序，与阶段3 CLI 输出顺序一致）。"""
    stmt = select(AnomalyEventRow).order_by(AnomalyEventRow.detected_at, AnomalyEventRow.id)
    if status:
        stmt = stmt.where(AnomalyEventRow.status == status)
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
