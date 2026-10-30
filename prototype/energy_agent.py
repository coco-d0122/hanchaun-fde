#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 瀚川工业园 · 园区能耗监测与异常定位智能体
 第一阶段：命令行最小可运行原型（Single-File Prototype）
================================================================================

【本原型要验证的三件事】
  1. 能不能靠"知识库检索"回答园区能源管理问题（BM25 + 向量，RRF 融合）
  2. 能不能读取模拟电表数据并做确定性计量异常检测（倍率变化 / 总分表平衡差）
  3. 能不能把"异常检测结果 + 知识库内容"喂给大模型，产出有依据的分析

【四条设计红线（与架构方案一致，不得突破）】
  R1. 所有数值计算由 Python 确定性代码完成，大模型不参与算数
  R2. 大模型只能提出"可能原因"，不得把推测写成已确认事实
  R3. 大模型不修改原始数据、不控制设备（本原型中 LLM 是纯函数调用，无副作用）
  R4. 每条 AI 结论必须附"数据来源 + 知识来源"，缺证据时必须输出"待人工核实"

【代码标记说明】
  【核心模块-后续复用】 → 可直接搬进 FastAPI 后端（或只改依赖注入方式）
  【原型模块-后续替换】 → 仅用于命令行演示，正式系统会被 API + 前端替换

【运行】
  pip install -r requirements.txt
  python energy_agent.py            # 交互菜单
  python energy_agent.py --demo     # 非交互，一把跑完全链路（推荐先跑这个）
  python energy_agent.py --ask "电表倍率发生变化时应该如何处理？"

作者：架构组 · 2026-10
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

# ------------------------------------------------------------------------------
# 依赖加载：核心依赖缺失时给出明确指引，可选依赖缺失时自动降级
# ------------------------------------------------------------------------------
try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("缺少核心依赖 numpy，请执行： pip install -r requirements.txt")

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少核心依赖 requests，请执行： pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        """没有 python-dotenv 时退化为空实现，仍可通过系统环境变量配置。"""
        return False

# FAISS：向量检索主路径；缺失时降级为 numpy 暴力检索（功能等价，仅性能差）
try:
    import faiss  # type: ignore
    HAS_FAISS = True
except ImportError:
    faiss = None  # type: ignore
    HAS_FAISS = False

# 中文分词：BM25 的前提；缺失时降级为"单字+二元组"切分
try:
    import jieba  # type: ignore
    jieba.setLogLevel(logging.ERROR)   # 关闭 jieba 首次运行的构建日志噪音
    HAS_JIEBA = True
except ImportError:
    jieba = None  # type: ignore
    HAS_JIEBA = False

# BM25：优先用成熟库；缺失时用内置实现（算法一致，代码见 SimpleBM25）
try:
    from rank_bm25 import BM25Okapi  # type: ignore
    HAS_RANK_BM25 = True
except ImportError:
    BM25Okapi = None  # type: ignore
    HAS_RANK_BM25 = False


# ==============================================================================
# 第一部分：全局配置
# 【核心模块-后续复用】——正式系统改为 pydantic-settings 读取，字段名保持一致
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent


def _env(key: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白（.env 里经常带多余空格）。"""
    return (os.getenv(key) or default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(_env(key, str(default))))
    except ValueError:
        return default


@dataclass
class Config:
    """运行配置。全部来自 .env 或系统环境变量，代码中不出现任何密钥。"""

    # ---- 大模型 ----
    llm_provider: str = "openai"          # openai | deepseek | qwen | moonshot | mock
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60
    llm_temperature: float = 0.2          # 低温度：分析类任务要稳，不要发散
    llm_max_tokens: int = 1400

    # ---- Embedding ----
    embedding_provider: str = "auto"      # auto | hash | api | st
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_dim: int = 1024             # 仅 hash 模式使用

    # ---- 知识库与切片 ----
    kb_dir: str = "knowledge_base"
    data_dir: str = "data"
    cache_dir: str = ".cache"
    output_dir: str = "output"
    chunk_size: int = 500                 # 目标切片长度（中文字符）
    chunk_overlap: int = 50               # 相邻切片重叠字符数
    top_k: int = 3                        # 最终送入 Prompt 的切片数

    # ---- 检索参数 ----
    rrf_k: int = 60                       # RRF 融合平滑常数，业界常用 60
    vector_candidates: int = 10           # 向量召回候选数
    bm25_candidates: int = 10             # BM25 召回候选数

    # ---- 异常检测参数（模拟测试用，非园区已确认规则）----
    balance_diff_threshold: float = 0.03  # 总分表差异率阈值 3%
    balance_min_energy_kwh: float = 1.0   # 低于此电量不参与平衡判定，避免除零/噪声
    ratio_change_factor: float = 1.5      # 倍率变化倍数超过 1.5 倍即触发待核查

    @classmethod
    def load(cls) -> "Config":
        """从 .env / 环境变量装配配置对象。"""
        load_dotenv(BASE_DIR / ".env", override=False)
        cfg = cls(
            llm_provider=_env("LLM_PROVIDER", "openai").lower(),
            llm_api_key=_env("LLM_API_KEY"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            llm_model=_env("LLM_MODEL", "deepseek-chat"),
            llm_timeout=_env_int("LLM_TIMEOUT", 60),
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.2),
            llm_max_tokens=_env_int("LLM_MAX_TOKENS", 1400),
            embedding_provider=_env("EMBEDDING_PROVIDER", "auto").lower(),
            embedding_model=_env("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
            embedding_api_key=_env("EMBEDDING_API_KEY") or _env("LLM_API_KEY"),
            embedding_base_url=(_env("EMBEDDING_BASE_URL") or _env("LLM_BASE_URL")).rstrip("/"),
            embedding_dim=_env_int("EMBEDDING_DIM", 1024),
            kb_dir=_env("KB_DIR", "knowledge_base"),
            data_dir=_env("DATA_DIR", "data"),
            cache_dir=_env("CACHE_DIR", ".cache"),
            output_dir=_env("OUTPUT_DIR", "output"),
            chunk_size=_env_int("CHUNK_SIZE", 500),
            chunk_overlap=_env_int("CHUNK_OVERLAP", 50),
            top_k=_env_int("TOP_K", 3),
            balance_diff_threshold=_env_float("BALANCE_DIFF_THRESHOLD", 0.03),
            balance_min_energy_kwh=_env_float("BALANCE_MIN_ENERGY_KWH", 1.0),
            ratio_change_factor=_env_float("RATIO_CHANGE_FACTOR", 1.5),
        )
        return cfg


CFG = Config.load()


# ==============================================================================
# 第二部分：终端输出美化
# 【原型模块-后续替换】——正式系统由前端渲染，这部分整体丢弃
# ==============================================================================

class C:
    """ANSI 颜色常量。Windows 下由 enable_ansi() 打开 VT 支持。"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[36m"
    MAGENTA = "\033[35m"
    GRAY = "\033[90m"


def enable_ansi() -> None:
    """在 Windows 控制台启用 ANSI 转义序列（否则会打印出乱码般的 [?32m）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # type: ignore[attr-defined]
    except Exception:
        pass


def hr(ch: str = "─", width: int = 78, color: str = C.GRAY) -> None:
    print(f"{color}{ch * width}{C.RESET}")


def h1(text: str) -> None:
    print()
    hr("═")
    print(f"{C.BOLD}{C.BLUE}  {text}{C.RESET}")
    hr("═")


def h2(text: str) -> None:
    print(f"\n{C.BOLD}{C.MAGENTA}▌ {text}{C.RESET}")


def ok(text: str) -> None:
    print(f"{C.GREEN}✔ {text}{C.RESET}")


def warn(text: str) -> None:
    print(f"{C.YELLOW}⚠ {text}{C.RESET}")


def err(text: str) -> None:
    print(f"{C.RED}✘ {text}{C.RESET}")


def info(text: str) -> None:
    print(f"{C.GRAY}· {text}{C.RESET}")


def _dwidth(text: str) -> int:
    """计算字符串的终端显示宽度：中文/全角字符占 2 列，其余占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格，保证中文标签也能对齐（str.ljust 对中文会错位）。"""
    return text + " " * max(0, width - _dwidth(text))


def kv(key: str, value: Any, width: int = 18) -> None:
    print(f"  {C.GRAY}{_pad(key, width)}{C.RESET}{value}")


# ==============================================================================
# 第三部分：中文分词工具
# 【核心模块-后续复用】——BM25 与 hash embedding 共用
# ==============================================================================

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


# ==============================================================================
# 第四部分：Embedding 封装
# 【核心模块-后续复用】——正式系统替换为依赖注入，接口签名不变
# ==============================================================================

class EmbeddingError(Exception):
    """Embedding 调用异常（网络、鉴权、维度不匹配等）。"""


class BaseEmbedder:
    """
    Embedding 抽象基类。

    约定：encode() 返回 shape=(n, dim) 的 float32 矩阵，且**每行已 L2 归一化**。
    归一化是必须的——FAISS 的 IndexFlatIP 只有在向量归一化后才等价于余弦相似度。
    """

    name: str = "base"
    dim: int = 0

    def encode(self, texts: Sequence[str]) -> "np.ndarray":
        raise NotImplementedError

    def encode_one(self, text: str) -> "np.ndarray":
        return self.encode([text])[0]


class HashEmbedder(BaseEmbedder):
    """
    离线哈希 Embedding（无任何外部依赖，保证原型在任何机器上都能跑）。

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


def build_embedder(cfg: Config) -> BaseEmbedder:
    """
    Embedding 工厂：按配置选择实现，失败时逐级降级，保证程序一定能启动。

    降级顺序：显式配置 → st（本地）→ api（有 Key 时）→ hash（离线兜底）
    """
    choice = cfg.embedding_provider

    if choice == "hash":
        info(f"Embedding：hash 模式（离线，{cfg.embedding_dim} 维）")
        return HashEmbedder(cfg.embedding_dim)

    if choice == "st":
        try:
            emb = SentenceTransformerEmbedder(cfg.embedding_model)
            info(f"Embedding：本地模型 {cfg.embedding_model}（{emb.dim} 维）")
            return emb
        except Exception as exc:  # noqa: BLE001
            warn(f"本地 Embedding 加载失败，降级：{exc}")
            return HashEmbedder(cfg.embedding_dim)

    if choice == "api":
        if not cfg.embedding_api_key:
            warn("EMBEDDING_PROVIDER=api 但缺少 API Key，降级为 hash 模式")
            return HashEmbedder(cfg.embedding_dim)
        info(f"Embedding：API 模式 {cfg.embedding_model}")
        return ApiEmbedder(cfg.embedding_model, cfg.embedding_base_url,
                           cfg.embedding_api_key, cfg.llm_timeout)

    # ---- auto：自动探测 ----
    try:
        emb = SentenceTransformerEmbedder(cfg.embedding_model)
        info(f"Embedding：自动选择本地模型 {cfg.embedding_model}（{emb.dim} 维）")
        return emb
    except Exception:
        pass
    if cfg.embedding_api_key:
        info(f"Embedding：自动选择 API 模式 {cfg.embedding_model}")
        return ApiEmbedder(cfg.embedding_model, cfg.embedding_base_url,
                           cfg.embedding_api_key, cfg.llm_timeout)

    info(f"Embedding：未检测到本地模型或 API Key，使用离线 hash 模式（{cfg.embedding_dim} 维）")
    info("        提示：配置 EMBEDDING_PROVIDER=api 可获得语义级检索效果")
    return HashEmbedder(cfg.embedding_dim)


# ==============================================================================
# 第五部分：Markdown 加载与切片
# 【核心模块-后续复用】——正式系统原样搬进 knowledge 服务
# ==============================================================================

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


@dataclass
class Chunk:
    """一个可检索的文本切片及其全部溯源元数据。"""

    chunk_id: str            # 全局唯一：{文件名}::c{序号}
    content: str
    source_file: str         # 相对知识库根目录的文件名
    doc_title: str           # 文档标题（一级标题）
    heading_path: str        # 标题路径，如 "2 倍率管理 > 2.2 变更流程"
    chunk_index: int         # 文档内块序号（从 1 开始）
    char_start: int          # 在原文中的起始字符偏移（便于人工复核定位）
    char_end: int

    def citation(self) -> str:
        """生成人可读的引用标签，用于在回答中标注来源。"""
        return f"{self.source_file} · {self.heading_path} · 块{self.chunk_index}"

    def index_text(self) -> str:
        """
        用于建索引的文本 = 标题路径 + 正文。

        为什么要拼标题：切片正文里往往不出现"总表""分表"这类主题词
        （它们在文档标题里），不拼的话 BM25 与向量都会漏召。
        这是检索质量上性价比最高的一个改动，几乎零成本。
        注意：拼进去的标题只用于"匹配"，展示与喂给大模型时仍用纯正文 content。
        """
        return f"{self.doc_title} · {self.heading_path}\n{self.content}"


def _split_long_paragraph(text: str, chunk_size: int, overlap: int) -> list[str]:
    """单段文字超过 chunk_size 时按窗口硬切，保留 overlap 重叠，避免语义被拦腰截断。"""
    pieces: list[str] = []
    step = max(1, chunk_size - overlap)
    for start in range(0, len(text), step):
        piece = text[start:start + chunk_size]
        if piece.strip():
            pieces.append(piece.strip())
        if start + chunk_size >= len(text):
            break
    return pieces


def pack_paragraphs(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    按段落把正文打包成接近 chunk_size 的切片（核心切片算法）。

    为什么按段落而不是定长切？
      定长切会把"倍率变更需填写《计量配置变更单》"这种完整句子切成两半，
      检索命中后上下文残缺，喂给大模型反而制造幻觉。

    算法：
      1. 按空行拆成段落
      2. 逐段往 buffer 里塞，塞不下就落盘当前 buffer
      3. 新 buffer 用"上一块末尾 overlap 个字符"作为前缀，保证跨块语义连续
      4. 超长段落单独硬切
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        # 超长段落：先落盘当前 buffer，再硬切
        if len(para) > chunk_size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_split_long_paragraph(para, chunk_size, overlap))
            continue

        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 1 <= chunk_size:
            buffer = f"{buffer}\n{para}"
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap > 0 else ""
            buffer = f"{tail}\n{para}".strip() if tail else para

    if buffer:
        chunks.append(buffer)
    return chunks


def load_markdown_chunks(kb_dir: Path, chunk_size: int, overlap: int) -> list[Chunk]:
    """
    加载知识库目录下所有 .md 文件并切片。

    标题处理：维护一个标题栈，遇到新标题就更新路径；
              这样每个切片都能带上"2 倍率管理 > 2.2 变更流程"这样的路径，
              引用时可读性远好于只给文件名。
    """
    if not kb_dir.exists():
        raise FileNotFoundError(f"知识库目录不存在：{kb_dir}")

    md_files = sorted(kb_dir.rglob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"知识库目录下没有 .md 文件：{kb_dir}")

    all_chunks: list[Chunk] = []

    for md_path in md_files:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        rel_name = md_path.relative_to(kb_dir).as_posix()

        # --- 1) 按标题切分为"节"，同时记录每节在原文中的偏移 ---
        lines = raw.splitlines()
        sections: list[tuple[str, str, int]] = []   # (heading_path, body, char_offset)
        stack: list[tuple[int, str]] = []
        buf: list[str] = []
        sec_start = 0
        cursor = 0

        def _flush() -> None:
            body = "\n".join(buf).strip()
            if body:
                path = " > ".join(t for _, t in stack) if stack else md_path.stem
                sections.append((path, body, sec_start))

        for line in lines:
            m = HEADING_RE.match(line)
            if m:
                _flush()
                level = len(m.group(1))
                title = m.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                buf = []
                sec_start = cursor + len(line) + 1
            else:
                # 跳过引用块（> 开头的行）。知识库文档顶部的"模拟规则"声明属于
                # 文档元信息而非规则正文，若进入索引会被检索命中并当作依据返回，
                # 既污染上下文又浪费 token。
                if not line.lstrip().startswith(">"):
                    buf.append(line)
            cursor += len(line) + 1
        _flush()

        # --- 2) 文档标题 = 第一个一级标题 ---
        doc_title = md_path.stem
        for path, _, _ in sections:
            first = path.split(" > ")[0]
            if first:
                doc_title = first
                break

        # --- 3) 把相邻小节点合并成接近 chunk_size 的切片 ---
        # 为什么要合并：Markdown 的小节往往只有一两百字，若"一节=一块"，
        # 切片过碎会让单个片段缺少上下文，既降低检索质量，也让大模型看到的依据残缺。
        # 合并后每块约 500 字，通常覆盖 2~4 个相邻小节，语义更完整。
        idx = 0

        def _emit(path: str, text: str, offset: int) -> None:
            nonlocal idx
            idx += 1
            all_chunks.append(Chunk(
                chunk_id=f"{Path(rel_name).stem}::c{idx:03d}",
                content=text.strip(),
                source_file=rel_name,
                doc_title=doc_title,
                heading_path=path,
                chunk_index=idx,
                char_start=offset,
                char_end=offset + len(text),
            ))

        buffer_text = ""
        buffer_path = ""
        buffer_offset = 0

        for heading_path, body, offset in sections:
            # 单节超长：先落盘当前缓冲，再对超长节单独按段落打包
            if len(body) > chunk_size:
                if buffer_text:
                    _emit(buffer_path, buffer_text, buffer_offset)
                    buffer_text, buffer_path = "", ""
                for piece in pack_paragraphs(body, chunk_size, overlap):
                    _emit(heading_path, piece, offset)
                continue

            if not buffer_text:
                buffer_text, buffer_path, buffer_offset = body, heading_path, offset
            elif len(buffer_text) + len(body) + 2 <= chunk_size:
                buffer_text = f"{buffer_text}\n\n{body}"
            else:
                _emit(buffer_path, buffer_text, buffer_offset)
                buffer_text, buffer_path, buffer_offset = body, heading_path, offset

        if buffer_text:
            _emit(buffer_path or md_path.stem, buffer_text, buffer_offset)

    return all_chunks


# ==============================================================================
# 第六部分：检索索引（向量 FAISS + BM25 + RRF 融合）
# 【核心模块-后续复用】——正式系统换成独立索引服务时，接口保持不变
# ==============================================================================

class VectorIndex:
    """
    向量索引。优先 FAISS IndexFlatIP（内积 = 余弦，因为向量已归一化）。

    为什么用 Flat 而不是 IVF/HNSW？
      知识库切片量级在 10^2 ~ 10^4，Flat 暴力检索耗时 < 1ms，
      而 IVF/HNSW 需要训练且会引入近似误差。这个规模下 Flat 是唯一正确选择。
    """

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


@dataclass
class Retrieved:
    """一条检索结果，同时保留两路召回各自的分数与排名，便于在功能5中展示与调参。"""

    chunk: Chunk
    vector_score: float = 0.0
    vector_rank: int = 0        # 0 表示未被向量召回
    bm25_score: float = 0.0
    bm25_rank: int = 0          # 0 表示未被 BM25 召回
    rrf_score: float = 0.0


def _bm25_doc_tokens(chunk: "Chunk") -> list[str]:
    """
    BM25 文档词序列：正文词 + 标题词重复 3 次。

    这是对 BM25F（字段加权 BM25）的轻量近似：标题/小节名往往就是主题词
    （"总表与分表数据核对""倍率变更"），而正文里未必反复出现。
    不加权时，短文片段容易仅凭一两个偶然命中的词排到前面。
    """
    body = tokenize_zh(chunk.content)
    heading = tokenize_zh(f"{chunk.doc_title} {chunk.heading_path}")
    return body + heading * 3


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
      （k=60 是原论文给出的鲁棒默认值）。对于本项目这种"两路召回都还算靠谱"
      的场景，RRF 是最省心且效果稳定的选择。
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for pos, doc_idx in enumerate(ranking, start=1):
            fused[doc_idx] = fused.get(doc_idx, 0.0) + 1.0 / (k + pos)
    return fused


class HybridRetriever:
    """
    混合检索器：BM25 ∥ 向量 → RRF 融合 → Top-K。

    【核心模块-后续复用】正式系统只需把 _build 换成从数据库/缓存加载。
    """

    # 缓存清单版本号：切片逻辑或索引结构变更时 +1，强制重建缓存
    INDEX_VERSION = "v1"
    # "依据不足"判定的向量相似度门槛。
    # 为什么不能只用 RRF 分数判定：RRF 是"排名"函数，只要有候选就一定有分数，
    # 即使问题与知识库毫无关系，向量那一路也会返回 Top10，RRF 分数照样能到 0.03。
    # 所以必须回到原始相似度上判定。
    # hash 模式的余弦值天然被压缩（相关也就 0.15 左右），门槛要低；
    # 真实语义模型（bge / text-embedding）相关时通常在 0.4 以上，门槛可高。
    HASH_MIN_SCORE = 0.08
    SEMANTIC_MIN_SCORE = 0.30

    def __init__(self, cfg: Config, embedder: BaseEmbedder):
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
        kb_dir = BASE_DIR / self.cfg.kb_dir
        cache_root = BASE_DIR / self.cfg.cache_dir / f"kb_{self._manifest(kb_dir)}"

        chunks_json = cache_root / "chunks.json"
        vectors_npy = cache_root / "vectors.npy"
        faiss_index = cache_root / "faiss.index"

        if chunks_json.exists() and vectors_npy.exists():
            self._load_cache(chunks_json, vectors_npy, faiss_index)
            self.cache_hit = True
            return

        # ---- 缓存未命中：从头构建 ----
        t0 = time.perf_counter()
        info("首次构建知识库索引（后续运行将直接命中缓存）…")
        self.chunks = load_markdown_chunks(kb_dir, self.cfg.chunk_size, self.cfg.chunk_overlap)
        if not self.chunks:
            raise ValueError("知识库切片结果为空，请检查文档内容")

        info(f"切片完成：{len(self.chunks)} 个文本块，开始向量化…")
        texts = [c.index_text() for c in self.chunks]   # 索引文本 = 标题路径 + 正文
        vectors = self.embedder.encode(texts)
        self._vectors = vectors
        self.vector_index.build(vectors)

        info("构建 BM25 索引…")
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

        ok(f"索引构建完成，耗时 {time.perf_counter() - t0:.2f}s，缓存目录：{cache_root.name}")

    def _load_cache(self, chunks_json: Path, vectors_npy: Path, faiss_index: Path) -> None:
        data = json.loads(chunks_json.read_text(encoding="utf-8"))
        self.chunks = [Chunk(**d) for d in data]
        self._vectors = np.load(vectors_npy)
        self.vector_index.load(faiss_index, vectors_npy)
        self.bm25_index = BM25Index([_bm25_doc_tokens(c) for c in self.chunks])
        ok(f"命中索引缓存：{len(self.chunks)} 个文本块（向量后端：{self.vector_index.backend}）")

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
          1. 命中了关键词：Top1 在 BM25 那一路有排名且分数 > 0 —— 说明问题里的词
             确实出现在该片段中，这是最硬的证据；
          2. 语义足够接近：Top1 的余弦相似度超过该 Embedding 模式的门槛。

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


# ==============================================================================
# 第七部分：大模型统一调用
# 【核心模块-后续复用】——正式系统改为异步 httpx + 熔断，接口签名不变
# ==============================================================================

class LLMError(Exception):
    """大模型调用异常。所有上层业务只需捕获这一个类型。"""


# Prompt 中使用的结构化分隔符。用显眼标记而不是 Markdown 标题，
# 是为了让 mock provider 能可靠解析（真实模型也能理解这种结构）。
CTX_OPEN, CTX_CLOSE = "<<<CONTEXT>>>", "<<<END_CONTEXT>>>"
EV_OPEN, EV_CLOSE = "<<<EVIDENCE>>>", "<<<END_EVIDENCE>>>"


def _mock_llm(prompt: str) -> str:
    """
    离线模拟大模型（无 API Key 时使用）。

    ⚠️ 这不是"假装有大模型"，而是把"链路是否打通"与"模型是否可用"解耦：
       - 它严格只复述 Prompt 里已有的证据与检索片段，不引入任何新信息
       - 输出格式与真实模型完全一致，因此可以验证 Prompt 结构、解析逻辑、
         引用标注、以及"无依据时必须说不知道"的行为
       - 一旦配置了真实 API Key，这个分支永远不会被走到

    正式环境请务必配置 LLM_PROVIDER + LLM_API_KEY。
    """
    banner = "【离线模拟输出｜未调用真实大模型，配置 LLM_API_KEY 后自动切换】\n"

    # ---- 分支 1：异常分析 ----
    if EV_OPEN in prompt:
        try:
            raw = prompt.split(EV_OPEN, 1)[1].split(EV_CLOSE, 1)[0]
            ev = json.loads(raw)
        except (IndexError, json.JSONDecodeError):
            return banner + "证据解析失败，无法生成分析。请人工核实。"

        etype = ev.get("异常类型", "未知")
        meter = ev.get("电表编号", "-")
        lines = [banner]
        lines.append("【异常现象】")
        lines.append(
            f"{ev.get('监测窗口', '')} 期间，电表 {meter}（所属楼栋 {ev.get('楼栋', '-')}）"
            f"触发「{etype}」规则。规则判定结果：{ev.get('规则判定', '')}"
        )
        lines.append("")
        lines.append("【可能原因】（以下为待核实假设，不是已确认事实）")
        for i, cause in enumerate(ev.get("候选原因", []), start=1):
            lines.append(f"{i}. {cause}  —— 可能性：待核实")
        lines.append("")
        lines.append("【相关知识依据】")
        refs = ev.get("知识引用", [])
        if refs:
            for i, ref in enumerate(refs, start=1):
                lines.append(f"[{i}] {ref}")
        else:
            lines.append("未检索到直接相关的知识条目，本条分析无知识库依据。")
        lines.append("")
        lines.append("【建议核查的数据或记录】")
        for item in ev.get("建议核查项", []):
            lines.append(f"- {item}")
        lines.append("")
        lines.append("【是否需要人工复核】")
        lines.append(f"需要。{ev.get('复核理由', '该结论依赖计量配置与现场情况，必须人工确认。')}")
        return "\n".join(lines)

    # ---- 分支 2：知识库问答 ----
    if CTX_OPEN in prompt:
        ctx = prompt.split(CTX_OPEN, 1)[1].split(CTX_CLOSE, 1)[0].strip()
        if not ctx or "未检索到" in ctx:
            return banner + (
                "知识库中没有检索到与该问题直接相关的内容，依据不足，无法给出结论。\n"
                "建议：补充相关管理制度文档，或转人工咨询能源经理。"
            )
        # 抽取每个上下文片段的来源与首句，拼成"带引用"的回答
        blocks = re.split(r"\n\[(\d+)\]\s", "\n" + ctx)
        pairs: list[tuple[str, str]] = []
        for i in range(1, len(blocks) - 1, 2):
            idx = blocks[i]
            body = blocks[i + 1].strip()
            head, _, rest = body.partition("\n")
            # head 形如 "来源：02_xxx.md / 标题 > 小节"，只留文件名，避免引用过长
            short = head.replace("来源：", "").strip().split("/")[-1].strip()
            text = (rest or head).strip().replace("\n", " ")
            sentence = text.split("。")[0][:110]
            pairs.append((f"[{idx}] {short}", sentence))

        lines = [banner, "根据园区能源管理知识库，可以按以下要点处理：", ""]
        for cite, snippet in pairs:
            lines.append(f"- {snippet}。 {cite}")
        lines.append("")
        lines.append("以上内容均来自知识库检索片段，具体执行请以现行有效版本制度为准。")
        return "\n".join(lines)

    return banner + "（模拟模式）已收到请求，但未识别的 Prompt 结构。"


def call_llm(
    prompt: str,
    model: str | None = None,
    provider: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    retries: int = 2,
) -> str:
    """
    大模型统一调用入口（全项目唯一出口，业务代码不得直接 requests.post）。

    支持：
      - 任意 OpenAI 兼容接口（OpenAI / DeepSeek / 通义千问兼容模式 / Moonshot /
        硅基流动 / vLLM / Xinference / Ollama 的 /v1 兼容端点）
      - 通过 provider="mock" 强制走离线模拟（用于无 Key 演示与单元测试）

    错误处理策略：
      - 超时、连接错误、5xx：指数退避重试（默认 2 次）
      - 401/403（鉴权失败）、400（参数错误）：立即失败，重试没有意义
      - 所有异常统一包装成 LLMError，调用方只需处理一种异常
    """
    provider = (provider or CFG.llm_provider or "openai").lower()
    model = model or CFG.llm_model
    temperature = CFG.llm_temperature if temperature is None else temperature
    max_tokens = CFG.llm_max_tokens if max_tokens is None else max_tokens
    timeout = timeout or CFG.llm_timeout

    if provider == "mock":
        return _mock_llm(prompt)

    if not CFG.llm_api_key:
        # 没有 Key 不报错终止，而是降级到模拟模式——保证原型在任何机器上可演示
        warn("未配置 LLM_API_KEY，自动降级为离线模拟模式")
        return _mock_llm(prompt)

    url = f"{CFG.llm_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {CFG.llm_api_key}", "Content-Type": "application/json"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout as exc:
            last_exc = exc
            warn(f"LLM 请求超时（第 {attempt} 次，{timeout}s）")
        except requests.RequestException as exc:
            last_exc = exc
            warn(f"LLM 网络异常（第 {attempt} 次）：{exc}")
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, ValueError) as exc:
                    raise LLMError(f"LLM 响应结构异常：{resp.text[:300]}") from exc
            if resp.status_code in (401, 403):
                raise LLMError(f"LLM 鉴权失败（{resp.status_code}），请检查 LLM_API_KEY")
            if resp.status_code == 404:
                raise LLMError(f"接口不存在（404）：{url}，请检查 LLM_BASE_URL 是否需要带 /v1")
            if 400 <= resp.status_code < 500:
                raise LLMError(f"LLM 请求被拒绝（{resp.status_code}）：{resp.text[:300]}")
            last_exc = LLMError(f"服务端错误 {resp.status_code}")
            warn(f"LLM 服务端错误（第 {attempt} 次）：{resp.status_code}")

        if attempt <= retries:
            time.sleep(min(2 ** attempt, 8))     # 指数退避：2s, 4s

    warn(f"LLM 调用最终失败，降级为离线模拟输出。原因：{last_exc}")
    return _mock_llm(prompt)


# ==============================================================================
# 第八部分：Prompt 构建
# 【核心模块-后续复用】——正式系统把模板抽到 prompts/ 目录做版本管理
# ==============================================================================

SYSTEM_PROMPT_QA = (
    "你是瀚川工业园能源管理系统的知识库问答助手。"
    "你只能依据【检索到的知识片段】作答，不得引用片段之外的任何规定、数字或条款。"
    "如果片段不足以回答，必须直接说明“依据不足”，并提示需要补充哪类资料。"
    "回答中使用 [1][2] 标注依据来自哪个片段。不要编造文件名、条款号、阈值。"
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


def build_qa_prompt(question: str, results: list[Retrieved]) -> str:
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
        f"【用户问题】\n{question}\n\n"
        f"{CTX_OPEN}\n{context}\n{CTX_CLOSE}\n\n"
        "请依据上述片段回答，要求：\n"
        "1. 先给结论，再给要点，最后列出引用编号；\n"
        "2. 每个要点后用 [n] 标注依据；\n"
        "3. 若片段不足以回答，直接说“知识库依据不足”，并说明缺什么资料；\n"
        "4. 不要补充片段以外的内容。"
    )


# ==============================================================================
# 第九部分：能耗数据加载与增量计算
# 【核心模块-后续复用】——正式系统替换为数据库查询，计算逻辑不变
# ==============================================================================

REQUIRED_FIELDS = ("meter_id", "timestamp", "raw_reading", "multiplier", "building_id")

UNIT_TO_KWH = {           # 统一折算到 kWh，避免"总表 MWh、分表 kWh"直接相加的经典错误
    "kwh": 1.0,
    "度": 1.0,
    "wh": 0.001,
    "mwh": 1000.0,
    "gwh": 1_000_000.0,
}


class DataError(Exception):
    """数据文件格式或内容错误。"""


@dataclass
class Reading:
    """一条电表读数（已解析）。"""

    meter_id: str
    ts: datetime
    raw_reading: float          # 累计示值（表底），单位见 unit
    multiplier: float           # 倍率（CT×PT 折算系数）
    unit: str
    building_id: str
    parent_meter_id: str | None


@dataclass
class Interval:
    """相邻两个读数之间的用电增量（一次侧电量，单位 kWh）。"""

    meter_id: str
    ts: datetime                # 区间起点
    energy_kwh: float
    raw_delta: float
    multiplier: float
    unit: str
    building_id: str
    parent_meter_id: str | None
    flags: list[str] = field(default_factory=list)   # rollback / unit_converted / multiplier_changed


def _parse_ts(value: str) -> datetime:
    """解析时间戳，兼容 '2026-10-23 10:00'、'2026-10-23T10:00:00'、'2026/10/23 10:00'。"""
    v = value.strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise DataError(f"无法解析时间戳：{value!r}（支持 2026-10-23 10:00 或 ISO8601 格式）")


def load_readings(path: Path) -> list[Reading]:
    """
    读取 CSV / JSON 格式的模拟电表数据。

    CSV：标准逗号分隔，首行为表头
    JSON：数组，每个元素是一条记录（字段同 CSV）
    """
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在：{path}")

    rows: list[dict[str, Any]] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise DataError("CSV 文件缺少表头")
            missing = [f for f in REQUIRED_FIELDS if f not in reader.fieldnames]
            if missing:
                raise DataError(f"CSV 缺少必需字段：{', '.join(missing)}（当前表头：{reader.fieldnames}）")
            rows = [dict(r) for r in reader]
    elif path.suffix.lower() == ".json":
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DataError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(rows, list):
            raise DataError("JSON 顶层必须是数组")
    else:
        raise DataError(f"不支持的文件格式：{path.suffix}（仅支持 .csv / .json）")

    readings: list[Reading] = []
    for lineno, row in enumerate(rows, start=2):
        try:
            meter_id = str(row["meter_id"]).strip()
            if not meter_id:
                raise DataError("meter_id 为空")
            unit = str(row.get("unit") or "kWh").strip()
            parent = row.get("parent_meter_id")
            parent = str(parent).strip() if parent not in (None, "", "None", "null") else None
            readings.append(Reading(
                meter_id=meter_id,
                ts=_parse_ts(str(row["timestamp"])),
                raw_reading=float(row["raw_reading"]),
                multiplier=float(row["multiplier"]),
                unit=unit,
                building_id=str(row.get("building_id") or "").strip(),
                parent_meter_id=parent,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            raise DataError(f"第 {lineno} 行数据不合法：{exc}") from exc
    if not readings:
        raise DataError("数据文件为空")
    return readings


def compute_intervals(readings: list[Reading]) -> dict[str, list[Interval]]:
    """
    把累计示值（表底）转换为相邻时间点的用电增量，并按倍率与单位折算到 kWh。

    ★ 这是整个能耗分析最关键的一步，三个必须处理对的坑：

    坑 1｜不能把累计读数直接相加。
          表底是"从装表到现在的累计量"，一天内累加会得到天文数字。
          正确做法是差分：Δ示值 = 本点示值 − 上一点示值。

    坑 2｜必须先差分、再乘倍率。
          energy(一次侧) = Δ示值(二次侧) × 倍率。
          若先乘后差（(a×r) − (b×r) 数学上等价，但一旦中间有倍率变更就会错），
          且更容易在换表/清零时算出负数。这里按"逐区间取当时倍率"处理。

    坑 3｜单位不统一。
          total 表可能是 MWh，分表是 kWh。必须统一折算后再做平衡比较。

    另外处理表底回退（换表、清零、通讯补采导致的示值倒退）：
      差值 < 0 时不产生电量，打 rollback 标记，交由数据质量环节处置，
      绝不参与总分平衡计算（否则会凭空造出一个巨大差异）。
    """
    grouped: dict[str, list[Reading]] = {}
    for r in readings:
        grouped.setdefault(r.meter_id, []).append(r)

    result: dict[str, list[Interval]] = {}
    for meter_id, items in grouped.items():
        items.sort(key=lambda x: x.ts)
        intervals: list[Interval] = []
        for prev, cur in zip(items, items[1:]):
            raw_delta = cur.raw_reading - prev.raw_reading
            flags: list[str] = []

            if raw_delta < 0:
                # 表底回退：不产生电量，只记录异常标记
                flags.append("rollback")
                raw_delta = 0.0

            factor = UNIT_TO_KWH.get(cur.unit.lower(), 1.0)
            if factor != 1.0:
                flags.append("unit_converted")

            # 倍率取"区间起点时刻生效的倍率"（即 prev.multiplier）。
            # 理由：这段走字是在上一个读数到本读数之间产生的，理应按当时的配置换算。
            # 若用 cur.multiplier，倍率刚切换的那个区间会被整体放大/缩小，制造一天虚假跳变。
            energy = raw_delta * prev.multiplier * factor
            intervals.append(Interval(
                meter_id=meter_id,
                ts=prev.ts,                 # 区间起点
                energy_kwh=round(energy, 4),
                raw_delta=round(raw_delta, 6),
                multiplier=prev.multiplier,
                unit=cur.unit,
                building_id=cur.building_id,
                parent_meter_id=cur.parent_meter_id,
                flags=flags,
            ))
        result[meter_id] = intervals

    # 后置一遍：标出"相对上一区间发生了倍率变化"的区间，供规则 A 使用
    for items in result.values():
        for i in range(1, len(items)):
            if abs(items[i].multiplier - items[i - 1].multiplier) > 1e-9:
                items[i].flags.append("multiplier_changed")
    return result


# ==============================================================================
# 第十部分：异常检测规则
# 【核心模块-后续复用】——纯函数，输入 Interval 列表，输出事件，无任何 IO
# ==============================================================================

@dataclass
class AnomalyEvent:
    """一条异常事件。字段设计与正式库表 anomaly_events 一一对应，便于后续迁移。"""

    event_id: str
    rule_code: str
    event_type: str            # DATA_QUALITY / METERING / CONSUMPTION
    severity: str              # low / medium / high
    title: str
    building_id: str
    meter_id: str
    window_start: datetime
    window_end: datetime
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)    # 全部由 Python 计算得出
    suspects: list[str] = field(default_factory=list)        # 疑似关联表
    evidence: dict[str, Any] = field(default_factory=dict)   # 原始数据快照
    suggestions: list[str] = field(default_factory=list)
    need_review: bool = True

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.strftime("%Y-%m-%d %H:%M")
        d["window_end"] = self.window_end.strftime("%Y-%m-%d %H:%M")
        return d


class RuleA_RatioChange:
    """
    规则 A：电表倍率变化待核查

    ★ 重要设计立场（写在代码里，防止后续维护者误改）：
      倍率变化**不等于**数据错误。换表、增容、CT 更换、产权变更都可能合法地改倍率。
      所以本规则只产出"待核查（METERING / medium）"级别的事件，
      而不是"计量错误"。是否错误，必须由计量工程师调阅配置历史与工单后判定。

    判定逻辑（全部为确定性计算）：
      1. 逐表比较相邻两个区间的 multiplier
      2. 变化倍数 factor = 新/旧，若 factor ≥ 阈值 或 ≤ 1/阈值，则触发
      3. 同时计算"倍率变更前后该表电量的中位数比值"，作为旁证：
         - 若电量比值 ≈ 倍率比值 → 强烈指向"倍率配置问题"（电量被整体放大）
         - 若电量基本不变 → 更像"换表/换 CT 但实际负荷未变"，需人工确认
      4. 严重度按变化倍数分级
    """

    code = "R04_RATIO_CHANGE"
    event_type = "METERING"
    version = 1

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run(self, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
        events: list[AnomalyEvent] = []
        for meter_id, items in intervals.items():
            for i in range(1, len(items)):
                prev, cur = items[i - 1], items[i]
                if abs(cur.multiplier - prev.multiplier) < 1e-9:
                    continue
                if prev.multiplier <= 0:
                    continue

                factor = cur.multiplier / prev.multiplier
                if factor < self.cfg.ratio_change_factor and factor > 1 / self.cfg.ratio_change_factor:
                    continue

                # ---- 旁证：变更前后各取 4 个区间的电量中位数 ----
                before = [x.energy_kwh for x in items[max(0, i - 4):i]]
                after = [x.energy_kwh for x in items[i:i + 4]]
                med_before = float(np.median(before)) if before else 0.0
                med_after = float(np.median(after)) if after else 0.0
                energy_ratio = (med_after / med_before) if med_before > 0 else float("nan")

                # 严重度：倍数越大越严重
                if factor >= 5 or factor <= 0.2:
                    severity = "high"
                elif factor >= 3 or factor <= 1 / 3:
                    severity = "medium"
                else:
                    severity = "low"

                # 电量比值与倍率比值是否吻合（吻合 → 更可能是配置问题）
                consistent = (
                    not math.isnan(energy_ratio)
                    and abs(energy_ratio - factor) / factor < 0.25
                )

                suggestions = [
                    f"调阅电表 {meter_id} 的计量配置变更记录（METER_CONFIG_HISTORY），确认是否有对应的换表/增容工程单",
                    f"核对现场铭牌与 CT 变比：系统记录新倍率 {cur.multiplier:g}，旧倍率 {prev.multiplier:g}",
                    "确认倍率生效时间是否与实际投运时间一致（是否存在录入延迟或提前生效）",
                ]
                if consistent:
                    suggestions.insert(0, "变更前后的电量中位数比值与倍率比值基本吻合，优先排查倍率录入/生效时间问题")
                else:
                    suggestions.insert(0, "电量变化幅度与倍率变化不吻合，需同时排除负荷真实变化与换表清零")

                events.append(AnomalyEvent(
                    event_id="",   # 由 detect_all 统一编号
                    rule_code=self.code,
                    event_type=self.event_type,
                    severity=severity,
                    title=f"电表 {meter_id} 倍率变化待核查（{prev.multiplier:g} → {cur.multiplier:g}）",
                    building_id=cur.building_id,
                    meter_id=meter_id,
                    window_start=prev.ts,
                    window_end=cur.ts,
                    summary=(
                        f"自 {cur.ts:%Y-%m-%d %H:%M} 的读数起，电表 {meter_id} 的倍率由 "
                        f"{prev.multiplier:g} 变为 {cur.multiplier:g}（{factor:.1f} 倍）。"
                        f"倍率变化本身不构成异常，需核对配置历史确认是否为合法变更。"
                    ),
                    metrics={
                        "旧倍率": prev.multiplier,
                        "新倍率": cur.multiplier,
                        "变化倍数": round(factor, 3),
                        "变更前电量中位数_kWh": round(med_before, 3),
                        "变更后电量中位数_kWh": round(med_after, 3),
                        "电量比值": None if math.isnan(energy_ratio) else round(energy_ratio, 3),
                        "电量与倍率是否吻合": consistent,
                        "单位": cur.unit,
                    },
                    evidence={
                        "变更时刻": cur.ts.strftime("%Y-%m-%d %H:%M"),
                        "变更前区间": [{"时间": x.ts.strftime("%m-%d %H:%M"), "电量kWh": x.energy_kwh,
                                    "倍率": x.multiplier} for x in items[max(0, i - 4):i]],
                        "变更后区间": [{"时间": x.ts.strftime("%m-%d %H:%M"), "电量kWh": x.energy_kwh,
                                    "倍率": x.multiplier} for x in items[i:i + 4]],
                    },
                    suggestions=suggestions,
                    need_review=True,
                ))
        return events


class RuleB_BalanceDiff:
    """
    规则 B：总表与分表数据差异（母线平衡校验）

    判定逻辑：
      1. 按 parent_meter_id 建立"父表 → 直接子表"映射（只取直接子表，避免多级重复累加）
      2. 按时间桶（默认 1 小时）汇总父表电量与子表电量和
      3. 差异率 = (父表电量 − Σ子表电量) / 父表电量
      4. |差异率| > 阈值 且 父表电量 ≥ 最小电量 → 触发
      5. 把连续的超阈值时间桶合并为一个事件（否则一天 24 个桶会刷出 24 条重复告警）

    为什么分母用父表电量？
      因为"总表"是结算与损耗的基准。用父表作分母，差异率的物理含义是
      "总表电量中有多少比例无法被分表解释"，这与业务方对账时的口径一致。

    差异方向的含义（写清楚，避免误读）：
      正差异（父 > 子）：有负荷未被分表覆盖 —— 私接、漏装表、分表计量偏低、线路损耗偏大
      负差异（子 > 父）：分表合计超过总表 —— 倍率配置错误、重复计量、串表、抄表口径错
    """

    code = "R05_BALANCE_DIFF"
    event_type = "METERING"
    version = 1
    BUCKET_MINUTES = 60

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _bucket(self, ts: datetime) -> datetime:
        """把时间归到小时桶（00:00、01:00 …），用于跨表对齐。"""
        return ts.replace(minute=0, second=0, microsecond=0)

    def run(self, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
        # ---- 1) 建立父子关系 ----
        children: dict[str, list[str]] = {}
        for meter_id, items in intervals.items():
            parent = next((x.parent_meter_id for x in items if x.parent_meter_id), None)
            if parent:
                children.setdefault(parent, []).append(meter_id)

        if not children:
            return []

        # ---- 2) 按 (表, 小时桶) 汇总电量 ----
        def bucket_sum(meter_id: str) -> dict[datetime, float]:
            acc: dict[datetime, float] = {}
            for x in intervals.get(meter_id, []):
                if "rollback" in x.flags:
                    continue                      # 表底回退的数据不参与平衡计算
                b = self._bucket(x.ts)
                acc[b] = acc.get(b, 0.0) + x.energy_kwh
            return acc

        events: list[AnomalyEvent] = []

        for parent_id, child_ids in children.items():
            parent_buckets = bucket_sum(parent_id)
            child_buckets = {cid: bucket_sum(cid) for cid in child_ids}
            all_buckets = sorted(parent_buckets)

            # ---- 3) 逐桶计算差异 ----
            deviations: list[dict[str, Any]] = []
            for b in all_buckets:
                p = parent_buckets.get(b, 0.0)
                c = sum(cb.get(b, 0.0) for cb in child_buckets.values())
                if p < self.cfg.balance_min_energy_kwh:
                    continue                      # 电量太小，差异率没有统计意义
                rate = (p - c) / p
                deviations.append({
                    "bucket": b, "parent": round(p, 3), "children": round(c, 3),
                    "rate": rate, "over": abs(rate) > self.cfg.balance_diff_threshold,
                })

            if not deviations:
                continue

            # ---- 4) 连续超阈值桶合并为一个事件 ----
            runs: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            step = timedelta(minutes=self.BUCKET_MINUTES)
            for d in deviations:
                if not d["over"]:
                    if current:
                        runs.append(current)
                        current = []
                    continue
                # 方向必须一致才合并（正差异与负差异是两类问题）
                if current and (d["rate"] > 0) != (current[0]["rate"] > 0):
                    runs.append(current)
                    current = []
                if current and d["bucket"] - current[-1]["bucket"] > step:
                    runs.append(current)
                    current = []
                current.append(d)
            if current:
                runs.append(current)

            # ---- 5) 为每个 run 生成事件 ----
            for run in runs:
                events.append(self._make_event(parent_id, child_ids, child_buckets, run,
                                               intervals.get(parent_id, [])))

        return events

    def _make_event(self, parent_id: str, child_ids: list[str],
                    child_buckets: dict[str, dict[datetime, float]],
                    run: list[dict[str, Any]],
                    parent_intervals: list[Interval]) -> AnomalyEvent:
        """把一段连续的平衡偏差整理成结构化事件，并找出最可疑的分表。"""
        total_parent = sum(d["parent"] for d in run)
        total_child = sum(d["children"] for d in run)
        rate = (total_parent - total_child) / total_parent if total_parent else 0.0
        positive = rate > 0
        building = next((x.building_id for x in parent_intervals if x.building_id), "")

        # ---- 找可疑表：对比"本窗口电量"与该表在数据集中的中位数 ----
        # 思路：平衡差总得由某几块表解释。哪块表本窗口电量明显偏离它自己的常态，就先查它。
        suspects: list[tuple[str, float]] = []
        for cid in child_ids:
            cb = child_buckets[cid]
            win = sum(cb.get(d["bucket"], 0.0) for d in run)
            base_vals = [v for v in cb.values() if v > 0]
            if not base_vals:
                continue
            base = float(np.median(base_vals))
            if base <= 0:
                continue
            suspects.append((cid, win / (base * len(run))))
        suspects.sort(key=lambda x: -x[1])
        top_suspects = [s for s in suspects[:2]]
        suspect_desc = "、".join(f"{cid}（本窗口电量为自身常态的 {ratio:.1f} 倍）"
                                 for cid, ratio in top_suspects)

        direction = "总表电量大于分表合计" if positive else "分表合计大于总表电量"
        if positive:
            causes = [
                "存在未被分表覆盖的负荷（私接、漏装计量点、临时用电未计量）",
                "分表计量偏低（CT 二次侧接触不良、表计精度漂移、倍率配置偏小）",
                "线路损耗与变压器损耗超出经验值（本原型按 1.5% 模拟正常损耗）",
                "分表在该时段存在数据缺失（缺数会被计入差异，需先排除数据质量问题）",
            ]
        else:
            causes = [
                "分表倍率配置偏高（同一负荷被放大计量）",
                "同一回路被两块表重复计量（串表/重复接入）",
                "分表与总表的统计口径不一致（如一方含无功、或计量时段错位）",
                "分表覆盖了总表计量范围之外的负荷（接线越界）",
            ]

        return AnomalyEvent(
            event_id="",
            rule_code=self.code,
            event_type=self.event_type,
            severity="high" if abs(rate) >= 0.10 else "medium",
            title=f"{parent_id} 总表与分表合计差异 {rate * 100:+.1f}%",
            building_id=building,
            meter_id=parent_id,
            window_start=run[0]["bucket"],
            window_end=run[-1]["bucket"] + timedelta(minutes=self.BUCKET_MINUTES),
            summary=(
                f"在 {run[0]['bucket']:%m-%d %H:%M} 至 "
                f"{(run[-1]['bucket'] + timedelta(minutes=self.BUCKET_MINUTES)):%m-%d %H:%M} 的 "
                f"{len(run)} 个小时区间内，总表 {parent_id} 与 {len(child_ids)} 块直接分表的电量合计"
                f"持续存在差异，累计差异率 {rate * 100:+.1f}%（阈值 ±{self.cfg.balance_diff_threshold * 100:.0f}%）。"
                f"方向：{direction}。"
            ),
            metrics={
                "总表电量_kWh": round(total_parent, 3),
                "分表合计_kWh": round(total_child, 3),
                "差异电量_kWh": round(total_parent - total_child, 3),
                "差异率": round(rate, 4),
                "差异方向": direction,
                "阈值": self.cfg.balance_diff_threshold,
                "超阈值小时数": len(run),
            },
            suspects=[s[0] for s in top_suspects],
            evidence={
                "总表": parent_id,
                "直接分表": child_ids,
                "逐小时明细": [
                    {"时间": d["bucket"].strftime("%m-%d %H:00"),
                     "总表kWh": d["parent"], "分表合计kWh": d["children"],
                     "差异率": f"{d['rate'] * 100:+.1f}%"}
                    for d in run
                ],
                "分表偏离度": {cid: round(r, 2) for cid, r in suspects},
                "可疑分表说明": suspect_desc or "未识别出明显偏离的分表",
            },
            suggestions=[
                f"优先核查 {suspect_desc}" if suspect_desc else "逐块核对该总表下各分表的计量配置与现场接线",
                "先排除数据质量问题（分表缺数、表底回退），再判断是否为真实计量偏差",
                "调阅该时段的用电负荷记录与检修/试机记录，确认是否存在未纳入计量的临时负荷",
                "核对总表与分表的统计时段是否严格对齐（分表冻结时间不同会造成系统性错位）",
            ],
            need_review=True,
        )


def detect_all(cfg: Config, intervals: dict[str, list[Interval]]) -> list[AnomalyEvent]:
    """
    执行全部规则并统一编号。

    编号规则：AN-{日期}-{序号}，便于人工沟通与检索（对应库表 event_no 字段）。
    """
    rules = [RuleA_RatioChange(cfg), RuleB_BalanceDiff(cfg)]
    events: list[AnomalyEvent] = []
    for rule in rules:
        try:
            events.extend(rule.run(intervals))
        except Exception as exc:  # noqa: BLE001
            # 单条规则失败不应导致整个检测中断——这是"故障隔离"的基本要求
            warn(f"规则 {getattr(rule, 'code', rule.__class__.__name__)} 执行失败：{exc}")
    events.sort(key=lambda e: (e.window_start, e.rule_code))

    day = events[0].window_start.strftime("%Y%m%d") if events else datetime.now().strftime("%Y%m%d")
    for i, ev in enumerate(events, start=1):
        ev.event_id = f"AN-{day}-{i:04d}"
    return events


# ==============================================================================
# 第十一部分：AI 辅助分析（Prompt 构建 + 结果校验）
# 【核心模块-后续复用】——正式系统接入 Tool Layer 后，本段结构不变
# ==============================================================================

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
    ★ 数值溯源校验（Guardrail）——本原型最重要的"反幻觉"工程手段。

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


def build_anomaly_prompt(event: AnomalyEvent, results: list[Retrieved]) -> str:
    """构建异常分析 Prompt：证据 JSON + 知识片段 + 严格的输出格式要求。"""
    evidence = build_evidence_payload(event, results)

    ctx_blocks = []
    for i, r in enumerate(results, start=1):
        ctx_blocks.append(f"[{i}] 来源：{r.chunk.source_file} / {r.chunk.heading_path}\n{r.chunk.content}")
    context = "\n\n".join(ctx_blocks) if ctx_blocks else "（未检索到相关知识片段）"

    checklist = "\n".join(f"- {s}" for s in event.suggestions)

    return (
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


def analyze_anomaly(event: AnomalyEvent, retriever: HybridRetriever) -> dict[str, Any]:
    """
    AI 辅助分析主流程：事件 → 检索知识 → 构建 Prompt → 调用 LLM → 校验 → 报告。

    返回结构同时包含"分析正文"与"溯源信息"，前者给人看，后者给审计看。
    """
    # ---- 1) 构造检索问句：用异常类型 + 关键实体，而不是把整段 summary 丢进去 ----
    if event.rule_code == "R04_RATIO_CHANGE":
        query = "电表倍率变化如何处理 倍率配置变更流程 生效时间"
    elif event.rule_code == "R05_BALANCE_DIFF":
        query = "总表与分表数据不一致的原因 平衡差核对规则 损耗 允许偏差"
    else:
        query = event.title

    results = retriever.retrieve(query, top_k=CFG.top_k)
    evidence_text = json.dumps(build_evidence_payload(event, results), ensure_ascii=False, default=str)
    context_text = "\n".join(r.chunk.content for r in results)

    # ---- 2) 调用模型（无 Key 时自动走离线模拟，链路照常走通）----
    prompt = build_anomaly_prompt(event, results)
    t0 = time.perf_counter()
    try:
        answer = call_llm(prompt, system=SYSTEM_PROMPT_ANOMALY)
    except LLMError as exc:
        # 降级：模型不可用时仍然产出"规则结论 + 证据清单"，只是没有自然语言分析
        warn(f"LLM 调用失败，降级为规则结论输出：{exc}")
        answer = (
            "【异常现象】\n" + event.summary + "\n\n"
            "【可能原因】\n" + "\n".join(f"{i}. {c}（待核实）" for i, c in
                                        enumerate(build_evidence_payload(event, results)["候选原因"], 1)) + "\n\n"
            "【相关知识依据】\n" + ("\n".join(f"[{i}] {r.chunk.citation()}"
                                        for i, r in enumerate(results, 1)) or "无") + "\n\n"
            "【建议核查的数据或记录】\n" + "\n".join(f"- {s}" for s in event.suggestions) + "\n\n"
            "【是否需要人工复核】\n需要。大模型不可用，以上仅为规则引擎结论，必须人工核实。"
        )
    elapsed = (time.perf_counter() - t0) * 1000

    # ---- 3) 数值溯源校验 ----
    trace = number_trace_guard(answer, evidence_text, context_text)

    return {
        "event": event,
        "answer": answer,
        "citations": [r.chunk.citation() for r in results],
        "retrieval": results,
        "guardrail": trace,
        "elapsed_ms": round(elapsed, 1),
        "model": CFG.llm_model if CFG.llm_api_key else "mock(offline)",
    }


def _rule(ch: str = "─", width: int = 78) -> str:
    """返回一条分隔线字符串（注意：render 系列函数里必须"拼进字符串"而不是直接 print，
    否则先打印的分隔线会跑到报告正文前面去，破坏排版）。"""
    return f"{C.GRAY}{ch * width}{C.RESET}"


def render_analysis_report(result: dict[str, Any]) -> str:
    """把分析结果渲染成终端友好的报告，并在末尾附完整溯源信息。"""
    ev: AnomalyEvent = result["event"]
    trace = result["guardrail"]
    lines: list[str] = []

    lines.append("")
    lines.append(_rule("═"))
    lines.append(f"{C.BOLD}{C.BLUE}  异常分析报告  {ev.event_id}{C.RESET}")
    lines.append(_rule("═"))
    lines.append(f"  {C.GRAY}规则{C.RESET}  {ev.rule_code}    "
                 f"{C.GRAY}级别{C.RESET}  {_severity_label(ev.severity)}    "
                 f"{C.GRAY}楼栋{C.RESET}  {ev.building_id or '-'}")
    lines.append(f"  {C.GRAY}电表{C.RESET}  {ev.meter_id}    "
                 f"{C.GRAY}窗口{C.RESET}  {ev.window_start:%Y-%m-%d %H:%M} ~ {ev.window_end:%H:%M}")
    lines.append(_rule())
    lines.append("")

    # ---- AI 输出正文：按五个小标题分段，加粗标题 ----
    for line in result["answer"].splitlines():
        stripped = line.strip()
        if stripped.startswith("【") and stripped.endswith("】"):
            lines.append(f"{C.BOLD}{C.MAGENTA}{stripped}{C.RESET}")
        elif stripped.startswith("【") and "】" in stripped:
            head, rest = stripped.split("】", 1)
            lines.append(f"{C.BOLD}{C.MAGENTA}{head}】{C.RESET}{rest}")
        else:
            lines.append(line)

    # ---- 溯源区（正式系统里这块是"证据包"，不可编辑）----
    lines.append("")
    lines.append(_rule())
    lines.append(f"{C.BOLD}数据来源与溯源{C.RESET}")
    lines.append(f"  证据类型     规则引擎计算值（Python 确定性计算，非模型生成）")
    lines.append(f"  规则与版本   {ev.rule_code} v1  阈值参数见事件 metrics")
    key_metrics = list(ev.metrics.items())[:5]
    for k, v in key_metrics:
        lines.append(f"  {_pad(str(k), 22)}{v}")
    lines.append(f"  知识来源")
    if result["citations"]:
        for i, c in enumerate(result["citations"], start=1):
            lines.append(f"    [{i}] {c}")
    else:
        lines.append(f"    （无，知识库未检索到相关条目）")
    lines.append(f"  AI 模型      {result['model']}    耗时 {result['elapsed_ms']} ms")

    # ---- 数值溯源结果 ----
    if trace["passed"]:
        lines.append(f"  数值溯源     {C.GREEN}通过{C.RESET}（校验 {trace['checked']} 个数值，均在证据中找到来源）")
    else:
        lines.append(f"  数值溯源     {C.RED}未通过{C.RESET}（{len(trace['unmatched'])} 个数值无来源："
                     f"{', '.join(trace['unmatched'])}）")
        lines.append(f"  {C.RED}  ↑ 该报告不可直接采信，必须人工复核{C.RESET}")

    lines.append("")
    lines.append(f"{C.YELLOW}  ⚠ 本条结论为 AI 辅助分析草稿（DRAFT），未构成业务结论。{C.RESET}")
    lines.append(f"{C.YELLOW}    责任认定与处置必须由人工复核签认后方可生效。{C.RESET}")
    lines.append(_rule("═"))
    return "\n".join(lines)


def _severity_label(s: str) -> str:
    return {"high": f"{C.RED}高{C.RESET}", "medium": f"{C.YELLOW}中{C.RESET}",
            "low": f"{C.GRAY}低{C.RESET}"}.get(s, s)


# ==============================================================================
# 第十二部分：命令行界面
# 【原型模块-后续替换】——正式系统由 FastAPI 路由 + 前端页面替代，整段丢弃
# ==============================================================================

class Session:
    """一次程序运行期间的会话状态（加载的数据、检测出的事件）。"""

    def __init__(self):
        self.retriever: HybridRetriever | None = None
        self.readings: list[Reading] = []
        self.intervals: dict[str, list[Interval]] = {}
        self.events: list[AnomalyEvent] = []
        self.data_file: Path | None = None


def print_banner(session: Session) -> None:
    """打印主菜单。"""
    print()
    hr("═")
    print(f"{C.BOLD}{C.BLUE}      瀚川工业园能耗异常定位智能体 · 第一阶段原型{C.RESET}")
    hr("═")
    print(f"  {C.BOLD}1{C.RESET}. 能源管理知识库问答")
    print(f"  {C.BOLD}2{C.RESET}. 读取模拟能耗数据")
    print(f"  {C.BOLD}3{C.RESET}. 执行能耗异常检测")
    print(f"  {C.BOLD}4{C.RESET}. AI 辅助分析异常事件")
    print(f"  {C.BOLD}5{C.RESET}. 查看知识库检索结果")
    print(f"  {C.BOLD}6{C.RESET}. 退出程序")
    hr()
    state = []
    state.append(f"知识库 {C.GREEN}已就绪{C.RESET}" if session.retriever else "知识库 未加载")
    state.append(f"数据 {C.GREEN}{session.data_file.name}{C.RESET}" if session.data_file else "数据 未读取")
    state.append(f"异常 {C.GREEN}{len(session.events)} 条{C.RESET}" if session.events else "异常 未检测")
    print("  " + f"  {C.GRAY}|{C.RESET}  ".join(state))
    hr()


def _ensure_retriever(session: Session) -> HybridRetriever:
    """懒加载检索器：只在第一次真正需要时才建索引，避免启动变慢。"""
    if session.retriever is None:
        embedder = build_embedder(CFG)
        retriever = HybridRetriever(CFG, embedder)
        retriever.build_or_load()
        session.retriever = retriever
    return session.retriever


def action_qa(session: Session) -> None:
    """功能 1：知识库问答（支持连续提问，空行返回主菜单）。"""
    h1("能源管理知识库问答")
    info("输入问题后回车；直接回车返回主菜单。")
    retriever = _ensure_retriever(session)

    while True:
        try:
            question = input(f"\n{C.BOLD}问题>{C.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            return

        results = retriever.retrieve(question)
        if not retriever.has_evidence(results, question):
            warn("知识库中没有检索到相关内容（相关度低于阈值）")
            print(f"\n{C.YELLOW}依据不足，无法回答。{C.RESET}")
            print(f"{C.GRAY}  建议：确认问题是否属于园区能源管理范畴，或补充对应制度文档后重建索引。{C.RESET}")
            continue

        # 展示检索到的片段（可追溯性：让用户看到"模型看到了什么"）
        print(f"\n{C.GRAY}检索到 {len(results)} 个相关片段：{C.RESET}")
        for i, r in enumerate(results, start=1):
            print(f"  {C.GRAY}[{i}]{C.RESET} {r.chunk.citation()}  "
                  f"{C.GRAY}(向量#{r.vector_rank or '-'} / BM25#{r.bm25_rank or '-'}){C.RESET}")

        print(f"\n{C.BOLD}回答：{C.RESET}")
        try:
            answer = call_llm(build_qa_prompt(question, results), system=SYSTEM_PROMPT_QA)
        except LLMError as exc:
            err(f"LLM 调用失败：{exc}")
            continue
        print(answer)

        print(f"\n{C.BOLD}知识来源：{C.RESET}")
        for i, r in enumerate(results, start=1):
            print(f"  [{i}] {r.chunk.citation()}")


def action_load_data(session: Session) -> None:
    """功能 2：选择并读取模拟数据文件。"""
    h1("读取模拟能耗数据")
    data_dir = BASE_DIR / CFG.data_dir
    candidates = sorted(list(data_dir.glob("*.csv")) + list(data_dir.glob("*.json")))
    if not candidates:
        err(f"{data_dir} 下没有找到 csv/json 数据文件")
        return

    print("  可选数据文件：")
    for i, p in enumerate(candidates, start=1):
        size_kb = p.stat().st_size / 1024
        print(f"  {C.BOLD}{i}{C.RESET}. {p.name}  {C.GRAY}({size_kb:.1f} KB){C.RESET}")

    try:
        choice = input(f"\n{C.BOLD}请选择编号（回车取消）>{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not choice:
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
        err("输入无效")
        return

    path = candidates[int(choice) - 1]
    try:
        readings = load_readings(path)
    except (FileNotFoundError, DataError) as exc:
        err(f"读取失败：{exc}")
        return

    session.readings = readings
    session.data_file = path
    session.intervals = compute_intervals(readings)

    meters = sorted({r.meter_id for r in readings})
    ts_all = [r.ts for r in readings]
    ok(f"已读取 {len(readings)} 条读数，共 {len(meters)} 块电表")
    kv("数据文件", path.name)
    kv("时间范围", f"{min(ts_all):%Y-%m-%d %H:%M} ~ {max(ts_all):%Y-%m-%d %H:%M}")
    kv("电表清单", "、".join(meters))

    total_kwh = sum(x.energy_kwh for v in session.intervals.values() for x in v)
    kv("区间总电量", f"{total_kwh:,.2f} kWh")

    flagged = sum(1 for v in session.intervals.values() for x in v if x.flags)
    if flagged:
        warn(f"有 {flagged} 个区间带有质量标记（表底回退/单位折算/倍率变更），已从平衡计算中排除回退区间")


def action_detect(session: Session) -> None:
    """功能 3：执行确定性异常检测。"""
    h1("执行能耗异常检测")
    if not session.intervals:
        warn("尚未读取数据，请先执行功能 2")
        return

    info(f"规则集：R04 倍率变化（阈值 {CFG.ratio_change_factor}×） | "
         f"R05 总分平衡差（阈值 ±{CFG.balance_diff_threshold * 100:.0f}%）")
    info("阈值仅为模拟测试参数，不代表已确认的园区验收规则")

    events = detect_all(CFG, session.intervals)
    session.events = events

    if not events:
        ok("未检测到异常（所有规则均未触发）")
        return

    print(f"\n{C.BOLD}检测到 {len(events)} 条待核查异常：{C.RESET}\n")
    header = f"  {'编号':<18}{'规则':<20}{'电表':<12}{'级别':<6}{'时间窗':<28}{'摘要'}"
    print(f"{C.GRAY}{header}{C.RESET}")
    hr("·")
    for ev in events:
        print(f"  {ev.event_id:<18}{ev.rule_code:<20}{ev.meter_id:<12}"
              f"{_severity_label(ev.severity):<6}{ev.window_start:%m-%d %H:%M}~{ev.window_end:%H:%M}   "
              f"{ev.title}")

    # 落盘，便于后续复核与交接（对应正式系统的 anomaly_events 表）
    out_dir = BASE_DIR / CFG.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"anomaly_events_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_file.write_text(json.dumps([e.to_json() for e in events], ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print()
    ok(f"事件已保存：{out_file.relative_to(BASE_DIR)}")


def action_analyze(session: Session) -> None:
    """功能 4：选择一条异常，调用 AI 生成分析报告。"""
    h1("AI 辅助分析异常事件")
    if not session.events:
        warn("尚未检测到异常，请先执行功能 3")
        return

    print("  待分析事件：")
    for i, ev in enumerate(session.events, start=1):
        print(f"  {C.BOLD}{i}{C.RESET}. {ev.event_id}  {ev.title}")

    try:
        choice = input(f"\n{C.BOLD}请选择编号（回车分析第 1 条）>{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    idx = 0 if not choice else (int(choice) - 1 if choice.isdigit() else -1)
    if not (0 <= idx < len(session.events)):
        err("输入无效")
        return

    event = session.events[idx]
    retriever = _ensure_retriever(session)

    info("正在检索相关知识并调用大模型…")
    result = analyze_anomaly(event, retriever)
    print(render_analysis_report(result))

    # 报告落盘（正式系统对应"证据包 + 分析草稿"记录）
    out_dir = BASE_DIR / CFG.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"report_{event.event_id}.md"
    out_file.write_text(
        f"# {event.event_id} 异常分析报告（AI 草稿，未生效）\n\n"
        f"- 规则：{event.rule_code} v1\n- 电表：{event.meter_id}\n"
        f"- 窗口：{event.window_start:%Y-%m-%d %H:%M} ~ {event.window_end:%Y-%m-%d %H:%M}\n"
        f"- 数值溯源：{'通过' if result['guardrail']['passed'] else '未通过 -> 需复核'}\n\n"
        f"## AI 分析\n\n{result['answer']}\n\n"
        f"## 知识来源\n\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(result["citations"], 1)) +
        f"\n\n## 规则计算结果（Python 确定性计算）\n\n```json\n"
        f"{json.dumps(event.metrics, ensure_ascii=False, indent=2)}\n```\n",
        encoding="utf-8",
    )
    ok(f"报告已保存：{out_file.relative_to(BASE_DIR)}")


def action_inspect_retrieval(session: Session) -> None:
    """功能 5：查看检索细节（两路召回各自的排名与分数），用于调参与验收演示。"""
    h1("查看知识库检索结果")
    info("用于观察 BM25 与向量两路召回的实际表现，以及 RRF 融合后的排序")
    retriever = _ensure_retriever(session)

    try:
        question = input(f"\n{C.BOLD}检索问题>{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not question:
        return

    # 借用 retrieve 的中间信息，这里重新算一遍两路原始排名以便展示
    query = normalize_query(question)
    bm25_scores, bm25_idxs = retriever.bm25_index.search(query, CFG.bm25_candidates)
    qv = retriever.embedder.encode_one(query)
    vec_scores, vec_idxs = retriever.vector_index.search(np.asarray(qv), CFG.vector_candidates)
    fused = rrf_fuse([bm25_idxs, vec_idxs], k=CFG.rrf_k)

    print(f"\n{C.BOLD}① BM25 关键词召回 Top{len(bm25_idxs)}{C.RESET}"
          f"{C.GRAY}（分词：{'jieba' if HAS_JIEBA else '字符二元组'}）{C.RESET}")
    if not bm25_idxs:
        print(f"  {C.GRAY}（无命中）{C.RESET}")
    for r, (i, s) in enumerate(zip(bm25_idxs, bm25_scores), start=1):
        print(f"  #{r} score={s:6.3f}  {retriever.chunks[i].citation()}")

    print(f"\n{C.BOLD}② 向量语义召回 Top{len(vec_idxs)}{C.RESET}"
          f"{C.GRAY}（模型：{retriever.embedder.name}，后端：{retriever.vector_index.backend}）{C.RESET}")
    for r, (i, s) in enumerate(zip(vec_idxs, vec_scores), start=1):
        if i < 0:
            continue
        print(f"  #{r} cos={s:6.3f}  {retriever.chunks[i].citation()}")

    print(f"\n{C.BOLD}③ RRF 融合结果（k={CFG.rrf_k}）→ 取 Top{CFG.top_k}{C.RESET}")
    for r, (i, s) in enumerate(sorted(fused.items(), key=lambda kv_: -kv_[1])[:CFG.top_k], start=1):
        c = retriever.chunks[i]
        print(f"  [{r}] rrf={s:.4f}  {c.citation()}")
        preview = c.content.replace("\n", " ")[:100]
        print(f"      {C.GRAY}{preview}…{C.RESET}")

    print(f"\n{C.GRAY}RRF 公式：score(d) = Σ 1/(k + rank_i(d))，k={CFG.rrf_k}{C.RESET}")
    print(f"{C.GRAY}该公式只看排名不看分数，规避了余弦相似度与 BM25 分数量纲不可比的问题{C.RESET}")


def interactive() -> None:
    """主循环：菜单 → 分发 → 异常兜底。"""
    enable_ansi()
    session = Session()

    # 启动时预加载知识库，让用户第一次提问就有结果
    print(f"{C.GRAY}正在初始化知识库…{C.RESET}")
    try:
        _ensure_retriever(session)
    except (FileNotFoundError, ValueError) as exc:
        err(f"知识库初始化失败：{exc}")
        warn("程序仍可启动，但知识库相关功能不可用")

    actions = {
        "1": action_qa,
        "2": action_load_data,
        "3": action_detect,
        "4": action_analyze,
        "5": action_inspect_retrieval,
    }

    while True:
        print_banner(session)
        try:
            choice = input(f"{C.BOLD}请选择功能 [1-6]>{C.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if choice == "6":
            print(f"\n{C.GRAY}已退出。生成的报告与事件记录在 {CFG.output_dir}/ 目录。{C.RESET}")
            return
        handler = actions.get(choice)
        if handler is None:
            err("无效选择，请输入 1-6")
            continue
        try:
            handler(session)
        except (FileNotFoundError, DataError, LLMError) as exc:
            err(f"操作失败：{exc}")
        except KeyboardInterrupt:
            print("\n已中断，返回主菜单")
        except Exception as exc:  # noqa: BLE001
            # 兜底：任何未预期异常都不应让程序崩溃退出
            err(f"发生未预期错误：{type(exc).__name__}: {exc}")
            if os.getenv("DEBUG"):
                import traceback
                traceback.print_exc()


# ==============================================================================
# 第十三部分：非交互演示（--demo / --ask）
# 【原型模块-后续替换】——仅用于快速自检与录屏演示
# ==============================================================================

def run_demo() -> int:
    """
    一键跑通全链路，用于验收与录屏：
      加载知识库 → 知识问答 → 读取异常数据 → 检测 → AI 分析 → 输出报告
    """
    enable_ansi()
    print()
    hr("═")
    print(f"{C.BOLD}{C.BLUE}  瀚川工业园能耗异常定位智能体 · 全链路演示{C.RESET}")
    print(f"{C.GRAY}  （使用模拟数据与模拟规则，不代表真实园区制度）{C.RESET}")
    hr("═")

    session = Session()

    # ---------- Step 1：知识库 ----------
    h2("Step 1 / 6  加载知识库并构建索引")
    t0 = time.perf_counter()
    retriever = _ensure_retriever(session)
    ok(f"知识库就绪：{len(retriever.chunks)} 个切片，"
       f"Embedding={retriever.embedder.name}，向量后端={retriever.vector_index.backend}，"
       f"BM25={retriever.bm25_index.backend}，耗时 {time.perf_counter() - t0:.2f}s")

    # ---------- Step 2：知识问答 ----------
    h2("Step 2 / 6  知识库混合检索问答")
    question = "总表与分表数据不一致可能有哪些原因？"
    print(f"{C.BOLD}问题：{C.RESET}{question}\n")
    results = retriever.retrieve(question)
    print(f"{C.GRAY}Top{len(results)} 检索结果：{C.RESET}")
    for i, r in enumerate(results, start=1):
        print(f"  [{i}] {r.chunk.citation()}   "
              f"{C.GRAY}rrf={r.rrf_score:.4f} vec#{r.vector_rank or '-'} bm25#{r.bm25_rank or '-'}{C.RESET}")
    print(f"\n{C.BOLD}回答：{C.RESET}")
    print(call_llm(build_qa_prompt(question, results), system=SYSTEM_PROMPT_QA))

    # ---------- Step 3：读取异常数据 ----------
    h2("Step 3 / 6  读取模拟能耗数据（含注入异常）")
    data_path = BASE_DIR / CFG.data_dir / "meter_readings_anomaly.csv"
    if not data_path.exists():
        err(f"缺少数据文件：{data_path}")
        return 1
    session.readings = load_readings(data_path)
    session.data_file = data_path
    session.intervals = compute_intervals(session.readings)
    meters = sorted({r.meter_id for r in session.readings})
    ok(f"读取 {len(session.readings)} 条读数 / {len(meters)} 块电表 / "
       f"{min(r.ts for r in session.readings):%Y-%m-%d %H:%M} ~ "
       f"{max(r.ts for r in session.readings):%H:%M}")
    kv("电表", "、".join(meters))

    # ---------- Step 4：异常检测 ----------
    h2("Step 4 / 6  执行确定性异常检测（Python 规则引擎）")
    events = detect_all(CFG, session.intervals)
    session.events = events
    print(f"{C.GRAY}规则：R04 倍率变化 ≥{CFG.ratio_change_factor}× | "
          f"R05 总分平衡差 >±{CFG.balance_diff_threshold * 100:.0f}%{C.RESET}\n")
    for ev in events:
        print(f"  {C.YELLOW}●{C.RESET} {ev.event_id}  [{ev.rule_code}]  {ev.title}")
        print(f"      {C.GRAY}{ev.summary}{C.RESET}")

    # ---------- Step 5：AI 分析 ----------
    analysis = None
    if events:
        h2("Step 5 / 6  AI 辅助分析（检索知识 + 调用大模型 + 数值溯源）")
        # 演示优先选倍率类事件：它的因果链最直观（倍率变 10 倍 → 电量变 10 倍 → 总分差转负）
        target = next((e for e in events if e.rule_code == "R04_RATIO_CHANGE"), events[0])
        print(f"{C.GRAY}选中事件：{target.event_id}  {target.title}{C.RESET}")
        analysis = analyze_anomaly(target, session.retriever)
        print(render_analysis_report(analysis))
    else:
        warn("未检测到异常，跳过 AI 分析")

    # ---------- Step 6：落盘 ----------
    h2("Step 6 / 6  结果落盘")
    out_dir = BASE_DIR / CFG.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ev_file = out_dir / f"anomaly_events_{ts}.json"
    ev_file.write_text(json.dumps([e.to_json() for e in events], ensure_ascii=False, indent=2),
                       encoding="utf-8")
    ok(f"异常事件：{ev_file.relative_to(BASE_DIR)}  （{len(events)} 条）")
    if analysis:
        ev: AnomalyEvent = analysis["event"]
        rep_file = out_dir / f"report_{ev.event_id}.md"
        rep_file.write_text(
            f"# {ev.event_id} 异常分析报告（AI 草稿，未生效）\n\n"
            f"- 规则：{ev.rule_code} v1\n- 电表：{ev.meter_id}（{ev.building_id}）\n"
            f"- 窗口：{ev.window_start:%Y-%m-%d %H:%M} ~ {ev.window_end:%Y-%m-%d %H:%M}\n"
            f"- 数值溯源：{'通过' if analysis['guardrail']['passed'] else '未通过 -> 需复核'}\n\n"
            f"## AI 分析\n\n{analysis['answer']}\n\n"
            f"## 知识来源\n\n" +
            "\n".join(f"{i}. {c}" for i, c in enumerate(analysis["citations"], 1)) +
            f"\n\n## 规则计算结果（Python 确定性计算）\n\n```json\n"
            f"{json.dumps(ev.metrics, ensure_ascii=False, indent=2)}\n```\n",
            encoding="utf-8")
        ok(f"分析报告：{rep_file.relative_to(BASE_DIR)}")

    print()
    hr("═")
    print(f"{C.GREEN}{C.BOLD}  演示完成。核心链路已验证：检索 → 计算 → 检测 → AI 分析 → 溯源{C.RESET}")
    hr("═")
    return 0


def run_single_question(question: str) -> int:
    """单问模式：只做知识库问答，便于脚本化验证与回归测试。"""
    enable_ansi()
    embedder = build_embedder(CFG)
    retriever = HybridRetriever(CFG, embedder)
    retriever.build_or_load()

    results = retriever.retrieve(question)
    print(f"\n{C.BOLD}问题：{C.RESET}{question}\n")
    if not retriever.has_evidence(results, question):
        warn("知识库中未检索到相关内容，依据不足")
        return 0
    print(f"{C.GRAY}命中片段：{C.RESET}")
    for i, r in enumerate(results, start=1):
        print(f"  [{i}] {r.chunk.citation()}  rrf={r.rrf_score:.4f}")
    print(f"\n{C.BOLD}回答：{C.RESET}")
    print(call_llm(build_qa_prompt(question, results), system=SYSTEM_PROMPT_QA))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="瀚川工业园能耗异常定位智能体 · 第一阶段命令行原型")
    parser.add_argument("--demo", action="store_true", help="非交互跑通全链路演示")
    parser.add_argument("--ask", metavar="问题", help="单次知识库问答")
    args = parser.parse_args()

    try:
        if args.demo:
            return run_demo()
        if args.ask:
            return run_single_question(args.ask)
        interactive()
        return 0
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
