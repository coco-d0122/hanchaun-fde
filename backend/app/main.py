# -*- coding: utf-8 -*-
"""
FastAPI 应用入口（阶段4）

lifespan（方案 7.3 红线）：
  1. 建表 → 自愈悬挂消息 → 初始化异常事件（幂等）
  2. 构建混合检索器（线程池，FAISS/BM25 一次构建挂 app.state，请求期只读）
  3. 健康探测 LLM（可选降级）

日志脱敏：LLM_API_KEY 等敏感信息绝不进日志（logger 只记配置了"与否"，不记值）。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import anomalies, auth, chat, sessions, users
from app.core.config import get_settings
from app.core.embedder import build_embedder
from app.core.llm import call_llm
from app.core.retriever import HybridRetriever
from app.db import repo
from app.db.models import Base
from app.db.session import engine, session_factory
from app.errors import register_error_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("hanchuan")


def _ensure_column(db_path: Path, table: str, column: str, ddl_type: str) -> None:
    """轻量迁移：给已有表补列（SQLite ALTER TABLE ADD COLUMN）。

    create_all 不会给已存在的表加列；conversations.user_id 属于阶段5增量，
    老库升级时在这里补，避免用户丢掉会话历史。
    """
    if not db_path.exists():
        return
    try:
        con = sqlite3.connect(str(db_path))
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if cols and column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            con.commit()
            logger.info("轻量迁移：%s.%s 已补充", table, column)
        con.close()
    except sqlite3.Error:
        logger.exception("轻量迁移失败 %s.%s", table, column)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()

    # ---- 0) 轻量迁移（老库补列）----
    db_file = Path(cfg.db_url.split("///")[-1])
    _ensure_column(db_file, "conversations", "user_id", "INTEGER")

    # ---- 1) 建表 + 自愈 + 异常事件初始化 + 角色/权限种子 ----
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_factory() as db:
        async with db.begin():
            healed = await repo.heal_generating_messages(
                db, before=datetime.now(timezone.utc))
            if healed:
                logger.info("启动自愈：%s 条悬挂消息改标 failed", healed)
            seeded = await repo.seed_anomaly_events(db, cfg)
            if seeded:
                logger.info("异常事件落库：%s 条", seeded)
            await repo.seed_roles_permissions(db)
            app.state.permission_cache = await repo.list_role_permissions(db)
    logger.info("角色/权限种子就绪：%s 个角色", len(app.state.permission_cache))

    # ---- 2) 检索器构建（线程池：Embedding 可能分钟级，不能阻塞事件循环启动）----
    retriever = await anyio.to_thread.run_sync(
        lambda: _build_retriever(cfg))
    app.state.retriever = retriever
    app.state.settings = cfg

    # ---- 3) LLM 健康探测（只记可用性，不记任何 Key）----
    app.state.llm_ready = False
    if cfg.llm_provider != "mock" and cfg.llm_api_key:
        try:
            await anyio.to_thread.run_sync(
                lambda: call_llm("回复：ok", cfg, max_tokens=8, timeout=15))
            app.state.llm_ready = True
            logger.info("LLM 探测成功：%s @ %s", cfg.llm_model, cfg.llm_base_url)
        except Exception as exc:  # noqa: BLE001
            # 探测失败不阻断启动：请求期会按 C2 降级 mock（带角标）
            logger.warning("LLM 探测失败（将按配置降级）：%s", exc)

    logger.info("启动完成：http://%s:%s", cfg.serving_host, cfg.serving_port)
    yield
    await engine.dispose()


def _build_retriever(cfg) -> HybridRetriever:  # noqa: ANN001（Settings 循环导入风险，宽松标注）
    """同步构建检索器（在线程池中执行）。"""
    embedder = build_embedder(cfg)
    retriever = HybridRetriever(cfg, embedder)
    retriever.build_or_load()
    return retriever


app = FastAPI(
    title="瀚川工业园能耗异常定位智能体",
    version="0.4.0",
    description="阶段4：Web 聊天 + SSE 流式问答 + 异常事件查询",
    lifespan=lifespan,
)

register_error_handlers(app)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(anomalies.router)
app.include_router(users.router)


@app.get("/api/health", tags=["ops"])
async def health() -> dict:
    """健康检查：知识库 / LLM 状态（不暴露任何密钥与内部地址）。"""
    retriever: HybridRetriever | None = getattr(app.state, "retriever", None)
    cfg = get_settings()
    return {
        "status": "ok",
        "kb": {
            "ready": retriever is not None and bool(retriever.chunks),
            "chunks": len(retriever.chunks) if retriever else 0,
            "cache_hit": bool(getattr(retriever, "cache_hit", False)),
        },
        "llm": {
            "provider": cfg.llm_provider,
            "model": cfg.llm_model if (cfg.llm_api_key or cfg.llm_provider == "mock") else "mock(offline)",
            "ready": bool(getattr(app.state, "llm_ready", False)),
        },
    }


# ---- 静态页面（聊天界面宿主）----
_static_dir = Path(__file__).resolve().parents[1] / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        """登录页（未登录用户可访问）。"""
        return FileResponse(_static_dir / "login.html")

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        """聊天页：未登录 302 跳登录页（页面路由级保护）。"""
        from app.api.deps import get_current_user_optional
        user = await get_current_user_optional(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(_static_dir / "index.html")
