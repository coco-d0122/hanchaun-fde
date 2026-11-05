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
import re
import time
from typing import Any, AsyncIterator, Callable

from app.agent.context import ConversationContext
from app.agent.dependencies import (check_hard_gate, plan_batches, summarize_round)
from app.agent.guards import ToolExecutor, ToolResult
from app.agent.loop import AgentRunner, _sse
from app.agent.schemas import ParamInvalid, validate_args
from app.agent.tools import load_dataset
from app.agent.specs import RETRYABLE, TOOL_SPECS
from app.core.config import Settings
from app.core.llm import LLMError, call_llm_tools
from app.rag.retriever import HybridRetriever
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
        # ---- 阶段8：多轮上下文 ----
        self.building_id: str | None = None
        self.resolution = None          # context.Resolution
        self.conv = None                # context.ConversationContext
        self.last_knowledge: dict | None = None    # 最近一次知识检索结果（判“依据是否充足”）
        self.last_query: dict | None = None         # 最近一次电量查询结果（回答只用它）
        self.last_config: dict | None = None
        self.last_validate: dict | None = None
        self.last_relationship: dict | None = None
        self.compare_meters: list[str] = []        # 比较类任务要查的多块表
        # 违规意图识别：只用于"回答里明确拒绝 + 选择替代路径"，不是安全边界本身
        # （安全边界在后端：工具注册表里没有写数据/控设备/签核通道）
        self.violation = self._detect_violation(question)

    # ---------------- 场景分类（离线规划器用；真实 LLM 时由模型自行判断）----------------
    # 分类只影响"离线规划器选哪条剧本"，不影响任何门禁：
    # 无论分成哪一类，validate→detect、证据→report 的硬约束都由状态机与依赖图强制。
    REPORT_MARKERS = ("生成报告", "报告草稿", "分析报告", "并生成", "出具报告")
    KNOWLEDGE_MARKERS = ("哪些信息", "检查什么", "制度", "规范", "流程", "步骤", "口径",
                         "允许偏差", "要求", "怎么处理", "如何处理", "如何确认")
    ANALYSIS_MARKERS = ("分析", "为什么", "原因", "排查", "定位", "突增", "突然增加",
                        "校验", "不吻合", "跳变", "突变", "比较", "差异")
    QUERY_MARKERS = ("查询", "查一下", "用电量情况", "采样点", "读数是多少", "电量是多少")

    @classmethod
    def _classify(cls, question: str) -> str:
        q = question or ""
        has_meter = bool(re.search(r"M[-_]?\d{3}|MT-[A-Z]\d|METER[_-]?\d{3}", q, re.I))

        if "提交审核" in q or "提交人工复核" in q or "提交复核" in q:
            return "followup_submit"
        if any(k in q for k in ("需要人工复核", "要人工复核", "要不要复核", "需要复核吗")):
            return "followup_review"
        if any(k in q for k in cls.REPORT_MARKERS):
            return "report"
        if any(k in q for k in ("归属", "计量关系", "租户")) and has_meter:
            return "ownership"
        # 纯粹的制度/流程问答：没有电表编号、也不在做数据分析
        if any(k in q for k in cls.KNOWLEDGE_MARKERS) and not has_meter \
                and not any(k in q for k in ("为什么", "突增", "突然增加", "定位",
                                             "判断", "最终结论")):
            return "knowledge"
        if any(k in q for k in cls.QUERY_MARKERS) \
                and not any(k in q for k in ("异常", "突增", "突然增加", "为什么", "报告")):
            return "query"
        if "倍率" in q and any(k in q for k in ("是多少", "多少", "是不是", "坏了", "故障")):
            if any(k in q for k in cls.ANALYSIS_MARKERS):
                return "analysis"          # "帮我分析是不是计量配置问题" → 分析
            return "config" if ("是多少" in q or "多少" in q) else "meter_question"
        return "analysis"

    # ---------------- 电表定位（多轮继承 + 需求别名 + 楼栋→总表）----------------
    async def _resolve_scope(self) -> None:
        await super()._resolve_scope()
        res = self.resolution
        if res is not None:
            if res.window:
                self.window = res.window
            if res.compare_meters:
                self.compare_meters = res.compare_meters
            if self.anomaly_id and not res.meter_explicit:
                # 事件是本轮分析对象：事件电表与事件窗口优先，问题里的楼栋口径不覆盖它
                res.building_id = None
            if res.meter_id:
                self.meter_id = res.meter_id
                self.building_id = None
            elif res.building_id:
                # 只给了楼栋：清掉上游按"倍率变化的表"打上的默认电表，改用楼栋口径
                self.meter_id = None
                self.building_id = res.building_id

        if self.building_id and not self.meter_id:
            # 楼栋级问题落到该楼总表（"B1 楼用电量"= MT-B1），
            # 总表才有完整的父子平衡关系可供校验；没有总表时保留楼栋口径。
            total = f"MT-{self.building_id}"
            if any(r.meter_id == total for r in load_dataset(self.cfg)):
                self.meter_id = total
                self.building_id = None
                if res is not None:
                    res.inherited.append(f"楼栋口径落到总表：{total}")

        if res is not None:
            # 继承说明写进任务账本，事后可复盘"这条参数从哪继承来的"
            self.state.result_digest["context"] = {
                "inherited": res.inherited, "ambiguous": res.ambiguous,
                "followup_of_task": res.followup_of_task,
            }
            if res.followup_of_task:
                self.state.result_digest["followup_of_task"] = res.followup_of_task

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

    # ---------------- 违规意图识别（回答层；边界在执行器）----------------
    @staticmethod
    def _detect_violation(question: str) -> str | None:
        q = question or ""
        if any(k in q for k in ("改成正常值", "修改原始", "改掉读数", "覆盖读数",
                                "把读数改", "改数据", "篡改")):
            return "data_tamper"
        if any(k in q for k in ("停机", "关掉空调", "关空调", "控制设备", "断掉", "拉闸")):
            return "device_control"
        if any(k in q for k in ("签字", "签核", "写成已确认", "伪造", "替我确认")):
            return "forge_review"
        if ("关闭" in q or "关掉" in q) and (
                "异常" in q or "事件" in q or re.search(r"AN-\d", q, re.I)):
            return "close_anomaly"
        return None

    REFUSAL_TEXT = {
        "data_tamper": [
            "【拒绝执行】不会把电表读数改成“正常值”，也不会修改/覆盖任何原始计量记录。",
            "原因：原始计量数据是计费与责任认定的唯一凭据，只能保留、不能被 AI 修改；"
            "本系统的工具集里没有任何写数据通道（强行调用会被后端以 UNKNOWN_TOOL 拒绝）。",
            "替代路径：先做数据校验定位问题（缺数/倍率/口径）→ 由程序生成数据质量异常标记 "
            "→ 提交人工复核，由计量人员按流程处理。",
        ],
        "device_control": [
            "【拒绝执行】不会停机关调设备，也不会给出可执行的停机指令或控制参数。",
            "原因：本系统只做能耗监测与分析，工具集里没有设备控制能力；"
            "绕过审批直接控制工业设备属于违规操作，存在安全风险。",
            "替代路径：先核查倍率与计量口径是否导致“虚高”，再按流程申报节能调整，"
            "必要时提交人工复核由业务与电气安全岗位决策。",
        ],
        "forge_review": [
            "【拒绝执行】不会代替电气安全主管签字，也不会把复核结果伪造成“已确认”。",
            "原因：创建复核请求 ≠ 人工已完成审核；用户确认提交报告 ≠ 主管签核。"
            "签核是人工职责，系统只记录请求与状态（pending），任何人（含管理员）都没有代签通道。",
            "替代路径：由我发起一条 pending 的人工复核请求，再走线下签核流程。",
        ],
        "close_anomaly": [
            "【拒绝执行】不会直接关闭异常事件，也不会改动事件状态。",
            "原因：异常关闭属于人工签核事项，AI 只能分析、不能定性；"
            "提交结论（POST /api/anomalies/{id}/reviews）的通道未开放（405），"
            "工具集里也没有 close_anomaly 之类的工具。",
            "替代路径：先核验数据与制度依据，再提交人工复核，由有权限的岗位签核关闭。",
        ],
    }

    def _refusal_block(self) -> list[str]:
        return list(self.REFUSAL_TEXT.get(self.violation or "", []))

    # ---------------- 工具参数组装 ----------------
    def _scope_args(self) -> dict:
        """按当前作用域（电表优先，其次楼栋）组装时间窗参数。"""
        args = {"start_time": self.window[0], "end_time": self.window[1]}
        if self.meter_id:
            args["meter_id"] = self.meter_id
        elif self.building_id:
            args["building_id"] = self.building_id
        return args

    def _read_calls(self) -> list[dict]:
        """首轮相互独立的只读工具（可并行）。

        比较类任务（"比较 M-001 和 M-002"）查两次电量，其余一次；
        配置与拓扑只有拿到电表号才有意义（楼栋口径下不调用，避免 PARAM_INVALID）。
        """
        base = self._scope_args()
        calls: list[dict] = []
        if self.compare_meters:
            for m in self.compare_meters:
                calls.append({"name": "query_energy_data",
                              "arguments": {**base, "meter_id": m,
                                            "granularity": "15min"}})
        else:
            calls.append({"name": "query_energy_data",
                          "arguments": {**base, "granularity": "15min"}})
        if self.meter_id:
            calls.append({"name": "get_meter_config",
                          "arguments": {"meter_id": self.meter_id}})
            calls.append({"name": "get_device_relationship",
                          "arguments": {"meter_id": self.meter_id}})
        return calls

    def _knowledge_call(self, query: str) -> dict:
        return {"name": "search_energy_knowledge",
                "arguments": {"query": query[:200], "top_k": 3}}

    def _has_knowledge(self) -> bool:
        return any(e["source_type"] == "knowledge" for e in self.ctx.evidence)

    def _needs_review(self) -> bool:
        return self.state.review_required and self.state.review_status != "pending"

    def _review_reason(self) -> str:
        if self.intent == "ownership":
            return "ownership_unclear"
        return self.state.review_reason_hint or "final_confirmation"

    def _review_call(self, note: str) -> dict:
        return {"name": "request_human_review",
                "arguments": {"reason": self._review_reason(),
                              "related_anomaly_id": self.anomaly_id,
                              "evidence_ids": [], "note": note}}

    # ---------------- 确定性规划器 ----------------
    def _offline_plan(self) -> list[dict] | str:
        """确定性规划器：按 intent 产出每轮 tool_calls，模拟"模型决策"。

        所有轮次的工具**不越过门禁**：detect 之前必有 validate，report 之前必有
        查询+校验+证据；这些在真实模型下由执行器拦截，在这里由同一套状态机判定。
        """
        r = self.round_no
        it = self.intent

        # ---- 违规意图（要控制设备）：一句话拒绝，不调任何工具 ----
        if self.violation == "device_control":
            return self._offline_answer()

        # ---- 只读问答类：一两轮就拿依据作答，绝不碰能耗数据 ----
        if it == "knowledge":
            if r == 1:
                return [self._knowledge_call(self.question)]
            return self._offline_answer()

        if it == "followup_submit":
            # 提交审核要走用户确认与二次校验，模型不能"一句话提交"
            return self._offline_answer()

        if it == "config":
            if r == 1 and self.meter_id:
                return [{"name": "get_meter_config",
                         "arguments": {"meter_id": self.meter_id}}]
            return self._offline_answer()

        if it == "query":
            if r == 1:
                return self._read_calls()[:len(self.compare_meters) or 1]
            return self._offline_answer()

        # ---- 追问复核：上一轮已定性（校验未通过/存在强制复核条件）时，直接按前置状态建单 ----
        if it == "followup_review" and r == 1 and self._prior_review_required():
            return [self._review_call("继承上一轮分析结论，需人工复核")]

        # ---- 分析类（含 report / ownership / meter_question）----
        if r == 1:
            return self._read_calls()

        if r == 2:
            return [{"name": "validate_meter_data", "arguments": self._scope_args()}]

        if r == 3:
            if self.state.validation_status == "passed":
                return [{"name": "detect_energy_anomaly",
                         "arguments": self._scope_args()}]
            # 校验未通过：检测被门禁拒绝，改走知识库取证（诊断性分析）
            return [self._knowledge_call("电表倍率变更 配置冲突 数据缺失 处理流程 核对要求")]

        if r == 4:
            if it == "ownership" and not self._has_knowledge():
                return [self._knowledge_call("设备与计量点归属说明 租户归属 分摊规则")]
            if it == "report":
                calls: list[dict] = []
                if not self._has_knowledge():
                    calls.append(self._knowledge_call("倍率变更管理规范 人工复核要求"))
                calls.append({"name": "generate_anomaly_report",
                              "arguments": {"anomaly_id": self.anomaly_id,
                                            "focus": self.question[:80]}})
                return calls
            if not self._has_knowledge() and self.intent != "ownership":
                return [self._knowledge_call("能耗异常排查流程 人工复核要求")]
            if self._needs_review():
                return [self._review_call("状态机强制触发：存在需人工核查事项")]
            return self._offline_answer()

        if r == 5:
            if it == "report" and self._needs_review() and not self.require_confirmation:
                return [self._review_call("报告为待核查草稿，需人工复核")]
            if it != "report" and self._needs_review():
                return [self._review_call("状态机强制触发：存在需人工核查事项")]
            return self._offline_answer()

        return self._offline_answer()

    def _prior_review_required(self) -> bool:
        """上一轮任务是否已经积累了"需复核"的前置状态（同一会话内）。"""
        prior = (self.conv.prior_task if self.conv else None) or {}
        return bool(prior) and bool(prior.get("review_required") or
                                    prior.get("validation_status") == "issues_found")

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
            # 保留各工具最近一次返回，供离线摘要按"只用事实"的方式引用
            if tool == "query_energy_data":
                self.last_query = result.payload
            elif tool == "get_meter_config":
                self.last_config = result.payload
            elif tool == "validate_meter_data":
                self.last_validate = result.payload
            elif tool == "get_device_relationship":
                self.last_relationship = result.payload
                # 归属不明（设备台账/租户为 null）→ 强制人工核实，不靠模型自觉
                if self.intent == "ownership" and (
                        result.payload.get("tenant") is None
                        or result.payload.get("devices") is None
                        or not result.payload.get("ownership_clear")):
                    self.state.review_required = True
                    self.state.review_reason_hint = "ownership_unclear"
            elif tool == "search_energy_knowledge":
                self.last_knowledge = result.payload
            self.completed_tools.add(tool)
            async with session_factory() as db:
                async with db.begin():
                    await repo.update_agent_task(db, self.state)
        return result

    def _answer_body(self) -> str:
        """
        按场景定制离线解释正文（有真实 LLM 时由模型生成）。

        ★ 刻意不写“共 N 条”“编号 xxx”这类派生数字：它们不是证据里的原始值，
          写出来会被 NumberTrace 判为“未溯源”。编号类信息留在任务账本里查。
        """
        knowledge = [e for e in self.ctx.evidence
                     if e["source_type"] == "knowledge"]
        head = "【离线确定性摘要｜未调用大模型，配置 LLM_API_KEY 后由模型生成解释】"
        refusal = self._refusal_block()

        # ---- “提交审核”：不调工具，交给用户确认 + 二次校验 ----
        if self.intent == "followup_submit":
            return "\n".join([
                head, "",
                "① 报告草稿不能由对话里一句话直接提交：需要您在草稿卡片上明确确认，",
                "   系统会在确认时二次校验身份、权限、报告版本与证据 hash。",
                "② 确认只代表“请求人工复核”，不等于确认异常原因，也不等于电气安全主管签核。",
                "③ 本次未调用任何提交工具，草稿状态保持不变（仍为待确认）。",
            ])

        # ---- 知识问答：依据不足就明说没找到，绝不编条款 ----
        if self.intent == "knowledge":
            if not knowledge:
                return "\n".join([
                    head, "",
                    "当前知识库中未找到相关规定。",
                    "检索到的片段与问题的限定条件不匹配（只命中通用词），因此不作为依据；",
                    "建议补充对应制度文档，或换一种问法再问。",
                    "",
                    "说明：本次只做了知识库检索，未查询能耗数据、未执行异常检测，"
                    "也未创建人工复核请求；不对限额、比例、时长等数值做任何推测。",
                ])
            lines = [head, "", "根据园区能源管理知识库，需要检查的信息如下："]
            for e in knowledge[:3]:
                lines.append(f"- {e['source_file']}：{e['excerpt'][:110]}")
            lines.append("")
            lines.append("以上均来自检索到的知识片段；未查询任何能耗数据，"
                         "也未执行异常检测或创建人工复核请求。")
            return "\n".join(lines)

        # ---- 普通数据查询：只报工具返回值 ----
        if self.intent == "query":
            lines = [head, ""]
            q = self.last_query
            if q:
                for m in q.get("meters", []):
                    lines.append(f"{m['meter_id']}：电量合计 {m['energy_kwh_total']} kWh，"
                                 f"采样点 {m['points']} 个，缺失点 {m['missing_points']} 个，"
                                 f"倍率 {m['multipliers_seen']}")
                lines.append(f"合计 {q.get('total_energy_kwh')} kWh；数据来源 "
                             f"{q.get('_trace', {}).get('source')}"
                             "（模拟测试数据，非园区真实记录）。")
            else:
                lines.append("本次没有取到读数：查询工具返回了错误（见工具轨迹）。")
                for e in self.ctx.evidence[-2:]:
                    lines.append(f"- {e['excerpt'][:100]}")
                lines.append("不会用其他日期或其他电表的数据替代，请确认电表编号与时间范围。")
            lines.append("说明：这是只读查询，未做数据校验与异常检测，也未生成报告。")
            return "\n".join(lines)

        # ---- 单表配置查询（“它的倍率是多少”）----
        if self.intent == "config":
            cfg_payload = self.last_config or {}
            cur = cfg_payload.get("current") or {}
            hist = cfg_payload.get("history") or []
            lines = [head, "",
                     f"电表 {self.meter_id} 当前倍率 {cur.get('multiplier')}，"
                     f"单位 {cur.get('unit')}，配置版本 v{cur.get('version')}，"
                     f"生效时间 {cur.get('effective_from')}。"]
            if len(hist) > 1:
                lines.append("版本史：" + "；".join(
                    f"v{h.get('version')} 倍率 {h.get('multiplier')} 自 {h.get('effective_from')}"
                    for h in hist[:4]))
            if cfg_payload.get("has_conflict"):
                lines.append("存在配置冲突信号（倍率在同窗内变化且无变更单可比对），"
                             "需人工核对变更工单与生效时间。")
            lines.append("说明：此处只读取计量配置，未修改任何数据。")
            return "\n".join(lines)

        # ---- 追问“需要人工复核吗”：继承上一轮结论作答 ----
        if self.intent == "followup_review":
            prior = (self.conv.prior_task if self.conv else None) or {}
            lines = [head, "",
                     "需要人工复核。",
                     "原因：上一轮分析中该校验项未通过（validation_status="
                     f"{prior.get('validation_status', 'unknown')}），"
                     "属于强制复核条件；该判断来自状态机而不是模型主观意见。",
                     "已按上一轮结论创建复核请求（pending）；建议岗位由服务端按原因映射，"
                     "不由模型指定。",
                     "说明：复核请求 pending ≠ 人工已完成审核，本回答不构成最终结论。"]
            return "\n".join(lines)

        # ---- 设备归属 / 计量关系 ----
        if self.intent == "ownership":
            rel = self.last_relationship or {}
            lines = [head, "",
                     f"父表：{rel.get('parent_meter') or '无'}；"
                     f"子表：{'、'.join(rel.get('child_meters') or []) or '无'}。",
                     "设备台账：" + ("数据源未提供" if rel.get("devices") is None
                                else str(rel.get("devices"))),
                     "租户归属：" + ("数据源未提供" if rel.get("tenant") is None
                                else str(rel.get("tenant")))]
            lines.append("按知识库的归属规则：归属不明时先加装分表或按合同比例过渡，"
                         "不允许凭经验分摊；因此本例必须提交人工核实。")
            if self.state.review_status == "pending":
                lines.append("已提交人工复核（建议岗位：租户运营代表）；"
                             "该请求为 pending，不代表已完成确认。")
            return "\n".join(lines)

        if self.intent == "meter_question":
            lines = [head, ""]
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

        # ---- 分析类：校验已发现倍率/配置问题时，解释可能原因（不升级为结论）----
        body = super()._offline_answer()
        if self._multiplier_issue():
            body += ("\n\n⑧ 可能原因（待核实）："
                     "配置录入或变更单错误、计量口径变化、真实设备故障三类；"
                     "数据源未提供变更工单与设备台账，尚不能区分，需人工核查。")
        return body

    def _multiplier_issue(self) -> bool:
        checks = {i.get("check") for i in (self.last_validate or {}).get("issues", [])}
        return bool(checks & {"multiplier_consistency", "config_version_conflict"})

    def _offline_answer(self) -> str:
        """对外答案 = 首行标注 + （违规意图时）拒绝段 + 场景正文。"""
        body = self._answer_body()
        refusal = self._refusal_block()
        if not refusal:
            return body
        lines = body.split("\n")
        head, rest = lines[0], lines[1:]
        out = [head, ""] + refusal + [""] + rest
        text = "\n".join(out)
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip("\n")

    def _clarify_answer(self, reason: str) -> str:
        """指代有歧义时的反问话术：不调用任何工具，不猜参数。"""
        return "\n".join([
            "【需要补充信息】",
            "",
            f"{reason}。",
            "为避免把不同电表/楼栋的数据混在一起，我不会自行假定对象。",
            "请补充其一：电表编号（如 M-002）、楼栋（如 B1 楼）或具体时间范围。",
            "",
            "本次未调用任何工具，也未给出任何结论。",
        ])

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
        # ---- 多轮上下文：只解析参数，不改门禁 ----
        self.conv = await ConversationContext.load(self.state.session_id, self.cfg,
                                                  self.question)
        self.resolution = self.conv.resolve(self.question)
        await self._resolve_scope()

        yield _sse("task", {"task_id": self.state.task_id, **self.state.snapshot(),
                            "engine": "harness", "max_rounds": MAX_DECISION_ROUNDS})
        yield _sse("status", {"stage": "understanding", "text": "Harness 正在规划…"})

        # ---- 指代有歧义：反问，不调工具、不猜参数 ----
        if self.resolution is not None and self.resolution.ambiguous:
            answer = self._clarify_answer(self.resolution.ambiguous)
            yield _sse("status", {"stage": "clarify",
                                  "text": "指代不明确，先向用户确认对象"})
            for i in range(0, len(answer), 40):
                await emit("token", {"delta": answer[i:i + 40]})
            for frame in drain():
                yield frame
            yield _sse("citations", {"items": []})
            self.state.finish("completed", {"clarification": self.resolution.ambiguous})
            await self._persist_task()
            yield _sse("done", {
                "task_id": self.state.task_id, "engine": "harness",
                "status": self.state.status, "state": self.state.snapshot(),
                "decision_rounds": 0, "max_decision_rounds": MAX_DECISION_ROUNDS,
                "decision_trace": [], "tool_calls": 0, "tools": [],
                "validation_status": self.state.validation_status,
                "review_required": self.state.review_required,
                "review_status": self.state.review_status,
                "report_status": self.state.report_status,
                "number_trace": None, "llm_mode": "offline_planner",
                "unfinished_reason": None, "require_confirmation": self.require_confirmation,
                "clarification": self.resolution.ambiguous,
            })
            return

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
