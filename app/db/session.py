# -*- coding: utf-8 -*-
"""
数据库会话工厂（阶段4）

红线（方案 7.3）：
  - async_sessionmaker，每请求独立会话，绝无全局共享的可变 session
  - SQLite 开 WAL 模式（PRAGMA journal_mode=WAL），允许多读单写并发
  - 不使用任何 SQLite 方言特性，PG 迁移时改连接串即可
"""

from __future__ import annotations

import json

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

# check_same_thread=False：aio­sqlite 的连接在线程池与事件循环间交接，必须放开
# timeout：写锁冲突时的等待秒数（压测实测 30 并发下默认 0 会直接抛
# "database is locked" → 5xx；配合 WAL 与 busy_timeout 可消除）。
# json_serializer：JSON 列（audit_logs.detail / user_roles.scope_values）保留中文原文，
#   否则写库时被转成 \uXXXX，排查日志时不可读（对外接口不受影响，那里是反序列化后的对象）
engine = create_async_engine(
    settings.db_url,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30}
    if settings.db_url.startswith("sqlite") else {},
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
)

session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
    """每个新连接上打开 WAL、外键约束与写锁等待（SQLite 默认关闭外键）。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()
