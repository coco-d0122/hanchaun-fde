# -*- coding: utf-8 -*-
"""
BM25 关键词检索（阶段3原样迁移）
"""

from __future__ import annotations

import math
from typing import Sequence

from app.rag.loader import Chunk
from app.rag.text import tokenize_zh

try:
    from rank_bm25 import BM25Okapi  # type: ignore
    HAS_RANK_BM25 = True
except ImportError:  # pragma: no cover
    BM25Okapi = None  # type: ignore
    HAS_RANK_BM25 = False


class SimpleBM25:
    """
    内置 BM25 实现（当 rank-bm25 未安装时的后备方案）。

    公式：score(D,Q) = Σ_{q∈Q} IDF(q) · f(q,D)·(k1+1) / (f(q,D) + k1·(1-b+b·|D|/avgdl))
    参数：k1=1.5（词频饱和速度），b=0.75（文档长度归一化强度），业界默认值。
    """

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus = corpus_tokens
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in corpus_tokens:
            counter: dict[str, int] = {}
            for t in doc:
                counter[t] = counter.get(t, 0) + 1
            self.tf.append(counter)
            for t in counter:
                df[t] = df.get(t, 0) + 1
        n = len(corpus_tokens)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def get_scores(self, query_tokens: Sequence[str]) -> list[float]:
        scores = [0.0] * len(self.corpus)
        for i, counter in enumerate(self.tf):
            dl = self.doc_len[i] or 1
            s = 0.0
            for q in query_tokens:
                f = counter.get(q, 0)
                if f == 0:
                    continue
                idf = self.idf.get(q, 0.0)
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            scores[i] = s
        return scores


class BM25Index:
    """BM25 关键词索引封装：屏蔽 rank-bm25 与内置实现的差异。"""

    def __init__(self, corpus_tokens: list[list[str]]):
        self.backend = "rank_bm25" if HAS_RANK_BM25 else "builtin"
        if HAS_RANK_BM25:
            self._bm25 = BM25Okapi(corpus_tokens)
        else:
            self._bm25 = SimpleBM25(corpus_tokens)

    def search(self, query: str, k: int) -> tuple[list[float], list[int]]:
        """返回 (分数列表, 文档下标列表)，按分数降序。"""
        tokens = tokenize_zh(query)
        if not tokens:
            return [], []
        scores = list(self._bm25.get_scores(tokens))
        # 只保留真正命中了查询词的文档：分数为 0 意味着一个查询词都没落在该片段里，
        # 这类"陪跑"结果若混进候选，会被 RRF 当成有效排名，进而污染融合排序。
        order = sorted((i for i in range(len(scores)) if scores[i] > 0),
                       key=lambda i: -scores[i])[:k]
        if not order:
            return [], []
        return [float(scores[i]) for i in order], order


def _bm25_doc_tokens(chunk: Chunk) -> list[str]:
    """
    BM25 文档词序列：正文词 + 标题词重复 3 次。

    这是对 BM25F（字段加权 BM25）的轻量近似：标题/小节名往往就是主题词
    （"总表与分表数据核对""倍率变更"），而正文里未必反复出现。
    不加权时，短文片段容易仅凭一两个偶然命中的词排到前面。
    """
    body = tokenize_zh(chunk.content)
    heading = tokenize_zh(f"{chunk.doc_title} {chunk.heading_path}")
    return body + heading * 3
