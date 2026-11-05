# -*- coding: utf-8 -*-
"""
提示词模板服务（阶段5第三批）

生效机制（方案 6.2 版本快照式）：
  聊天请求进来 → 取 current_version_id + 内容（30s 内存缓存防抖）
              → 本次请求用它拼 Prompt（生成期间不变）
              → version_id 写入 assistant 消息，审计可追溯

  PUT 保存 = 插入新版本 + 移动指针 + **立即失效缓存** → 下一个请求即用新版。
  在途请求持有旧内容字符串的引用，改库不影响它——这正是"版本快照"的意义。

★ 代码级硬约束（方案 6.3）：渲染时在内容尾部追加 HARD_CONSTRAINT，
  管理员把模板改成"允许编造数据"也删不掉这一行；数值溯源、引用组装、
  依据不足拒答都不读模板，拦截动作与提示词物理分离。
"""

from __future__ import annotations

import logging
import time

from app.core.config import Settings
from app.agent.prompts import (HARD_CONSTRAINT, MAX_TEMPLATE_CHARS,
                                     MIN_TEMPLATE_CHARS)
from app.db import repo
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)

# code → (version_id, 系统提示词全文, 缓存写入时刻)
_cache: dict[str, tuple[int, str, float]] = {}
_TTL_SECONDS = 30.0


def invalidate(code: str | None = None) -> None:
    """失效缓存：改模板后必须调用，保证"下一个请求即用新版"。"""
    if code is None:
        _cache.clear()
    else:
        _cache.pop(code, None)


async def get_system_prompt(code: str) -> tuple[int | None, str]:
    """
    取模板渲染后的系统提示词：(version_id, 内容 + 硬约束)。

    缓存未命中才查库；DB 异常时降级为内置默认内容（version_id=None），
    聊天不至于因为提示词表出问题而整体不可用。
    """
    now = time.monotonic()
    hit = _cache.get(code)
    if hit and now - hit[2] < _TTL_SECONDS:
        return hit[0], hit[1]

    try:
        async with session_factory() as db:
            async with db.begin():
                row = await repo.get_current_prompt(db, code)
    except Exception:  # noqa: BLE001
        logger.exception("读取提示词模板失败，降级为内置默认 content(code=%s)", code)
        row = None

    if row is None:
        from app.agent.prompts import DEFAULT_TEMPLATES
        fallback = DEFAULT_TEMPLATES.get(code, ("", "", ""))[2]
        rendered = fallback + HARD_CONSTRAINT
        return None, rendered

    rendered = (row["content"] or "").strip() + HARD_CONSTRAINT
    _cache[code] = (row["version_id"], rendered, now)
    return row["version_id"], rendered


async def get_current(code: str) -> dict:
    """当前模板详情（管理页用；不走缓存，保证编辑页看到的就是库里最新的）。"""
    async with session_factory() as db:
        async with db.begin():
            row = await repo.get_current_prompt(db, code)
    if row is None:
        raise AppError("PROMPT_NOT_FOUND", f"提示词模板不存在：{code}", 404)
    return {
        "code": row["code"],
        "name": row["name"],
        "version_id": row["version_id"],
        "version_no": row["version_no"],
        "content": row["content"],
        "change_note": row["change_note"],
        "created_by_name": row["created_by_name"] or "系统内置",
        "created_at": row["created_at"],
        "hard_constraint": HARD_CONSTRAINT.strip(),
        "max_chars": MAX_TEMPLATE_CHARS,
    }


async def list_templates() -> list[dict]:
    async with session_factory() as db:
        async with db.begin():
            return await repo.list_prompt_templates(db)


async def list_versions(code: str) -> dict:
    async with session_factory() as db:
        async with db.begin():
            tpl = await repo.get_prompt_template(db, code)
            if tpl is None:
                raise AppError("PROMPT_NOT_FOUND", f"提示词模板不存在：{code}", 404)
            items = await repo.list_prompt_versions(db, tpl.id)
    return {"code": code, "total": len(items), "items": items}


def _validate_content(content: str) -> str:
    """长度与内容校验（中文错误）：太短等于删空模板，太长会挤爆上下文。"""
    text = (content or "").strip()
    if len(text) < MIN_TEMPLATE_CHARS:
        raise AppError("PROMPT_TOO_SHORT",
                       f"模板内容至少 {MIN_TEMPLATE_CHARS} 字，当前 {len(text)} 字", 400)
    if len(text) > MAX_TEMPLATE_CHARS:
        raise AppError("PROMPT_TOO_LONG",
                       f"模板内容最多 {MAX_TEMPLATE_CHARS} 字，当前 {len(text)} 字", 400)
    return text


async def update_current(code: str, content: str, change_note: str,
                         user_id: int | None) -> dict:
    """
    保存编辑：插入新版本 → 移动指针 → 失效缓存 → 写审计。

    不修改任何历史版本行（版本不可变），恢复历史同样是"以旧内容生成新版本"。
    """
    text = _validate_content(content)
    async with session_factory() as db:
        async with db.begin():
            tpl = await repo.get_prompt_template(db, code)
            if tpl is None:
                raise AppError("PROMPT_NOT_FOUND", f"提示词模板不存在：{code}", 404)
            ver = await repo.create_prompt_version(
                db, tpl.id, text, change_note or "管理员编辑", user_id)
            await repo.write_audit_log(
                db, "prompt_update", user_id,
                object_type="prompt_template", object_id=str(tpl.id),
                detail={"code": code, "version_no": ver.version_no,
                        "change_note": (change_note or "管理员编辑")[:120],
                        "chars": len(text)})
            version_no, version_id = ver.version_no, ver.id

    invalidate(code)
    logger.info("提示词已更新：%s → v%s（下一个请求生效）", code, version_no)
    return {"ok": True, "code": code, "version_id": version_id,
            "version_no": version_no,
            "message": f"已保存为 v{version_no}，下一个新提问即生效"}


async def restore_version(code: str, version_id: int,
                          user_id: int | None) -> dict:
    """恢复历史版本：读取旧内容 → 生成**新版本号**（不覆盖、不复用版本号）。"""
    async with session_factory() as db:
        async with db.begin():
            tpl = await repo.get_prompt_template(db, code)
            if tpl is None:
                raise AppError("PROMPT_NOT_FOUND", f"提示词模板不存在：{code}", 404)
            src = await repo.get_prompt_version(db, version_id)
            if src is None or src.template_id != tpl.id:
                raise AppError("PROMPT_VERSION_NOT_FOUND",
                               "该版本不存在或不属于当前模板", 404)
            text = _validate_content(src.content)
            ver = await repo.create_prompt_version(
                db, tpl.id, text, f"恢复自 v{src.version_no}", user_id)
            await repo.write_audit_log(
                db, "prompt_update", user_id,
                object_type="prompt_template", object_id=str(tpl.id),
                detail={"code": code, "version_no": ver.version_no,
                        "restored_from": src.version_no, "via": "restore"})
            version_no = ver.version_no

    invalidate(code)
    logger.info("提示词已恢复：%s 由 v%s 内容生成 v%s", code, src.version_no, version_no)
    return {"ok": True, "code": code, "version_no": version_no,
            "message": f"已恢复为 v{version_no}（内容来自历史版本，版本号继续递增）"}


def append_hard_constraint(content: str, cfg: Settings | None = None) -> str:  # noqa: ARG001
    """外部渲染用（供测试与排障）：内容 + 代码级硬约束。"""
    return (content or "").strip() + HARD_CONSTRAINT
