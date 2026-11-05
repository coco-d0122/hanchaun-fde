# -*- coding: utf-8 -*-
"""
数据库会话工厂（阶段4）

红线（方案 7.3）：
  - async_sessionmaker，每请求独立会话，绝无全局共享的可变 session
  - SQLite 开 WAL 模式（PRAGMA journal_mode=WAL），允许多读单写并发
  - 不使用任何 SQLite 方言特性，PG 迁移时改连接串即可
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

# check_same_thread=False：aio­sqlite 的连接在线程池与事件循环间交接，必须放开
engine = create_async_engine(
    settings.db_url,
    echo=False,
    connect_args={"check_same_thread": False} if settings.db_url.startswith("sqlite") else {},
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    """每个新连接上打开 WAL 与外键约束（SQLite 默认关闭外键）。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
