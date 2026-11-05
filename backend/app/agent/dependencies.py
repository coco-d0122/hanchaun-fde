# -*- coding: utf-8 -*-
"""
工具依赖图与并行编排（Harness 核心之一）

设计要点：
  1. 依赖由后端程序强制检查，**不靠提示词自觉**：模型在同一轮里同时请求
     query + config + validate 时，编排器会拆成「并行只读批 → 依赖批」顺序执行。
  2. 相互独立且只读的工具放进同一批 `asyncio.gather` 并行执行。
  3. 有依赖的工具必须等前置工具**成功**后才执行；前置失败则整批跳过并记错误码。
  4. 达到 5 轮决策上限后停止自动执行，输出"已完成步骤 + 未完成原因"。

依赖矩阵（方案"二、工具集成"六条约束的物理落点）：
  - validate 需要 query + config（涉及倍率时还要求 target_time 有效配置，由 get_meter_config 提供）
  - detect 需要 validate 通过（校验不通过时执行器直接拒绝）
  - report 需要 query + validate 已执行，且证据集非空
  - request_human_review 无强制依赖，但配置冲突/数据缺失/归属不明时由状态机强制触发
"""

from __future__ import annotations

from typing import Any

# 工具 → 必须在**本任务中已成功执行**的前置工具集合
DEPENDENCIES: dict[str, set[str]] = {
    "validate_meter_data": {"query_energy_data", "get_meter_config"},
    "detect_energy_anomaly": {"validate_meter_data"},
    "generate_anomaly_report": {"query_energy_data", "validate_meter_data"},
    "request_human_review": set(),
    "search_energy_knowledge": set(),
    "get_device_relationship": set(),
    "query_energy_data": set(),
    "get_meter_config": set(),
}

# 只读工具（可并行）；不在此表中的工具一律串行
READ_ONLY_TOOLS: set[str] = {
    "query_energy_data", "get_meter_config", "get_device_relationship",
    "search_energy_knowledge", "detect_energy_anomaly", "validate_meter_data",
}

# 门禁级硬约束（执行器之外再做一层，防止有人绕过 executor 直接调工具）
HARD_GATES: dict[str, str] = {
    "detect_energy_anomaly": "validation_passed",
    "generate_anomaly_report": "evidence_ready",
}


def plan_batches(calls: list[dict], completed: set[str]) -> tuple[list[list[dict]], list[dict]]:
    """
    把一轮模型决策里的 tool_calls 拆成有序批次。

    返回 (batches, deferred)：
      - batches[0]：无未满足依赖的独立只读工具，可并行；
      - batches[1..]：依赖前一批结果的工具，顺序执行；
      - deferred：依赖缺失（本轮和已完成任务里都找不到前置工具）的调用，
        直接回 TOOL_PRECONDITION_FAILED 让模型下一轮纠正。

    例如同一轮请求 [validate, query, config, detect]：
      → [[query, config], [validate], [detect]]
    """
    names = {c.get("name", "") for c in calls}
    pending = list(calls)
    batches: list[list[dict]] = []
    done = set(completed)
    deferred: list[dict] = []

    # 最多迭代 len(calls) 轮，避免畸形依赖图造成死循环
    for _ in range(len(calls) + 1):
        ready: list[dict] = []
        rest: list[dict] = []
        for call in pending:
            tool = call.get("name", "")
            need = DEPENDENCIES.get(tool, set())
            missing = need - done
            if not missing:
                ready.append(call)
            elif missing & (names - done):
                # 前置工具就在本轮里，只是还没轮到 → 下一批
                rest.append(call)
            else:
                # 前置工具既没做过、本轮也没请求 → 无法满足，交给下一轮决策纠正
                deferred.append(call)
        if not ready:
            if rest:      # 理论上不会发生（依赖图无环），防御性兜底
                deferred.extend(rest)
            break
        # 同批内：只要全部是只读工具就并行；否则拆成单元素批保证顺序
        if all(c.get("name") in READ_ONLY_TOOLS for c in ready):
            batches.append(ready)
        else:
            batches.extend([[c] for c in ready])
        for c in ready:
            done.add(c.get("name", ""))
        pending = rest
        if not pending:
            break

    return batches, deferred


def check_hard_gate(tool: str, state: Any, context: Any) -> tuple[bool, str]:
    """执行器之外的第二道硬门禁（读任务状态机，不读模型或用户的话）。"""
    gate = HARD_GATES.get(tool)
    if gate == "validation_passed" and state.validation_status != "passed":
        return False, ("正式异常检测必须先完成数据查询与计量校验"
                       f"（当前 validation_status={state.validation_status}）")
    if gate == "evidence_ready":
        if not state.data_queried:
            return False, "生成报告前必须已查询能耗数据"
        if state.validation_status == "not_run":
            return False, "生成报告前必须已执行计量校验"
        if not context.evidence:
            return False, "生成报告前必须关联有效证据（当前证据集为空）"
    return True, ""


def summarize_round(calls: list[dict]) -> str:
    """决策轮摘要（只记"选了哪些工具"，绝不记录模型隐藏推理过程）。"""
    return "选定工具：" + ("、".join(c.get("name", "?") for c in calls)
                          if calls else "（无工具调用，直接输出结论）")
