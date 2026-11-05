# -*- coding: utf-8 -*-
"""
审计服务（阶段5第四批）：风险标记的写路径 + 审计日志。

为什么单独一层而不是直接写在 API 里：
  标记是"写操作 + 审计留痕 + 存在性校验"三件事，API 层只负责收参与权限；
  后续要做自动标记（P2 的三条规则：无引用却下结论 / guardrail 失败 /
  写"已确认"但事件仍 pending）时，只需要在服务层加一个入口，接口不用动。
"""

from __future__ import annotations

import logging

from app.db import repo
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)


async def create_flag(message_id: int, flag_type: str, note: str,
                      user_id: int | None) -> dict:
    """添加风险标记：校验消息存在 → 插入 → 写 audit_logs（同一事务）。"""
    async with session_factory() as db:
        async with db.begin():
            detail = await repo.get_audit_message_detail(db, message_id)
            if detail is None:
                raise AppError("MESSAGE_NOT_FOUND", "消息不存在或已被删除", 404)
            flag = await repo.create_flag(db, message_id, flag_type, note, user_id)
            await repo.write_audit_log(
                db, "flag_update", user_id,
                object_type="message", object_id=str(message_id),
                detail={"flag_id": flag.id, "flag_type": flag_type,
                        "flag_type_name": repo.FLAG_TYPES.get(flag_type, flag_type),
                        "note": note[:120], "via": "create"})
            flag_id = flag.id

    logger.info("风险标记已添加：message=%s type=%s by=%s", message_id, flag_type, user_id)
    return {
        "ok": True, "flag_id": flag_id,
        "flag_type_name": repo.FLAG_TYPES.get(flag_type, flag_type),
        "message": f"已标记为「{repo.FLAG_TYPES.get(flag_type, flag_type)}」",
    }


async def update_flag(flag_id: int, status: str, note: str | None,
                      user_id: int | None) -> dict:
    """更新标记状态；不存在 → 404。写审计日志便于追溯处理过程。"""
    async with session_factory() as db:
        async with db.begin():
            target = await repo.get_flag(db, flag_id)
            if target is None:
                raise AppError("FLAG_NOT_FOUND", "风险标记不存在", 404)
            prev_status, message_id = target.status, target.message_id
            await repo.update_flag(db, flag_id, status, note)
            await repo.write_audit_log(
                db, "flag_update", user_id,
                object_type="message", object_id=str(message_id),
                detail={"flag_id": flag_id, "from": prev_status,
                        "to": status, "via": "update"})

    return {"ok": True, "flag_id": flag_id, "status": status,
            "message_id": message_id,
            "message": {"pending": "已置为待处理", "processed": "已置为已处理",
                        "dismissed": "已置为已驳回"}.get(status, "状态已更新")}
