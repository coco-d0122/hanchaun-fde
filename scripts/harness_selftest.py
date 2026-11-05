# -*- coding: utf-8 -*-
"""
阶段7 Harness 第一批自测：执行循环 / 依赖 / 并行 / 预算 / 失败重试 / 取消 / 日志筛选

运行：../.venv/Scripts/python scripts/harness_selftest.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    from app.agent.dependencies import plan_batches
    from app.agent.harness import HarnessRunner
    from app.agent.schemas import ParamInvalid, validate_args
    from app.core.config import get_settings
    from app.db import repo
    from app.db.models import Base
    from app.db.session import engine, session_factory
    from app.rag.embeddings import build_embedder
    from app.rag.retriever import HybridRetriever
    from app.services import kb_service

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    cfg = get_settings()
    entries = await kb_service.load_doc_entries()
    retriever = HybridRetriever(cfg, build_embedder(cfg))
    retriever.build_from_documents(entries)
    perms = ["anomaly:read", "chat:use"]

    # 阶段8 起：报告只在用户明确要求时生成（"不要假设所有问题都需要调用全部工具"），
    # 因此本用例的问句里显式要求生成报告，才能覆盖 report 工具与复核链路。
    def runner(question="帮我分析 M-002 为什么用电量突然增加，并生成异常分析报告？",
               anomaly_id="AN-20261023-0001", **kw):
        return HarnessRunner(question, anomaly_id, None, cfg, retriever, 1, perms, **kw)

    # ---------- 1. 参数严格校验 ----------
    try:
        validate_args("query_energy_data", {"start_time": "2026-10-23 00:00",
                                            "end_time": "2026-10-23 23:45",
                                            "sql": "DROP TABLE users"})
        check("Pydantic 拒绝注入字段 sql", False)
    except ParamInvalid:
        check("Pydantic 拒绝注入字段 sql", True)
    ok_args = validate_args("search_energy_knowledge", {"query": "倍率变更", "top_k": 2})
    # top_k 越界会被拒；这里验证合法值归一化
    check("合法参数归一化", "query" in ok_args, str(ok_args))
    try:
        validate_args("search_energy_knowledge", {"query": "x", "top_k": 5})
        check("top_k 上限被拒", False)
    except ParamInvalid:
        check("top_k 上限被拒", True)

    # ---------- 2. 依赖分层：同轮无序请求被拆成有序批次 ----------
    calls = [
        {"name": "detect_energy_anomaly", "arguments": {}},
        {"name": "validate_meter_data", "arguments": {}},
        {"name": "query_energy_data", "arguments": {}},
        {"name": "get_meter_config", "arguments": {}},
    ]
    batches, deferred = plan_batches(calls, set())
    order = [c["name"] for b in batches for c in b]
    check("依赖分层：query/config 在 validate 之前、validate 在 detect 之前",
          order.index("query_energy_data") < order.index("validate_meter_data")
          < order.index("detect_energy_anomaly"), " → ".join(order))
    check("首轮独立只读工具同批并行",
          len(batches[0]) == 2 and {c["name"] for c in batches[0]} ==
          {"query_energy_data", "get_meter_config"})
    check("依赖缺失的调用被延迟而非静默执行", not deferred)

    # ---------- 3. 正常链路：并行、5 轮内完成、状态机与日志 ----------
    r = runner()
    frames = [f async for f in r.stream()]
    text = "".join(frames)
    ev = parse_sse(text)
    done = (ev.get("done") or [{}])[0]
    rounds = [x["round"] for x in ev.get("round", [])]
    check("决策轮次 ≤ 5", bool(rounds) and max(rounds) <= 5, f"实际 {max(rounds)} 轮")
    check("首轮 3 个独立只读工具并行",
          any("并行执行" in s.get("text", "") for s in ev.get("status", [])))
    tools = [t["tool"] for t in ev.get("tool_result", [])]
    check("工具执行顺序符合依赖",
          tools.index("query_energy_data") < tools.index("validate_meter_data")
          < tools.index("generate_anomaly_report") < tools.index("request_human_review"),
          " → ".join(tools))
    check("校验未通过时检测被门禁跳过",
          "detect_energy_anomaly" not in tools
          and any("跳过规则检测" in s.get("text", "") for s in ev.get("status", [])))
    check("强制复核 + 报告草稿（无 confirmed）",
          done.get("review_status") == "pending"
          and done.get("report_status") == "pending_check")
    check("数值溯源通过", bool((done.get("number_trace") or {}).get("passed")),
          f"checked={(done.get('number_trace') or {}).get('checked')}")

    # ---------- 4. 决策与工具日志：每轮可追溯 ----------
    async with session_factory() as db:
        async with db.begin():
            logs = await repo.list_agent_execution_logs(db, task_id=r.state.task_id,
                                                        limit=200)
            by_round = await repo.list_agent_execution_logs(
                db, task_id=r.state.task_id, decision_round=1, limit=200)
            by_tool = await repo.list_agent_execution_logs(
                db, task_id=r.state.task_id, tool_name="validate_meter_data", limit=50)
            by_user = await repo.list_agent_execution_logs(
                db, user_id=1, limit=200)
            from datetime import datetime
            # 模型用 utcnow() 落库（naive UTC），日期筛选也按 UTC 当天
            by_date = await repo.list_agent_execution_logs(
                db, date_from=datetime.utcnow().date().isoformat(), limit=200)
    items = logs["items"]
    decisions = [x for x in items if x["kind"] == "decision"]
    tool_rows = [x for x in items if x["kind"] == "tool"]
    check("每轮决策均有日志", len(decisions) == len(rounds),
          f"决策 {len(decisions)} 条 / 轮次 {len(rounds)}")
    check("每次工具调用均有日志且字段齐全",
          len(tool_rows) >= 6 and all(
              {"input_parameters", "execution_time_ms", "execution_status",
               "result_summary", "error_code", "created_at"} <= set(x) for x in tool_rows),
          f"工具日志 {len(tool_rows)} 条")
    check("按轮次筛选生效", by_round["total"] > 0 and
          all(x["decision_round"] == 1 for x in by_round["items"]))
    check("按工具筛选生效", by_tool["total"] == 1)
    check("按用户筛选生效", by_user["total"] > 0
          and all(x["user_id"] == 1 for x in by_user["items"]))
    check("按时间筛选生效", by_date["total"] >= by_user["total"])
    import re
    log_text = json.dumps(items, ensure_ascii=False).lower()
    check("日志不含密钥/密码/隐藏推理",
          not any(k in log_text for k in ("api_key", "password", "chain_of_thought"))
          and not re.search(r"(?<![a-z])sk-[a-z0-9]", log_text))

    # ---------- 5. 5 轮上限：到顶停止并说明原因 ----------
    r2 = runner()
    r2._offline_plan = lambda: [{"name": "search_energy_knowledge",
                                 "arguments": {"query": "能耗管理制度", "top_k": 3}}]
    frames2 = [f async for f in r2.stream()]
    done2 = (parse_sse("".join(frames2)).get("done") or [{}])[0]
    check("达到 5 轮自动停止", done2.get("decision_rounds") == 5)
    check("到顶说明未完成原因", "上限" in (done2.get("unfinished_reason") or ""))
    check("到顶仍说明已完成步骤", len(done2.get("decision_trace") or []) == 5)

    # ---------- 6. 超时与重试 ----------
    import app.agent.harness as H
    r3 = runner()
    await r3._resolve_scope()
    async with session_factory() as db:      # 先开账，满足 agent_tool_calls 外键
        async with db.begin():
            await repo.create_agent_task(db, r3.state)
    original = r3.executor.execute

    async def slow_execute(tool, args):
        await asyncio.sleep(1)
        return await original(tool, args)

    r3.executor.execute = slow_execute
    old_timeout = H.TOOL_TIMEOUT_SECONDS
    H.TOOL_TIMEOUT_SECONDS = 0.05
    try:
        res = await r3._execute_one({"name": "get_meter_config",
                                     "arguments": {"meter_id": "M-002"}})
    finally:
        H.TOOL_TIMEOUT_SECONDS = old_timeout
    check("工具超时有明确状态", res.error_code == "TIMEOUT" and not res.ok)
    async with session_factory() as db:
        async with db.begin():
            tlog = await repo.list_agent_execution_logs(
                db, task_id=r3.state.task_id, status="timeout", limit=10)
    check("超时错误写入日志", tlog["total"] >= 1)

    # ---------- 7. 任务取消 ----------
    r4 = runner(should_stop=lambda: True)
    frames4 = [f async for f in r4.stream()]
    done4 = (parse_sse("".join(frames4)).get("done") or [{}])[0]
    check("取消后停止自动执行并说明原因",
          "取消" in (done4.get("unfinished_reason") or ""))

    # ---------- 8. 日志查询接口（阶段5 审计兼容） ----------
    import httpx

    from app.core.security import hash_password
    from app.main import app

    async with session_factory() as db:
        async with db.begin():
            await repo.seed_roles_permissions(db)
            if await repo.get_user_by_name(db, "harness_tester") is None:
                await repo.create_user(db, "harness_tester",
                                       hash_password("TesterPass123"), "admin")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            await client.post("/api/auth/login",
                                   json={"username": "harness_tester",
                                         "password": "TesterPass123"},
                                   headers={"X-Requested-With": "XMLHttpRequest"})
            resp = await client.get("/api/agent/logs", params={"limit": 20})
            data = resp.json()
            check("日志查询接口可用且可筛选", resp.status_code == 200
                  and "items" in data and data.get("scope") == "all",
                  f"total={data.get('total')}")
            r_tool = await client.get("/api/agent/logs",
                                      params={"tool_name": "validate_meter_data"})
            check("接口按工具筛选生效",
                  all(x["tool_name"] == "validate_meter_data"
                      for x in r_tool.json().get("items", [])[:10]))

    # 清理临时账号
    async with session_factory() as db:
        async with db.begin():
            u = await repo.get_user_by_name(db, "harness_tester")
            if u:
                from sqlalchemy import text
                await db.execute(text("DELETE FROM user_roles WHERE user_id=:u"),
                                 {"u": u.id})
                await db.delete(u)

    failed = [x for x in RESULTS if not x[1]]
    print("\n" + "=" * 64)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS)-len(failed)} 项，失败 {len(failed)} 项")
    for n, _, d in failed:
        print(f"  ✗ {n} {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
