# -*- coding: utf-8 -*-
"""
工具执行器：五道闸 + 防循环预算 + 调用留痕（阶段6 方案 8.1）

① 身份与权限闸  权限码不在用户集合 → SCOPE_DENIED / FORBIDDEN
② 数据范围闸    scope 列未启用（阶段5 预留）→ 本阶段恒通过，接口保留
③ 前置条件闸    task 状态机检查 → TOOL_PRECONDITION_FAILED + 缺什么
④ 参数校验闸    Pydantic 严格校验（工具内部 raise PARAM_INVALID），无任意 SQL 通道
⑤ 输出闸        _trace 注入、evidence_id 签发、NumberTrace（报告工具内）后才回给模型

模型看不到 DB、拿不到写状态通道：它只能发起 tool_call，状态推进在 apply_result 里。
"""

from __future__ import annotations

import json
import logging
import time

from app.agent.specs import MAX_SAME_TOOL_CALLS, MAX_TOTAL_CALLS, TOOL_NAMES, TOOL_PERMISSION
from app.agent.state import TaskState
from app.agent.tools import TOOL_IMPLS, AgentContext, AgentToolError

logger = logging.getLogger(__name__)


class ToolResult:
    """执行器返回给调用方的一步结果（同时用于 SSE 事件与落库）。"""

    def __init__(self, tool: str, ok: bool, payload: dict, error_code: str | None = None,
                 duration_ms: int = 0, cached: bool = False):
        self.tool = tool
        self.ok = ok
        self.payload = payload
        self.error_code = error_code
        self.duration_ms = duration_ms
        self.cached = cached

    def as_message(self) -> dict:
        """回给模型的 tool 消息内容（错误结构由执行器包裹，模型无法伪造）。"""
        if self.ok:
            return {"role": "tool", "name": self.tool,
                    "content": json.dumps(self.payload, ensure_ascii=False, default=str)}
        return {"role": "tool", "name": self.tool,
                "content": json.dumps({"error": {"code": self.error_code,
                                                 "message": self.payload.get("message", "")}},
                                      ensure_ascii=False)}


class ToolExecutor:
    """一次任务一个执行器实例（持有状态、预算、缓存、证据库）。"""

    def __init__(self, state: TaskState, ctx: AgentContext, permissions: list[str]):
        self.state = state
        self.ctx = ctx
        self.permissions = set(permissions)
        self.same_tool_counts: dict[str, int] = {}
        self.cache: dict[str, ToolResult] = {}
        self.precondition_failures = 0
        self.started = time.monotonic()

    # ---------------- 主入口 ----------------
    async def execute(self, tool: str, args: dict) -> ToolResult:
        import anyio

        t0 = time.perf_counter()

        # 预算闸（先于一切：预算耗尽不再执行任何工具）
        if self.state.tool_call_count >= MAX_TOTAL_CALLS:
            return self._fail(tool, "BUDGET_EXCEEDED",
                              f"任务已达 {MAX_TOTAL_CALLS} 步上限，停止调用工具", t0)
        if time.monotonic() - self.started > 180:
            return self._fail(tool, "TIME_BUDGET_EXCEEDED", "任务已超过 180 秒预算", t0)

        # ① 权限闸
        if tool not in TOOL_NAMES:
            return self._fail(tool, "UNKNOWN_TOOL", f"没有名为 {tool} 的工具（工具集里不存在）", t0)
        need = TOOL_PERMISSION.get(tool, "")
        if need and need not in self.permissions:
            return self._fail(tool, "SCOPE_DENIED",
                              f"当前账号缺少权限码 {need}，拒绝执行 {tool}", t0)

        # ② 数据范围闸（scope 未启用：保留判定位置，日志留痕便于未来收紧）
        #    stage5 的 user_roles.scope_type 恒为 all，本阶段必然通过。

        # ③ 前置条件闸
        ok, why = self.state.check_precondition(tool)
        if not ok:
            self.precondition_failures += 1
            return self._fail(tool, "TOOL_PRECONDITION_FAILED", why, t0)

        # 防循环：同一工具次数
        used = self.same_tool_counts.get(tool, 0)
        if used >= MAX_SAME_TOOL_CALLS:
            return self._fail(tool, "TOOL_CALL_LIMIT",
                              f"{tool} 已调用 {used} 次，超过单工具上限", t0)

        # 防循环：完全相同参数 → 返回缓存并提示
        cache_key = tool + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            cached.cached = True
            return cached

        # ④ 参数校验 + 执行（工具内部 raise PARAM_INVALID；放线程池不卡事件循环）
        impl = TOOL_IMPLS[tool]
        try:
            if tool == "generate_anomaly_report":
                payload = await anyio.to_thread.run_sync(
                    lambda: impl(self.ctx, state=self.state, **args))
            else:
                payload = await anyio.to_thread.run_sync(lambda: impl(self.ctx, **args))
        except AgentToolError as exc:
            return self._fail(tool, exc.code, exc.message, t0)
        except TypeError as exc:
            return self._fail(tool, "PARAM_INVALID", f"参数不合法：{exc}", t0)
        except Exception as exc:  # noqa: BLE001  工具内部异常不应炸掉整个任务
            logger.exception("工具执行异常：%s", tool)
            return self._fail(tool, "TOOL_EXECUTION_FAILED", f"工具执行失败：{exc}", t0)

        # ⑤ 输出闸通过 → 推进状态
        self.same_tool_counts[tool] = used + 1
        self.state.apply_result(tool, payload, self.ctx)
        result = ToolResult(tool, True, payload,
                            duration_ms=int((time.perf_counter() - t0) * 1000))
        self.cache[cache_key] = result
        return result

    # ---------------- 失败构造 ----------------
    def _fail(self, tool: str, code: str, message: str, t0: float) -> ToolResult:
        if code == "TOOL_PRECONDITION_FAILED":
            # 连续撞闸 = 模型陷入错误规划，由循环层止损
            pass
        return ToolResult(tool, False, {"message": message}, error_code=code,
                          duration_ms=int((time.perf_counter() - t0) * 1000))

    # ---------------- 兜底：review_required 但模型没调 HR ----------------
    async def ensure_review_request(self, reason: str, anomaly_id: str | None,
                                    note: str) -> ToolResult | None:
        """
        双保险（方案规则 4）：状态机 review_required=true 却没有任何 HR 请求时，
        由组装器直接补一条——模型忘了也会被程序兜住。
        """
        if not self.state.review_required or self.state.review_status == "pending":
            return None
        return await self.execute("request_human_review",
                                  {"reason": reason,
                                   "related_anomaly_id": anomaly_id,
                                   "evidence_ids": [],
                                   "note": note})
