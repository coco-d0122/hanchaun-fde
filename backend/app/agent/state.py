# -*- coding: utf-8 -*-
"""
任务状态机（阶段6 方案 8.2）

状态只由执行器在工具"成功执行后"推进；模型没有写状态的通道。
门禁读的就是这里的字段，所以"用户要求跳过校验"改变不了它——
自然语言绕不过状态机。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class TaskState:
    task_id: str = field(default_factory=lambda: f"TASK-{uuid4().hex[:12]}")
    session_id: str | None = None
    user_id: int | None = None
    intent: str = "anomaly_analysis"
    current_step: str = "init"
    data_queried: bool = False
    config_checked: bool = False
    validation_status: str = "not_run"      # not_run / passed / issues_found
    anomaly_status: str = "not_run"         # not_run / detected / none
    evidence_refs: list[dict] = field(default_factory=list)
    review_required: bool = False
    review_status: str = "none"             # none / pending
    report_status: str = "none"             # none / draft / pending_check
    tool_call_count: int = 0
    status: str = "running"                 # running / completed / budget_exceeded / failed
    result_digest: dict = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ---- 门禁读取 ----
    def check_precondition(self, tool_name: str) -> tuple[bool, str]:
        """返回 (是否满足, 缺什么的中文说明)。"""
        from app.agent.specs import PRECONDITIONS

        need = PRECONDITIONS.get(tool_name, [])
        missing: list[str] = []
        for cond in need:
            if cond == "data_queried" and not self.data_queried:
                missing.append("尚未查询能耗数据（先调 query_energy_data）")
            elif cond == "config_checked" and not self.config_checked:
                missing.append("尚未查询计量配置（先调 get_meter_config）")
            elif cond == "validation_passed" and self.validation_status != "passed":
                missing.append(
                    f"数据校验未通过（当前 validation_status={self.validation_status}），"
                    "不得把检测结果当作已确认异常")
            elif cond == "validation_run" and self.validation_status == "not_run":
                missing.append("尚未执行数据校验（先调 validate_meter_data）")
        return (not missing), "；".join(missing)

    # ---- 工具成功后推进状态 ----
    def apply_result(self, tool_name: str, result: dict, ctx: Any = None) -> None:
        now_step = {
            "query_energy_data": "data_queried",
            "get_meter_config": "config_checked",
            "validate_meter_data": "validated",
            "detect_energy_anomaly": "detected",
            "search_energy_knowledge": "knowledge_searched",
            "generate_anomaly_report": "report_drafted",
            "request_human_review": "review_requested",
        }.get(tool_name, self.current_step)
        self.current_step = now_step
        self.tool_call_count += 1

        if tool_name == "query_energy_data":
            self.data_queried = True
        elif tool_name == "get_meter_config":
            self.config_checked = True
            if result.get("has_conflict"):
                # 版本冲突信号 → 强制需要人工复核（不依赖模型自觉）
                self.review_required = True
        elif tool_name == "validate_meter_data":
            ok = bool(result.get("passed"))
            self.validation_status = "passed" if ok else "issues_found"
            self.review_required = self.review_required or bool(result.get("review_required"))
        elif tool_name == "detect_energy_anomaly":
            self.anomaly_status = "detected" if result.get("events") else "none"
            if result.get("events"):
                self.review_required = True
        elif tool_name == "generate_anomaly_report":
            self.report_status = result.get("status", "draft")
        elif tool_name == "request_human_review":
            self.review_required = True
            self.review_status = "pending"

        if ctx is not None:
            self.evidence_refs = [{"id": e["id"], "source_type": e["source_type"],
                                   "source_file": e["source_file"]} for e in ctx.evidence]

    # ---- 序列化 ----
    def snapshot(self) -> dict:
        return {
            "task_id": self.task_id, "current_step": self.current_step,
            "data_queried": self.data_queried, "config_checked": self.config_checked,
            "validation_status": self.validation_status,
            "anomaly_status": self.anomaly_status,
            "review_required": self.review_required, "review_status": self.review_status,
            "report_status": self.report_status,
            "tool_call_count": self.tool_call_count, "status": self.status,
            "evidence_count": len(self.evidence_refs),
        }

    def finish(self, status: str, digest: dict | None = None) -> None:
        self.status = status
        # 合并而不是覆盖：final_answer 已写入的 number_trace 等摘要必须保留
        self.result_digest = {**self.result_digest, **(digest or {})}
