# -*- coding: utf-8 -*-
"""
数值溯源校验 Guardrail（阶段3原样迁移）
"""

from __future__ import annotations

import re
from typing import Any

# 日期时间模式：做数值溯源前必须先剔除，否则 "2026-10-23 10:00" 会被拆成
# 2026、10、23、10、00 五个数字，与证据里的数值比对必然失败，产生大量误报。
_DT_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?",
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}:\d{2}(?::\d{2})?",
    r"\[\d+\]",          # 引用编号 [1][2]，不是数据
    r"第\s*\d+\s*[条步项点]",   # "第2步""第3条"，属于序数
]


def extract_numbers(text: str) -> list[str]:
    """从文本中提取"实质性数值"（已剔除日期、时间、引用编号、序数）。"""
    cleaned = text
    for pat in _DT_PATTERNS:
        cleaned = re.sub(pat, " ", cleaned)
    return re.findall(r"\d+(?:\.\d+)?", cleaned)


def _number_in_evidence(num_text: str, allowed: set[str]) -> bool:
    """
    判断某个数值是否能在证据集合中找到来源。

    容差策略：先做字符串精确匹配（覆盖 10 / 10.0 / 1.50 这类写法差异），
    再做数值近似匹配（相对误差 < 0.5%，吸收四舍五入带来的差异）。
    """
    if num_text in allowed:
        return True
    try:
        v = float(num_text)
    except ValueError:
        return False
    for a in allowed:
        try:
            av = float(a)
        except ValueError:
            continue
        if av == 0:
            if abs(v) < 1e-9:
                return True
        elif abs(v - av) / abs(av) < 0.005:
            return True
    return False


def number_trace_guard(answer: str, evidence_text: str, context_text: str) -> dict[str, Any]:
    """
    ★ 数值溯源校验（Guardrail）——本系统最重要的"反幻觉"工程手段。

    为什么要做：大模型在写分析报告时，极容易"顺手"生成一个看起来合理的数字
    （比如把 3% 阈值写成 5%，把 10 倍变化说成 12 倍）。这类错误在正式系统里
    会导致错误定责，代价远高于整个项目预算。

    做法：把所有证据（Python 算出来的 metrics + 原始数据快照）与检索到的知识片段
    拼成一个"允许数值集合"，然后逐个检查回答中的数字是否都在这个集合里。
    找不到来源的数字会被单独列出并告警——注意是"告警"而不是"删除"，
    因为人工需要看到模型到底编了什么，才能判断它是否可信。

    局限（必须承认）：它只能发现"数字没有来源"，无法发现"数字有来源但被错误解读"。
    后一类问题仍然依赖人工复核，这也是为什么系统强制所有 AI 结论必须人工签认。
    """
    allowed = set(extract_numbers(evidence_text)) | set(extract_numbers(context_text))
    claimed = extract_numbers(answer)
    unmatched = [n for n in claimed if not _number_in_evidence(n, allowed)]

    # 去重但保持顺序，便于阅读
    seen: set[str] = set()
    unique_unmatched = [n for n in unmatched if not (n in seen or seen.add(n))]

    return {
        "passed": len(unique_unmatched) == 0,
        "checked": len(claimed),
        "unmatched": unique_unmatched[:12],
    }
