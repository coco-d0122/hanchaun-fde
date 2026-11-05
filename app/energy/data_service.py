# -*- coding: utf-8 -*-
"""
能耗数据加载与增量（差分 × 倍率）计算。

★ 纯函数 + 只读缓存：模型无法触达，只有工具层调用。
★ 异常判定不在这里（见 anomaly_detector.py），本模块只负责"读数 → 区间电量"。
"""

from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.errors import AgentToolError

from app.core.config import Settings

logger = logging.getLogger(__name__)


REQUIRED_FIELDS = ("meter_id", "timestamp", "raw_reading", "multiplier", "building_id")

UNIT_TO_KWH = {           # 统一折算到 kWh，避免"总表 MWh、分表 kWh"直接相加的经典错误
    "kwh": 1.0,
    "度": 1.0,
    "wh": 0.001,
    "mwh": 1000.0,
    "gwh": 1_000_000.0,
}


class DataError(Exception):
    """数据文件格式或内容错误。"""


@dataclass
class Reading:
    """一条电表读数（已解析）。"""

    meter_id: str
    ts: datetime
    raw_reading: float          # 累计示值（表底），单位见 unit
    multiplier: float           # 倍率（CT×PT 折算系数）
    unit: str
    building_id: str
    parent_meter_id: str | None


@dataclass
class Interval:
    """相邻两个读数之间的用电增量（一次侧电量，单位 kWh）。"""

    meter_id: str
    ts: datetime                # 区间起点
    energy_kwh: float
    raw_delta: float
    multiplier: float
    unit: str
    building_id: str
    parent_meter_id: str | None
    flags: list[str] = field(default_factory=list)   # rollback / unit_converted / multiplier_changed


def _parse_ts(value: str) -> datetime:
    """解析时间戳，兼容 '2026-10-23 10:00'、'2026-10-23T10:00:00'、'2026/10/23 10:00'。"""
    v = value.strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise DataError(f"无法解析时间戳：{value!r}（支持 2026-10-23 10:00 或 ISO8601 格式）")


def load_readings(path: Path) -> list[Reading]:
    """
    读取 CSV / JSON 格式的模拟电表数据。

    CSV：标准逗号分隔，首行为表头
    JSON：数组，每个元素是一条记录（字段同 CSV）
    """
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在：{path}")

    rows: list[dict[str, Any]] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise DataError("CSV 文件缺少表头")
            missing = [f for f in REQUIRED_FIELDS if f not in reader.fieldnames]
            if missing:
                raise DataError(f"CSV 缺少必需字段：{', '.join(missing)}（当前表头：{reader.fieldnames}）")
            rows = [dict(r) for r in reader]
    elif path.suffix.lower() == ".json":
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DataError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(rows, list):
            raise DataError("JSON 顶层必须是数组")
    else:
        raise DataError(f"不支持的文件格式：{path.suffix}（仅支持 .csv / .json）")

    readings: list[Reading] = []
    for lineno, row in enumerate(rows, start=2):
        try:
            meter_id = str(row["meter_id"]).strip()
            if not meter_id:
                raise DataError("meter_id 为空")
            unit = str(row.get("unit") or "kWh").strip()
            parent = row.get("parent_meter_id")
            parent = str(parent).strip() if parent not in (None, "", "None", "null") else None
            readings.append(Reading(
                meter_id=meter_id,
                ts=_parse_ts(str(row["timestamp"])),
                raw_reading=float(row["raw_reading"]),
                multiplier=float(row["multiplier"]),
                unit=unit,
                building_id=str(row.get("building_id") or "").strip(),
                parent_meter_id=parent,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            raise DataError(f"第 {lineno} 行数据不合法：{exc}") from exc
    if not readings:
        raise DataError("数据文件为空")
    return readings


def compute_intervals(readings: list[Reading]) -> dict[str, list[Interval]]:
    """
    把累计示值（表底）转换为相邻时间点的用电增量，并按倍率与单位折算到 kWh。

    ★ 这是整个能耗分析最关键的一步，三个必须处理对的坑：

    坑 1｜不能把累计读数直接相加。
          表底是"从装表到现在的累计量"，一天内累加会得到天文数字。
          正确做法是差分：Δ示值 = 本点示值 − 上一点示值。

    坑 2｜必须先差分、再乘倍率。
          energy(一次侧) = Δ示值(二次侧) × 倍率。
          若先乘后差（(a×r) − (b×r) 数学上等价，但一旦中间有倍率变更就会错），
          且更容易在换表/清零时算出负数。这里按"逐区间取当时倍率"处理。

    坑 3｜单位不统一。
          total 表可能是 MWh，分表是 kWh。必须统一折算后再做平衡比较。

    另外处理表底回退（换表、清零、通讯补采导致的示值倒退）：
      差值 < 0 时不产生电量，打 rollback 标记，交由数据质量环节处置，
      绝不参与总分平衡计算（否则会凭空造出一个巨大差异）。
    """
    grouped: dict[str, list[Reading]] = {}
    for r in readings:
        grouped.setdefault(r.meter_id, []).append(r)

    result: dict[str, list[Interval]] = {}
    for meter_id, items in grouped.items():
        items.sort(key=lambda x: x.ts)
        intervals: list[Interval] = []
        for prev, cur in zip(items, items[1:]):
            raw_delta = cur.raw_reading - prev.raw_reading
            flags: list[str] = []

            if raw_delta < 0:
                # 表底回退：不产生电量，只记录异常标记
                flags.append("rollback")
                raw_delta = 0.0

            factor = UNIT_TO_KWH.get(cur.unit.lower(), 1.0)
            if factor != 1.0:
                flags.append("unit_converted")

            # 倍率取"区间起点时刻生效的倍率"（即 prev.multiplier）。
            # 理由：这段走字是在上一个读数到本读数之间产生的，理应按当时的配置换算。
            # 若用 cur.multiplier，倍率刚切换的那个区间会被整体放大/缩小，制造一天虚假跳变。
            energy = raw_delta * prev.multiplier * factor
            intervals.append(Interval(
                meter_id=meter_id,
                ts=prev.ts,                 # 区间起点
                energy_kwh=round(energy, 4),
                raw_delta=round(raw_delta, 6),
                multiplier=prev.multiplier,
                unit=cur.unit,
                building_id=cur.building_id,
                parent_meter_id=cur.parent_meter_id,
                flags=flags,
            ))
        result[meter_id] = intervals

    # 后置一遍：标出"相对上一区间发生了倍率变化"的区间，供规则 A 使用
    for items in result.values():
        for i in range(1, len(items)):
            if abs(items[i].multiplier - items[i - 1].multiplier) > 1e-9:
                items[i].flags.append("multiplier_changed")
    return result


# ---------------------------------------------------------------- 参数与筛选（工具层共用）


def parse_param_ts(value: str | None) -> datetime | None:
    """解析工具入参时间（宽松：支持 2026-10-23 / 2026-10-23 00:00 / ISO）。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.replace("T", " ")[:19]
                                     if len(value) >= 19 else value, fmt)
        except ValueError:
            continue
    raise AgentToolError("PARAM_INVALID", f"时间格式无法识别：{value}（示例：2026-10-23 00:00）")


def select_window(cfg: Settings, meter_id: str | None, building_id: str | None,
                  start: datetime, end: datetime) -> tuple[list[Reading], list[str]]:
    """按电表/楼栋与时间窗筛选读数。返回 (读数, 命中的电表编号列表)。"""
    if end < start:
        raise AgentToolError("PARAM_INVALID", "结束时间早于起始时间")
    if (end - start).days > 7:
        raise AgentToolError("TIME_RANGE_TOO_LARGE", "单次查询时间范围不得超过 7 天")
    if not meter_id and not building_id:
        raise AgentToolError("PARAM_INVALID", "meter_id 与 building_id 至少提供一项")

    readings = load_dataset(cfg)
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


def energy_by_meter(window: list[Reading],
                    granularity: str) -> dict[str, list[dict[str, Any]]]:
    """按粒度聚合电量（差分后再乘倍率，复用 compute_intervals）。"""
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


# ---------------------------------------------------------------- 数据集加载（工具层共用）

_DATASET_CACHE: dict[str, list[Reading]] = {}


def load_dataset(cfg: Settings) -> list[Reading]:
    """
    加载模拟读数（异常集优先：它包含倍率变更，是分析场景的真实数据）。

    进程内按数据目录缓存：240 表 × 96 点约 2.3 万行，重复解析会让每次工具调用多花数百毫秒。
    """
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
