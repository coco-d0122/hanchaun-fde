# -*- coding: utf-8 -*-
"""
阶段7 第三批：三个典型对话流程的完整执行轨迹与断言

运行：../.venv/Scripts/python scripts/harness_scenarios.py

场景A 能源管理知识问答：只调知识检索，不查能耗数据、不检测、不建复核请求
场景B 能耗异常分析与报告提交：并行只读 → 校验 → 检索 → 报告草稿 → 暂停 → 确认 → 复核请求
场景C 电表倍率异常判断：不认定电表损坏、区分三类原因、强制复核、不生成报告
场景D 附加：校验通过时 detect 必须在 validate 之后执行（依赖门禁的正向路径）
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


def print_trajectory(title: str, ev: dict[str, list[dict]], question: str) -> None:
    """把一次真实运行打印成"每轮决策 / 工具 / 返回摘要 / 最终回答"的可读轨迹。"""
    print("\n" + "=" * 78)
    print(f"【{title}】用户提问：{question}")
    print("=" * 78)
    tool_results = ev.get("tool_result", [])
    idx = 0
    for r in ev.get("round", []):
        print(f"  第 {r['round']} 轮模型决策：{', '.join(r['tools'])}")
        for tool in r["tools"]:
            if idx < len(tool_results) and tool_results[idx]["tool"] == tool:
                tr = tool_results[idx]
                mark = "✓" if tr["ok"] else "⛔"
                print(f"     {mark} {tool} → {tr['summary']}"
                      + (f" [{tr['error_code']}]" if tr.get("error_code") else ""))
                idx += 1
    answer = "".join(t.get("delta", "") for t in ev.get("token", []))
    print("  最终回答（节选）：", answer[:260].replace("\n", " / "))
    cites = (ev.get("citations") or [{}])[0].get("items", [])
    print("  引用来源：", ", ".join(c["source_file"] for c in cites[:3]) or "（无）")


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
            if await repo.get_user_by_name(db, "scenario_tester") is None:
                await repo.create_user(db, "scenario_tester",
                                       hash_password("TesterPass123"), "admin")

    H = {"X-Requested-With": "XMLHttpRequest"}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as c:
            await c.post("/api/auth/login",
                         json={"username": "scenario_tester", "password": "TesterPass123"},
                         headers=H)

            async def run(question: str, anomaly_id: str | None = None,
                          confirm: bool = False):
                r = await c.post("/api/agent/analyze",
                                 json={"question": question, "anomaly_id": anomaly_id,
                                       "require_confirmation": True},
                                 headers=H)
                ev = parse_sse(r.text)
                draft = (ev.get("draft_ready") or [{}])[0]
                confirm_result = None
                if confirm and draft.get("draft_id"):
                    cr = await c.post(
                        f"/api/agent/drafts/{draft['draft_id']}/confirm",
                        json={"idempotency_key": f"idem-{draft['draft_id']}",
                              "report_version": draft.get("report_version", 1)},
                        headers=H)
                    confirm_result = cr.json()
                return ev, draft, confirm_result

            # ================= 场景 A =================
            qa = "电表倍率变更后需要检查哪些信息？"
            ev_a, draft_a, _ = await run(qa)
            tools_a = [t["tool"] for t in ev_a.get("tool_result", [])]
            ans_a = "".join(t.get("delta", "") for t in ev_a.get("token", []))
            print_trajectory("场景A 能源管理知识问答", ev_a, qa)
            check("A 只调用 search_energy_knowledge", tools_a == ["search_energy_knowledge"],
                  str(tools_a))
            check("A 不查询能耗数据/不检测/不建复核请求",
                  not any(t in tools_a for t in ("query_energy_data", "get_meter_config",
                                                 "detect_energy_anomaly",
                                                 "generate_anomaly_report",
                                                 "request_human_review")))
            check("A 展示引用来源", bool((ev_a.get("citations") or [{}])[0].get("items")))
            check("A 回答基于知识库文本", "知识库" in ans_a)
            check("A 不生成报告草稿（无暂停点）", not draft_a.get("draft_id"))

            # ================= 场景 B =================
            qb = "帮我分析A栋楼昨天为什么用电量突然增加，并生成异常分析报告"
            ev_b, draft_b, confirmed_b = await run(qb, "AN-20261023-0001", confirm=True)
            tools_b = [t["tool"] for t in ev_b.get("tool_result", [])]
            print_trajectory("场景B 能耗异常分析与报告提交", ev_b, qb)
            check("B 首轮独立只读工具并行",
                  any("并行执行" in s.get("text", "") for s in ev_b.get("status", []))
                  and all(t in tools_b[:3] for t in ("query_energy_data",
                                                     "get_meter_config",
                                                     "get_device_relationship")))
            check("B 检测不跳过校验（校验未通过时 detect 被门禁拦截）",
                  "detect_energy_anomaly" not in tools_b
                  and tools_b.index("validate_meter_data") < tools_b.index("generate_anomaly_report"))
            check("B 生成报告草稿并经 SSE 暂停", bool(draft_b.get("draft_id"))
                  and draft_b.get("status") == "pending"
                  and "request_human_review" not in tools_b)
            check("B 确认前处于暂停态（paused + 双动作）",
                  draft_b.get("paused") is True
                  and len(draft_b.get("actions", [])) == 2)
            check("B 确认后返回复核请求编号",
                  bool((confirmed_b or {}).get("review_request_id")),
                  str((confirmed_b or {}).get("review_request_id")))
            check("B 决策轮次不超过 5",
                  ((ev_b.get("done") or [{}])[0].get("decision_rounds") or 0) <= 5)
            check("B 配置冲突时报告为待核查结论",
                  ((ev_b.get("done") or [{}])[0].get("report_status")) == "pending_check")

            # ================= 场景 C =================
            qc = "METER_001的倍率从10变成100，这是不是电表坏了？"
            ev_c, draft_c, _ = await run(qc)
            tools_c = [t["tool"] for t in ev_c.get("tool_result", [])]
            ans_c = "".join(t.get("delta", "") for t in ev_c.get("token", []))
            print_trajectory("场景C 电表倍率异常判断", ev_c, qc)
            check("C 不生成报告草稿/不调用报告工具",
                  "generate_anomaly_report" not in tools_c and not draft_c.get("draft_id"))
            check("C 强制复核条件成立时调用复核工具",
                  "request_human_review" in tools_c)
            check("C 不凭倍率变化认定电表损坏",
                  "不能仅凭倍率变化认定电表损坏" in ans_c
                  and "确认电表损坏" not in ans_c)
            check("C 区分配置错误/计量口径/设备故障三类",
                  all(k in ans_c for k in ("配置", "计量口径", "设备故障")))
            check("C 证据不足明确提示人工核查", "人工核查" in ans_c)

            # ================= 场景 D（附加：正向门禁路径） =================
            qd = "分析 M-001 为什么用电量突然增加，并生成异常分析报告"
            ev_d, _, _ = await run(qd)
            tools_d = [t["tool"] for t in ev_d.get("tool_result", [])]
            print_trajectory("场景D 校验通过时的检测路径", ev_d, qd)
            check("D 校验通过后 detect 在 validate 之后执行",
                  "detect_energy_anomaly" in tools_d
                  and tools_d.index("validate_meter_data")
                      < tools_d.index("detect_energy_anomaly"),
                  " → ".join(tools_d))

            # ================= 前端确认按钮契约 =================
            js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
            html_draft_ready = "draft_ready" in js and "renderDraftCard" in js
            check("前端已实现 draft_ready 处理与确认卡片", html_draft_ready)
            check("确认/取消请求带 CSRF 头与幂等键",
                  "idempotency_key" in js and "X-Requested-With" in js)
            check("draft_ready 提供两个动作端点",
                  len(draft_b.get("actions", [])) == 2
                  and {a["action"] for a in draft_b.get("actions", [])} == {"confirm", "cancel"})

    # 清理临时账号与本次任务/草稿（保留演示数据）
    async with session_factory() as db:
        async with db.begin():
            from sqlalchemy import text
            u = await repo.get_user_by_name(db, "scenario_tester")
            if u:
                await db.execute(text("DELETE FROM user_roles WHERE user_id=:u"), {"u": u.id})
                await db.delete(u)
            await db.execute(text("DELETE FROM agent_report_drafts"))
            await db.execute(text(
                "DELETE FROM agent_execution_logs WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text(
                "DELETE FROM agent_tool_calls WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text(
                "DELETE FROM agent_tasks WHERE task_id<>'TASK-59f75c1da70e'"))
            await db.execute(text(
                "DELETE FROM audit_logs WHERE user_id IS NOT NULL AND user_id NOT IN (1,2)"))
            for (cid,) in (await db.execute(text(
                "SELECT id FROM conversations WHERE id NOT IN "
                "('conv-0170fbbe29d5','conv-8222593d1efc')"))).fetchall():
                await db.execute(text("DELETE FROM message_citations WHERE message_id "
                                      "IN (SELECT id FROM messages WHERE conversation_id=:c)"),
                                 {"c": cid})
                await db.execute(text("DELETE FROM messages WHERE conversation_id=:c"), {"c": cid})
                await db.execute(text("DELETE FROM conversations WHERE id=:c"), {"c": cid})

    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 78)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS)-len(failed)} 项，失败 {len(failed)} 项")
    for n, _, d in failed:
        print(f"  ✗ {n} {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
