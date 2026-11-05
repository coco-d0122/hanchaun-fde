# -*- coding: utf-8 -*-
"""
Prompt 构建（阶段3迁移 + 阶段4改造：增加 history 参数支持多轮上下文）

改造点（阶段4方案 7.1）：
  - build_qa_prompt / build_anomaly_prompt 增加 history: list[dict] 参数
    （ [{"role": "user"|"assistant", "content": "..."}, ...] ）
  - 历史以"最近对话记录"块拼进 Prompt，并受字符预算约束（预算截断在服务层做）
  - 模板字符串加版本号注释，便于阶段5迁移到数据库版本化管理
"""

from __future__ import annotations

import json
from typing import Any

from app.core.llm import CTX_CLOSE, CTX_OPEN, EV_CLOSE, EV_OPEN
from app.core.retriever import Retrieved
from app.core.rules import AnomalyEvent, RuleA_RatioChange

# ---- 模板版本：v1（阶段4）。阶段5将迁入 prompt_templates 表做版本管理 ----

SYSTEM_PROMPT_QA = (
    "你是瀚川工业园能源管理系统的知识库问答助手。"
    "你只能依据【检索到的知识片段】作答，不得引用片段之外的任何规定、数字或条款。"
    "如果片段不足以回答，必须直接说明“依据不足”，并提示需要补充哪类资料。"
    "回答中使用 [1][2] 标注依据来自哪个片段。不要编造文件名、条款号、阈值。"
    "如果【最近对话记录】与当前问题无关，忽略它，只回答当前问题。"
)

SYSTEM_PROMPT_ANOMALY = (
    "你是工业园能耗异常分析助手。你的职责是帮助能源经理理解一条已由确定性规则引擎"
    "检测出的异常，并给出待核实的排查方向。必须遵守：\n"
    "1) 只能提出“可能原因”，一律使用“可能/待核实/需确认”等表述，禁止下确定性结论；\n"
    "2) 不得计算或改写任何数值，所有数字只能引用【证据】中已有的值；\n"
    "3) 不得建议直接修改计量数据或控制设备，只能建议“核查/复核/调阅记录”；\n"
    "4) 每条原因尽量标注依据，无依据的推测必须写明“无直接依据”；\n"
    "5) 证据不足时，明确输出“待人工核实”。"
)


def _history_block(history: list[dict[str, str]] | None) -> str:
    """把最近对话记录拼成 Prompt 块。空历史返回空串（不留空块）。"""
    if not history:
        return ""
    lines = []
    for m in history:
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{role}：{m.get('content', '')}")
    return (
        "【最近对话记录】（仅供理解当前问题的上下文，不是回答依据）\n"
        + "\n".join(lines) + "\n\n"
    )


def build_qa_prompt(question: str, results: list[Retrieved],
                    history: list[dict[str, str]] | None = None) -> str:
    """知识库问答 Prompt：把 Top-K 片段编号后交给模型，强制其按编号引用。"""
    if results:
        blocks = []
        for i, r in enumerate(results, start=1):
            blocks.append(
                f"[{i}] 来源：{r.chunk.source_file} / {r.chunk.heading_path}\n{r.chunk.content}"
            )
        context = "\n\n".join(blocks)
    else:
        context = "（未检索到任何相关片段）"

    return (
        f"{_history_block(history)}"
        f"【用户问题】\n{question}\n\n"
        f"{CTX_OPEN}\n{context}\n{CTX_CLOSE}\n\n"
        "请依据上述片段回答，要求：\n"
        "1. 先给结论，再给要点，最后列出引用编号；\n"
        "2. 每个要点后用 [n] 标注依据；\n"
        "3. 若片段不足以回答，直接说“知识库依据不足”，并说明缺什么资料；\n"
        "4. 不要补充片段以外的内容。"
    )


def build_evidence_payload(event: AnomalyEvent, results: list[Retrieved]) -> dict[str, Any]:
    """
    把事件整理成"喂给模型的结构化证据"。

    设计要点：这里的每一个字段都由 Python 计算/查询得出，
    模型只能引用，不能修改。这样"数值来自程序、解释来自模型"的边界才立得住。
    """
    citations = [f"{r.chunk.source_file} · {r.chunk.heading_path}" for r in results]

    # 按异常类型给出"候选原因清单"——注意这些是待核实假设，不是结论
    if event.rule_code == "R04_RATIO_CHANGE":
        candidate_causes = [
            "电表更换后新倍率未按实际 CT/PT 变比录入，导致电量被整体放大或缩小",
            "倍率变更生效时间录入错误（提前生效或延后生效），造成变更点附近电量错位",
            "存在同表多条倍率配置版本并存，系统取用了错误版本",
            "属于合法变更（增容/改造/产权调整），实际用电量未变，需工单佐证",
        ]
        review_reason = "倍率变化本身不构成异常，必须由计量工程师核对配置历史与工单后才能定性。"
    elif event.rule_code == "R05_BALANCE_DIFF":
        candidate_causes = [
            "分表倍率配置偏差，导致分表电量被系统性放大或缩小",
            "存在未纳入计量的负荷（临时用电、私接、漏装计量点）",
            "分表在该时段存在数据缺失，缺数被直接计入差异",
            "总表与分表统计时段未对齐（冻结时间不同），造成时间错位差异",
        ]
        review_reason = "平衡差可能由计量、接线、数据质量、真实负荷四类原因造成，需现场核实。"
    else:
        candidate_causes = ["待核实"]
        review_reason = "证据不足以定性，需人工核实。"

    return {
        "事件编号": event.event_id,
        "异常类型": event.title,
        "规则编号": f"{event.rule_code} v{RuleA_RatioChange.version}",
        "楼栋": event.building_id or "未标注",
        "电表编号": event.meter_id,
        "监测窗口": f"{event.window_start:%Y-%m-%d %H:%M} ~ {event.window_end:%Y-%m-%d %H:%M}",
        "规则判定": event.summary,
        "计算结果": event.metrics,
        "原始数据快照": event.evidence,
        "疑似关联表": event.suspects,
        "候选原因": candidate_causes,
        "建议核查项": event.suggestions,
        "知识引用": citations,
        "复核理由": review_reason,
        "证据充分性": "部分充分" if event.rule_code == "R05_BALANCE_DIFF" else "需补充工单与现场记录",
    }


def build_anomaly_prompt(event: AnomalyEvent, results: list[Retrieved],
                         history: list[dict[str, str]] | None = None) -> str:
    """构建异常分析 Prompt：证据 JSON + 知识片段 + 严格的输出格式要求。"""
    evidence = build_evidence_payload(event, results)

    ctx_blocks = []
    for i, r in enumerate(results, start=1):
        ctx_blocks.append(f"[{i}] 来源：{r.chunk.source_file} / {r.chunk.heading_path}\n{r.chunk.content}")
    context = "\n\n".join(ctx_blocks) if ctx_blocks else "（未检索到相关知识片段）"

    checklist = "\n".join(f"- {s}" for s in event.suggestions)

    return (
        f"{_history_block(history)}"
        f"请分析以下由规则引擎检测出的能耗异常。\n\n"
        f"{EV_OPEN}\n{json.dumps(evidence, ensure_ascii=False, indent=2, default=str)}\n{EV_CLOSE}\n\n"
        f"{CTX_OPEN}\n{context}\n{CTX_CLOSE}\n\n"
        "请严格按以下五个小标题输出，不要增删标题：\n"
        "【异常现象】\n"
        "  —— 只描述证据里已有的客观事实（时间、电表、数值、规则结论），不加推测。\n"
        "【可能原因】\n"
        "  —— 编号列出 2~4 条，每条后面标注“可能性：高/中/低（待核实）”，"
        "并注明依据来自证据的哪个字段或知识片段的编号。禁止使用确定性措辞。\n"
        "【相关知识依据】\n"
        "  —— 逐条引用 [n] 及来源标题。若无相关知识，写“无直接依据”。\n"
        "【建议核查的数据或记录】\n"
        f"  —— 基于以下已有建议整理，可按优先级重排，但不得新增未经证据支持的核查项：\n{checklist}\n"
        "【是否需要人工复核】\n"
        "  —— 明确回答“需要”或“不需要”，并说明理由。若证据不足，写“待人工核实”。\n\n"
        "硬性约束：\n"
        "1. 不得计算新的数值，只能引用【证据】中已出现的数字；\n"
        "2. 不得声称已确认某原因，不得给出责任人判定；\n"
        "3. 不得建议修改原始计量数据或操作设备，只能建议核查与复核。"
    )
