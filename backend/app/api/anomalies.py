# -*- coding: utf-8 -*-
"""
异常事件查询接口（阶段4方案 3.4 / 3.5）—— 全部只读
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Path

from app.db import repo
from app.db.session import session_factory
from app.errors import anomaly_not_found

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"])


@router.get("")
async def get_anomalies(
    status: Literal["pending_review", "confirmed", "dismissed"] | None = None,
) -> dict:
    """异常事件列表。status 只由人工在数据库层面维护，本阶段不做修改接口。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_anomaly_events(db, status)
    return {"items": items, "total": len(items)}


@router.get("/{anomaly_id}")
async def get_anomaly_detail(anomaly_id: str = Path(max_length=32)) -> dict:
    """异常事件详情：电表信息、规则检测结果、证据来源。不允许修改原始能耗数据。"""
    async with session_factory() as db:
        async with db.begin():
            row = await repo.get_anomaly_event(db, anomaly_id)
            if row is None:
                raise anomaly_not_found(anomaly_id)
            evidence_rows = await repo.get_anomaly_evidence(db, anomaly_id)
            extra = row.extra or {}

    return {
        "anomaly_id": row.id,
        "meter_id": row.meter_id,
        "building_id": row.building_id,
        "anomaly_type": row.anomaly_type,
        "rule_code": row.rule_code,
        "severity": row.severity,
        "detected_at": row.detected_at.isoformat(),
        "window": {"start": row.window_start.isoformat(), "end": row.window_end.isoformat()},
        "description": row.description,
        "rule_result": {
            "rule_code": row.rule_code,
            "metrics": row.rule_result or {},
        },
        "evidence": [
            {
                "evidence_type": e.evidence_type,
                "content": e.evidence_content,
                "source_reference": e.source_reference,
            }
            for e in evidence_rows
        ],
        "related_meters": [row.meter_id] + list(extra.get("suspects") or []),
        "suggestions": list(extra.get("suggestions") or []),
        "status": row.status,
    }
