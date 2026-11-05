# -*- coding: utf-8 -*-
"""
阶段7 第二批：报告草稿的人工确认机制（暂停点 / 二次校验 / 幂等 / 风险独立保留）

★ 立场（用户明确强调）：
  「用户确认提交报告」≠「确认异常原因」≠「电气安全主管完成签核」。
  本模块只创建**人工复核请求**（request_human_review），
  不写 review_records（签核结论）、不改 anomaly_events.status 到 confirmed/closed。

流程：
  SSE 流式下发草稿 → 落 agent_report_drafts(status=pending) → Agent 暂停
  → 前端显示「确认提交人工复核 / 取消」
  → 确认：二次校验身份 / 权限 / 报告版本 / 证据有效性 → 创建复核请求
  → 取消：不执行普通提交；但**强制复核风险独立保留**（后端自动建风险复核请求）
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from app.agent.prompts import MAX_TEMPLATE_CHARS
from app.agent.tools import TOOL_IMPLS, AgentContext
from app.core.config import Settings
from app.db import repo
from app.db.session import session_factory
from app.errors import AppError

logger = logging.getLogger(__name__)


def compute_evidence_hash(evidence_ids: list[str]) -> str:
    """证据集指纹：用于确认时判断证据是否已被改动/失效。"""
    joined = "|".join(sorted(evidence_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def detect_force_review(state: Any, evidence: list[dict]) -> tuple[bool, str]:
    """
    判断是否存在"必须人工处理"的风险（版本冲突 / 关键数据缺失 / 电气安全风险等）。

    ★ 该判定读的是工具返回的确定性证据与任务状态机，不读用户或模型的措辞，
      所以用户点"取消"也无法抹掉风险。
    """
    if state.validation_status == "issues_found":
        has_mult = any("倍率" in e.get("excerpt", "") or
                       "multiplier" in e.get("excerpt", "") for e in evidence)
        if has_mult:
            return True, "multiplier_anomaly"
        return True, "version_conflict"
    for e in evidence:
        text = e.get("excerpt", "")
        if "数据缺口" in text or "缺失" in text:
            return True, "data_missing"
        if "安全风险" in text or "作业票" in text:
            return True, "safety_risk"
        if "归属不明" in text:
            return True, "ownership_unclear"
    return False, ""


def build_draft_content(report: dict | None, answer: str, state: Any) -> str:
    """把结构化报告六要素 + 最终解释组装为可展示的草稿正文。"""
    parts = ["# 异常分析报告草稿（未经人工确认）", ""]
    if report:
        for key, items in (report.get("sections") or {}).items():
            if items:
                parts.append(f"## {key}")
                parts.extend(f"- {x}" for x in items)
                parts.append("")
        parts.append(f"**状态**：{report.get('status')}　**报告编号**：{report.get('report_id')}")
        parts.append("")
    parts.append("## AI 分析说明（草稿）")
    parts.append(answer.strip()[:MAX_TEMPLATE_CHARS])
    parts.append("")
    parts.append("> 本报告为 AI 草稿。确认提交仅代表「请求人工复核」，"
                 "不代表异常原因已确认，也不代表电气安全主管完成签核。")
    return "\n".join(parts)


def _request_review(cfg: Settings, draft: Any, reason: str, user_id: int | None,
                    note: str) -> dict:
    """调用阶段6 的 request_human_review 工具（只增不改，创建请求 ≠ 完成审核）。"""
    ctx = AgentContext(cfg=cfg, user_id=user_id)
    for eid in (draft.evidence_ids or []):
        ctx.evidence.append({"id": eid, "source_type": "draft_evidence",
                             "source_file": draft.task_id, "excerpt": "", "values": []})
    return TOOL_IMPLS["request_human_review"](
        ctx, reason=reason, related_anomaly_id=None,
        evidence_ids=list(draft.evidence_ids or [])[:8], note=note)


async def create_draft(cfg: Settings, runner: Any, answer: str,
                       user_id: int | None) -> dict:
    """SSE 流结束后落草稿账本（status=pending），Agent 在此暂停。"""
    evidence_ids = runner.ctx.evidence_ids()
    force, reason = detect_force_review(runner.state, runner.ctx.evidence)
    content = build_draft_content(runner.last_report, answer, runner.state)
    draft_id = f"DRAFT-{datetime.now():%Y%m%d%H%M%S}-{runner.state.task_id[-4:]}"
    evidence_hash = compute_evidence_hash(evidence_ids)

    async with session_factory() as db:
        async with db.begin():
            await repo.create_agent_draft(
                db, draft_id=draft_id, task_id=runner.state.task_id,
                session_id=runner.state.session_id, user_id=user_id,
                content=content, report_version=1, evidence_hash=evidence_hash,
                evidence_ids=evidence_ids, force_review=force,
                force_reason=reason)
            await repo.write_audit_log(
                db, "review_request", user_id,
                object_type="agent_draft", object_id=draft_id,
                business_ref=runner.state.task_id,
                detail={"action": "draft_created", "evidence_count": len(evidence_ids),
                        "force_review": force, "force_reason": reason})
    return {
        "draft_id": draft_id,
        "task_id": runner.state.task_id,
        "report_version": 1,
        "evidence_hash": evidence_hash,
        "evidence_count": len(evidence_ids),
        "force_review": force,
        "force_reason": reason,
        "status": "pending",
    }


async def _load_and_guard(draft_id: str, user_id: int | None,
                          permissions: list[str]) -> Any:
    """二次校验：身份 + 权限 + 草稿存在 + 状态为 pending。"""
    async with session_factory() as db:
        async with db.begin():
            draft = await repo.get_agent_draft(db, draft_id)
    if draft is None:
        raise AppError("DRAFT_NOT_FOUND", "报告草稿不存在", 404)
    if draft.user_id is not None and draft.user_id != user_id:
        # 按"不存在"处理，不泄露他人草稿
        raise AppError("DRAFT_NOT_FOUND", "报告草稿不存在", 404)
    if "anomaly:read" not in set(permissions):
        raise AppError("FORBIDDEN", "当前角色无权提交人工复核", 403)
    if draft.status != "pending":
        # 幂等：重复确认/取消直接返回既有结果，不重复建单
        return None
    return draft


async def confirm_draft(cfg: Settings, draft_id: str, user_id: int | None,
                        permissions: list[str], idempotency_key: str,
                        report_version: int) -> dict:
    """
    用户确认 → 二次校验 → 创建复核请求。

    校验四件事：身份（上一步）、权限、**报告版本**、**证据有效性**（hash 与任务账本一致）。
    idempotency_key 唯一：重复调用直接返回第一次的结果，绝不重复创建复核请求。
    """
    # 幂等键：同一 key 已处理过 → 直接返回既有结果
    async with session_factory() as db:
        async with db.begin():
            existing = await repo.get_draft_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return {
            "ok": True, "idempotent": True, "draft_id": existing.draft_id,
            "status": existing.status,
            "review_request_id": existing.review_request_id,
            "message": "该确认已处理，未重复创建复核请求",
        }

    draft = await _load_and_guard(draft_id, user_id, permissions)
    if draft is None:
        async with session_factory() as db:
            async with db.begin():
                cur = await repo.get_agent_draft(db, draft_id)
        return {"ok": True, "idempotent": True, "draft_id": draft_id,
                "status": cur.status, "review_request_id": cur.review_request_id,
                "message": "草稿已处理，未重复创建复核请求"}

    if report_version != draft.report_version:
        raise AppError("DRAFT_VERSION_MISMATCH",
                       f"报告版本已变化（当前 v{draft.report_version}），请刷新后重新确认", 409)

    # 证据有效性：任务账本里的证据集必须与草稿生成时一致
    async with session_factory() as db:
        async with db.begin():
            task = await repo.get_agent_task_detail(db, draft.task_id)
    live_ids = [e.get("id") for e in (task or {}).get("evidence_refs", []) if e.get("id")]
    if not live_ids or compute_evidence_hash(live_ids) != draft.evidence_hash:
        raise AppError("EVIDENCE_INVALID",
                       "证据集已变化或失效，无法提交，请重新发起分析", 409)

    reason = draft.force_reason or "final_confirmation"
    result = _request_review(cfg, draft, reason, user_id,
                             "用户确认提交报告草稿，请求人工复核")

    async with session_factory() as db:
        async with db.begin():
            updated = await repo.confirm_agent_draft_cas(
                db, draft_id, idempotency_key, result["review_request_id"])
            if not updated:      # 并发下另一个请求抢先确认
                cur = await repo.get_agent_draft(db, draft_id)
                return {"ok": True, "idempotent": True, "draft_id": draft_id,
                        "status": cur.status,
                        "review_request_id": cur.review_request_id,
                        "message": "并发确认已由先到请求处理，未重复建单"}
            await repo.write_audit_log(
                db, "review_request", user_id,
                object_type="agent_draft", object_id=draft_id,
                business_ref=draft.task_id,
                detail={"action": "confirmed", "reason": reason,
                        "review_request_id": result["review_request_id"],
                        "evidence_count": len(live_ids)})

    return {
        "ok": True, "idempotent": False, "draft_id": draft_id,
        "status": "confirmed",
        "review_request_id": result["review_request_id"],
        "review_status": result["status"],
        "suggested_role": result["suggested_role"],
        "message": ("已提交人工复核（本次为 AI 草稿，不代表异常原因已确认，"
                    "也不代表电气安全主管完成签核）"),
    }


async def cancel_draft(cfg: Settings, draft_id: str, user_id: int | None,
                       permissions: list[str], idempotency_key: str) -> dict:
    """
    用户取消 → 不执行普通报告提交。

    ★ 但存在强制复核风险时，风险记录**独立保留**：后端自动创建风险类复核请求，
      并把 review_request_id 挂在草稿上（标记为 risk_retained），用户取消不影响它。
    """
    async with session_factory() as db:
        async with db.begin():
            existing = await repo.get_draft_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return {"ok": True, "idempotent": True, "draft_id": existing.draft_id,
                "status": existing.status, "message": "该取消已处理"}

    draft = await _load_and_guard(draft_id, user_id, permissions)
    if draft is None:
        return {"ok": True, "idempotent": True, "draft_id": draft_id,
                "message": "草稿已处理，未重复操作"}

    risk_request_id = None
    if draft.force_review:
        reason = draft.force_reason or "final_confirmation"
        result = _request_review(cfg, draft, reason, user_id,
                                 "用户取消提交，但强制复核风险独立保留")
        risk_request_id = result["review_request_id"]

    async with session_factory() as db:
        async with db.begin():
            updated = await repo.cancel_agent_draft_cas(db, draft_id, idempotency_key)
            if updated and risk_request_id:
                await repo.set_draft_risk_request(db, draft_id, risk_request_id)
            await repo.write_audit_log(
                db, "review_request", user_id,
                object_type="agent_draft", object_id=draft_id,
                business_ref=draft.task_id,
                detail={"action": "cancelled", "force_review": bool(draft.force_review),
                        "risk_request_id": risk_request_id})

    return {
        "ok": True, "idempotent": False, "draft_id": draft_id,
        "status": "cancelled",
        "report_submitted": False,
        "risk_retained": bool(risk_request_id),
        "risk_request_id": risk_request_id,
        "message": ("已取消提交，未执行普通报告提交操作"
                    + ("；但检测到强制复核风险，已独立保留风险复核请求" if risk_request_id else "")),
    }
