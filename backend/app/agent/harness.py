# -*- coding: utf-8 -*-
"""
阶段7：轻量级 Agent Harness 执行循环（单 Agent + 工具集，无多智能体编排）

循环语义：
    模型决策 → 工具调用前校验 → 执行工具 → 结果回填 → 模型再次决策 → 输出结果或继续执行

硬约束：
  - **最多 5 轮模型决策**（MAX_DECISION_ROUNDS），到顶即停止自动执行并说明已完成/未完成
  - 同一轮里相互独立的只读工具并行执行（asyncio.gather）
  - 有依赖关系的工具按 DEPENDENCIES 分层顺序执行；前置未成功则整批拒绝
  - 工具失败/超时/重试/任务取消都有明确状态并落日志
  - 每轮决策与每次工具调用都写 agent_execution_logs，可追溯、可筛选
  - 模型没有写状态/写数据库/控制设备的通道；门禁读任务状态机

离线策略：未配置真实 LLM 时由「确定性规划器」按场景产出等价的 tool_calls 序列，
工具链、门禁、并行、日志、预算上限全部真实执行——只是"决策"这一步不花 token。
配置 LLM_API_KEY 后自动切换为 Function Calling 自主决策，失败则回落规划器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

from app.agent.dependencies import (check_hard_gate, plan_batches, summarize_round)
from app.agent.executor import ToolExecutor, ToolResult
from app.agent.loop import AgentRunner, _sse
from app.agent.schemas import ParamInvalid, validate_args
from app.agent.specs import RETRYABLE, TOOL_SPECS
from app.core.config import Settings
from app.core.llm import LLMError, call_llm_tools
from app.core.retriever import HybridRetriever
from app.db import repo
from app.db.session import session_factory
from app.services.prompt_service import get_system_prompt

logger = logging.getLogger(__name__)

# ---- Harness 预算（用户要求：每任务最多 5 轮模型决策）----
MAX_DECISION_ROUNDS = 5
TOOL_TIMEOUT_SECONDS = 15          # 单工具超时
MAX_TOOL_ATTEMPTS = 2              # 含首次，共 2 次尝试（1 次重试）
RETRYABLE_ERRORS = {"TIMEOUT", "TOOL_EXECUTION_FAILED", "RETRIEVE_FAILED"}


class HarnessRunner(AgentRunner):
    """继承阶段6 的上下文/证据库/摘要/落库能力，替换决策与执行编排。"""

    def __init__(self, question: str, anomaly_id: str | None, session_id: str | None,
                 cfg: Settings, retriever: HybridRetriever | None,
                 user_id: int | None, permissions: list[str],
                 should_stop: Callable[[], Any] | None = None,
                 require_confirmation: bool = False):
        super().__init__(question, anomaly_id, session_id, cfg, retriever,
                         user_id, permissions)
        self.should_stop = should_stop
        self.round_no = 0
        self.completed_tools: set[str] = set()
        self.log_seq = 0
        self._seq_lock = asyncio.Lock()
        self.llm_failed = False
        self.unfinished_reason = ""
        self.decision_trace: list[dict] = []
        self.messages: list[dict] = []
        self.intent = self._classify(question)
        self.state.intent = self.intent
        # 人工确认模式：报告草稿经 SSE 下发后暂停，不在循环内自动创建复核请求
        self.require_confirmation = require_confirmation
        self.last_report: dict | None = None

    # ---------------- 场景分类（离线规划器用；真实 LLM 时由模型自行判断）----------------
    @staticmethod
    def _classify(question: str) -> str:
        q = question or ""
        if any(k in q for k in ("生成报告", "报告草稿", "分析报告", "并生成")):
            return "report"
        if ("哪些信息" in q or "检查什么" in q or "制度" in q or "规范" in q) and \
                not any(k in q for k in ("分析", "为什么", "是不是", "报告")):
            return "knowledge"
        if "倍率" in q and ("是不是" in q or "坏了" in q or "故障" in q):
            return "meter_question"
        return "analysis"

    # ---------------- 电表定位（场景C 的 METER_001 别名）----------------
    async def _resolve_scope(self) -> None:
        await super()._resolve_scope()
        # 需求场景写的是 METER_001（倍率 10→100），模拟数据集里对应 M-002：
        # 这是**唯一**含该倍率变更的表，映射到它才能演示真实的校验/冲突链路。
        if "METER_001" in (self.question or "").upper():
            self.meter_id = "M-002"

    # ---------------- 日志 ----------------
    async def _log(self, kind: str, tool_name: str, params: dict, status: str,
                   summary: str, error_code: str | None, duration_ms: int) -> None:
        """写一行 Harness 日志；seq 用锁分配，保证并行批次里的顺序稳定。"""
        async with self._seq_lock:
            self.log_seq += 1
            seq = self.log_seq
        async with session_factory() as db:
            async with db.begin():
                await repo.log_agent_execution(
                    db, self.state.task_id, self.state.session_id, self.user_id,
                    self.round_no, seq, kind, tool_name,
                    self._safe_params(params), duration_ms, status,
                    summary, error_code)

    @staticmethod
    def _safe_params(params: dict) -> dict:
        """白名单化：只保留已通过 Pydantic Schema 的键，且值截断，防敏感信息入库。"""
        out: dict[str, Any] = {}
        for k, v in (params or {}).items():
            if k.lower() in ("api_key", "password", "token", "sql", "authorization",
                             "secret", "system_prompt"):
                continue                       # 即便模型夹带也绝不落库
            text = json.dumps(v, ensure_ascii=False, default=str) \
                if not isinstance(v, (str, int, float, bool, type(None))) else v
            if isinstance(text, str) and len(text) > 300:
                text = text[:300] + "…"
            out[k] = text
        return out

    # ---------------- 模型决策 ----------------
    async def _decide(self) -> list[dict] | str:
        """
        一轮模型决策。返回 tool_calls 列表，或直接返回最终回答文本（结束循环）。
        真实 LLM 失败 → 置 llm_failed 并回落确定性规划器（后续轮次不再重试 LLM）。
        """
        if not self.llm_failed and bool(self.cfg.llm_api_key) \
                and self.cfg.llm_provider != "mock":
            try:
                if not self.messages:
                    system = (await get_system_prompt("agent"))[1]
                    self.messages = [{"role": "system", "content": system},
                                     {"role": "user", "content": self.question}]
                turn = await call_llm_tools(self.messages, TOOL_SPECS, self.cfg)
                if not turn["tool_calls"]:
                    return turn["content"] or self._offline_answer()
                self.messages.append({
                    "role": "assistant", "content": turn["content"],
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["arguments"],
                                                             ensure_ascii=False)}}
                        for c in turn["tool_calls"]],
                })
                calls = []
                for c in turn["tool_calls"]:
                    args = dict(c["arguments"])
                    # 模型漏填时间窗/电表时按任务上下文补齐（仍走严格参数校验）
                    if c["name"] in ("query_energy_data", "validate_meter_data",
                                     "detect_energy_anomaly") and self.window:
                        args.setdefault("start_time", self.window[0])
                        args.setdefault("end_time", self.window[1])
                        if not args.get("meter_id") and not args.get("building_id"):
                            args["meter_id"] = self.meter_id
                    calls.append({"name": c["name"], "arguments": args})
                return calls
            except LLMError as exc:
                logger.info("Harness LLM 决策不可用，回落确定性规划器：%s", exc)
                self.llm_failed = True
                self.messages = []

        return self._offline_plan()

    def _offline_plan(self) -> list[dict] | str:
        """确定性规划器：按场景产出每轮 tool_calls，模拟"模型决策"。"""
        r = self.round_no
        start, end = self.window
        meter = self.meter_id

        if self.intent == "knowledge":
            if r == 1:
                return [{"name": "search_energy_knowledge",
                         "arguments": {"query": self.question[:200], "top_k": 3}}]
            return self._offline_answer()

        base = {"start_time": start, "end_time": end}
        if r == 1:
            # 相互独立的三个只读工具：并行执行（场景 A/B/C 的第一轮）
            return [
                {"name": "query_energy_data",
                 "arguments": {**base, "meter_id": meter, "granularity": "15min"}},
                {"name": "get_meter_config", "arguments": {"meter_id": meter}},
                {"name": "get_device_relationship", "arguments": {"meter_id": meter}},
            ]
        if r == 2:
            return [{"name": "validate_meter_data",
                     "arguments": {**base, "meter_id": meter}}]
        if r == 3:
            if self.state.validation_status == "passed":
                return [{"name": "detect_energy_anomaly",
                         "arguments": {**base, "meter_id": meter}}]
            # 校验未通过：检测被门禁拒绝，改走知识库取证（诊断性分析）
            return [{"name": "search_energy_knowledge",
                     "arguments": {"query": "电表倍率变更 配置冲突 数据核查 处理流程",
                                   "top_k": 3}}]
        if r == 4:
            if self.intent == "meter_question":
                # 场景C：不生成报告；存在强制复核条件时先调用复核工具
                # 场景C 不会生成报告草稿（没有暂停点），因此无论是否开启确认模式，
                # 强制复核条件成立时都必须调用复核工具。
                if self.state.review_required and self.state.review_status != "pending":
                    return [{"name": "request_human_review",
                             "arguments": {"reason": "multiplier_anomaly",
                                           "related_anomaly_id": self.anomaly_id,
                                           "evidence_ids": [],
                                           "note": "倍率变更与生效时间存疑，需人工核查"}}]
                return self._offline_answer()
            calls = [{"name": "generate_anomaly_report",
                      "arguments": {"anomaly_id": self.anomaly_id,
                                    "focus": self.question[:80]}}]
            if not any(e["source_type"] == "knowledge" for e in self.ctx.evidence):
                calls.insert(0, {"name": "search_energy_knowledge",
                                 "arguments": {"query": "倍率变更管理规范 人工复核",
                                               "top_k": 3}})
            return calls
        if r == 5 and self.state.review_required \
                and self.state.review_status != "pending" \
                and not self.require_confirmation:
            reason = "multiplier_anomaly" if self.state.validation_status == "issues_found" \
                else "final_confirmation"
            return [{"name": "request_human_review",
                     "arguments": {"reason": reason,
                                   "related_anomaly_id": self.anomaly_id,
                                   "evidence_ids": [], "note": "状态机强制触发"}}]
        return self._offline_answer()

    # ---------------- 单次工具执行（含超时/重试/门禁/日志）----------------
    async def _execute_one(self, call: dict) -> ToolResult:
        tool = call.get("name", "")
        args = call.get("arguments") or {}
        t0 = time.perf_counter()

        # ① 参数严格校验（Pydantic / JSON Schema）
        try:
            clean = validate_args(tool, args)
        except ParamInvalid as exc:
            await self._log("tool", tool, {}, "rejected", exc.message,
                            "PARAM_INVALID", int((time.perf_counter() - t0) * 1000))
            return ToolResult(tool, False, {"message": exc.message},
                              error_code="PARAM_INVALID",
                              duration_ms=int((time.perf_counter() - t0) * 1000))

        # ② 依赖硬门禁（执行器之外的第二道，读任务状态机）
        ok, why = check_hard_gate(tool, self.state, self.ctx)
        if not ok:
            await self._log("tool", tool, clean, "rejected", why,
                            "TOOL_PRECONDITION_FAILED",
                            int((time.perf_counter() - t0) * 1000))
            return ToolResult(tool, False, {"message": why},
                              error_code="TOOL_PRECONDITION_FAILED",
                              duration_ms=int((time.perf_counter() - t0) * 1000))

        # ③ 执行 + 超时 + 重试（仅瞬时错误、仅可重试工具）
        result: ToolResult | None = None
        for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
            try:
                result = await asyncio.wait_for(
                    self.executor.execute(tool, clean),
                    timeout=TOOL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                result = ToolResult(tool, False, {"message": "工具执行超时"},
                                    error_code="TIMEOUT",
                                    duration_ms=TOOL_TIMEOUT_SECONDS * 1000)
            if result.ok or result.error_code not in RETRYABLE_ERRORS \
                    or tool not in RETRYABLE or attempt == MAX_TOOL_ATTEMPTS:
                break
            await asyncio.sleep(0.1 * attempt)     # 轻量退避，不拖慢离线自测

        assert result is not None
        summary = self._summarize(tool, result)
        status = "success" if result.ok else (
            "timeout" if result.error_code == "TIMEOUT" else "failed")
        await self._log("tool", tool, clean, status, summary,
                        result.error_code, result.duration_ms)
        # 兼容阶段6 的 /admin/agent-tasks 轨迹页：同时写旧表一行
        async with self._seq_lock:
            self.seq += 1
            legacy_seq = self.seq
        async with session_factory() as db:
            async with db.begin():
                await repo.add_agent_tool_call(
                    db, self.state.task_id, legacy_seq, tool,
                    json.dumps(self._safe_params(clean), ensure_ascii=False)[:512],
                    summary[:1024], result.ok, result.error_code,
                    result.duration_ms)
        if result.ok and tool == "generate_anomaly_report":
            self.last_report = result.payload
        if result.ok:
            self.completed_tools.add(tool)
            async with session_factory() as db:
                async with db.begin():
                    await repo.update_agent_task(db, self.state)
        return result

    def _offline_answer(self) -> str:
        """
        按场景定制离线解释文字（有真实 LLM 时由模型生成）。

        ★ 刻意不写"共 N 条""编号 xxx"这类派生数字：它们不是证据里的原始值，
          写出来会被 NumberTrace 判为未溯源。编号类信息留在任务账本里查。
        """
        knowledge = [e for e in self.ctx.evidence
                     if e["source_type"] == "knowledge"]

        if self.intent == "knowledge":
            lines = ["【离线确定性摘要｜未调用大模型，配置 LLM_API_KEY 后由模型生成解释】", ""]
            if not knowledge:
                lines.append("当前知识库中未找到相关规定，建议补充制度文档或换一种问法。")
                return "\n".join(lines)
            lines.append("根据园区能源管理知识库，需要检查的信息如下：")
            for e in knowledge[:3]:
                lines.append(f"- {e['source_file']}：{e['excerpt'][:110]}")
            lines.append("")
            lines.append("以上均来自检索到的知识片段；未查询任何能耗数据，"
                         "也未执行异常检测或创建人工复核请求。")
            return "\n".join(lines)

        if self.intent == "meter_question":
            lines = ["【离线确定性摘要｜未调用大模型，配置 LLM_API_KEY 后由模型生成解释】", ""]
            lines.append("① 已确认事实（工具返回）：该电表存在倍率配置变化，"
                         "配置版本、生效时间与能耗数据可从任务账本回查。")
            lines.append("② 判断：**不能仅凭倍率变化认定电表损坏**。"
                         "可能原因分为三类，均属「可能/待核实」，需人工核对：")
            lines.append("   - 配置录入或变更单错误（倍率被误录、生效时间存疑）；")
            lines.append("   - 计量口径变化（倍率折算导致示值跳变，设备本身未必异常）；")
            lines.append("   - 真实设备故障（需现场校验互感器、表计与接线）。")
            lines.append("③ 证据不足：数据源未提供配置变更工单与设备台账，"
                         "无法通过数据单独区分以上三类，明确提示人工核查。")
            if self.state.review_status == "pending":
                lines.append("④ 已提交人工复核（AI 草稿，不构成最终结论）。")
            else:
                lines.append("④ 存在强制复核条件，已记录待人工核查事项。")
            if knowledge:
                lines.append("⑤ 知识依据：" + "；".join(
                    e["source_file"] for e in knowledge[:2]))
            return "\n".join(lines)

        return super()._offline_answer()

    # ---------------- 取消检测 ----------------
    async def _cancelled(self) -> bool:
        if self.should_stop is None:
            return False
        try:
            res = self.should_stop()
            if asyncio.iscoroutine(res):
                res = await res
            return bool(res)
        except Exception:  # noqa: BLE001
            return False

    # ---------------- 主循环 ----------------
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
        await self._resolve_scope()

        yield _sse("task", {"task_id": self.state.task_id, **self.state.snapshot(),
                            "engine": "harness", "max_rounds": MAX_DECISION_ROUNDS})
        yield _sse("status", {"stage": "understanding", "text": "Harness 正在规划…"})

        final_text: str | None = None
        rounds_used = 0

        for round_no in range(1, MAX_DECISION_ROUNDS + 1):
            if await self._cancelled():
                self.unfinished_reason = "用户取消/连接断开，任务提前终止"
                break
            self.round_no = round_no
            rounds_used = round_no

            decision = await self._decide()
            if isinstance(decision, str):
                final_text = decision
                break

            calls = decision
            # 决策日志：只记"选了哪些工具"，不记隐藏推理、不记 Key/密码
            await self._log("decision", "(decision)",
                            {"selected_tools": [c.get("name") for c in calls]},
                            "recorded", summarize_round(calls), None, 0)
            self.decision_trace.append({
                "round": round_no, "tools": [c.get("name") for c in calls]})
            yield _sse("round", {"round": round_no, "max": MAX_DECISION_ROUNDS,
                                 "tools": [c.get("name") for c in calls]})

            if round_no == 3 and self.state.validation_status != "passed" \
                    and self.intent != "knowledge":
                msg = "数据校验未通过，跳过规则检测（检测结果不作为定性依据）"
                yield _sse("status", {"stage": "gate_blocked", "text": msg})
                for frame in drain():
                    yield frame

            batches, deferred = plan_batches(calls, self.completed_tools)
            for call in deferred:
                tool = call.get("name", "")
                why = "缺少前置工具执行结果，本轮不执行（请下一轮先补依赖）"
                await self._log("tool", tool, call.get("arguments") or {},
                                "rejected", why, "TOOL_PRECONDITION_FAILED", 0)
                yield _sse("tool_result", {
                    "tool": tool, "ok": False,
                    "error_code": "TOOL_PRECONDITION_FAILED",
                    "summary": why, "duration_ms": 0})

            for batch in batches:
                if await self._cancelled():
                    self.unfinished_reason = "用户取消/连接断开，任务提前终止"
                    break
                parallel = len(batch) > 1
                if parallel:
                    yield _sse("status", {"stage": "parallel_tools",
                                          "text": f"并行执行 {len(batch)} 个只读工具…"})
                for call in batch:
                    tool = call.get("name", "")
                    yield _sse("status", {"stage": "tool_calling", "tool": tool,
                                          "text": f"正在调用 {tool}…"})

                # 相互独立的只读工具并行；有依赖的批次单工具顺序执行
                results = await asyncio.gather(
                    *[self._execute_one(c) for c in batch])

                for call, result in zip(batch, results):
                    tool = call.get("name", "")
                    summary = self._summarize(tool, result)
                    yield _sse("tool_result", {
                        "tool": tool, "ok": result.ok,
                        "error_code": result.error_code, "summary": summary,
                        "duration_ms": result.duration_ms,
                        "parallel": parallel})
                    # 结果回填给模型（Function Calling 的 tool 消息）
                    if self.messages and not self.llm_failed:
                        self.messages.append(result.as_message())
                if self.unfinished_reason:
                    break
            if self.unfinished_reason:
                break

            for frame in drain():
                yield frame

        else:
            # for 循环自然走完 = 到达 5 轮上限
            self.unfinished_reason = (
                f"已达 {MAX_DECISION_ROUNDS} 轮模型决策上限，停止自动执行；"
                "已完成步骤见 tool_result 轨迹，未完成的是最终结论生成")

        for frame in drain():
            yield frame

        # ---- 最终输出 ----
        yield _sse("status", {"stage": "generating", "text": "正在汇总分析结论…"})
        if final_text is None:
            final_text = self._offline_answer()
        answer = final_text
        from app.core.guardrail import number_trace_guard
        trace = number_trace_guard(answer, self.ctx.evidence_text(), "")
        self.state.result_digest["number_trace"] = trace
        for i in range(0, len(answer), 40):
            await emit("token", {"delta": answer[i:i + 40]})
        for frame in drain():
            yield frame

        citations = [
            {"source_type": e["source_type"], "source_file": e["source_file"],
             "chunk_id": e["id"], "excerpt": e["excerpt"][:120]}
            for e in self.ctx.evidence
            if e["source_type"] in ("knowledge", "rule_evidence")
        ]
        yield _sse("citations", {"items": citations})

        status = "failed" if self.unfinished_reason.startswith("用户取消") else "completed"
        self.state.finish(status, {
            "engine": "harness", "decision_rounds": rounds_used,
            "decision_trace": self.decision_trace,
            "unfinished_reason": self.unfinished_reason,
            "answer_chars": len(answer),
        })
        await self._persist_task()

        done = {
            "task_id": self.state.task_id, "engine": "harness",
            "status": self.state.status, "state": self.state.snapshot(),
            "decision_rounds": rounds_used,
            "max_decision_rounds": MAX_DECISION_ROUNDS,
            "decision_trace": self.decision_trace,
            "tool_calls": self.state.tool_call_count,
            "tools": [t["tool"] for t in self.tool_events],
            "validation_status": self.state.validation_status,
            "review_required": self.state.review_required,
            "review_status": self.state.review_status,
            "report_status": self.state.report_status,
            "number_trace": self.state.result_digest.get("number_trace"),
            "llm_mode": "llm" if (self.cfg.llm_api_key and not self.llm_failed)
                        else "offline_planner",
            "unfinished_reason": self.unfinished_reason or None,
            "require_confirmation": self.require_confirmation,
        }
        yield _sse("done", done)
