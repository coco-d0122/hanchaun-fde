# -*- coding: utf-8 -*-
"""
核心配置（阶段4）

由阶段3的 dataclass Config 改造为 pydantic-settings：
  - 字段名与阶段3 .env 完全一致（无前缀），旧 .env 文件可直接复用
  - 全部默认值逐项保持，新增字段只服务于 Web 后端
  - BASE_DIR 指向 backend/ 根目录（知识库 / 数据 / 缓存都在这里）
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 根目录：app/core/config.py -> app/core -> app -> backend
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """运行配置。全部来自 .env / 环境变量，代码中不出现任何密钥。"""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",          # 忽略 .env 中的无关字段（如阶段3的 OUTPUT_DIR）
    )

    # ---- 大模型 ----
    llm_provider: str = "openai"          # openai | deepseek | qwen | moonshot | mock
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 60
    llm_temperature: float = 0.2          # 低温度：分析类任务要稳，不要发散
    llm_max_tokens: int = 1400
    # LLM 失败时是否降级为离线模拟输出（阶段4方案 C2 默认：降级 + 前端角标）
    llm_fallback_mock: bool = True

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

    # ---- 阶段4新增：会话与持久化 ----
    db_url: str = f"sqlite+aiosqlite:///{(BACKEND_DIR / 'hanchuan.db').as_posix()}"
    history_rounds: int = 6               # 上下文历史轮数（6 组 user+assistant）
    history_char_budget: int = 3000       # 历史上下文总字符预算，超出则从最旧开始截断
    citation_excerpt_len: int = 120       # 引用摘录截断长度（存库，避免重复存全文）
    serving_host: str = "127.0.0.1"       # C3 默认：仅本机
    serving_port: int = 8000

    # ---- 只读路径（相对 backend 根目录解析）----
    @property
    def kb_path(self) -> Path:
        return BACKEND_DIR / self.kb_dir

    @property
    def data_path(self) -> Path:
        return BACKEND_DIR / self.data_dir

    @property
    def cache_path(self) -> Path:
        return BACKEND_DIR / self.cache_dir

    @property
    def uploads_path(self) -> Path:
        """上传文档的原始文件存放根目录（阶段5第二批）。"""
        return BACKEND_DIR / "uploads"

    # ---- 阶段5第二批：知识库上传限制（D1/D2 默认值）----
    kb_upload_max_mb: int = 10            # 单文件上限（MB）
    kb_allowed_exts: list[str] = [".md", ".pdf"]   # 上传白名单

    @property
    def normal_dataset(self) -> Path:
        return self.data_path / "meter_readings_normal.csv"

    @property
    def anomaly_dataset(self) -> Path:
        return self.data_path / "meter_readings_anomaly.csv"


@lru_cache
def get_settings() -> Settings:
    """单例配置（lru_cache：进程内只装配一次）。"""
    return Settings()
