# -*- coding: utf-8 -*-
"""
进程内监控指标（阶段11，对应自检清单 N-06）

设计：
  - 纯 ASGI 中间件（与 ratelimit 同思路，不用 BaseHTTPMiddleware，避免缓冲 SSE）：
    统计请求总数（按 方法+状态码分类）、5xx 失败数、耗时累计/最大值、在途请求数
  - 线程安全用不到：全部计数发生在事件循环单线程内（int 自增原子性足够试点）
  - Agent 工具失败率、当日 LLM 用量在 /api/metrics 抓取时实时查库
    （试点三指标：请求量、请求失败率、工具失败率 —— 对齐阶段9 N-06 建议）

多实例部署时需换成 Prometheus client + 汇聚（已登记 N-15）。
"""

from __future__ import annotations

import time
from collections import defaultdict


class Metrics:
    """进程内计数器。所有字段只增不减；/api/metrics 抓取时计算派生率。"""

    def __init__(self) -> None:
        self.requests_total: dict[str, int] = defaultdict(int)   # key: "POST:2xx"
        self.failures_5xx: int = 0
        self.duration_count: int = 0
        self.duration_sum: float = 0.0
        self.duration_max: float = 0.0
        self.in_flight: int = 0

    def observe(self, method: str, path: str, status: int | None, seconds: float) -> None:
        code = "ERR" if status is None else f"{status // 100}xx"
        # 路径只保留 coarse 分桶，避免标签基数爆炸（/api/agent/tasks/TASK-x → /api/agent）
        bucket = _bucket(path)
        self.requests_total[f"{method}:{code}:{bucket}"] += 1
        if status is None or status >= 500:
            self.failures_5xx += 1
        self.duration_count += 1
        self.duration_sum += seconds
        self.duration_max = max(self.duration_max, seconds)


def _bucket(path: str) -> str:
    for prefix in ("/api/chat", "/api/agent", "/api/anomalies", "/api/sessions",
                   "/api/kb", "/api/users", "/api/prompts", "/api/audit",
                   "/api/logs", "/api/auth", "/api/health", "/api/metrics", "/admin"):
        if path.startswith(prefix):
            return prefix
    return "other"


METRICS = Metrics()


class MetricsMiddleware:
    """纯 ASGI：请求进出各记一次。挂载在限流中间件之内/之外均可。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        METRICS.in_flight += 1
        start = time.perf_counter()
        status_holder = {"status": None}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            METRICS.in_flight -= 1
            METRICS.observe(scope.get("method", "?"),
                            scope.get("path", ""), status_holder["status"],
                            time.perf_counter() - start)


def render_prometheus() -> str:
    """Prometheus 文本格式输出（text/plain; version=0.0.4）。"""
    m = METRICS
    lines: list[str] = []

    total = sum(m.requests_total.values())
    lines.append("# HELP http_requests_total 总请求数（按方法/状态类/路径桶）")
    lines.append("# TYPE http_requests_total counter")
    for key, n in sorted(m.requests_total.items()):
        method, code, bucket = key.split(":", 2)
        lines.append(f'http_requests_total{{method="{method}",code="{code}",path="{bucket}"}} {n}')
    lines.append(f"http_requests_total {total}")

    lines.append("# HELP http_request_failures_5xx 服务端失败请求数（5xx/异常）")
    lines.append("# TYPE http_request_failures_5xx counter")
    lines.append(f"http_request_failures_5xx {m.failures_5xx}")

    lines.append("# HELP http_request_duration_seconds 请求耗时（累计/次数/最大值）")
    lines.append("# TYPE http_request_duration_seconds summary")
    lines.append(f"http_request_duration_seconds_count {m.duration_count}")
    lines.append(f"http_request_duration_seconds_sum {m.duration_sum:.6f}")
    lines.append(f"http_request_duration_seconds_max {m.duration_max:.6f}")

    lines.append("# HELP http_requests_in_flight 在途请求数")
    lines.append("# TYPE http_requests_in_flight gauge")
    lines.append(f"http_requests_in_flight {m.in_flight}")
    return "\n".join(lines) + "\n"
