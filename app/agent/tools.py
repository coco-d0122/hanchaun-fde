# -*- coding: utf-8 -*-
"""
Agent 原子工具实现（阶段6 方案第四节）

七个工具全部是"单一查/算/校验动作"，不含任何写业务状态的路径：
  query_energy_data / get_meter_config / validate_meter_data / detect_energy_anomaly /
  search_energy_knowledge / generate_anomaly_report / request_human_review

约定：
  - 所有输出必含 "_trace"（来源、时间范围、行数、证据编号）
  - 所有失败抛 AgentToolError(code, message)，由执行器包成
    {"error": {"code": ..., "message": ...}} 回给模型——模型无法伪造这个结构
  - 所有函数同步执行，由执行器放进线程池（不阻塞事件循环）

数据来源：backend/data/ 下两份模拟数据集（试点阶段无真实计量系统）。
  - 事实类字段（倍率、读数、父子关系）全部来自数据文件
  - 数据文件里没有的字段（关联设备、归属租户）返回 null 并在 _trace 注明"数据源未提供"，
    绝不编造——这是"AI 只读数据边界"在工具层的落地
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.energy.anomaly_detector import detect_all
from app.energy.data_service import (
    compute_intervals,
    energy_by_meter,
    load_dataset,
    parse_param_ts,
    select_window,
)
from app.energy.meter_validator import validate_meter_data
from app.energy.report_service import generate_anomaly_report
from app.errors import AgentToolError

# ---------------------------------------------------------------- 数据结构


@dataclass
class AgentContext:
    """一次任务执行期共享的上下文（证据库、配置、检索器）。"""

    cfg: Settings
    retriever: Any = None
    user_id: int | None = None
    evidence: list[dict] = field(default_factory=list)   # [{id, source_type, source_file, excerpt, values}]
    _seq: int = 0

    def next_evidence_id(self) -> str:
        self._seq += 1
        return f"EV-{self._seq:04d}"

    def add_evidence(self, source_type: str, source_file: str, excerpt: str,
                     values: list[float] | None = None) -> str:
        ev_id = self.next_evidence_id()
        self.evidence.append({
            "id": ev_id, "source_type": source_type, "source_file": source_file,
            "excerpt": excerpt[:300], "values": values or [],
        })
        return ev_id

    def evidence_ids(self) -> list[str]:
        return [e["id"] for e in self.evidence]

    def evidence_text(self) -> str:
        """数值溯源的"允许数值集合"：证据里出现过的所有数值 + 原文。"""
        parts: list[str] = []
        for e in self.evidence:
            parts.append(e["excerpt"])
            parts.extend(str(v) for v in e["values"])
        return "\n".join(parts)


# ---------------------------------------------------------------- 工具 1

def query_energy_data(ctx: AgentContext, meter_id: str | None = None,
                      building_id: str | None = None, start_time: str | None = None,
                      end_time: str | None = None, granularity: str = "15min") -> dict:
    """查能耗数据：返回每块表的点数、电量合计、缺失点、前若干点明细与来源。"""
    start, end = parse_param_ts(start_time), parse_param_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = select_window(ctx.cfg, meter_id, building_id, start, end)

    granularity = granularity if granularity in ("15min", "hour", "day") else "15min"
    energy = energy_by_meter(window, granularity)

    result_meters: list[dict[str, Any]] = []
    total = 0.0
    for mid in meters:
        points = sorted([r for r in window if r.meter_id == mid], key=lambda r: r.ts)
        series = energy.get(mid, [])
        kwh = sum(x["energy_kwh"] for x in series)
        total += kwh
        span_minutes = (points[-1].ts - points[0].ts).total_seconds() / 60
        expected = int(span_minutes // 15) if span_minutes > 0 else len(points) - 1
        result_meters.append({
            "meter_id": mid,
            "points": len(points),
            "granularity": granularity,
            "energy_kwh_total": round(kwh, 3),
            "missing_points": max(0, expected - (len(points) - 1)),
            "multipliers_seen": sorted({r.multiplier for r in points}),
            "series": series[:50],      # 截断：模型看趋势，不看全量
        })

    ev = ctx.add_evidence(
        "energy_data", ",".join(meters),
        f"{start:%Y-%m-%d %H:%M}~{end:%Y-%m-%d %H:%M} 电量合计 {round(total, 3)} kWh",
        [round(total, 3)] + [m["energy_kwh_total"] for m in result_meters])

    return {
        "meters": result_meters,
        "total_energy_kwh": round(total, 3),
        "_trace": {
            "source": "energy_readings", "time_range": [start.isoformat(), end.isoformat()],
            "row_count": len(window), "granularity": granularity,
            "evidence_ids": [ev], "note": "电量=相邻示值差×当区间倍率（阶段3规则引擎同一算法）",
        },
    }


# ---------------------------------------------------------------- 工具 2

def get_meter_config(ctx: AgentContext, meter_id: str | None = None,
                     target_time: str | None = None,
                     include_topology: bool = True) -> dict:
    """查计量配置与拓扑：从读数中还原倍率版本史，给出父子关系与冲突信号。"""
    if not meter_id:
        raise AgentToolError("PARAM_INVALID", "meter_id 必填")
    readings = load_dataset(ctx.cfg)
    rows = sorted([r for r in readings if r.meter_id == meter_id], key=lambda r: r.ts)
    if not rows:
        raise AgentToolError("METER_NOT_FOUND", f"未找到电表 {meter_id}")

    # ---- 版本史：倍率值变化即视为一次配置变更（数据源未提供独立配置表）----
    history: list[dict[str, Any]] = []
    prev_mult: float | None = None
    for r in rows:
        if prev_mult is None or r.multiplier != prev_mult:
            history.append({
                "version": len(history) + 1,
                "multiplier": r.multiplier,
                "unit": r.unit,
                "effective_from": r.ts.isoformat(),
                "effective_to": None,
            })
            if len(history) > 1:
                history[-2]["effective_to"] = r.ts.isoformat()
            prev_mult = r.multiplier

    target = parse_param_ts(target_time) if target_time else None
    current = history[-1] if history else None
    if target is not None:
        for h in history:
            start = datetime.fromisoformat(h["effective_from"])
            end = datetime.fromisoformat(h["effective_to"]) if h["effective_to"] else None
            if start <= target and (end is None or target < end):
                current = h
                break

    # ---- 冲突信号：倍率变更点与电量跳变点是否吻合（只给事实，判定交给 validate）----
    conflicts: list[dict[str, Any]] = []
    if len(history) > 1:
        change_at = datetime.fromisoformat(history[-1]["effective_from"])
        intervals = compute_intervals(rows).get(meter_id, [])
        before = [i.energy_kwh for i in intervals if i.ts < change_at]
        after = [i.energy_kwh for i in intervals if i.ts >= change_at]
        if before and after and len(before) >= 4 and len(after) >= 4:
            ratio_energy = statistics.median(after) / max(statistics.median(before), 1e-9)
            ratio_mult = history[-1]["multiplier"] / max(history[-2]["multiplier"], 1e-9)
            conflicts.append({
                "type": "effective_time_mismatch",
                "detail": (f"倍率 {history[-2]['multiplier']:g}→{history[-1]['multiplier']:g} "
                           f"({ratio_mult:.1f}×)，变更前后电量中位数比 {ratio_energy:.2f}×"),
                "energy_ratio": round(ratio_energy, 3),
                "multiplier_ratio": round(ratio_mult, 3),
                "change_at": change_at.isoformat(),
            })
        # 数据源未提供配置变更单/工单号 → 无法确认变更是否有审批
        conflicts.append({
            "type": "no_change_ticket",
            "detail": "数据源未提供计量配置变更单，无法确认本次倍率变更是否经过审批",
        })

    topology: dict[str, Any] | None = None
    if include_topology:
        parent = rows[0].parent_meter_id or None
        children = sorted({r.meter_id for r in readings if r.parent_meter_id == meter_id})
        topology = {
            "parent": parent,
            "children": children,
            "devices": None,      # 数据源未提供设备台账
            "tenant": None,       # 数据源未提供租户归属
            "binding_version": 1,
            "note": "关联设备与归属租户当前数据源未提供（None ≠ 无归属）",
        }

    ev = ctx.add_evidence(
        "meter_config", meter_id,
        f"{meter_id} 倍率版本 {len(history)} 个，当前 {current['multiplier'] if current else '?'}",
        [h["multiplier"] for h in history])

    return {
        "meter_id": meter_id,
        "unit": rows[-1].unit,
        "current": current,
        "history": history,
        "topology": topology,
        "conflicts": conflicts,
        "has_conflict": bool(conflicts),
        "_trace": {
            "source": "energy_readings（倍率列）", "config_table": "数据源未提供独立配置表",
            "row_count": len(rows), "evidence_ids": [ev],
        },
    }


# ---------------------------------------------------------------- 工具 3

# ---------------------------------------------------------------- 工具 4

def detect_energy_anomaly(ctx: AgentContext, meter_id: str | None = None,
                          building_id: str | None = None,
                          start_time: str | None = None,
                          end_time: str | None = None) -> dict:
    """执行阶段3规则引擎（R04/R05），结果原样返回，模型不得改数值。"""
    start, end = parse_param_ts(start_time), parse_param_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = select_window(ctx.cfg, meter_id, building_id, start, end)

    intervals = compute_intervals(window)
    if meter_id:
        intervals = {meter_id: intervals.get(meter_id, [])}
    events = detect_all(ctx.cfg, intervals)

    out: list[dict[str, Any]] = []
    for ev in events:
        ev_id = ctx.add_evidence("rule_evidence", ev.event_id,
                                 f"{ev.title}：{ev.summary}", list(ev.metrics.values()))
        out.append({
            "anomaly_id": ev.event_id, "rule_code": ev.rule_code,
            "meter_id": ev.meter_id, "title": ev.title, "summary": ev.summary,
            "severity": ev.severity,
            "metrics": ev.metrics, "evidence_ids": [ev_id],
        })

    return {
        "events": out,
        "from_version": "rules_v1（阶段3 RuleA/RuleB 原样）",
        "_trace": {"source": "rules.detect_all", "meters": meters,
                   "time_range": [start.isoformat(), end.isoformat()],
                   "evidence_ids": ctx.evidence_ids()},
    }


# ---------------------------------------------------------------- 工具 5

def search_energy_knowledge(ctx: AgentContext, query: str | None = None,
                            top_k: int = 3) -> dict:
    """知识库混合检索（BM25+FAISS+RRF），复用阶段3/4 检索器。"""
    if not query or not query.strip():
        raise AgentToolError("PARAM_INVALID", "query 必填")
    retriever = ctx.retriever
    if retriever is None or not getattr(retriever, "chunks", None):
        raise AgentToolError("KB_NOT_READY", "知识库尚未就绪，无法检索")
    top_k = max(1, min(int(top_k or 3), 3))
    results = retriever.retrieve(query, top_k=top_k)
    has_evidence = retriever.has_evidence(results, query)

    items: list[dict[str, Any]] = []
    for r in results:
        # ★ 依据不足时**不登记证据**：证据库是引用与报告的唯一来源，
        #   把"只是长得像"的片段登记成证据，等于给幻觉发了通行证（缺陷 F-01 的第二处落点）。
        ev_id = (ctx.add_evidence("knowledge", r.chunk.source_file, r.chunk.content[:300])
                 if has_evidence else None)
        items.append({
            "chunk_id": r.chunk.chunk_id,
            "source_file": r.chunk.source_file,
            "section": r.chunk.heading_path if hasattr(r.chunk, "heading_path") else "",
            "excerpt": r.chunk.content[:300],
            "score": {"rrf": round(r.rrf_score, 6), "vector": round(r.vector_score, 4),
                      "bm25": round(r.bm25_score, 3)},
            "evidence_id": ev_id,
        })
    return {
        "has_evidence": bool(has_evidence),
        "results": items,
        "note": ("依据充足，片段已登记为证据" if has_evidence else
                 "检索到的片段与问题不匹配（仅命中通用词），未登记为证据，"
                 "不得据此回答，应明确告知知识库中未找到相关规定"),
        "_trace": {"source": "hybrid_retrieval", "query": query,
                   "row_count": len(items),
                   "evidence_ids": [i["evidence_id"] for i in items if i["evidence_id"]]},
    }


# ---------------------------------------------------------------- 工具 6

# ---------------------------------------------------------------- 工具 7

def request_human_review(ctx: AgentContext, reason: str | None = None,
                         related_anomaly_id: str | None = None,
                         evidence_ids: list[str] | None = None,
                         note: str = "") -> dict:
    """
    创建人工复核请求（只增不改）。证据编号必须存在于本任务证据库——
    这是"防模型编造证据编号"的检查点。
    """
    from app.agent.specs import REVIEW_REASON_LABEL, REVIEW_REASON_ROLE

    if reason not in REVIEW_REASON_ROLE:
        raise AgentToolError("PARAM_INVALID", f"未知的复核原因：{reason}")
    known = set(ctx.evidence_ids())
    used = list(evidence_ids or [])
    missing = [e for e in used if e not in known]
    if missing:
        raise AgentToolError("EVIDENCE_NOT_FOUND",
                             f"引用了本任务不存在的证据编号：{missing[:3]}")
    if not used:
        used = ctx.evidence_ids()[-3:]

    from uuid import uuid4
    request_id = f"HR-{datetime.now():%Y%m%d%H%M%S}-{uuid4().hex[:4]}"
    ctx.add_evidence("review_request", request_id,
                     f"复核请求：{REVIEW_REASON_LABEL.get(reason, reason)}，证据 {len(used)} 条")
    return {
        "review_request_id": request_id,
        "status": "pending",
        "reason": reason,
        "reason_label": REVIEW_REASON_LABEL.get(reason, reason),
        "suggested_role": REVIEW_REASON_ROLE[reason],
        "related_anomaly_id": related_anomaly_id,
        "evidence_ids": used,
        "note": note[:300],
        "_trace": {"source": "review_request", "note": "创建请求 ≠ 人工已完成审核",
                   "evidence_ids": used},
    }


# 工具名 → 实现（注册表里没有的工具一律 unknown_tool）
TOOL_IMPLS = {
    "query_energy_data": query_energy_data,
    "get_meter_config": get_meter_config,
    "validate_meter_data": validate_meter_data,
    "detect_energy_anomaly": detect_energy_anomaly,
    "search_energy_knowledge": search_energy_knowledge,
    "generate_anomaly_report": generate_anomaly_report,
    "request_human_review": request_human_review,
}


# ---------------------------------------------------------------- 工具 8（薄封装）

def get_device_relationship(ctx: AgentContext, meter_id: str | None = None,
                            include_devices: bool = True) -> dict:
    """
    查询电表-设备-楼栋关联关系（阶段6 并入 get_meter_config，本轮按需求补独立入口）。

    ★ 同一份事实、同一套溯源：内部直接复用 get_meter_config，不另造口径。
      数据源没有设备台账/租户归属时返回 None 并在 _trace 注明，绝不编造。
    """
    cfg = get_meter_config(ctx, meter_id=meter_id, include_topology=True)
    topo = dict(cfg.get("topology") or {})
    if not include_devices:
        topo.pop("devices", None)
    ev = ctx.add_evidence("device_relationship", meter_id or "",
                          f"父表 {topo.get('parent') or '无'}，子表 "
                          f"{len(topo.get('children') or [])} 个")
    return {
        "meter_id": meter_id,
        "building_id": None,          # 数据源未提供独立楼栋台账
        "parent_meter": topo.get("parent"),
        "child_meters": topo.get("children") or [],
        "devices": topo.get("devices"),
        "tenant": topo.get("tenant"),
        "binding_version": topo.get("binding_version"),
        "ownership_clear": bool(topo.get("parent")),
        "_trace": {"source": "get_meter_config.topology（同一事实来源）",
                   "evidence_ids": [ev],
                   "note": topo.get("note", "")},
    }


# 第 8 个工具（薄封装）在函数定义之后注册，避免模块加载时的前向引用
TOOL_IMPLS["get_device_relationship"] = get_device_relationship
