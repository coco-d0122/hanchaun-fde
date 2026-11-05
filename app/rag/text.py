# -*- coding: utf-8 -*-
"""
中文分词与文本规范化（阶段3原样迁移，纯函数无 IO）
"""

from __future__ import annotations

import re

# 可选依赖：jieba 缺失时降级为"单字+二元组"切分，保证召回不崩（精度略降）
try:
    import logging as _logging

    import jieba  # type: ignore
    jieba.setLogLevel(_logging.ERROR)   # 关闭 jieba 首次运行的构建日志噪音
    HAS_JIEBA = True
except ImportError:  # pragma: no cover
    jieba = None  # type: ignore
    HAS_JIEBA = False

# 极简停用词表：只去掉对检索毫无贡献的高频虚词，避免误删专业词
STOPWORDS = set("""
的 了 是 在 和 与 及 或 对 中 为 以 于 上 下 有 无 不 也 就 都 而 但 若 如 若 该 其 之 者
请 问 如何 怎么 怎样 什么 哪些 为什么 是否 可以 需要 应该 我们 你们 他们 一个 一种 这个 那个
""".split())


def tokenize_zh(text: str) -> list[str]:
    """
    中文分词，供 BM25 使用。

    策略：
      1) 有 jieba 时用 jieba 精确模式切词（专业词如"倍率""总分表"能被正确切出）
      2) 无 jieba 时退化为"单字 + 相邻二元组"，保证召回不崩（精度略降）
      3) 统一转小写、去停用词与纯标点，保留数字与英文（如 kWh、CT、0.5S）
    """
    if not text:
        return []
    text = text.lower()
    if HAS_JIEBA:
        raw = [t.strip() for t in jieba.lcut(text)]
    else:
        chars = [c for c in text if not c.isspace()]
        raw = list(chars) + [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]

    tokens = []
    for t in raw:
        if not t or t in STOPWORDS:
            continue
        if all(not ch.isalnum() for ch in t):      # 纯标点/符号直接丢
            continue
        tokens.append(t)
    return tokens


def normalize_query(q: str) -> str:
    """问题清洗：全角转半角标点、压缩空白。避免用户输入带全角问号影响匹配。"""
    trans = str.maketrans("，。？！；：“”‘’（）【】", ",.?!;:\"\"''()[]")
    return re.sub(r"\s+", " ", q.translate(trans)).strip()
