# -*- coding: utf-8 -*-
"""
工具参数的严格校验（Function Calling 规范）

★ 为什么工具参数必须用 Pydantic/JSON Schema 校验：
  模型生成的 arguments 是自由文本 JSON，可能出现缺字段、类型错、越界值，
  甚至夹带 "sql": "DROP TABLE ..." 之类的注入字段。
  这里统一 `extra="forbid"`：不在 Schema 里的字段一律拒绝，
  仓库层只用结构化字段做 SQLAlchemy 参数化查询——**没有任意 SQL 通道**。

阶段6 把 get_device_relationship 并入了 get_meter_config（方案 2 节决策）；
本轮用户明确列出 8 个工具名，因此补一个只读薄封装（见 tools.py），
参数校验也在此登记，两套入口返回同一份拓扑事实，不产生第二套口径。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 所有 Schema 的共同配置：拒绝多余字段（防注入）、禁止类型强转导致的静默错误
_STRICT = ConfigDict(extra="forbid", strict=False)


class QueryEnergyDataArgs(BaseModel):
    model_config = _STRICT

    meter_id: str | None = Field(None, max_length=32)
    building_id: str | None = Field(None, max_length=16)
    start_time: str = Field(..., min_length=8, max_length=32,
                            description="起始时间，如 2026-10-23 00:00")
    end_time: str = Field(..., min_length=8, max_length=32,
                          description="结束时间，如 2026-10-23 23:45")
    granularity: Literal["15min", "hour", "day"] = "15min"


class GetMeterConfigArgs(BaseModel):
    model_config = _STRICT

    meter_id: str = Field(..., min_length=1, max_length=32)
    target_time: str | None = None
    include_topology: bool = True


class GetDeviceRelationshipArgs(BaseModel):
    """电表-设备-楼栋关联关系（只读薄封装，复用 get_meter_config 的拓扑事实）。"""
    model_config = _STRICT

    meter_id: str = Field(..., min_length=1, max_length=32)
    include_devices: bool = True


class ValidateMeterDataArgs(BaseModel):
    model_config = _STRICT

    meter_id: str | None = None
    building_id: str | None = None
    start_time: str = Field(..., min_length=8, max_length=32)
    end_time: str = Field(..., min_length=8, max_length=32)


class DetectEnergyAnomalyArgs(BaseModel):
    model_config = _STRICT

    meter_id: str | None = None
    building_id: str | None = None
    start_time: str = Field(..., min_length=8, max_length=32)
    end_time: str = Field(..., min_length=8, max_length=32)


class SearchEnergyKnowledgeArgs(BaseModel):
    model_config = _STRICT

    query: str = Field(..., min_length=1, max_length=300)
    top_k: int = Field(3, ge=1, le=3)


class GenerateAnomalyReportArgs(BaseModel):
    model_config = _STRICT

    anomaly_id: str | None = Field(None, max_length=32)
    focus: str | None = Field(None, max_length=200)


class RequestHumanReviewArgs(BaseModel):
    model_config = _STRICT

    reason: Literal["multiplier_anomaly", "version_conflict", "data_missing",
                    "ownership_unclear", "safety_risk", "insufficient_evidence",
                    "final_confirmation"]
    related_anomaly_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = Field("", max_length=300)


# 工具名 → 参数模型（注册表即校验表，新增工具必须在这里登记）
ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "query_energy_data": QueryEnergyDataArgs,
    "get_meter_config": GetMeterConfigArgs,
    "get_device_relationship": GetDeviceRelationshipArgs,
    "validate_meter_data": ValidateMeterDataArgs,
    "detect_energy_anomaly": DetectEnergyAnomalyArgs,
    "search_energy_knowledge": SearchEnergyKnowledgeArgs,
    "generate_anomaly_report": GenerateAnomalyReportArgs,
    "request_human_review": RequestHumanReviewArgs,
}


class ParamInvalid(Exception):
    """参数校验失败（错误码 PARAM_INVALID，中文说明回传给模型让其自行纠正）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def validate_args(tool: str, args: dict) -> dict:
    """
    校验并归一化工具参数；返回可直接交给工具实现的 dict。

    未知工具 → UNKNOWN_TOOL 由执行器处理，这里只抛"参数不合法"。
    Schema 里没有的字段（例如 sql / api_key / password）会被 extra=forbid 拦下，
    且绝不进入 input_parameters 日志。
    """
    schema = ARG_SCHEMAS.get(tool)
    if schema is None:
        raise ParamInvalid(f"工具 {tool} 未登记参数 Schema")
    if not isinstance(args, dict):
        raise ParamInvalid("工具参数必须是 JSON 对象")
    try:
        obj = schema.model_validate(args)
    except ValidationError as exc:
        # 只回传字段名与原因，不回显原始值（可能含敏感内容）
        brief = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
                          for e in exc.errors()[:4])
        raise ParamInvalid(f"参数不合法：{brief}") from exc
    return obj.model_dump(exclude_none=True)


def tool_json_schemas() -> dict[str, dict]:
    """给 OpenAPI/文档用：导出全部工具的 JSON Schema。"""
    return {name: model.model_json_schema() for name, model in ARG_SCHEMAS.items()}
