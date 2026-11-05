# -*- coding: utf-8 -*-
"""
Agent 工具规格表（阶段6 方案 2.4 节）

这里只放"给模型看的声明"和"执行器要用的元数据"：
  - TOOL_SPECS：OpenAI Function Calling 的 tools 数组（name/description/parameters）
  - TOOL_META：权限码、是否需要前置条件、是否可重试、错误码语义
  - REVIEW_REASON_ROLE：复核原因 → 建议岗位（服务端硬编码，非模型判断）

★ 注册表里没有的工具，模型幻想出来只会得到 unknown_tool——
  "不得控制设备、不得改数据、不得签核"不是靠提示词劝阻，而是工具根本不存在。
"""

from __future__ import annotations

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_energy_data",
            "description": ("查询指定电表、楼栋或时间范围内的能耗数据（15 分钟粒度读数与折算电量）。"
                            "只读。返回数据附完整性状态与来源。一切分析的起点。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "meter_id": {"type": "string", "description": "电表编号，如 M-002；与 building_id 至少提供一项"},
                    "building_id": {"type": "string", "description": "楼栋编号，如 B1；返回该楼栋全部电表"},
                    "start_time": {"type": "string", "description": "起始时间，如 2026-10-23 00:00"},
                    "end_time": {"type": "string", "description": "结束时间，如 2026-10-23 23:45"},
                    "granularity": {"type": "string", "enum": ["15min", "hour", "day"],
                                    "description": "聚合粒度，默认 15min"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meter_config",
            "description": ("查询电表计量配置、历史版本与拓扑归属关系（上级/下级电表、关联设备、归属租户、"
                            "关系版本）。倍率/单位/版本/生效时间是计量分析的前提。只读。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "meter_id": {"type": "string"},
                    "target_time": {"type": "string", "description": "查询该时间点生效的配置；缺省返回当前与全部历史"},
                    "include_topology": {"type": "boolean", "description": "是否包含父/子电表与归属，默认 true"},
                },
                "required": ["meter_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_meter_data",
            "description": ("对指定电表/楼栋在指定时间范围的数据执行确定性校验：数据缺失、时间戳异常、"
                            "倍率配置异常、单位不一致、配置版本冲突、计量关系完整性。"
                            "校验结果由规则程序生成，非模型判断。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "meter_id": {"type": "string"},
                    "building_id": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_energy_anomaly",
            "description": ("执行预设确定性异常检测规则（倍率突变 R04、总分表平衡差 R05）。"
                            "检测结果由规则引擎计算生成，模型不得修改数值。必须在数据校验完成后调用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "meter_id": {"type": "string"},
                    "building_id": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_energy_knowledge",
            "description": ("混合检索（BM25+FAISS+RRF）能源管理知识库：计量规范、排查流程、归属规则、"
                            "复核制度、历史案例。检索不到相关内容时返回依据不足标记。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 3, "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_anomaly_report",
            "description": ("基于本任务已完成的查询、校验、检测结果与证据，生成异常分析报告草稿。"
                            "只能生成 draft 状态草稿；证据不足时生成待核查报告。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "anomaly_id": {"type": "string"},
                    "focus": {"type": "string", "description": "报告侧重，如用户关心的角度"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_review",
            "description": ("创建人工复核请求（倍率配置异常/版本冲突/数据缺失/归属不明/安全风险/证据不足/"
                            "需最终业务确认）。创建请求不等于人工已完成审核。只新增请求，不能修改或关闭异常。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["multiplier_anomaly", "version_conflict", "data_missing",
                                 "ownership_unclear", "safety_risk", "insufficient_evidence",
                                 "final_confirmation"],
                    },
                    "related_anomaly_id": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]

TOOL_NAMES: list[str] = [t["function"]["name"] for t in TOOL_SPECS]

# 工具 → 权限码（RBAC 在工具执行器里，不在提示词里）
TOOL_PERMISSION: dict[str, str] = {
    "query_energy_data": "anomaly:read",
    "get_meter_config": "anomaly:read",
    "validate_meter_data": "anomaly:read",
    "detect_energy_anomaly": "anomaly:read",
    "search_energy_knowledge": "chat:use",
    "generate_anomaly_report": "anomaly:read",
    "request_human_review": "anomaly:read",
}

# 前置条件说明（执行器据此返回"缺什么"，让模型能自我纠正）
PRECONDITIONS: dict[str, list[str]] = {
    "validate_meter_data": ["data_queried", "config_checked"],
    "detect_energy_anomaly": ["validation_passed"],
    "generate_anomaly_report": ["data_queried", "validation_run"],
    "request_human_review": [],
}

# 允许重试（参数修正型）的工具；SCOPE_DENIED / KB_NOT_READY 永不重试
RETRYABLE: set[str] = {"query_energy_data", "get_meter_config",
                       "validate_meter_data", "detect_energy_anomaly",
                       "search_energy_knowledge"}

# 复核原因 → 建议岗位（服务端硬编码，模型不能指定岗位）
REVIEW_REASON_ROLE: dict[str, str] = {
    "multiplier_anomaly": "计量数据工程师",
    "version_conflict": "计量数据工程师",
    "data_missing": "计量运维班组",
    "ownership_unclear": "租户运营代表",
    "safety_risk": "电气安全主管",
    "insufficient_evidence": "能源管理员",
    "final_confirmation": "电气安全主管",
}

REVIEW_REASON_LABEL: dict[str, str] = {
    "multiplier_anomaly": "倍率配置异常",
    "version_conflict": "配置版本冲突",
    "data_missing": "数据缺失",
    "ownership_unclear": "归属不明",
    "safety_risk": "安全风险",
    "insufficient_evidence": "证据不足",
    "final_confirmation": "需最终业务确认",
}

# 防循环预算（方案第六节）
MAX_TOTAL_CALLS = 12
MAX_SAME_TOOL_CALLS = 3
MAX_TIME_BUDGET_SECONDS = 180
MAX_TOOL_OUTPUT_CHARS = 60_000
