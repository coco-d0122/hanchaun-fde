# -*- coding: utf-8 -*-
"""
异常事件查询接口（阶段4方案 3.4 / 3.5 + 阶段5第四批审计扩展）—— 全部只读

★ 本阶段仍然**不提供任何写接口**：
  - 异常事件的最终结论由业务角色签核，`anomaly:review:submit` 不授予任何角色；
  - `POST /api/anomalies/{id}/reviews` 按方案 7.2 不注册路由（防越权签核）；
  - 页面上的"提交复核"按钮渲染为禁用态并注明"待业务角色上线后开放"。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import get_current_user, require_permission
from app.db import repo
from app.db.models import User
from app.db.session import session_factory
from app.errors import anomaly_not_found

router = APIRouter(prefix="/api/anomalies", tags=["anomalies"],
                   dependencies=[Depends(require_permission("anomaly:read"))])

# 六态机（方案 4.2）：本阶段只会发生前两态，后四态由业务签核推进
StatusLiteral = Literal["pending_analysis", "pending_review", "reviewing",
                        "confirmed", "dismissed", "closed"]


@router.get("")
async def get_anomalies(
    user: Annotated[User, Depends(get_current_user)],
    status: StatusLiteral | None = None,
    meter_id: str | None = Query(None, max_length=32),
    building_id: str | None = Query(None, max_length=16),
    anomaly_type: str | None = Query(None, max_length=64),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
) -> dict:
    """异常事件列表：状态（六态）/ 电表 / 楼栋 / 类型 / 时间范围组合筛选。"""
    async with session_factory() as db:
        async with db.begin():
            items = await repo.list_anomaly_events(
                db, status=status, meter_id=meter_id, building_id=building_id,
                anomaly_type=anomaly_type, date_from=date_from, date_to=date_to)
    return {"items": items, "total": len(items)}


@router.get("/{anomaly_id}")
async def get_anomaly_detail(anomaly_id: str = Path(max_length=32)) -> dict:
    """异常事件详情：电表信息、规则检测结果、证据来源。不允许修改原始能耗数据。

    规则结果（原始）与 AI 分析（草稿）在审计页**分离展示**：
    本接口只返回规则侧，AI 分析草稿通过 /api/audit/conversations 按 anomaly_id 追溯。
    """
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
        # 本阶段恒为 false：签核权未授予任何角色（含 admin）
        "review_submit_available": False,
    }


@router.get("/{anomaly_id}/reviews")
async def get_reviews(anomaly_id: str = Path(max_length=32)) -> dict:
    """人工复核记录（只读）。

    本阶段正常情况下返回空数组：写入接口不注册、签核权不授予任何角色。
    表结构即为最终结构，业务角色上线后无需迁移。
    """
    async with session_factory() as db:
        async with db.begin():
            row = await repo.get_anomaly_event(db, anomaly_id)
            if row is None:
                raise anomaly_not_found(anomaly_id)
            items = await repo.list_review_records(db, anomaly_id)
    return {
        "anomaly_id": anomaly_id,
        "total": len(items),
        "items": items,
        "note": "本阶段不提供提交复核接口（签核权待业务角色上线后授予）",
    }
