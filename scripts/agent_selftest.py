# -*- coding: utf-8 -*-
"""
阶段6 三场景自测脚本（可作 CI 验收）

运行：
    cd backend
    ../.venv/Scripts/python scripts/agent_selftest.py

覆盖：
  场景1 正常知识问答 → 不进 Agent 循环，有引用
  场景2 倍率异常分析 → 工具链完整、校验拦截检测、强制人工复核、报告最多 pending_check
  场景3 要求跳过校验 → 门禁物理拒绝（TOOL_PRECONDITION_FAILED），且幂等
  额外：未知工具 / 权限不足 / 任务账本可回查

脚本会临时建一个 tester 账号，结束后删除（任务账本保留作为验收证据）。
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
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))


def parse_sse(text: str) -> dict[str, list[dict]]:
    """把 SSE 文本解析成 {event: [data,...]}。"""
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
    from app.agent.guards import ToolExecutor
    from app.agent.state import TaskState
    from app.agent.tools import AgentContext
    from app.core.config import get_settings
    from app.core.security import hash_password
    from app.db import repo
    from app.db.models import Base
    from app.db.session import engine, session_factory
    from app.main import app

    # ---- 建表 + 临时账号 ----
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory() as db:
        async with db.begin():
            await repo.seed_roles_permissions(db)
            await repo.seed_prompt_templates(db)
            await repo.seed_anomaly_events(db, get_settings())
            if await repo.get_user_by_name(db, "tester") is None:
                await repo.create_user(db, "tester", hash_password("TesterPass123"), "admin")

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login",
                                  json={"username": "tester", "password": "TesterPass123"},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            check("登录成功", r.status_code == 200, f"status={r.status_code}")

            # ================= 场景1：知识问答（直达通道） =================
            r = await client.post("/api/chat",
                                  json={"question": "电表倍率变更后应该检查哪些信息？",
                                        "mode": "knowledge"},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            ev = parse_sse(r.text)
            cites = (ev.get("citations") or [{}])[0].get("items", [])
            check("场景1 直达通道有引用", bool(cites),
                  f"引用 {len(cites)} 条，首条 {cites[0]['source_file'] if cites else '-'}")
            check("场景1 未进入 Agent 循环（无 tool_result 事件）",
                  not ev.get("tool_result"))

            # ================= 场景2：倍率异常分析（Agent 通道） =================
            r = await client.post("/api/agent/analyze",
                                  json={"question": "帮我分析 M-002 为什么用电量突然增加，"
                                                     "并生成异常分析报告",
                                        "anomaly_id": "AN-20261023-0001"},
                                  headers={"X-Requested-With": "XMLHttpRequest"})
            ev = parse_sse(r.text)
            tools = [t["tool"] for t in ev.get("tool_result", [])]
            done = (ev.get("done") or [{}])[0]
            answer = "".join(t.get("delta", "") for t in ev.get("token", []))
            state = done.get("state", {})

            check("场景2 工具链完整（query→config→validate→knowledge→report→review）",
                  {"query_energy_data", "get_meter_config", "validate_meter_data",
                   "search_energy_knowledge", "generate_anomaly_report",
                   "request_human_review"}.issubset(set(tools)), f"调用 {tools}")
            check("场景2 校验未通过时 detect 被门禁跳过",
                  "detect_energy_anomaly" not in tools
                  and any("跳过规则检测" in s.get("text", "")
                          for s in ev.get("status", [])))
            check("场景2 强制人工复核（review_status=pending）",
                  state.get("review_status") == "pending" and state.get("review_required"))
            check("场景2 报告最多 pending_check（无 confirmed）",
                  state.get("report_status") == "pending_check")
            check("场景2 回答不含确定性归因",
                  "已确认" not in answer and ("待核实" in answer or "可能" in answer))
            check("场景2 有知识引用", bool((ev.get("citations") or [{}])[0].get("items")))
            check("场景2 数值溯源可执行", done.get("number_trace") is not None,
                  f"passed={(done.get('number_trace') or {}).get('passed')}")

            task_id = done.get("task_id")
            r2 = await client.get(f"/api/agent/tasks/{task_id}")
            detail = r2.json()
            check("场景2 任务账本可回查（含调用轨迹）",
                  r2.status_code == 200 and len(detail.get("tool_calls", [])) >= 5,
                  f"{len(detail.get('tool_calls', []))} 条轨迹")

    # ================= 场景3：要求跳过校验（门禁物理拒绝） =================
    cfg = get_settings()
    state = TaskState(intent="anomaly_analysis")
    ctx = AgentContext(cfg=cfg, user_id=None)
    ex = ToolExecutor(state, ctx, ["anomaly:read", "chat:use"])

    r1 = await ex.execute("generate_anomaly_report", {"anomaly_id": "AN-20261023-0001"})
    r2 = await ex.execute("detect_energy_anomaly",
                          {"meter_id": "M-002", "start_time": "2026-10-23 00:00",
                           "end_time": "2026-10-23 23:45"})
    check("场景3 无证据生成报告被拒", r1.error_code == "TOOL_PRECONDITION_FAILED",
          r1.payload.get("message", "")[:40])
    check("场景3 未校验直接检测被拒", r2.error_code == "TOOL_PRECONDITION_FAILED",
          r2.payload.get("message", "")[:40])
    r3 = await ex.execute("generate_anomaly_report", {"anomaly_id": "AN-20261023-0001"})
    check("场景3 重复请求行为一致（不被话术绕过）",
          r3.error_code == "TOOL_PRECONDITION_FAILED")
    r4 = await ex.execute("set_anomaly_confirmed", {"anomaly_id": "AN-20261023-0001"})
    check("场景3 幻想工具不存在（UNKNOWN_TOOL）", r4.error_code == "UNKNOWN_TOOL")
    ex2 = ToolExecutor(TaskState(), AgentContext(cfg=cfg), [])
    r5 = await ex2.execute("query_energy_data",
                           {"meter_id": "M-002", "start_time": "2026-10-23 00:00",
                            "end_time": "2026-10-23 01:00"})
    check("场景3 无权限调用被拒（SCOPE_DENIED）", r5.error_code == "SCOPE_DENIED")

    # ---- 清理临时账号（任务账本保留）----
    async with session_factory() as db:
        async with db.begin():
            user = await repo.get_user_by_name(db, "tester")
            if user is not None:
                await db.execute(__import__("sqlalchemy").text(
                    "DELETE FROM user_roles WHERE user_id=:u"), {"u": user.id})
                await db.delete(user)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 62)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    for name, _, detail in failed:
        print(f"  ✗ {name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
