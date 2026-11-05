# -*- coding: utf-8 -*-
"""
阶段7 第二批自测：报告草稿 SSE + 人工确认 / 取消 / 幂等 / 二次校验 / 风险独立保留

运行：cd backend && ../.venv/Scripts/python scripts/confirmation_selftest.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))


def parse_sse(text: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    event = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: ") and event:
            try:
                out.setdefault(event, []).append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


async def main() -> int:
    from app.core.security import hash_password
    from app.db import repo
    from app.db.models import Base
    from app.db.session import engine, session_factory
    from app.main import app

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory() as db:
        async with db.begin():
            await repo.seed_roles_permissions(db)
            for name, role in (("draft_admin", "admin"), ("draft_other", "user")):
                if await repo.get_user_by_name(db, name) is None:
                    await repo.create_user(db, name, hash_password("TesterPass123"), role)

    from uuid import uuid4
    H = {"X-Requested-With": "XMLHttpRequest"}
    def newkey(prefix): return f"idem-{prefix}-{uuid4().hex[:10]}"
    QUESTION = "帮我分析A栋楼昨天为什么用电量突然增加，并生成异常分析报告"

    async def analysis(client) -> tuple[dict, str]:
        r = await client.post("/api/agent/analyze",
                              json={"question": QUESTION, "anomaly_id": "AN-20261023-0001",
                                    "require_confirmation": True},
                              headers=H)
        ev = parse_sse(r.text)
        return ev, r.text

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as c:
            lr = await c.post("/api/auth/login",
                              json={"username": "draft_admin", "password": "TesterPass123"},
                              headers=H)
            check("登录成功", lr.status_code == 200)

            # ---- 1. 草稿流式下发 + 暂停 ----
            ev, raw = await analysis(c)
            draft = (ev.get("draft_ready") or [{}])[0]
            tools = [t["tool"] for t in ev.get("tool_result", [])]
            check("SSE 流式下发草稿正文（token 事件）", len(ev.get("token", [])) > 5,
                  f"{len(ev.get('token', []))} 段")
            check("生成报告草稿并暂停（draft_ready）", bool(draft.get("draft_id")))
            check("暂停期间未自动创建复核请求",
                  "request_human_review" not in tools
                  and draft.get("status") == "pending", " → ".join(tools))
            check("草稿标注强制复核风险", bool(draft.get("force_review")),
                  str(draft.get("force_reason")))
            did = draft.get("draft_id")
            ver = draft.get("report_version", 1)

            gd = await c.get(f"/api/agent/drafts/{did}")
            check("草稿账本可查询且为 pending", gd.json().get("status") == "pending")

            # ---- 2. 二次校验：报告版本 / CSRF / 身份 ----
            bad_ver = await c.post(f"/api/agent/drafts/{did}/confirm",
                                   json={"idempotency_key": newkey("version-bad"),
                                         "report_version": ver + 1}, headers=H)
            check("报告版本不一致被拒", bad_ver.status_code == 409,
                  bad_ver.json().get("code", ""))
            no_csrf = await c.post(f"/api/agent/drafts/{did}/confirm",
                                   json={"idempotency_key": newkey("nocsrf"),
                                         "report_version": ver})
            check("缺少 CSRF 头被拒", no_csrf.status_code == 403)

            # 身份校验：换一个账号确认同一草稿 → 按不存在处理
            await c.post("/api/auth/login",
                         json={"username": "draft_other", "password": "TesterPass123"},
                         headers=H)
            other = await c.post(f"/api/agent/drafts/{did}/confirm",
                                 json={"idempotency_key": newkey("other"),
                                       "report_version": ver}, headers=H)
            check("他人确认草稿被拒（身份二次校验）", other.status_code == 404)
            await c.post("/api/auth/login",
                         json={"username": "draft_admin", "password": "TesterPass123"},
                         headers=H)

            # ---- 3. 确认 → 复核请求；幂等防重复 ----
            confirm_key = newkey("confirm")
            ok = await c.post(f"/api/agent/drafts/{did}/confirm",
                              json={"idempotency_key": confirm_key, "report_version": ver},
                              headers=H)
            data = ok.json()
            rid = data.get("review_request_id")
            check("确认后返回复核请求编号", ok.status_code == 200 and bool(rid), str(rid))
            check("确认状态为 confirmed 且提示不等于签核",
                  data.get("status") == "confirmed" and "不代表" in data.get("message", ""))

            again = await c.post(f"/api/agent/drafts/{did}/confirm",
                                 json={"idempotency_key": confirm_key, "report_version": ver},
                                 headers=H)
            check("同一幂等键重复确认不重复建单",
                  again.json().get("idempotent") is True
                  and again.json().get("review_request_id") == rid)
            again2 = await c.post(f"/api/agent/drafts/{did}/confirm",
                                  json={"idempotency_key": newkey("confirm2"),
                                        "report_version": ver}, headers=H)
            check("不同幂等键重复确认也不重复建单",
                  again2.json().get("idempotent") is True
                  and again2.json().get("review_request_id") == rid)

            # ---- 4. 确认 ≠ 签核：异常状态与签核记录不变 ----
            async with session_factory() as db:
                async with db.begin():
                    from sqlalchemy import select, func
                    from app.db.models import AnomalyEventRow, ReviewRecord
                    statuses = (await db.execute(
                        select(AnomalyEventRow.status))).scalars().all()
                    reviews = (await db.execute(
                        select(func.count(ReviewRecord.id)))).scalar_one()
            check("确认未把异常改为 confirmed/closed",
                  all(s in ("pending_analysis", "pending_review") for s in statuses),
                  str(set(statuses)))
            check("确认未写签核结论表 review_records", reviews == 0)

            # ---- 5. 证据有效性校验（篡改后拒绝提交）----
            ev2, _ = await analysis(c)
            d2 = (ev2.get("draft_ready") or [{}])[0]
            did2 = d2.get("draft_id")
            async with session_factory() as db:
                async with db.begin():
                    task = await repo.get_agent_task_detail(db, d2.get("task_id", ""))
                    from sqlalchemy import text
                    await db.execute(text(
                        "UPDATE agent_tasks SET evidence_refs='[]' WHERE task_id=:t"),
                        {"t": task["task_id"]})
            tampered = await c.post(f"/api/agent/drafts/{did2}/confirm",
                                    json={"idempotency_key": newkey("tamper"),
                                          "report_version": d2.get("report_version", 1)},
                                    headers=H)
            check("证据集被改动后确认被拒", tampered.status_code == 409,
                  tampered.json().get("code", ""))

            # ---- 6. 取消：不提交普通报告，但强制复核风险独立保留 ----
            ev3, _ = await analysis(c)
            d3 = (ev3.get("draft_ready") or [{}])[0]
            did3 = d3.get("draft_id")
            cancel = await c.post(f"/api/agent/drafts/{did3}/cancel",
                                  json={"idempotency_key": newkey("cancel")},
                                  headers=H)
            cdata = cancel.json()
            check("取消后未执行普通报告提交", cdata.get("report_submitted") is False
                  and cdata.get("status") == "cancelled")
            check("强制复核风险独立保留（取消也建风险请求）",
                  cdata.get("risk_retained") is True
                  and bool(cdata.get("risk_request_id")),
                  str(cdata.get("risk_request_id")))
            cancel2 = await c.post(f"/api/agent/drafts/{did3}/cancel",
                                   json={"idempotency_key": newkey("cancel")},
                                   headers=H)
            check("重复取消幂等", cancel2.json().get("idempotent") is True)

    # 清理临时账号与草稿/任务（保留原有演示数据）
    async with session_factory() as db:
        async with db.begin():
            from sqlalchemy import text
            for name in ("draft_admin", "draft_other"):
                u = await repo.get_user_by_name(db, name)
                if u:
                    await db.execute(text("DELETE FROM user_roles WHERE user_id=:u"),
                                     {"u": u.id})
                    await db.delete(u)
            await db.execute(text("DELETE FROM agent_report_drafts"))
            await db.execute(text(
                "DELETE FROM agent_execution_logs WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text(
                "DELETE FROM agent_tool_calls WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text(
                "DELETE FROM agent_tasks WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text("DELETE FROM audit_logs WHERE action='review_request'"))
            await db.execute(text(
                "DELETE FROM message_citations WHERE message_id NOT IN (SELECT id FROM messages)"))

    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 64)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS)-len(failed)} 项，失败 {len(failed)} 项")
    for n, _, d in failed:
        print(f"  ✗ {n} {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
