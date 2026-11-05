# -*- coding: utf-8 -*-
"""
异常分析报告草稿生成（status 只到 draft / pending_check，枚举里没有 confirmed）。

★ 正文数值必须通过 NumberTrace（逐个可溯源到证据），否则整篇退回。
★ 报告只是草稿：提交人工复核 ≠ 完成业务签核，签核通道不在系统内。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.guardrail import number_trace_guard
from app.errors import AgentToolError


def generate_anomaly_report(ctx: AgentContext, anomaly_id: str | None = None,
                            focus: str | None = None, *,
                            state: Any = None) -> dict:
    """
    生成报告草稿。status 只可能是 draft / pending_check——枚举里没有 confirmed，
    任何参数都改不成"已确认"。正文数值跑 NumberTrace，不过则整篇退回。
    """
    evidence = ctx.evidence
    if not evidence:
        raise AgentToolError("TOOL_PRECONDITION_FAILED",
                             "本任务尚无可引用的证据，无法生成报告（请先查询/校验）")

    has_rule = any(e["source_type"] == "rule_evidence" for e in evidence)
    status = "draft" if has_rule else "pending_check"

    sections: dict[str, Any] = {
        "现象": [], "规则检测结果": [], "可能原因": [], "知识依据": [], "建议核查": [],
    }
    for e in evidence:
        if e["source_type"] == "energy_data":
            sections["现象"].append(e["excerpt"])
        elif e["source_type"] == "rule_evidence":
            sections["规则检测结果"].append(e["excerpt"])
        elif e["source_type"] == "validation":
            sections["现象"].append(e["excerpt"])
        elif e["source_type"] == "knowledge":
            sections["知识依据"].append(f"{e['source_file']}：{e['excerpt'][:80]}")
    if focus:
        sections["建议核查"].append(f"用户关注点：{focus}")

    body = "\n".join(
        f"【{k}】\n" + "\n".join(f"- {x}" for x in v)
        for k, v in sections.items() if v)
    trace = number_trace_guard(body, json.dumps(evidence, ensure_ascii=False, default=str), "")
    if not trace["passed"]:
        raise AgentToolError(
            "NUMBER_TRACE_FAILED",
            f"报告正文数值无法在证据中找到来源：{trace['unmatched'][:5]}，已退回")

    report_id = f"RPT-{datetime.now():%Y%m%d%H%M%S}-{uuid4().hex[:4]}"
    ctx.add_evidence("report", report_id, f"报告草稿 {status}，证据 {len(evidence)} 条")
    return {
        "report_id": report_id,
        "status": status,
        "anomaly_id": anomaly_id,
        "sections": sections,
        "number_check": trace,
        "_trace": {"source": "report_builder", "evidence_ids": ctx.evidence_ids(),
                   "status_ceiling": "draft/pending_check（无 confirmed 枚举）"},
    }
