# -*- coding: utf-8 -*-
"""
并发压测脚本（阶段11，对应自检清单 N-04 / 阶段9 D2-01）

用法（先启动服务，LLM_PROVIDER=mock 或已配置 Key）：
    python scripts/load_test.py --url http://127.0.0.1:8000 \
        --username admin --password <密码> \
        --users 30 --duration 60 --scenario read

场景：
  read   ：GET /api/anomalies（查询路径，含权限与行级范围）
  agent  ：POST /api/agent/analyze（SSE 全链路：检索→Harness→报告）

输出：P50/P95/P99 延迟、错误率、吞吐；同时写 eval/load_test_report.json。
压测口径与阶段9 清单一致：5xx=0、SSE 中断率 <1% 为硬性下限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def one_read(client: httpx.AsyncClient, stop_at: float,
                   latencies: list[float], errors: list[str]) -> None:
    while time.monotonic() < stop_at:
        t0 = time.perf_counter()
        try:
            r = await client.get("/api/anomalies")
            if r.status_code != 200:
                errors.append(f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        latencies.append(time.perf_counter() - t0)
        await asyncio.sleep(0.05)


async def one_agent(client: httpx.AsyncClient, stop_at: float,
                    latencies: list[float], errors: list[str]) -> None:
    headers = {"X-Requested-With": "XMLHttpRequest"}
    while time.monotonic() < stop_at:
        t0 = time.perf_counter()
        try:
            first_byte = None
            frames = 0
            async with client.stream(
                    "POST", "/api/agent/analyze", headers=headers,
                    json={"question": "分析 M-002 倍率异常事件", "engine": "script"}) as r:
                if r.status_code != 200:
                    errors.append(f"HTTP {r.status_code}")
                    continue
                async for _line in r.aiter_lines():
                    if first_byte is None:
                        first_byte = time.perf_counter() - t0
                    frames += 1
                    if time.monotonic() > stop_at + 30:  # 允许最后一个请求收尾
                        break
            if frames == 0:
                errors.append("SSE_EMPTY")
            latencies.append(time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--users", type=int, default=30)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--scenario", choices=["read", "agent"], default="read")
    args = ap.parse_args()

    latencies: list[float] = []
    errors: list[str] = []
    stop_at = time.monotonic() + args.duration

    async with httpx.AsyncClient(base_url=args.url, timeout=60.0) as client:
        r = await client.post("/api/auth/login",
                              json={"username": args.username, "password": args.password},
                              headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            raise SystemExit(f"登录失败：{r.status_code} {r.text[:200]}")
        worker = one_read if args.scenario == "read" else one_agent
        started = time.perf_counter()
        await asyncio.gather(*[worker(client, stop_at, latencies, errors)
                               for _ in range(args.users)])
        wall = time.perf_counter() - started

    lat_ms = sorted(x * 1000 for x in latencies)

    def pct(p: float) -> float:
        return lat_ms[min(len(lat_ms) - 1, int(len(lat_ms) * p))] if lat_ms else 0.0

    report = {
        "scenario": args.scenario,
        "url": args.url,
        "concurrency": args.users,
        "duration_s": round(wall, 1),
        "requests": len(lat_ms),
        "throughput_rps": round(len(lat_ms) / wall, 2) if wall else 0,
        "error_count": len(errors),
        "error_rate": round(len(errors) / (len(lat_ms) + len(errors) or 1), 4),
        "latency_ms": {
            "p50": round(pct(0.50), 1), "p95": round(pct(0.95), 1),
            "p99": round(pct(0.99), 1), "max": round(lat_ms[-1] if lat_ms else 0, 1),
        },
        "error_samples": errors[:10],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    out = ROOT / "eval" / "load_test_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{out}")


if __name__ == "__main__":
    asyncio.run(main())
