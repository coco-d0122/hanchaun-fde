# -*- coding: utf-8 -*-
"""
混合检索器（阶段3迁移，改造点：去掉全局 CFG/BASE_DIR 依赖，路径由 Settings 注入）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from app.core.bm25 import BM25Index, _bm25_doc_tokens
from app.core.config import Settings
from app.core.embedder import BaseEmbedder
from app.core.loader import Chunk, load_markdown_chunks
from app.core.text import normalize_query, tokenize_zh
from app.core.vector_store import VectorIndex

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """
    RRF（Reciprocal Rank Fusion，倒数排名融合）——混合检索的融合算法。

    公式： RRF(d) = Σ_i 1 / (k + rank_i(d))
      - rank_i(d)：文档 d 在第 i 路召回中的排名（从 1 开始）
      - k：平滑常数，默认 60。k 越大，排名靠前与靠后的差距被压得越平

    为什么用 RRF 而不是"加权分数相加"？
      向量的余弦相似度（0~1）与 BM25 分数（0~几十）量纲完全不同，
      直接加权必须先做归一化，而归一化方式本身又是个超参。
      RRF 只用"排名"这一个信息，天然免疫量纲问题，且没有需要调的超参
      （k=60 是原论文给出的鲁棒默认值）。
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for pos, doc_idx in enumerate(ranking, start=1):
            fused[doc_idx] = fused.get(doc_idx, 0.0) + 1.0 / (k + pos)
    return fused


@dataclass
class Retrieved:
    """一条检索结果，同时保留两路召回各自的分数与排名，便于展示与调参。"""

    chunk: Chunk
    vector_score: float = 0.0
    vector_rank: int = 0        # 0 表示未被向量召回
    bm25_score: float = 0.0
    bm25_rank: int = 0          # 0 表示未被 BM25 召回
    rrf_score: float = 0.0


class HybridRetriever:
    """
    混合检索器：BM25 ∥ 向量 → RRF 融合 → Top-K。

    生命周期约定（阶段4方案 7.3 红线）：
      - 只在应用 lifespan 阶段 build_or_load() 一次，挂在 app.state
      - 请求期只读（FAISS search 线程安全，BM25 只读数组），无锁需求
      - 磁盘缓存沿用 .cache/，冷启动一次构建、热启动秒级加载
    """

    # 缓存清单版本号：切片逻辑或索引结构变更时 +1，强制重建缓存
    INDEX_VERSION = "v1"
    # "依据不足"判定的向量相似度门槛。
    # 为什么不能只用 RRF 分数判定：RRF 是"排名"函数，只要有候选就一定有分数，
    # 即使问题与知识库毫无关系，向量那一路也会返回 Top10，RRF 分数照样能到 0.03。
    # 所以必须回到原始相似度上判定。
    HASH_MIN_SCORE = 0.08
    SEMANTIC_MIN_SCORE = 0.30

    def __init__(self, cfg: Settings, embedder: BaseEmbedder):
        self.cfg = cfg
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self.vector_index = VectorIndex()
        self.bm25_index: BM25Index | None = None
        self._vectors: "np.ndarray | None" = None
        self.cache_hit = False

    # ---------------- 缓存清单 ----------------
    def _manifest(self, kb_dir: Path) -> str:
        """
        计算知识库指纹：文件内容 + 切片参数 + Embedding 模型名。
        三者任一变化都必须重建索引，否则会出现"文档改了但检索还是旧内容"的严重问题。
        """
        hasher = hashlib.sha256()
        hasher.update(self.INDEX_VERSION.encode())
        hasher.update(f"{self.cfg.chunk_size}|{self.cfg.chunk_overlap}".encode())
        hasher.update(self.embedder.name.encode())
        for md in sorted(kb_dir.rglob("*.md")):
            hasher.update(md.relative_to(kb_dir).as_posix().encode())
            hasher.update(md.read_bytes())
        return hasher.hexdigest()[:16]

    # ---------------- 构建 / 加载 ----------------
    def build_or_load(self) -> None:
        kb_dir = self.cfg.kb_path
        cache_root = self.cfg.cache_path / f"kb_{self._manifest(kb_dir)}"

        chunks_json = cache_root / "chunks.json"
        vectors_npy = cache_root / "vectors.npy"
        faiss_index = cache_root / "faiss.index"

        if chunks_json.exists() and vectors_npy.exists():
            self._load_cache(chunks_json, vectors_npy, faiss_index)
            self.cache_hit = True
            return

        # ---- 缓存未命中：从头构建 ----
        t0 = time.perf_counter()
        logger.info("首次构建知识库索引（后续运行将直接命中缓存）…")
        self.chunks = load_markdown_chunks(kb_dir, self.cfg.chunk_size, self.cfg.chunk_overlap)
        if not self.chunks:
            raise ValueError("知识库切片结果为空，请检查文档内容")

        logger.info("切片完成：%s 个文本块，开始向量化…", len(self.chunks))
        texts = [c.index_text() for c in self.chunks]   # 索引文本 = 标题路径 + 正文
        vectors = self.embedder.encode(texts)
        self._vectors = vectors
        self.vector_index.build(vectors)

        logger.info("构建 BM25 索引…")
        self.bm25_index = BM25Index([_bm25_doc_tokens(c) for c in self.chunks])

        # ---- 落盘缓存 ----
        cache_root.mkdir(parents=True, exist_ok=True)
        self.vector_index.save(faiss_index, vectors_npy, vectors)
        chunks_json.write_text(
            json.dumps([asdict(c) for c in self.chunks], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        (cache_root / "manifest.json").write_text(json.dumps({
            "embedder": self.embedder.name,
            "dim": int(vectors.shape[1]),
            "chunks": len(self.chunks),
            "chunk_size": self.cfg.chunk_size,
            "chunk_overlap": self.cfg.chunk_overlap,
            "built_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info("索引构建完成，耗时 %.2fs，缓存目录：%s", time.perf_counter() - t0, cache_root.name)

    def _load_cache(self, chunks_json: Path, vectors_npy: Path, faiss_index: Path) -> None:
        data = json.loads(chunks_json.read_text(encoding="utf-8"))
        self.chunks = [Chunk(**d) for d in data]
        self._vectors = np.load(vectors_npy)
        self.vector_index.load(faiss_index, vectors_npy)
        self.bm25_index = BM25Index([_bm25_doc_tokens(c) for c in self.chunks])
        logger.info("命中索引缓存：%s 个文本块（向量后端：%s）",
                    len(self.chunks), self.vector_index.backend)

    # ---------------- 检索 ----------------
    def retrieve(self, query: str, top_k: int | None = None) -> list[Retrieved]:
        """执行混合检索，返回按 RRF 分数降序的 Top-K。"""
        if not self.chunks:
            raise RuntimeError("检索器尚未初始化，请先调用 build_or_load()")
        query = normalize_query(query)
        if not query:
            return []
        top_k = top_k or self.cfg.top_k

        # ---- 路 1：BM25 ----
        bm25_scores, bm25_idxs = self.bm25_index.search(query, self.cfg.bm25_candidates)
        bm25_rank = {doc: r for r, doc in enumerate(bm25_idxs, start=1)}
        bm25_map = dict(zip(bm25_idxs, bm25_scores))

        # ---- 路 2：向量 ----
        qv = self.embedder.encode_one(query)
        vec_scores, vec_idxs = self.vector_index.search(np.asarray(qv), self.cfg.vector_candidates)
        vec_rank = {doc: r for r, doc in enumerate(vec_idxs, start=1) if doc >= 0}
        vec_map = {doc: s for doc, s in zip(vec_idxs, vec_scores) if doc >= 0}

        # ---- 融合：RRF 只看排名，不掺分数 ----
        fused = rrf_fuse([bm25_idxs, vec_idxs], k=self.cfg.rrf_k)

        results: list[Retrieved] = []
        for doc_idx, score in sorted(fused.items(), key=lambda kv_: -kv_[1])[:top_k]:
            results.append(Retrieved(
                chunk=self.chunks[doc_idx],
                vector_score=float(vec_map.get(doc_idx, 0.0)),
                vector_rank=vec_rank.get(doc_idx, 0),
                bm25_score=float(bm25_map.get(doc_idx, 0.0)),
                bm25_rank=bm25_rank.get(doc_idx, 0),
                rrf_score=float(score),
            ))
        return results

    def has_evidence(self, results: list[Retrieved], query: str = "") -> bool:
        """
        判断检索结果是否足以支撑回答。

        判定规则（二者满足其一即可）：
          1. 命中了关键词：Top1 片段至少命中 2 个不同的查询词
          2. 语义足够接近：Top1 的余弦相似度超过该 Embedding 模式的门槛

        这一层很重要：没有它，问"今天食堂有什么菜"也会被强行检索出 3 个片段，
        然后大模型基于无关片段硬编一个答案——这正是我们要杜绝的行为。
        """
        if not results:
            return False
        top = results[0]

        # 规则 1：要求 Top1 片段至少命中 2 个不同的查询词。
        # 只命中 1 个词不足以作为依据——问"今天食堂有什么菜"时，"今天"这个词
        # 恰好出现在某条归属说明里，单靠它就命中会得到完全无关的答案。
        if query:
            q_tokens = {t for t in tokenize_zh(normalize_query(query)) if len(t) > 0}
            if q_tokens:
                text = top.chunk.index_text()
                hit = sum(1 for t in q_tokens if t in text)
                need = 1 if len(q_tokens) == 1 else 2
                if hit >= need:
                    return True

        # 规则 2：语义相似度达到该 Embedding 模式的门槛
        threshold = self.HASH_MIN_SCORE if self.embedder.name.startswith("hash") else self.SEMANTIC_MIN_SCORE
        return top.vector_score >= threshold
