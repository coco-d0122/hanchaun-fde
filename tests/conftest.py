# -*- coding: utf-8 -*-
"""
pytest 公共装置。

隔离原则：**测试不碰仓库里的 hanchuan.db / .cache / 模拟数据文件**。
环境变量必须在 import app 之前设置（Settings 是 lru_cache 单例），
所以这里放在模块顶部而不是 fixture 里。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="hc_test_"))
os.environ["DB_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["CACHE_DIR"] = str(_TMP / "cache")
os.environ["LLM_PROVIDER"] = "mock"
os.environ["LLM_API_KEY"] = ""
# 功能测试会连发几十个请求，把限流阈值抬高，避免测试本身被 429 打断；
# 限流行为由 tests/unit/test_ratelimit.py 单独验证。
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000000"
os.environ["RATE_LIMIT_GLOBAL_PER_MINUTE"] = "1000000"

import httpx  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
async def client():  # noqa: ANN201
    """带 lifespan 的异步客户端（触发建表、种子数据、知识库索引构建）。"""
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     timeout=60) as c:
            yield c


async def _make_user(username: str, password: str = "TestPass12345",
                     role: str = "user") -> None:
    """直接落库造用户（不走管理员接口，避免测试互相依赖）。"""
    from app.core.security import hash_password
    from app.db import repo
    from app.db.session import session_factory

    async with session_factory() as db:
        async with db.begin():
            if await repo.get_user_by_name(db, username) is None:
                await repo.create_user(db, username, hash_password(password), role)


@pytest.fixture
async def user_client(client):  # noqa: ANN001, ANN201
    """已登录的普通用户客户端。"""
    await _make_user("pytest_user", role="user")
    r = await client.post("/api/auth/login",
                          json={"username": "pytest_user", "password": "TestPass12345"},
                          headers=CSRF_HEADERS)
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
async def admin_client(client):  # noqa: ANN001, ANN201
    """已登录的管理员客户端。"""
    await _make_user("pytest_admin", role="admin")
    r = await client.post("/api/auth/login",
                          json={"username": "pytest_admin", "password": "TestPass12345"},
                          headers=CSRF_HEADERS)
    assert r.status_code == 200, r.text
    return client


def sse_events(text: str) -> list[tuple[str, str]]:
    """把 SSE 文本解析成 [(event, data)]，用于断言事件契约。"""
    events: list[tuple[str, str]] = []
    event = "message"
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            events.append((event, line[6:]))
    return events
