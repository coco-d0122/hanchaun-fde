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

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.rules import Reading, compute_intervals, detect_all, load_readings

# ---------------------------------------------------------------- 数据结构


class AgentToolError(Exception):
    """工具层错误：code 进错误结构，message 是给模型/用户看的中文说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


# ---------------------------------------------------------------- 数据集


_DATASET_CACHE: dict[str, list[Reading]] = {}


def load_dataset(cfg: Settings) -> list[Reading]:
    """加载模拟读数（异常集优先：它包含倍率变更，是分析场景的真实数据）。"""
    key = str(cfg.data_path)
    if key in _DATASET_CACHE:
        return _DATASET_CACHE[key]
    # ★ 只取一份数据集：两份文件覆盖同一批电表与时间窗，合并会造成同一时刻两条读数，
    #   差分后电量翻倍、倍率"逐点变化"（实测踩过）。异常集含倍率变更，优先使用。
    candidates = [Path(cfg.anomaly_dataset), Path(cfg.normal_dataset)]
    readings: list[Reading] = []
    for path in candidates:
        if path.exists():
            readings = load_readings(path)
            break
    if not readings:
        raise AgentToolError("DATASET_MISSING", "未找到任何电表读数数据文件")
    _DATASET_CACHE[key] = readings
    return readings


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.replace("T", " ")[:19]
                                     if len(value) >= 19 else value, fmt)
        except ValueError:
            continue
    raise AgentToolError("PARAM_INVALID", f"时间格式无法识别：{value}（示例：2026-10-23 00:00）")


def _select(ctx: AgentContext, meter_id: str | None, building_id: str | None,
            start: datetime, end: datetime) -> tuple[list[Reading], list[str]]:
    """按电表/楼栋与时间窗筛选读数。返回 (读数, 命中的电表编号列表)。"""
    if end < start:
        raise AgentToolError("PARAM_INVALID", "结束时间早于起始时间")
    if (end - start).days > 7:
        raise AgentToolError("TIME_RANGE_TOO_LARGE", "单次查询时间范围不得超过 7 天")
    if not meter_id and not building_id:
        raise AgentToolError("PARAM_INVALID", "meter_id 与 building_id 至少提供一项")

    readings = load_dataset(ctx.cfg)
    if meter_id:
        picked = [r for r in readings if r.meter_id == meter_id]
        if not picked:
            raise AgentToolError("METER_NOT_FOUND", f"未找到电表 {meter_id} 的读数数据")
    else:
        picked = [r for r in readings if r.building_id == building_id]
        if not picked:
            raise AgentToolError("METER_NOT_FOUND", f"楼栋 {building_id} 下没有读数数据")

    window = [r for r in picked if start <= r.ts <= end]
    if not window:
        raise AgentToolError("NO_DATA_IN_RANGE",
                             f"所选时间范围内没有读数（数据时间窗："
                             f"{min(r.ts for r in picked)} ~ {max(r.ts for r in picked)}）")
    meters = sorted({r.meter_id for r in window})
    return window, meters


def _energy_by_meter(window: list[Reading],
                     granularity: str) -> dict[str, list[dict[str, Any]]]:
    """按粒度聚合电量（差分后再乘倍率，复用阶段3 compute_intervals）。"""
    intervals = compute_intervals(window)
    out: dict[str, list[dict[str, Any]]] = {}
    for meter, items in intervals.items():
        buckets: dict[str, float] = {}
        for it in items:
            if granularity == "hour":
                key = it.ts.strftime("%Y-%m-%d %H:00")
            elif granularity == "day":
                key = it.ts.strftime("%Y-%m-%d")
            else:
                key = it.ts.strftime("%Y-%m-%d %H:%M")
            buckets[key] = buckets.get(key, 0.0) + it.energy_kwh
        out[meter] = [{"time": k, "energy_kwh": round(v, 3)}
                      for k, v in sorted(buckets.items())]
    return out


# ---------------------------------------------------------------- 工具 1

def query_energy_data(ctx: AgentContext, meter_id: str | None = None,
                      building_id: str | None = None, start_time: str | None = None,
                      end_time: str | None = None, granularity: str = "15min") -> dict:
    """查能耗数据：返回每块表的点数、电量合计、缺失点、前若干点明细与来源。"""
    start, end = _parse_ts(start_time), _parse_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = _select(ctx, meter_id, building_id, start, end)

    granularity = granularity if granularity in ("15min", "hour", "day") else "15min"
    energy = _energy_by_meter(window, granularity)

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

    target = _parse_ts(target_time) if target_time else None
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

def validate_meter_data(ctx: AgentContext, meter_id: str | None = None,
                        building_id: str | None = None,
                        start_time: str | None = None,
                        end_time: str | None = None) -> dict:
    """
    确定性校验（六项）。所有 issue 都带 evidence_id，编号由校验程序签发，
    模型只能引用不能生成——引用不存在的 ID 会被复核工具拒绝。
    """
    start, end = _parse_ts(start_time), _parse_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = _select(ctx, meter_id, building_id, start, end)

    issues: list[dict[str, Any]] = []
    checked = 0

    for mid in meters:
        rows = sorted([r for r in window if r.meter_id == mid], key=lambda r: r.ts)

        # ① 完整性：15 分钟间隔是否连续
        checked += 1
        gaps = [(b.ts - a.ts).total_seconds() / 60 for a, b in zip(rows, rows[1:])]
        abnormal_gap = [g for g in gaps if g > 15.0001]
        if abnormal_gap:
            ev = ctx.add_evidence("validation", mid,
                                  f"{mid} 存在 {len(abnormal_gap)} 处 >15 分钟的时间缺口",
                                  abnormal_gap)
            issues.append({"check": "data_completeness", "severity": "high",
                           "evidence_id": ev, "need_human_check": True,
                           "detail": f"时间间隔异常 {len(abnormal_gap)} 处，最大 "
                                     f"{max(abnormal_gap):.0f} 分钟"})

        # ② 时间戳单调性
        checked += 1
        if any(b.ts <= a.ts for a, b in zip(rows, rows[1:])):
            ev = ctx.add_evidence("validation", mid, f"{mid} 时间戳非单调递增")
            issues.append({"check": "timestamp_order", "severity": "high",
                           "evidence_id": ev, "need_human_check": True,
                           "detail": "存在重复或倒序时间戳"})

        # ③ 单位一致性
        checked += 1
        units = {r.unit for r in rows}
        if len(units) > 1:
            ev = ctx.add_evidence("validation", mid, f"{mid} 单位不一致：{units}")
            issues.append({"check": "unit_consistency", "severity": "medium",
                           "evidence_id": ev, "need_human_check": True,
                           "detail": f"区间内出现多种单位：{'/'.join(sorted(units))}"})

        # ④ 倍率配置一致性（变更前后电量比 vs 倍率比）
        checked += 1
        mults = [r.multiplier for r in rows]
        if len(set(mults)) > 1:
            change_idx = next(i for i, m in enumerate(mults) if m != mults[0])
            change_at = rows[change_idx].ts
            intervals = compute_intervals(rows).get(mid, [])
            before = [i.energy_kwh for i in intervals if i.ts < change_at]
            after = [i.energy_kwh for i in intervals if i.ts >= change_at]
            if before and after:
                r_energy = statistics.median(after) / max(statistics.median(before), 1e-9)
                r_mult = mults[change_idx] / max(mults[0], 1e-9)
                deviation = abs(r_energy - r_mult) / max(r_mult, 1e-9)
                if deviation > 0.05:
                    ev = ctx.add_evidence(
                        "validation", mid,
                        f"{mid} 倍率 {mults[0]:g}→{mults[change_idx]:g}（{r_mult:.1f}×），"
                        f"电量中位数比 {r_energy:.2f}×，偏差 {deviation:.1%}",
                        [round(r_energy, 3), round(r_mult, 3), round(deviation, 4)])
                    issues.append({
                        "check": "multiplier_consistency", "severity": "high",
                        "evidence_id": ev, "need_human_check": True,
                        "detail": (f"倍率变更 {mults[0]:g}→{mults[change_idx]:g}，"
                                   f"电量跳变 {r_energy:.2f}× 与之偏差 {deviation:.1%}，"
                                   f"需核对变更工单与生效时间"),
                    })

        # ⑤ 配置版本冲突（数据源无独立配置表 → 倍率自变即为"无审批依据的变更"）
        checked += 1
        if len(set(mults)) > 1:
            ev = ctx.add_evidence("validation", mid,
                                  f"{mid} 读数中出现 {len(set(mults))} 种倍率，且无配置变更单可比对")
            issues.append({
                "check": "config_version_conflict", "severity": "high",
                "evidence_id": ev, "need_human_check": True,
                "detail": "倍率在同一数据窗内变化，且数据源未提供配置版本变更记录",
            })

    # ⑥ 计量关系完整性：总表 vs 分表电量（仅当窗口内同时含父子表）
    checked += 1
    parent_ids = {r.parent_meter_id for r in window if r.parent_meter_id}
    for parent in sorted(parent_ids):
        if parent not in meters:
            continue
        intervals = compute_intervals(window)
        parent_kwh = sum(i.energy_kwh for i in intervals.get(parent, []))
        child_kwh = sum(i.energy_kwh for m in meters
                        if m != parent and any(r.meter_id == m and r.parent_meter_id == parent
                                               for r in window)
                        for i in intervals.get(m, []))
        if parent_kwh > 0 and child_kwh > 0:
            diff_ratio = abs(parent_kwh - child_kwh) / parent_kwh
            if diff_ratio > 0.10:
                ev = ctx.add_evidence(
                    "validation", parent,
                    f"总表 {parent} {parent_kwh:.1f} kWh 与分表合计 {child_kwh:.1f} kWh "
                    f"偏差 {diff_ratio:.1%}",
                    [round(parent_kwh, 3), round(child_kwh, 3), round(diff_ratio, 4)])
                issues.append({
                    "check": "meter_relationship_balance", "severity": "medium",
                    "evidence_id": ev, "need_human_check": True,
                    "detail": f"总表与分表合计偏差 {diff_ratio:.1%}，超过 10% 阈值",
                })

    passed = not issues
    ctx.add_evidence("validation", ",".join(meters),
                     f"校验 {checked} 项，问题 {len(issues)} 项，结论 {'通过' if passed else '未通过'}")

    return {
        "passed": passed,
        "issues": issues,
        "summary": {"checked": checked, "failed": len(issues)},
        "review_required": any(i.get("need_human_check") for i in issues),
        "_trace": {
            "source": "deterministic_checks", "time_range": [start.isoformat(), end.isoformat()],
            "row_count": len(window), "evidence_ids": ctx.evidence_ids(),
            "note": "校验结论由规则程序生成，模型不得修改",
        },
    }


# ---------------------------------------------------------------- 工具 4

def detect_energy_anomaly(ctx: AgentContext, meter_id: str | None = None,
                          building_id: str | None = None,
                          start_time: str | None = None,
                          end_time: str | None = None) -> dict:
    """执行阶段3规则引擎（R04/R05），结果原样返回，模型不得改数值。"""
    start, end = _parse_ts(start_time), _parse_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = _select(ctx, meter_id, building_id, start, end)

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

    items: list[dict[str, Any]] = []
    for r in results:
        ev_id = ctx.add_evidence("knowledge", r.chunk.source_file,
                                 r.chunk.content[:300])
        items.append({
            "chunk_id": r.chunk.chunk_id,
            "source_file": r.chunk.source_file,
            "section": r.chunk.heading_path if hasattr(r.chunk, "heading_path") else "",
            "excerpt": r.chunk.content[:300],
            "score": {"rrf": round(r.rrf_score, 6), "vector": round(r.vector_score, 4),
                      "bm25": round(r.bm25_score, 3)},
            "evidence_id": ev_id,
        })
    has_evidence = retriever.has_evidence(results, query)
    return {
        "has_evidence": bool(has_evidence),
        "results": items,
        "_trace": {"source": "hybrid_retrieval", "query": query,
                   "row_count": len(items), "evidence_ids": [i["evidence_id"] for i in items]},
    }


# ---------------------------------------------------------------- 工具 6

def generate_anomaly_report(ctx: AgentContext, anomaly_id: str | None = None,
                            focus: str | None = None, *,
                            state: Any = None) -> dict:
    """
    生成报告草稿。status 只可能是 draft / pending_check——枚举里没有 confirmed，
    任何参数都改不成"已确认"。正文数值跑 NumberTrace，不过则整篇退回。
    """
    from app.core.guardrail import number_trace_guard

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

    report_id = f"RPT-{datetime.now():%Y%m%d%H%M%S}"
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

    request_id = f"HR-{datetime.now():%Y%m%d%H%M%S}"
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
