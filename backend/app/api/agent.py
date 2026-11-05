# -*- coding: utf-8 -*-
"""
Agent 分析接口（阶段6）

POST /api/agent/analyze  → SSE 流（事件：task/session/status/tool_result/token/citations/done）
GET  /api/agent/tasks/{id} → 任务账本 + 调用轨迹（排查"卡在哪一步"）

SSE 在阶段4 协议上扩展两类事件：
  status(stage=tool_calling|gate_blocked|fallback)   —— Agent 正在做什么
  tool_result({tool, ok, error_code, summary})        —— 摘要级，不含全量数据
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.harness import HarnessRunner
from app.agent.loop import AgentRunner
from app.api.deps import (get_current_user, get_permission_cache, require_csrf_header,
                          require_permission)
from app.core.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import AppError, kb_not_ready
from app.services.prompt_service import get_system_prompt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    anomaly_id: str | None = Field(None, max_length=32)
    session_id: str | None = Field(None, max_length=36)
    # engine=harness（默认）：阶段7 的 5 轮 Harness；script：阶段6 确定性剧本（回归对照）
    engine: Literal["harness", "script"] = "harness"


def _sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/analyze", dependencies=[Depends(require_csrf_header)])
async def analyze(req: AgentRequest, request: Request,
                  user: Annotated[User, Depends(require_permission("anomaly:read"))]
                  ) -> StreamingResponse:
    """进入 Agent 通道（分析/定位类任务）。知识问答请走 /api/chat。"""
    cfg = get_settings()
    retriever = getattr(request.app.state, "retriever", None)
    if retriever is None or not retriever.chunks:
        raise kb_not_ready()

    # 会话：沿用传入的（须属本人），否则新建——审计链需要 messages 落库
    created_conversation = False
    async with session_factory() as db:
        async with db.begin():
            conv = await repo.get_conversation(db, req.session_id) if req.session_id else None
            if conv is not None and conv.user_id is not None and conv.user_id != user.id:
                raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
            if conv is None:
                conv = await repo.create_conversation(
                    db, f"Agent 分析：{req.question[:24]}", user_id=user.id)
                created_conversation = True
            session_id = conv.id
            await repo.add_message(db, session_id, "user", req.question, "anomaly")

    permissions = sorted({p for r in user.roles
                          for p in get_permission_cache(request).get(r.code, [])})
    runner_cls = HarnessRunner if req.engine == "harness" else AgentRunner
    runner = runner_cls(req.question, req.anomaly_id, session_id, cfg, retriever,
                        user.id, permissions,
                        **({"should_stop": request.is_disconnected}
                           if req.engine == "harness" else {}))
    prompt_version_id, _ = await get_system_prompt("agent")

    async def event_stream():
        yield _sse("session", {"session_id": session_id,
                               "created": created_conversation})
        answer_parts: list[str] = []
        final: dict | None = None
        try:
            async for frame in runner.stream():
                # 顺带收集 token 与其后的 done，用于落库
                if frame.startswith("event: token"):
                    import json as _json
                    body = frame.split("data: ", 1)[1].strip()
                    answer_parts.append(_json.loads(body).get("delta", ""))
                if frame.startswith("event: done"):
                    import json as _json
                    final = _json.loads(frame.split("data: ", 1)[1].strip())
                yield frame
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent 执行失败")
            yield _sse("error", {"code": "AGENT_FAILED",
                                 "message": f"分析任务执行失败：{exc}", "details": None})
            return

        # ---- 落库：assistant 消息 + 引用（证据链与阶段4/5 对齐）----
        content = "".join(answer_parts)
        citations = [{"source_type": e["source_type"], "source_file": e["source_file"],
                      "chunk_id": e["id"], "excerpt": e["excerpt"][:120]}
                     for e in runner.ctx.evidence
                     if e["source_type"] in ("knowledge", "rule_evidence")]
        async with session_factory() as db:
            async with db.begin():
                msg = await repo.add_message(
                    db, session_id, "assistant", content, "anomaly",
                    status="complete", prompt_version_id=prompt_version_id,
                    anomaly_id=req.anomaly_id)
                await repo.save_citations(db, msg.id, cfg.citation_excerpt_len, citations)
                if req.anomaly_id and final:
                    await repo.write_audit_log(
                        db, "agent_analysis", user.id,
                        object_type="anomaly", object_id=req.anomaly_id,
                        business_ref=final.get("task_id", ""),
                        detail={"task_id": final.get("task_id"),
                                "tools": final.get("tools", []),
                                "review_status": final.get("review_status"),
                                "validation_status": final.get("validation_status")})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    limit: int = 50,
) -> dict:
    """Agent 任务列表：有审计权限看全库，否则只看本人任务。"""
    can_audit = "audit:conversation:read" in {
        p for r in user.roles for p in get_permission_cache(request).get(r.code, [])}
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_agent_tasks(
                db, user_id=None if can_audit else user.id, limit=limit)
    return {"total": len(items), "items": items, "scope": "all" if can_audit else "own"}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str = Path(min_length=6, max_length=32),
                   user: Annotated[User, Depends(get_current_user)] = None) -> dict:  # noqa: RUF013
    """任务账本 + 调用轨迹。本人任务可查；他人任务需审计权限。"""
    async with session_factory() as db:
        async with db.begin():
            detail = await repo.get_agent_task_detail(db, task_id)
    if detail is None:
        raise AppError("TASK_NOT_FOUND", "Agent 任务不存在", 404)

    if detail["user_id"] not in (None, user.id):
        # 他人任务：仅管理员可查（与审计页的可见范围一致）
        if "admin" not in {r.code for r in user.roles}:
            raise AppError("FORBIDDEN", "当前角色无权查看该任务", 403)
    return detail


@router.get("/logs")
async def list_execution_logs(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    user_id: int | None = Query(None, ge=1),
    task_id: str | None = Query(None, max_length=32),
    tool_name: str | None = Query(None, max_length=64),
    decision_round: int | None = Query(None, ge=1, le=5),
    status: str | None = Query(None, max_length=16),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Harness 执行日志：按用户/时间/任务/工具/轮次/状态组合筛选（阶段5 审计兼容）。"""
    can_audit = "audit:conversation:read" in {
        p for r in user.roles for p in get_permission_cache(request).get(r.code, [])}
    uid = user_id if can_audit else user.id
    async with session_factory() as db:
        async with db.begin():
            data = await repo.list_agent_execution_logs(
                db, user_id=uid, task_id=task_id, tool_name=tool_name,
                decision_round=decision_round, status=status,
                date_from=date_from, date_to=date_to, limit=limit, offset=offset)
    data["scope"] = "all" if can_audit else "own"
    return data
