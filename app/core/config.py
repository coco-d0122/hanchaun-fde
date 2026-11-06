# -*- coding: utf-8 -*-
"""
核心配置（阶段4）

由阶段3的 dataclass Config 改造为 pydantic-settings：
  - 字段名与阶段3 .env 完全一致（无前缀），旧 .env 文件可直接复用
  - 全部默认值逐项保持，新增字段只服务于 Web 后端
  - PROJECT_ROOT 指向仓库根目录（app/ 的上一级）：知识库、模拟数据、缓存、
    .env、SQLite 文件都放在根目录，容器里挂载一个卷即可（/data 与 /knowledge 也可外置）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录：app/core/config.py -> app/core -> app -> <root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 兼容旧名（阶段4~8 的代码/文档使用 BACKEND_DIR），语义已变为仓库根目录
BACKEND_DIR = PROJECT_ROOT


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
    # 阶段11：单用户 LLM 日 Token 预算（prompt+completion 估算值累计；0 = 不限）
    # 达到预算后当日新请求返回 429 中文提示，防 Key 泄漏或死循环烧穿费用。
    llm_daily_token_budget: int = 500_000

    # ---- Embedding ----
    embedding_provider: str = "auto"      # auto | hash | api | st
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_dim: int = 1024             # 仅 hash 模式使用

    # ---- 知识库与切片 ----
    kb_dir: str = "knowledge/samples"
    data_dir: str = "data/samples"
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
    # 评测时钟（阶段8）：把"今天/昨天/最近"解析成可复现的日期。
    # 留空 = 用系统日期；只有跑评测/回归时才会配置，生产默认不影响业务语义。
    eval_clock_date: str = ""
    citation_excerpt_len: int = 120       # 引用摘录截断长度（存库，避免重复存全文）
    serving_host: str = "127.0.0.1"       # C3 默认：仅本机
    serving_port: int = 8000
    uploads_dir: str = "uploads"   # 用户上传的原始文档（运行时数据，不入库）

    # ---- 阶段9：限流（进程内固定窗口；多实例部署需换 Redis/网关）----
    # ---- 阶段9：Agent 紧急停用开关 ----
    # 停用后 /api/agent/analyze 直接 503，但登录、异常列表、会话与复核记录仍可用
    # （"停 AI 不等于停系统"：出事时人工仍要能查证据）。
    agent_enabled: bool = True

    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 240          # 单用户每分钟请求数（试点建议值，待压测校准）
    rate_limit_global_per_minute: int = 1200  # 全系统每分钟请求数（兜底总量）

    # ---- 阶段11：监控指标（进程内计数器；多实例部署需换 Prometheus 客户端 + 汇聚）----
    metrics_enabled: bool = True

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
        return self._resolve(PROJECT_ROOT, self.uploads_dir)

    # ---- 阶段5第二批：知识库上传限制（D1/D2 默认值）----
    log_retention_days: int = 90          # 操作日志保留天数（方案 D6 默认 90）
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
