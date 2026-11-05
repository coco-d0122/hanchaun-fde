# -*- coding: utf-8 -*-
"""
Embedding 封装（阶段3原样迁移）

约定：encode() 返回 shape=(n, dim) 的 float32 矩阵，且每行已 L2 归一化。
归一化是必须的——FAISS 的 IndexFlatIP 只有在向量归一化后才等价于余弦相似度。

ApiEmbedder 仍用同步 requests（阶段4方案 7.1 的决策：仅在启动期调用，
不进请求路径，不改以降低回归风险）。
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Sequence

import numpy as np

from app.core.config import Settings
from app.core.text import tokenize_zh

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 调用异常（网络、鉴权、维度不匹配等）。"""


class BaseEmbedder:
    """Embedding 抽象基类。"""

    name: str = "base"
    dim: int = 0

    def encode(self, texts: Sequence[str]) -> "np.ndarray":
        raise NotImplementedError

    def encode_one(self, text: str) -> "np.ndarray":
        return self.encode([text])[0]


class HashEmbedder(BaseEmbedder):
    """
    离线哈希 Embedding（无任何外部依赖，保证系统在任何机器上都能跑）。

    原理（Hashing Trick + 字符 n-gram）：
      1. 把文本切成"词 + 字符二元组 + 字符三元组"，兼顾词级与字级信号
      2. 对每个 token 做稳定哈希，映射到 [0, dim) 的某一维
      3. 用哈希值再取一位决定符号（+1/-1），抵消哈希碰撞带来的系统性偏置
      4. 词频做 1+log(tf) 次线性压缩，避免长文本被高频词主导
      5. 最后 L2 归一化

    局限（必须写清楚，避免误导）：
      它本质是"词形相似"而非"语义相似"。同义词（如"倍率"与"变比"）不会自动靠近。
      因此它只作为**无 API Key 时的降级方案**；生产环境请用 api 或 st 模式。
    """

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.name = f"hash-{dim}"

    @staticmethod
    def _stable_hash(token: str) -> int:
        """用 blake2b 做稳定哈希（Python 内置 hash() 每次进程启动都会变，不能用）。"""
        return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")

    def _tokens(self, text: str) -> list[str]:
        text = text.lower()
        words = tokenize_zh(text)
        chars = [c for c in text if not c.isspace()]
        bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        trigrams = [chars[i] + chars[i + 1] + chars[i + 2] for i in range(len(chars) - 2)]
        return words + bigrams + trigrams

    def encode(self, texts: Sequence[str]) -> "np.ndarray":
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            tf: dict[str, int] = {}
            for tok in self._tokens(text):
                tf[tok] = tf.get(tok, 0) + 1
            for tok, cnt in tf.items():
                h = self._stable_hash(tok)
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                out[row, idx] += sign * (1.0 + math.log(cnt))
            norm = float(np.linalg.norm(out[row]))
            if norm > 0:
                out[row] /= norm
        return out


class ApiEmbedder(BaseEmbedder):
    """
    通过 OpenAI 兼容的 /embeddings 接口做向量化。

    兼容对象：OpenAI、DeepSeek（部分模型）、通义千问 DashScope 兼容模式、
             硅基流动、火山方舟、本地 vLLM / Xinference 等。
    """

    def __init__(self, model: str, base_url: str, api_key: str, timeout: int = 60, batch: int = 16):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.batch = batch
        self.name = f"api:{model}"
        self.dim = 0   # 首次调用后确定

    def _post(self, batch_texts: Sequence[str]) -> list[list[float]]:
        import requests  # 延迟导入：服务器环境可能刻意不装 requests
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "input": list(batch_texts), "encoding_format": "float"}
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.Timeout as exc:
            raise EmbeddingError(f"Embedding 请求超时（{self.timeout}s）：{url}") from exc
        except requests.RequestException as exc:
            raise EmbeddingError(f"Embedding 网络异常：{exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingError(f"Embedding 接口返回 {resp.status_code}：{resp.text[:300]}")

        try:
            data = resp.json()["data"]
            # 接口不保证按输入顺序返回，必须按 index 排序后再用
            data = sorted(data, key=lambda x: x.get("index", 0))
            return [d["embedding"] for d in data]
        except (KeyError, ValueError, TypeError) as exc:
            raise EmbeddingError(f"Embedding 响应解析失败：{resp.text[:300]}") from exc

    def encode(self, texts: Sequence[str]) -> "np.ndarray":
        if not texts:
            return np.zeros((0, 0), dtype="float32")
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            vectors.extend(self._post(texts[i:i + self.batch]))
        arr = np.asarray(vectors, dtype="float32")
        self.dim = arr.shape[1]
        # 强制 L2 归一化：不同供应商返回的向量可能未归一化
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


class SentenceTransformerEmbedder(BaseEmbedder):
    """
    本地中文 Embedding（sentence-transformers + BGE 系列）。

    优点：数据不出本机，适合"原始数据不得出园"的场景。
    代价：首次运行需下载模型（bge-small-zh 约 100MB），且依赖 torch。
    """

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise EmbeddingError(
                "未安装 sentence-transformers。请执行： pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str]) -> "np.ndarray":
        arr = self._model.encode(list(texts), batch_size=16,
                                 normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(arr, dtype="float32")


def build_embedder(cfg: Settings) -> BaseEmbedder:
    """
    Embedding 工厂：按配置选择实现，失败时逐级降级，保证程序一定能启动。

    降级顺序：显式配置 → st（本地）→ api（有 Key 时）→ hash（离线兜底）
    """
    choice = cfg.embedding_provider

    if choice == "hash":
        logger.info("Embedding：hash 模式（离线，%s 维）", cfg.embedding_dim)
        return HashEmbedder(cfg.embedding_dim)

    if choice == "st":
        try:
            emb = SentenceTransformerEmbedder(cfg.embedding_model)
            logger.info("Embedding：本地模型 %s（%s 维）", cfg.embedding_model, emb.dim)
            return emb
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地 Embedding 加载失败，降级：%s", exc)
            return HashEmbedder(cfg.embedding_dim)

    if choice == "api":
        if not cfg.embedding_api_key:
            logger.warning("EMBEDDING_PROVIDER=api 但缺少 API Key，降级为 hash 模式")
            return HashEmbedder(cfg.embedding_dim)
        logger.info("Embedding：API 模式 %s", cfg.embedding_model)
        return ApiEmbedder(cfg.embedding_model, cfg.embedding_base_url,
                           cfg.embedding_api_key, cfg.llm_timeout)

    # ---- auto：自动探测 ----
    try:
        emb = SentenceTransformerEmbedder(cfg.embedding_model)
        logger.info("Embedding：自动选择本地模型 %s（%s 维）", cfg.embedding_model, emb.dim)
        return emb
    except Exception:
        pass
    if cfg.embedding_api_key:
        logger.info("Embedding：自动选择 API 模式 %s", cfg.embedding_model)
        return ApiEmbedder(cfg.embedding_model, cfg.embedding_base_url,
                           cfg.embedding_api_key, cfg.llm_timeout)

    logger.info("Embedding：未检测到本地模型或 API Key，使用离线 hash 模式（%s 维）",
                cfg.embedding_dim)
    return HashEmbedder(cfg.embedding_dim)
