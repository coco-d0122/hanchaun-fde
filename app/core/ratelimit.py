# -*- coding: utf-8 -*-
"""
请求限流（阶段9 上线前补齐）

为什么需要：LLM 问答与 Agent 分析是"花钱且耗时"的接口。没有限流时，
单个用户（或一个写错的循环脚本）就能把工作进程和 LLM 配额全部占满，
表现为"其他人打不开页面"，而日志里看不出是谁干的。

实现取舍：
  - **进程内固定窗口计数**，不引入 Redis：试点是单实例部署，引入外部依赖
    反而增加上线风险。多实例部署时必须换成 Redis 或网关限流（见 docs/deployment.md）。
  - **纯 ASGI 中间件**，不用 BaseHTTPMiddleware：后者会缓冲响应体，
    会把 SSE 变成"等全部生成完再一次性吐出"，流式体验直接消失。
  - 只拦 `/api/`，放行静态资源、登录页与健康检查：限流不能把监控和登录也拦住。
  - 计数键：已登录用 `u:{user_id}`（跟着人走，换 IP 也限得住）；未登录用 `ip:{addr}`。
  - 超限返回 429 + 中文说明 + Retry-After，前端据此提示"请求过于频繁"。
"""

from __future__ import annotations

import json
import threading
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings
from app.core.security import COOKIE_NAME, decode_token


class FixedWindowLimiter:
    """固定窗口计数器（窗口 60s）。线程安全：中间件跑在线程池里也可能并发。"""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[int, int]] = {}   # key -> (window_start, count)
        self._last_gc = time.time()

    def hit(self, key: str) -> tuple[bool, int]:
        """
        记一次请求。返回 (是否放行, 需等待秒数)。

        过期的窗口不主动删也能工作，但键会无限增长（长期运行必漏内存），
        所以每 60 个窗口做一次全量清理——成本 O(n)，n 是活跃用户数量级。
        """
        now = int(time.time())
        window_start = now - (now % self.window)
        with self._lock:
            if now - self._last_gc > self.window * 60:
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if v[0] == window_start}
                self._last_gc = now

            start, count = self._buckets.get(key, (window_start, 0))
            if start != window_start:            # 进入新窗口，计数归零
                start, count = window_start, 0
            count += 1
            self._buckets[key] = (start, count)
            if count > self.limit:
                return False, self.window - (now % self.window)
            return True, 0


class RateLimitMiddleware:
    """用户级 + 系统级双重限流。系统级用于兜住"很多人同时用"的总量。"""

    # 不计入限流的路径：健康检查（监控每分钟探测）、登录页、静态资源
    EXEMPT_PREFIXES = ("/static/", "/api/health", "/login", "/docs", "/openapi.json")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._configure()

    def _configure(self) -> None:
        cfg = get_settings()
        self.enabled = bool(cfg.rate_limit_enabled)
        self.user_limiter = FixedWindowLimiter(max(1, cfg.rate_limit_per_minute))
        self.global_limiter = FixedWindowLimiter(max(1, cfg.rate_limit_global_per_minute))

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    @staticmethod
    def _user_key(scope: Scope) -> str | None:
        """从 Cookie 解析用户（只解析签名，不查库：限流层不做业务查询）。"""
        for name, value in scope.get("headers") or []:
            if name == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE_NAME and v:
                        payload = decode_token(v)
                        if payload and payload.get("sub"):
                            return f"u:{payload['sub']}"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(self.EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        key = self._user_key(scope) or f"ip:{self._client_ip(scope)}"
        allowed_user, wait_user = self.user_limiter.hit(key)
        allowed_global, wait_global = self.global_limiter.hit("global")
        if not allowed_user or not allowed_global:
            retry = max(wait_user, wait_global, 1)
            # 与 app.errors 的统一错误结构保持一致（前端只写一套错误渲染）
            body = {
                "code": "RATE_LIMITED",
                "message": f"请求过于频繁，请在 {retry} 秒后重试。"
                           "如需批量分析，请分批提交或联系管理员调整配额。",
                "details": {"retry_after": retry},
            }
            payload: bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"retry-after", str(retry).encode("ascii")),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": payload})
            return

        await self.app(scope, receive, send)
