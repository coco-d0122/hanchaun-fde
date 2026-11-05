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

from app.core.config import Settings
from app.rag.bm25 import BM25Index, _bm25_doc_tokens
from app.rag.embeddings import BaseEmbedder
from app.rag.index_manager import VectorIndex
from app.rag.loader import Chunk, load_markdown_chunks, markdown_file_chunks, plain_text_chunks
from app.rag.text import normalize_query, tokenize_zh

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class DocEntry:
    """
    一份"已发布"文档的检索输入（阶段5第二批）。

    知识库不再直接扫目录，而是以 knowledge_documents 表中 status=published
    的记录为准；file_path 指向原始文件（knowledge_base/ 内建或 uploads/ 上传），
    display_name 是检索引用时展示的文件名。
    """
    doc_id: int
    file_path: Path
    display_name: str
    content_hash: str        # 原始文件字节 SHA256 前 16 位（缓存复用与幂等重处理的依据）


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
    HASH_MIN_SCORE = 0.12
    SEMANTIC_MIN_SCORE = 0.30
    # "依据充足"的词汇覆盖率门槛（F-01 修复）：命中词数 / 查询词数。
    # 实测：7 条制度问答覆盖率 0.43~1.00；3 条陷阱题 0.25~0.30（只命中通用词）。
    MIN_LEXICAL_COVERAGE = 0.35
    # 语义兜底路径要求的最低覆盖率：避免"语义分数高但词基本对不上"时放行。
    MIN_FALLBACK_COVERAGE = 0.25

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

    # ---------------- 构建入口 2：按文档构建（阶段5第二批） ----------------
    def build_from_documents(self, entries: list[DocEntry]) -> None:
        """
        从文档档案列表构建检索器（上传/删除/重处理后的重建路径）。

        与 build_or_load 的区别：内容来源从"扫目录"变为"DB 中的已发布文档清单"。
        缓存粒度从"整库一个目录"细化到"每文档一个缓存文件"——
        这让上传一篇新文档不必重算全部 6 篇内建文档的向量。

        缓存键 = doc_id + content_hash：内容不变直接复用切片与向量
        （"重新处理"幂等；改一个字则内容哈希变化，自然重算）。

        ★ 影子构建语义：本方法在【新的 HybridRetriever 实例】上执行，
          构建过程中任何一步抛异常，调用方直接丢弃该实例，
          app.state.retriever 上的旧对象从未被触碰——线上问答不受影响。
          全部成功后由调用方做唯一的引用替换（原子切换）。
        """
        t0 = time.perf_counter()
        doc_cache_dir = self.cfg.cache_path / "docs"
        doc_cache_dir.mkdir(parents=True, exist_ok=True)

        all_chunks: list[Chunk] = []
        vec_list: list[np.ndarray] = []

        for entry in entries:
            chunks, vectors = self._load_doc_with_cache(doc_cache_dir, entry)
            if not chunks:
                logger.warning("文档 %s（#%s）切片为空，跳过", entry.display_name, entry.doc_id)
                continue
            all_chunks.extend(chunks)
            vec_list.append(vectors)

        if not all_chunks:
            raise ValueError("知识库没有已发布文档，检索器拒绝构建为空索引")

        # ---- 组装联合索引（FAISS + BM25）----
        self.chunks = all_chunks
        self._vectors = np.vstack(vec_list).astype("float32")
        self.vector_index = VectorIndex()
        self.vector_index.build(self._vectors)
        self.bm25_index = BM25Index([_bm25_doc_tokens(c) for c in self.chunks])
        logger.info("按文档构建索引完成：%s 份文档 / %s 个切片，耗时 %.2fs",
                    len(entries), len(self.chunks), time.perf_counter() - t0)

    def _load_doc_with_cache(self, cache_dir: Path,
                             entry: DocEntry) -> tuple[list[Chunk], np.ndarray]:
        """
        单文档：命中缓存直接读，未命中则 解析→切片→向量化→写缓存。

        缓存文件按 content_hash 命名，内容不可变 → 缓存文件写一半被中断也
        只是留下一个不会被命中的坏文件（下次 hash 相同重新计算并覆盖），
        不会污染正在服务的线上索引。
        """
        stem = Path(entry.display_name).stem
        prefix = f"doc{entry.doc_id}_{entry.content_hash[:12]}"
        chunks_file = cache_dir / f"{prefix}_chunks.json"
        vectors_file = cache_dir / f"{prefix}_vectors.npy"

        if chunks_file.exists() and vectors_file.exists():
            try:
                chunks = [Chunk(**d) for d in
                          json.loads(chunks_file.read_text(encoding="utf-8"))]
                vectors = np.load(vectors_file)
                if len(chunks) == len(vectors):
                    # 缓存里的 chunk_id 可能来自旧 display_name，重命名不影响检索，
                    # 但引用展示要一致——直接按当前名称重建 id 与来源字段
                    for i, c in enumerate(chunks, start=1):
                        c.chunk_id = f"{stem}::c{i:03d}"
                        c.source_file = entry.display_name
                    return chunks, vectors
                logger.warning("文档 %s 缓存维度不一致（%s 切片 vs %s 向量），重建",
                               entry.display_name, len(chunks), len(vectors))
            except Exception:  # noqa: BLE001  缓存损坏按未命中处理
                logger.warning("文档 %s 缓存读取失败，重建", entry.display_name)

        # ---- 未命中：解析 + 切片 + 向量化 ----
        chunks = self._parse_and_chunk(entry)
        if not chunks:
            return [], np.zeros((0, self.embedder.dim if hasattr(self.embedder, "dim") else 64),
                                dtype="float32")
        texts = [c.index_text() for c in chunks]
        vectors = self.embedder.encode(texts)

        chunks_file.write_text(
            json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=1),
            encoding="utf-8")
        np.save(vectors_file, vectors.astype("float32"))
        return chunks, vectors

    def _parse_and_chunk(self, entry: DocEntry) -> list[Chunk]:
        """按扩展名解析文档并切片。md 走标题路径切片；pdf 走纯文本切片。"""
        suffix = entry.file_path.suffix.lower()
        if suffix == ".md":
            return markdown_file_chunks(entry.file_path, entry.display_name,
                                        self.cfg.chunk_size, self.cfg.chunk_overlap)
        if suffix == ".pdf":
            from app.services.kb_service import extract_pdf_text  # 延迟导入避免环
            text = extract_pdf_text(entry.file_path)
            if not text.strip():
                return []
            return plain_text_chunks(text, entry.display_name,
                                      Path(entry.display_name).stem,
                                      self.cfg.chunk_size, self.cfg.chunk_overlap)
        raise ValueError(f"不支持的文档类型：{suffix}")

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

    def evidence_coverage(self, results: list[Retrieved], query: str = "") -> tuple[int, float]:
        """
        计算"查询词被证据集合覆盖的程度"，返回 (命中词数, 覆盖率)。

        为什么按"覆盖率"而不是"命中 2 个词"：
          陷阱题的典型形态是问题里的**通用词**在文档里出现过（园区 / 空调 / 租户），
          但问题真正问的**限定词**（运行小时 / 功率上限 / 违约金比例）在文档中根本不存在。
          只数命中个数时，这类问题会被误判为"有依据"（缺陷 F-01：实测命中 2~3 个通用词）。
          改用"命中词数 / 查询词总数"后，通用词再多也只能占查询词的一小部分。

        证据集合取 Top1 片段：这是一个经过实测的取舍。取 Top-K 并集会抬高陷阱题
        的覆盖率（3 条陷阱题从 0.25~0.30 抬到 0.50~0.60），因为无关片段里总能
        找到一两个问题里的通用词；而 Top1 才携带"该问题最相关的那段条款"。
        分片导致的漏判由渲染层补齐（回答只在命中的片段内引用），不靠放宽门禁。
        """
        if not results or not query:
            return 0, 0.0
        q_tokens = {t for t in tokenize_zh(normalize_query(query)) if t}
        if not q_tokens:
            return 0, 0.0
        text = results[0].chunk.index_text()
        hit = sum(1 for t in q_tokens if t in text)
        return hit, hit / len(q_tokens)

    def has_evidence(self, results: list[Retrieved], query: str = "") -> bool:
        """
        判断检索结果是否足以支撑回答（缺陷 F-01 修复版）。

        判定规则（按优先级）：
          1. 词汇覆盖：至少命中 2 个查询词，且覆盖率达到 MIN_LEXICAL_COVERAGE
             —— 主路径，短文档集上最可靠；
          2. 语义兜底：覆盖率 ≥ 语义兜底最低覆盖，且 Top1 相似度超过该模式门槛
             —— 处理同义改写的提问，但两道条件同时成立才算数。

        这一层很重要：没有它，问"每台空调每天最多运行多少小时"也会被强行检索出
        3 个片段，然后基于无关片段硬编一个"限额"——这正是必须杜绝的幻觉来源。
        """
        if not results:
            return False
        hit, coverage = self.evidence_coverage(results, query)
        if hit >= 2 and coverage >= self.MIN_LEXICAL_COVERAGE:
            return True

        top = results[0]
        threshold = (self.HASH_MIN_SCORE if self.embedder.name.startswith("hash")
                     else self.SEMANTIC_MIN_SCORE)
        return hit >= 2 and coverage >= self.MIN_FALLBACK_COVERAGE \
            and top.vector_score >= threshold
