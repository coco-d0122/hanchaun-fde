# -*- coding: utf-8 -*-
"""
电表数据确定性校验（六项）：完整性 / 时间戳 / 单位 / 倍率一致性 / 版本冲突 / 总分表平衡。

★ 结论由规则程序生成：issue 与 evidence_id 都由本模块签发，模型只能引用不能生成。
★ ctx 只用于读写证据库（鸭子类型），本模块不 import agent 层，避免循环依赖。
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any

from app.energy.data_service import compute_intervals, parse_param_ts, select_window
from app.errors import AgentToolError

if TYPE_CHECKING:  # 仅类型检查时导入：运行时靠鸭子类型，避免 energy ↔ agent 循环依赖
    from app.agent.context import AgentContext


def validate_meter_data(ctx: AgentContext, meter_id: str | None = None,
                        building_id: str | None = None,
                        start_time: str | None = None,
                        end_time: str | None = None) -> dict:
    """
    确定性校验（六项）。所有 issue 都带 evidence_id，编号由校验程序签发，
    模型只能引用不能生成——引用不存在的 ID 会被复核工具拒绝。
    """
    start, end = parse_param_ts(start_time), parse_param_ts(end_time)
    if start is None or end is None:
        raise AgentToolError("PARAM_INVALID", "start_time 与 end_time 必填")
    window, meters = select_window(ctx.cfg, meter_id, building_id, start, end)

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
