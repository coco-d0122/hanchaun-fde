# -*- coding: utf-8 -*-
"""限流：固定窗口计数 + 中间件 429 行为（不依赖网络与真实服务）。"""

from __future__ import annotations

import json

import pytest

from app.core.ratelimit import FixedWindowLimiter, RateLimitMiddleware


async def _call(mw, path: str = "/api/chat") -> dict:
    """跑一次中间件调用，返回 {status, headers, body}。"""
    sent: dict = {"headers": {}}

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            sent["body"] = message.get("body", b"")

    scope = {"type": "http", "path": path, "headers": [], "client": ("10.0.0.1", 1234)}
    await mw(scope, receive, send)
    return sent


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_fixed_window_blocks_beyond_limit():
    lim = FixedWindowLimiter(limit=2, window_seconds=60)
    assert lim.hit("u:1") == (True, 0)
    assert lim.hit("u:1") == (True, 0)
    allowed, wait = lim.hit("u:1")
    assert allowed is False and 0 < wait <= 60
    # 不同用户互不影响
    assert lim.hit("u:2")[0] is True


@pytest.mark.anyio
async def test_middleware_returns_429_with_chinese_message():
    mw = RateLimitMiddleware(_ok_app)
    mw.enabled = True
    mw.user_limiter = FixedWindowLimiter(limit=1)
    mw.global_limiter = FixedWindowLimiter(limit=100)

    first = await _call(mw)
    assert first["status"] == 200

    second = await _call(mw)
    assert second["status"] == 429
    assert "retry-after" in second["headers"]
    body = json.loads(second["body"].decode("utf-8"))
    assert body["code"] == "RATE_LIMITED"
    assert "请求过于频繁" in body["message"]


@pytest.mark.anyio
async def test_health_and_static_not_limited():
    mw = RateLimitMiddleware(_ok_app)
    mw.enabled = True
    mw.user_limiter = FixedWindowLimiter(limit=1)
    mw.global_limiter = FixedWindowLimiter(limit=1)
    await _call(mw, "/api/chat")                 # 打满
    assert (await _call(mw, "/api/health"))["status"] == 200
    assert (await _call(mw, "/static/app.js"))["status"] == 200
