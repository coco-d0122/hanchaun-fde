# -*- coding: utf-8 -*-
"""
LLM Token 日预算（阶段11，对应自检清单 N-05）

目标：Key 泄漏或模型死循环时，单用户单日费用有硬上限。
实现口径：
  - 每次调用前查 llm_usage 表 (user_id, 今天) 累计值，超预算 → AppError 429
  - 调用完成后把 prompt/completion 的**估算 token** 落一行 llm_usage
  - 估算函数：CJK 字符按 1 token、其他按 4 字符 1 token（对中文业务偏保守，
    真实模型接入后可替换为 API 返回的 usage 字段，行为不变）
预算值来自 cfg.llm_daily_token_budget（0 = 不限）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import LlmUsage
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)

BUDGET_EXCEEDED = AppError(
    "LLM_BUDGET_EXCEEDED",
    "今日 AI 调用额度已用完，请稍后再试或联系管理员。",
    429)


def utc_today() -> str:
    """预算日界用 UTC 日期（与 llm_usage.day 一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    """中文约 1 字 1 token，英文约 4 字符 1 token（保守估算，宁多勿少）。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    other = len(text) - cjk
    return cjk + (other + 3) // 4


async def get_daily_usage(db, user_id: int | None, day: str | None = None) -> int:
    """查询某用户当日已消耗 token 总量（无记录返回 0）。"""
    day = day or utc_today()
    stmt = select(func.coalesce(func.sum(
        LlmUsage.prompt_tokens + LlmUsage.completion_tokens), 0)).where(
        LlmUsage.day == day)
    if user_id is not None:
        stmt = stmt.where(LlmUsage.user_id == user_id)
    return int((await db.execute(stmt)).scalar_one() or 0)


async def enforce_daily_budget(user_id: int | None, cfg) -> int:
    """请求前调用：超预算抛 429 中文提示；返回当前已用量（供日志/测试断言）。"""
    budget = getattr(cfg, "llm_daily_token_budget", 0)
    if budget <= 0:
        return 0
    async with session_factory() as db:
        async with db.begin():
            used = await get_daily_usage(db, user_id)
    if used >= budget:
        logger.warning("LLM 日预算已用完：user_id=%s used=%s budget=%s",
                       user_id, used, budget)
        raise BUDGET_EXCEEDED
    return used


async def record_llm_usage(user_id: int | None, kind: str,
                           prompt_text: str, completion_text: str,
                           prompt_tokens: int | None = None,
                           completion_tokens: int | None = None) -> None:
    """调用后记账。显式传 token 值（真实模型 usage 字段）优先于文本估算。

    记账失败只告警不抛错：预算是护栏，不能反过来阻断业务回答。
    """
    try:
        row = LlmUsage(
            user_id=user_id,
            kind=kind if kind in ("chat", "agent", "probe") else "chat",
            prompt_tokens=prompt_tokens if prompt_tokens is not None
            else estimate_tokens(prompt_text),
            completion_tokens=completion_tokens if completion_tokens is not None
            else estimate_tokens(completion_text),
            day=utc_today(),
        )
        async with session_factory() as db:
            async with db.begin():
                db.add(row)
    except Exception:  # noqa: BLE001
        logger.exception("LLM 用量记账失败（不影响业务）user_id=%s kind=%s", user_id, kind)
