# -*- coding: utf-8 -*-
"""
向量索引（阶段3原样迁移）

FAISS IndexFlatIP（内积 = 余弦，因为向量已归一化）。
为什么用 Flat 而不是 IVF/HNSW：知识库切片量级在 10^2 ~ 10^4，
Flat 暴力检索耗时 < 1ms，而 IVF/HNSW 需要训练且引入近似误差。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import faiss  # type: ignore
    HAS_FAISS = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore
    HAS_FAISS = False


class VectorIndex:
    """向量索引。优先 FAISS，缺失时降级 numpy 暴力检索（功能等价，仅性能差）。"""

    def __init__(self):
        self._index = None
        self.dim = 0
        self.backend = "faiss" if HAS_FAISS else "numpy"
        self._matrix: "np.ndarray | None" = None

    def build(self, vectors: "np.ndarray") -> None:
        if vectors.size == 0:
            raise ValueError("向量矩阵为空，无法建索引")
        self.dim = vectors.shape[1]
        if HAS_FAISS:
            index = faiss.IndexFlatIP(self.dim)
            index.add(vectors.astype("float32"))
            self._index = index
        else:
            self._matrix = vectors.astype("float32")

    def search(self, query: "np.ndarray", k: int) -> tuple[list[float], list[int]]:
        if self.dim == 0:
            return [], []
        q = query.reshape(1, -1).astype("float32")
        k = min(k, self.size())
        if HAS_FAISS and self._index is not None:
            scores, idxs = self._index.search(q, k)
            return scores[0].tolist(), idxs[0].tolist()
        sims = (self._matrix @ q.T).ravel()          # 归一化向量内积 = 余弦相似度
        order = np.argsort(-sims)[:k]
        return sims[order].tolist(), order.tolist()

    def size(self) -> int:
        return int(self._index.ntotal) if (HAS_FAISS and self._index is not None) else (
            int(self._matrix.shape[0]) if self._matrix is not None else 0)

    # ---- 持久化 ----
    # 注意：Windows 版 faiss 的 write_index/read_index 使用窄字符 fopen，
    # 遇到含中文的路径会报 "could not open ... No such file or directory"。
    # 因此这里改用 faiss.serialize_index() 取字节流，再用 Python 自己的文件 IO 落盘，
    # 彻底绕开路径编码问题（在中文用户名目录下同样可用）。
    def save(self, index_path: Path, vectors_path: Path, vectors: "np.ndarray") -> None:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(vectors_path, vectors.astype("float32"))
        if HAS_FAISS and self._index is not None:
            np.save(self._blob_path(index_path), faiss.serialize_index(self._index))

    def load(self, index_path: Path, vectors_path: Path) -> None:
        vectors = np.load(vectors_path)
        self.dim = vectors.shape[1]
        blob = self._blob_path(index_path)
        if HAS_FAISS and blob.exists():
            self._index = faiss.deserialize_index(np.load(blob))
        else:
            self._matrix = vectors.astype("float32")

    @staticmethod
    def _blob_path(index_path: Path) -> Path:
        """faiss 序列化字节流的落盘路径（用 .npy 容器装 uint8 数组）。"""
        return index_path.with_name(index_path.name + ".npy")
