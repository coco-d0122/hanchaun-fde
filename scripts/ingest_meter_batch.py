# -*- coding: utf-8 -*-
"""
采集批次幂等导入脚本（阶段11，对应自检清单 N-01 / 阶段9 D2-05）

为试点落一个"采集网关落库"的最小可用通道：
  - 把 CSV 批次（字段与 data/samples 相同）导入 SQLite 的 meter_readings 表
  - 唯一键 (meter_id, ts) 保证重复导入不翻倍（D2-05 第一项验收）
  - 表由 SQLAlchemy 模型 MeterReadingRow 定义，应用与采集侧共用同一 schema

注意：当前 data_service 仍读 CSV（演示模式）；本脚本与表是接入真实采集
前的落库通道，接入时 data_service 增加 db 数据源即可（已在文档登记）。

用法：python scripts/ingest_meter_batch.py data/samples/meter_readings_anomaly.csv
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import Base, MeterReadingRow  # noqa: E402
from app.db.session import engine, session_factory  # noqa: E402


def _parse_ts(v: str) -> datetime:
    v = v.strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间戳：{v!r}")


async def ingest(csv_path: Path) -> dict:
    get_settings()  # 确保 .env 装配（与 app 同一配置来源）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    inserted = duplicated = 0
    async with session_factory() as db:
        async with db.begin():
            existing = set((await db.execute(
                select(MeterReadingRow.meter_id, MeterReadingRow.ts))).all())
            seen_this_batch: set[tuple[str, datetime]] = set()
            for row in rows:
                meter = str(row["meter_id"]).strip()
                ts = _parse_ts(str(row["timestamp"]))
                key = (meter, ts)
                if key in existing or key in seen_this_batch:
                    duplicated += 1
                    continue
                parent = row.get("parent_meter_id")
                db.add(MeterReadingRow(
                    meter_id=meter, ts=ts,
                    raw_reading=float(row["raw_reading"]),
                    multiplier=float(row["multiplier"]),
                    unit=str(row.get("unit") or "kWh").strip(),
                    building_id=str(row.get("building_id") or "").strip(),
                    parent_meter_id=str(parent).strip()
                    if parent not in (None, "", "None", "null") else None,
                ))
                seen_this_batch.add(key)
                inserted += 1
            total = (await db.execute(
                select(func.count(MeterReadingRow.id)))).scalar_one()
    return {"file": str(csv_path), "csv_rows": len(rows), "inserted": inserted,
            "duplicated_skipped": duplicated, "table_total": int(total)}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python scripts/ingest_meter_batch.py <csv 文件路径>")
    path = Path(sys.argv[1])
    if not path.exists():
        raise SystemExit(f"CSV 不存在：{path}")
    import asyncio
    report = asyncio.run(ingest(path))
    print("导入完成：", report)
    print("提示：立即重跑同一文件应 inserted=0、duplicated_skipped=全部行数（幂等）。")


if __name__ == "__main__":
    main()
