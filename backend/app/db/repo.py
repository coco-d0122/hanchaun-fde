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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.rules import AnomalyEvent, detect_all, load_readings, compute_intervals
from app.db.models import (AnomalyEventRow, AnomalyEvidenceRow, Conversation,
                            Message, MessageCitation)

logger = logging.getLogger(__name__)


# ============================== 会话与消息 ==============================

def new_session_id() -> str:
    """生成会话 ID：conv-<uuid4 十六进制>。"""
    return f"conv-{uuid.uuid4().hex[:12]}"


async def get_conversation(db: AsyncSession, session_id: str) -> Conversation | None:
    return (await db.execute(select(Conversation).where(Conversation.id == session_id))).scalar_one_or_none()


async def create_conversation(db: AsyncSession, title: str) -> Conversation:
    conv = Conversation(id=new_session_id(), title=(title or "新会话")[:64])
    db.add(conv)
    await db.flush()
    return conv


async def list_conversations(db: AsyncSession) -> list[dict[str, Any]]:
    """会话列表（按 updated_at 倒序），含消息数与最近一次模式。"""
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
