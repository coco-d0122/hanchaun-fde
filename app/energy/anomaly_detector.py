# -*- coding: utf-8 -*-
"""
异常检测规则：倍率变化待核查（规则A）、总分表平衡差（规则B）。

★ 规则引擎是系统的确定性基座：结论只能由本模块计算得出。
★ 倍率变化 ≠ 计量错误：规则只产出"待核查"事件，是否错误必须人工判定。
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from app.core.config import Settings
from app.energy.data_service import Interval

logger = logging.getLogger(__name__)


# ==============================================================================

@dataclass
class AnomalyEvent:
    """一条异常事件。字段设计与库表 anomaly_events 一一对应，便于持久化。"""

    event_id: str
    rule_code: str
    event_type: str            # DATA_QUALITY / METERING / CONSUMPTION
    severity: str              # low / medium / high
    title: str
    building_id: str
    meter_id: str
    window_start: datetime
    window_end: datetime
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)    # 全部由 Python 计算得出
    suspects: list[str] = field(default_factory=list)        # 疑似关联表
    evidence: dict[str, Any] = field(default_factory=dict)   # 原始数据快照
    suggestions: list[str] = field(default_factory=list)
    need_review: bool = True

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.strftime("%Y-%m-%d %H:%M")
        d["window_end"] = self.window_end.strftime("%Y-%m-%d %H:%M")
        return d


class RuleA_RatioChange:
    """
    规则 A：电表倍率变化待核查

    ★ 重要设计立场（写在代码里，防止后续维护者误改）：
      倍率变化**不等于**数据错误。换表、增容、CT 更换、产权变更都可能合法地改倍率。
      所以本规则只产出"待核查（METERING / medium）"级别的事件，
      而不是"计量错误"。是否错误，必须由计量工程师调阅配置历史与工单后判定。

    判定逻辑（全部为确定性计算）：
      1. 逐表比较相邻两个区间的 multiplier
      2. 变化倍数 factor = 新/旧，若 factor ≥ 阈值 或 ≤ 1/阈值，则触发
      3. 同时计算"倍率变更前后该表电量的中位数比值"，作为旁证
      4. 严重度按变化倍数分级
    """

    code = "R04_RATIO_CHANGE"
    event_type = "METERING"
    version = 1

    def __init__(self, cfg: Settings):
        self.cfg = cfg

    def run(self, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
        events: list[AnomalyEvent] = []
        for meter_id, items in intervals.items():
            for i in range(1, len(items)):
                prev, cur = items[i - 1], items[i]
                if abs(cur.multiplier - prev.multiplier) < 1e-9:
                    continue
                if prev.multiplier <= 0:
                    continue

                factor = cur.multiplier / prev.multiplier
                if factor < self.cfg.ratio_change_factor and factor > 1 / self.cfg.ratio_change_factor:
                    continue

                # ---- 旁证：变更前后各取 4 个区间的电量中位数 ----
                before = [x.energy_kwh for x in items[max(0, i - 4):i]]
                after = [x.energy_kwh for x in items[i:i + 4]]
                med_before = float(np.median(before)) if before else 0.0
                med_after = float(np.median(after)) if after else 0.0
                energy_ratio = (med_after / med_before) if med_before > 0 else float("nan")

                # 严重度：倍数越大越严重
                if factor >= 5 or factor <= 0.2:
                    severity = "high"
                elif factor >= 3 or factor <= 1 / 3:
                    severity = "medium"
                else:
                    severity = "low"

                # 电量比值与倍率比值是否吻合（吻合 → 更可能是配置问题）
                consistent = (
                    not math.isnan(energy_ratio)
                    and abs(energy_ratio - factor) / factor < 0.25
                )

                suggestions = [
                    f"调阅电表 {meter_id} 的计量配置变更记录（METER_CONFIG_HISTORY），确认是否有对应的换表/增容工程单",
                    f"核对现场铭牌与 CT 变比：系统记录新倍率 {cur.multiplier:g}，旧倍率 {prev.multiplier:g}",
                    "确认倍率生效时间是否与实际投运时间一致（是否存在录入延迟或提前生效）",
                ]
                if consistent:
                    suggestions.insert(0, "变更前后的电量中位数比值与倍率比值基本吻合，优先排查倍率录入/生效时间问题")
                else:
                    suggestions.insert(0, "电量变化幅度与倍率变化不吻合，需同时排除负荷真实变化与换表清零")

                events.append(AnomalyEvent(
                    event_id="",   # 由 detect_all 统一编号
                    rule_code=self.code,
                    event_type=self.event_type,
                    severity=severity,
                    title=f"电表 {meter_id} 倍率变化待核查（{prev.multiplier:g} → {cur.multiplier:g}）",
                    building_id=cur.building_id,
                    meter_id=meter_id,
                    window_start=prev.ts,
                    window_end=cur.ts,
                    summary=(
                        f"自 {cur.ts:%Y-%m-%d %H:%M} 的读数起，电表 {meter_id} 的倍率由 "
                        f"{prev.multiplier:g} 变为 {cur.multiplier:g}（{factor:.1f} 倍）。"
                        f"倍率变化本身不构成异常，需核对配置历史确认是否为合法变更。"
                    ),
                    metrics={
                        "旧倍率": prev.multiplier,
                        "新倍率": cur.multiplier,
                        "变化倍数": round(factor, 3),
                        "变更前电量中位数_kWh": round(med_before, 3),
                        "变更后电量中位数_kWh": round(med_after, 3),
                        "电量比值": None if math.isnan(energy_ratio) else round(energy_ratio, 3),
                        "电量与倍率是否吻合": consistent,
                        "单位": cur.unit,
                    },
                    evidence={
                        "变更时刻": cur.ts.strftime("%Y-%m-%d %H:%M"),
                        "变更前区间": [{"时间": x.ts.strftime("%m-%d %H:%M"), "电量kWh": x.energy_kwh,
                                    "倍率": x.multiplier} for x in items[max(0, i - 4):i]],
                        "变更后区间": [{"时间": x.ts.strftime("%m-%d %H:%M"), "电量kWh": x.energy_kwh,
                                    "倍率": x.multiplier} for x in items[i:i + 4]],
                    },
                    suggestions=suggestions,
                    need_review=True,
                ))
        return events


class RuleB_BalanceDiff:
    """
    规则 B：总表与分表数据差异（母线平衡校验）

    判定逻辑：
      1. 按 parent_meter_id 建立"父表 → 直接子表"映射（只取直接子表，避免多级重复累加）
      2. 按时间桶（默认 1 小时）汇总父表电量与子表电量和
      3. 差异率 = (父表电量 − Σ子表电量) / 父表电量
      4. |差异率| > 阈值 且 父表电量 ≥ 最小电量 → 触发
      5. 把连续的超阈值时间桶合并为一个事件（否则一天 24 个桶会刷出 24 条重复告警）

    差异方向的含义（写清楚，避免误读）：
      正差异（父 > 子）：有负荷未被分表覆盖 —— 私接、漏装表、分表计量偏低、线路损耗偏大
      负差异（子 > 父）：分表合计超过总表 —— 倍率配置错误、重复计量、串表、抄表口径错
    """

    code = "R05_BALANCE_DIFF"
    event_type = "METERING"
    version = 1
    BUCKET_MINUTES = 60

    def __init__(self, cfg: Settings):
        self.cfg = cfg

    def _bucket(self, ts: datetime) -> datetime:
        """把时间归到小时桶（00:00、01:00 …），用于跨表对齐。"""
        return ts.replace(minute=0, second=0, microsecond=0)

    def run(self, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
        # ---- 1) 建立父子关系 ----
        children: dict[str, list[str]] = {}
        for meter_id, items in intervals.items():
            parent = next((x.parent_meter_id for x in items if x.parent_meter_id), None)
            if parent:
                children.setdefault(parent, []).append(meter_id)

        if not children:
            return []

        # ---- 2) 按 (表, 小时桶) 汇总电量 ----
        def bucket_sum(meter_id: str) -> dict[datetime, float]:
            acc: dict[datetime, float] = {}
            for x in intervals.get(meter_id, []):
                if "rollback" in x.flags:
                    continue                      # 表底回退的数据不参与平衡计算
                b = self._bucket(x.ts)
                acc[b] = acc.get(b, 0.0) + x.energy_kwh
            return acc

        events: list[AnomalyEvent] = []

        for parent_id, child_ids in children.items():
            parent_buckets = bucket_sum(parent_id)
            child_buckets = {cid: bucket_sum(cid) for cid in child_ids}
            all_buckets = sorted(parent_buckets)

            # ---- 3) 逐桶计算差异 ----
            deviations: list[dict[str, Any]] = []
            for b in all_buckets:
                p = parent_buckets.get(b, 0.0)
                c = sum(cb.get(b, 0.0) for cb in child_buckets.values())
                if p < self.cfg.balance_min_energy_kwh:
                    continue                      # 电量太小，差异率没有统计意义
                rate = (p - c) / p
                deviations.append({
                    "bucket": b, "parent": round(p, 3), "children": round(c, 3),
                    "rate": rate, "over": abs(rate) > self.cfg.balance_diff_threshold,
                })

            if not deviations:
                continue

            # ---- 4) 连续超阈值桶合并为一个事件 ----
            runs: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            step = timedelta(minutes=self.BUCKET_MINUTES)
            for d in deviations:
                if not d["over"]:
                    if current:
                        runs.append(current)
                        current = []
                    continue
                # 方向必须一致才合并（正差异与负差异是两类问题）
                if current and (d["rate"] > 0) != (current[0]["rate"] > 0):
                    runs.append(current)
                    current = []
                if current and d["bucket"] - current[-1]["bucket"] > step:
                    runs.append(current)
                    current = []
                current.append(d)
            if current:
                runs.append(current)

            # ---- 5) 为每个 run 生成事件 ----
            for run in runs:
                events.append(self._make_event(parent_id, child_ids, child_buckets, run,
                                               intervals.get(parent_id, [])))

        return events

    def _make_event(self, parent_id: str, child_ids: list[str],
                    child_buckets: dict[str, dict[datetime, float]],
                    run: list[dict[str, Any]],
                    parent_intervals: list[Interval]) -> AnomalyEvent:
        """把一段连续的平衡偏差整理成结构化事件，并找出最可疑的分表。"""
        total_parent = sum(d["parent"] for d in run)
        total_child = sum(d["children"] for d in run)
        rate = (total_parent - total_child) / total_parent if total_parent else 0.0
        positive = rate > 0
        building = next((x.building_id for x in parent_intervals if x.building_id), "")

        # ---- 找可疑表：对比"本窗口电量"与该表在数据集中的中位数 ----
        # 思路：平衡差总得由某几块表解释。哪块表本窗口电量明显偏离它自己的常态，就先查它。
        suspects: list[tuple[str, float]] = []
        for cid in child_ids:
            cb = child_buckets[cid]
            win = sum(cb.get(d["bucket"], 0.0) for d in run)
            base_vals = [v for v in cb.values() if v > 0]
            if not base_vals:
                continue
            base = float(np.median(base_vals))
            if base <= 0:
                continue
            suspects.append((cid, win / (base * len(run))))
        suspects.sort(key=lambda x: -x[1])
        top_suspects = [s for s in suspects[:2]]
        suspect_desc = "、".join(f"{cid}（本窗口电量为自身常态的 {ratio:.1f} 倍）"
                                 for cid, ratio in top_suspects)

        direction = "总表电量大于分表合计" if positive else "分表合计大于总表电量"
        if positive:
            causes = [
                "存在未被分表覆盖的负荷（私接、漏装计量点、临时用电未计量）",
                "分表计量偏低（CT 二次侧接触不良、表计精度漂移、倍率配置偏小）",
                "线路损耗与变压器损耗超出经验值（本原型按 1.5% 模拟正常损耗）",
                "分表在该时段存在数据缺失（缺数会被计入差异，需先排除数据质量问题）",
            ]
        else:
            causes = [
                "分表倍率配置偏高（同一负荷被放大计量）",
                "同一回路被两块表重复计量（串表/重复接入）",
                "分表与总表的统计口径不一致（如一方含无功、或计量时段错位）",
                "分表覆盖了总表计量范围之外的负荷（接线越界）",
            ]

        return AnomalyEvent(
            event_id="",
            rule_code=self.code,
            event_type=self.event_type,
            severity="high" if abs(rate) >= 0.10 else "medium",
            title=f"{parent_id} 总表与分表合计差异 {rate * 100:+.1f}%",
            building_id=building,
            meter_id=parent_id,
            window_start=run[0]["bucket"],
            window_end=run[-1]["bucket"] + timedelta(minutes=self.BUCKET_MINUTES),
            summary=(
                f"在 {run[0]['bucket']:%m-%d %H:%M} 至 "
                f"{(run[-1]['bucket'] + timedelta(minutes=self.BUCKET_MINUTES)):%m-%d %H:%M} 的 "
                f"{len(run)} 个小时区间内，总表 {parent_id} 与 {len(child_ids)} 块直接分表的电量合计"
                f"持续存在差异，累计差异率 {rate * 100:+.1f}%（阈值 ±{self.cfg.balance_diff_threshold * 100:.0f}%）。"
                f"方向：{direction}。"
            ),
            metrics={
                "总表电量_kWh": round(total_parent, 3),
                "分表合计_kWh": round(total_child, 3),
                "差异电量_kWh": round(total_parent - total_child, 3),
                "差异率": round(rate, 4),
                "差异方向": direction,
                "阈值": self.cfg.balance_diff_threshold,
                "超阈值小时数": len(run),
            },
            suspects=[s[0] for s in top_suspects],
            evidence={
                "总表": parent_id,
                "直接分表": child_ids,
                "逐小时明细": [
                    {"时间": d["bucket"].strftime("%m-%d %H:00"),
                     "总表kWh": d["parent"], "分表合计kWh": d["children"],
                     "差异率": f"{d['rate'] * 100:+.1f}%"}
                    for d in run
                ],
                "分表偏离度": {cid: round(r, 2) for cid, r in suspects},
                "可疑分表说明": suspect_desc or "未识别出明显偏离的分表",
            },
            suggestions=[
                f"优先核查 {suspect_desc}" if suspect_desc else "逐块核对该总表下各分表的计量配置与现场接线",
                "先排除数据质量问题（分表缺数、表底回退），再判断是否为真实计量偏差",
                "调阅该时段的用电负荷记录与检修/试机记录，确认是否存在未纳入计量的临时负荷",
                "核对总表与分表的统计时段是否严格对齐（分表冻结时间不同会造成系统性错位）",
            ] + causes,
            need_review=True,
        )


def detect_all(cfg: Settings, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
    """
    执行全部规则并统一编号。

    编号规则：AN-{日期}-{序号}，便于人工沟通与检索。
    """
    rules = [RuleA_RatioChange(cfg), RuleB_BalanceDiff(cfg)]
    events: list[AnomalyEvent] = []
    for rule in rules:
        try:
            events.extend(rule.run(intervals))
        except Exception as exc:  # noqa: BLE001
            # 单条规则失败不应导致整个检测中断——这是"故障隔离"的基本要求
            logger.warning("规则 %s 执行失败：%s", getattr(rule, "code", rule.__class__.__name__), exc)
    events.sort(key=lambda e: (e.window_start, e.rule_code))

    day = events[0].window_start.strftime("%Y%m%d") if events else datetime.now().strftime("%Y%m%d")
    for i, ev in enumerate(events, start=1):
        ev.event_id = f"AN-{day}-{i:04d}"
    return events
