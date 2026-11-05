# -*- coding: utf-8 -*-
"""
Agent 执行循环（阶段6）

双通道：
  - 知识/制度类问题 → 直达 RAG（不进循环），与阶段4 一致
  - 分析/定位/排查 → Agent 通道：Skill B 剧本（确定性执行）+ 可选 LLM 自由规划

★ 离线可跑：未配置真实模型时，剧本照常执行（工具链、门禁、状态机、留痕全部真实），
  只有"最后一段解释文字"由确定性摘要生成器写出——这样验收不依赖外部 API。
★ 有模型时：先尝试 Function Calling 循环（最多 12 步），LLM 失败立刻回落到剧本。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, AsyncIterator

from app.agent.executor import ToolExecutor, ToolResult
from app.agent.specs import MAX_TOTAL_CALLS, TOOL_SPECS
from app.agent.state import TaskState
from app.agent.tools import AgentContext, load_dataset
from app.core.config import Settings
from app.core.llm import LLMError, call_llm_stream, call_llm_tools
from app.core.retriever import HybridRetriever
from app.db import repo
from app.db.session import session_factory
from app.services.prompt_service import get_system_prompt

logger = logging.getLogger(__name__)

ANALYSIS_KEYWORDS = ("分析", "为什么", "原因", "排查", "定位", "突增", "异常", "校验", "倍率")


def is_analysis_question(question: str, anomaly_id: str | None) -> bool:
    """轻量规则路由（一次字符串匹配，不花 token）；LLM 分类兜底由自由循环承担。"""
    if anomaly_id:
        return True
    return any(k in (question or "") for k in ANALYSIS_KEYWORDS)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _digest(obj: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return text[:limit]


class AgentRunner:
    """一次分析任务的执行器封装：状态 + 证据 + 事件流 + 落库。"""

    def __init__(self, question: str, anomaly_id: str | None, session_id: str | None,
                 cfg: Settings, retriever: HybridRetriever | None,
                 user_id: int | None, permissions: list[str]):
        self.question = question
        self.anomaly_id = anomaly_id
        self.cfg = cfg
        self.retriever = retriever
        self.user_id = user_id
        self.state = TaskState(session_id=session_id, user_id=user_id,
                               intent="anomaly_analysis")
        self.ctx = AgentContext(cfg=cfg, retriever=retriever, user_id=user_id)
        self.executor = ToolExecutor(self.state, self.ctx, permissions)
        self.seq = 0
        self.tool_events: list[dict] = []
        self.window: tuple[str, str] | None = None
        self.meter_id: str | None = None

    # ---------------- 事件与落库 ----------------
    async def _persist_task(self) -> None:
        async with session_factory() as db:
            async with db.begin():
                await repo.update_agent_task(db, self.state)

    async def _call_tool(self, tool: str, args: dict) -> ToolResult:
        self.seq += 1
        result = await self.executor.execute(tool, args)
        summary = self._summarize(tool, result)
        self.tool_events.append({
            "seq": self.seq, "tool": tool, "ok": result.ok,
            "error_code": result.error_code, "duration_ms": result.duration_ms,
            "summary": summary,
        })
        async with session_factory() as db:
            async with db.begin():
                await repo.add_agent_tool_call(
                    db, self.state.task_id, self.seq, tool,
                    _digest(args, 512), summary, result.ok,
                    result.error_code, result.duration_ms)
                await repo.update_agent_task(db, self.state)
        return result

    @staticmethod
    def _summarize(tool: str, result: ToolResult) -> str:
        if not result.ok:
            return f"拒绝：{result.error_code} — {result.payload.get('message', '')[:120]}"
        p = result.payload
        if tool == "query_energy_data":
            return (f"{len(p.get('meters', []))} 块表，合计 {p.get('total_energy_kwh')} kWh，"
                    f"来源 {p.get('_trace', {}).get('source')}")
        if tool == "get_meter_config":
            cur = p.get("current") or {}
            return (f"当前倍率 {cur.get('multiplier')}，版本 {len(p.get('history', []))} 个，"
                    f"冲突 {len(p.get('conflicts', []))} 项")
        if tool == "validate_meter_data":
            return f"{'通过' if p.get('passed') else '未通过'}，问题 {p.get('summary', {}).get('failed')} 项"
        if tool == "detect_energy_anomaly":
            return f"命中规则事件 {len(p.get('events', []))} 条"
        if tool == "search_energy_knowledge":
            return f"命中 {len(p.get('results', []))} 条，依据{'充足' if p.get('has_evidence') else '不足'}"
        if tool == "generate_anomaly_report":
            return f"报告 {p.get('report_id')}，状态 {p.get('status')}"
        if tool == "request_human_review":
            return f"复核请求 {p.get('review_request_id')}，建议 {p.get('suggested_role')}"
        return "ok"

    # ---------------- 时间窗与电表定位 ----------------
    async def _resolve_scope(self) -> None:
        """从异常事件或问题文本定位电表与时间窗；缺省用数据集全窗。"""
        if self.anomaly_id:
            async with session_factory() as db:
                async with db.begin():
                    row = await repo.get_anomaly_event(db, self.anomaly_id)
            if row is not None:
                self.meter_id = row.meter_id
                # 事件窗口只有 15 分钟，做"倍率变更前后对比"必须有基线：
                # 因此把分析窗口放宽到当日全窗（数据源覆盖 00:00~23:45），
                # 事件窗口本身仍随异常记录保留在库与详情里。
                readings = load_dataset(self.cfg)
                self.window = (min(r.ts for r in readings).strftime("%Y-%m-%d %H:%M"),
                               max(r.ts for r in readings).strftime("%Y-%m-%d %H:%M"))

        if not self.meter_id:
            m = re.search(r"\b(M[-\s]?\d{3}|MT-B\d)\b", self.question or "", re.I)
            if m:
                self.meter_id = m.group(1).replace(" ", "-").upper()
                if not self.meter_id.startswith(("M-", "MT-")):
                    self.meter_id = f"M-{self.meter_id}"

        if not self.window:
            readings = load_dataset(self.cfg)
            self.window = (min(r.ts for r in readings).strftime("%Y-%m-%d %H:%M"),
                           max(r.ts for r in readings).strftime("%Y-%m-%d %H:%M"))
        if not self.meter_id:
            # 没有任何定位线索时，取数据集中出现倍率变化的表（演示数据中即 M-002）
            readings = load_dataset(self.cfg)
            by_meter: dict[str, set[float]] = {}
            for r in readings:
                by_meter.setdefault(r.meter_id, set()).add(r.multiplier)
            changed = [k for k, v in by_meter.items() if len(v) > 1]
            self.meter_id = sorted(changed)[0] if changed else sorted(by_meter)[0]

    # ---------------- 剧本 B ----------------
    async def run_scripted(self, emit) -> None:
        """固定剧本：query → config → validate → [detect] → knowledge → report → HR。"""
        await self._resolve_scope()
        start, end = self.window
        meter = self.meter_id

        async def step(tool: str, args: dict, status_text: str) -> ToolResult:
            await emit("status", {"stage": "tool_calling", "tool": tool, "text": status_text})
            result = await self._call_tool(tool, args)
            await emit("tool_result", {"tool": tool, "ok": result.ok,
                                       "error_code": result.error_code,
                                       "summary": self.tool_events[-1]["summary"],
                                       "cached": result.cached,
                                       "duration_ms": result.duration_ms})
            return result

        await step("query_energy_data",
                   {"meter_id": meter, "start_time": start, "end_time": end,
                    "granularity": "15min"}, "正在查询能耗数据…")

        await step("get_meter_config", {"meter_id": meter}, "正在核对计量配置与倍率版本…")

        val = await step("validate_meter_data",
                         {"meter_id": meter, "start_time": start, "end_time": end},
                         "正在执行确定性数据校验…")

        if val.ok and val.payload.get("passed"):
            await step("detect_energy_anomaly",
                       {"meter_id": meter, "start_time": start, "end_time": end},
                       "校验通过，正在执行规则检测…")
        else:
            # 门禁不允许检测 → 这是"规则1"的物理落点，不是提示词劝阻
            await emit("status", {"stage": "gate_blocked",
                                  "text": "数据校验未通过，跳过规则检测（不得把检测当已确认异常）"})

        issues = [] if not val.ok else val.payload.get("issues", [])
        query = ("电表倍率变更处理流程 " + " ".join(i.get("check", "") for i in issues[:2])
                 if issues else "电表倍率变更处理流程 数据异常核查")
        await step("search_energy_knowledge", {"query": query, "top_k": 3},
                   "正在检索制度与排查依据…")

        await step("generate_anomaly_report",
                   {"anomaly_id": self.anomaly_id, "focus": self.question[:40]},
                   "正在生成报告草稿…")

        # 双保险：状态机要求复核但还没有 HR 请求 → 由编排层补发
        if self.state.review_required and self.state.review_status != "pending":
            reason = "multiplier_anomaly" if any(
                i.get("check") == "multiplier_consistency" for i in issues) else (
                "version_conflict" if issues else "final_confirmation")
            await step("request_human_review",
                       {"reason": reason, "related_anomaly_id": self.anomaly_id,
                        "evidence_ids": [], "note": "编排层兜底补发（模型未调用）"},
                       "正在提交人工复核请求…")

    # ---------------- LLM 自由循环（有真实模型时） ----------------
    async def run_llm_loop(self, emit) -> str:
        """最多 12 步的 Function Calling 循环；任何异常都抛给调用方回落剧本。"""
        system_prompt = (await get_system_prompt("agent"))[1]
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.question},
        ]
        await self._resolve_scope()

        for _ in range(MAX_TOTAL_CALLS):
            turn = await call_llm_tools(messages, TOOL_SPECS, self.cfg)
            if not turn["tool_calls"]:
                return turn["content"]
            messages.append({"role": "assistant", "content": turn["content"],
                             "tool_calls": [
                                 {"id": c["id"], "type": "function",
                                  "function": {"name": c["name"],
                                               "arguments": json.dumps(c["arguments"],
                                                                       ensure_ascii=False)}}
                                 for c in turn["tool_calls"]]})
            for call in turn["tool_calls"]:
                args = call["arguments"]
                # 兜底：模型没给时间窗时用事件/数据集窗口补上（仍走参数校验）
                if call["name"] in ("query_energy_data", "validate_meter_data",
                                    "detect_energy_anomaly"):
                    args.setdefault("start_time", self.window[0])
                    args.setdefault("end_time", self.window[1])
                    if not args.get("meter_id") and not args.get("building_id"):
                        args["meter_id"] = self.meter_id
                await emit("status", {"stage": "tool_calling", "tool": call["name"],
                                      "text": f"模型请求调用 {call['name']}…"})
                result = await self._call_tool(call["name"], args)
                await emit("tool_result", {"tool": call["name"], "ok": result.ok,
                                           "error_code": result.error_code,
                                           "summary": self.tool_events[-1]["summary"],
                                           "duration_ms": result.duration_ms})
                messages.append(result.as_message())
        return ""

    # ---------------- 最终回答 ----------------
    def _evidence_pack(self) -> str:
        lines = ["【工具执行结果】"]
        for ev in self.ctx.evidence:
            lines.append(f"[{ev['id']}][{ev['source_type']}] {ev['source_file']}: {ev['excerpt']}")
        lines.append(f"【任务状态】{json.dumps(self.state.snapshot(), ensure_ascii=False)}")
        return "\n".join(lines)

    def _offline_answer(self) -> str:
        """确定性摘要（无模型时）：只用工具返回的事实，不引入任何新信息。

        刻意不写"共 N 条""编号 xxx"这类派生数字——它们不是证据里的原始值，
        写出来会让 NumberTrace 判为"未溯源"。编号类信息留给任务账本查。
        """
        ev = {e["source_type"]: e for e in self.ctx.evidence}
        lines = ["【离线确定性摘要｜未调用大模型，配置 LLM_API_KEY 后由模型生成解释】", ""]
        lines.append(f"分析对象：{self.meter_id}　时间窗：{self.window[0]} ~ {self.window[1]}")
        if "energy_data" in ev:
            lines.append(f"① 现象：{ev['energy_data']['excerpt']}")
        cfg_ev = next((e for e in self.ctx.evidence
                       if e["source_type"] == "meter_config"), None)
        if cfg_ev:
            lines.append(f"② 配置：{cfg_ev['excerpt']}")
        val_issues = [e for e in self.ctx.evidence if e["source_type"] == "validation"]
        if val_issues:
            verdict = "通过" if self.state.validation_status == "passed" else "未通过"
            lines.append(f"③ 校验：{verdict}。")
            for e in val_issues[-4:]:
                lines.append(f"   - {e['excerpt']}")
        if self.state.anomaly_status == "detected":
            rule = [e for e in self.ctx.evidence if e["source_type"] == "rule_evidence"]
            lines.append("④ 规则检测：" + "；".join(e["excerpt"][:60] for e in rule[:3]))
        elif self.state.validation_status != "passed":
            lines.append("④ 规则检测：因数据校验未通过而跳过（检测结果不作为定性依据）")
        knowledge = [e for e in self.ctx.evidence if e["source_type"] == "knowledge"]
        if knowledge:
            lines.append("⑤ 知识依据：")
            for e in knowledge[:3]:
                lines.append(f"   - {e['source_file']}：{e['excerpt'][:70]}")
        lines.append("⑥ 可能原因（待核实）：倍率变更与电量跳变是否吻合、变更是否有审批工单、"
                     "是否存在换表未同步；均需人工核对，不构成定性结论。")
        if self.state.review_status == "pending":
            lines.append("⑦ 人工复核：已提交，建议岗位见任务账本。本次分析为 AI 草稿，"
                         "不构成最终结论。")
        else:
            lines.append("⑦ 人工复核：本任务未触发（证据充分且校验通过）。")
        return "\n".join(lines)

    async def final_answer(self, emit, llm_text: str | None = None) -> str:
        from app.core.guardrail import number_trace_guard

        if llm_text:
            return llm_text
        has_llm = bool(self.cfg.llm_api_key) and self.cfg.llm_provider != "mock"
        if has_llm and self.ctx.evidence:
            prompt = (f"用户问题：{self.question}\n\n{self._evidence_pack()}\n\n"
                      "请按【现象→规则检测结果→可能原因（分条，标注可能/待核实）→"
                      "知识依据→建议核查事项→是否已提交人工复核】的结构作答。")
            system = (await get_system_prompt("agent"))[1]
            pieces: list[str] = []
            try:
                async for piece in call_llm_stream(prompt, self.cfg, system=system):
                    pieces.append(piece)
                    await emit("token", {"delta": piece})
                return "".join(pieces)
            except LLMError as exc:
                logger.warning("Agent 最终回答生成失败，改用确定性摘要：%s", exc)

        text = self._offline_answer()
        for i in range(0, len(text), 40):
            await emit("token", {"delta": text[i:i + 40]})
        trace = number_trace_guard(text, self.ctx.evidence_text(), "")
        self.state.result_digest["number_trace"] = trace
        return text

    # ---------------- 主流程 ----------------
    async def stream(self) -> AsyncIterator[str]:
        queue: list[str] = []

        async def emit(event: str, data: dict) -> None:
            queue.append(_sse(event, data))

        def drain() -> list[str]:
            out = queue[:]
            queue.clear()
            return out

        async with session_factory() as db:
            async with db.begin():
                await repo.create_agent_task(db, self.state)

        yield _sse("task", {"task_id": self.state.task_id, **self.state.snapshot()})
        yield _sse("status", {"stage": "understanding", "text": "正在理解问题并规划分析步骤…"})

        llm_text: str | None = None
        llm_error: str | None = None
        try:
            llm_text = await self.run_llm_loop(emit)
            for frame in drain():
                yield frame
        except LLMError as exc:
            llm_error = str(exc)
            logger.info("Agent 自由循环不可用，回落剧本：%s", exc)
        except Exception as exc:  # noqa: BLE001  循环异常同样回落，不让任务炸掉
            llm_error = f"自由循环异常：{exc}"
            logger.exception("Agent 自由循环异常")

        if not llm_text:
            if llm_error:
                yield _sse("status", {"stage": "fallback",
                                      "text": f"已切换到确定性剧本执行（{llm_error[:40]}）"})
            if not self.state.data_queried:      # 剧本还没跑过
                await self.run_scripted(emit)
                for frame in drain():
                    yield frame

        # ---- 最终回答 ----
        yield _sse("status", {"stage": "generating", "text": "正在汇总分析结论…"})
        answer = await self.final_answer(emit)
        for frame in drain():
            yield frame

        # ---- 引用（服务端从证据组装，不经模型转手）----
        citations = []
        for e in self.ctx.evidence:
            if e["source_type"] in ("knowledge", "rule_evidence"):
                citations.append({
                    "source_type": e["source_type"], "source_file": e["source_file"],
                    "chunk_id": e["id"], "excerpt": e["excerpt"][:120],
                })
        yield _sse("citations", {"items": citations})

        self.state.finish("completed", {"answer_chars": len(answer),
                                        "citations": len(citations)})
        await self._persist_task()

        yield _sse("done", {
            "task_id": self.state.task_id,
            "status": self.state.status,
            "state": self.state.snapshot(),
            "tool_calls": len(self.tool_events),
            "tools": [t["tool"] for t in self.tool_events],
            "review_required": self.state.review_required,
            "review_status": self.state.review_status,
            "report_status": self.state.report_status,
            "validation_status": self.state.validation_status,
            "number_trace": self.state.result_digest.get("number_trace"),
            "llm_mode": "llm" if llm_text else "offline_scripted",
        })
