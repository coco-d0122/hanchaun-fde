# -*- coding: utf-8 -*-
"""
深度流程回归：直接驱动 scripts/ 下的四套自测。

为什么用子进程而不是改写：这些脚本各自建临时库、起完整 lifespan、发真实
HTTP/SSE 请求，是"端到端证据"；在 pytest 里重复实现会得到两套容易漂移的断言。
pytest 这一层只负责"把门守好"——退出码非 0 或出现失败项就红。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SUITES = [
    ("agent_selftest.py", 16),
    ("harness_selftest.py", 27),
    ("confirmation_selftest.py", 19),
    ("harness_scenarios.py", 21),
]


@pytest.mark.parametrize("script,expected", SUITES, ids=[s[0] for s in SUITES])
def test_selftest_suite_passes(script: str, expected: int):
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / script)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    assert "失败 0 项" in out, out[-2000:]
    # 断言条数也要对得上，防止脚本被误删成"空跑"
    assert f"{expected} 项" in out, f"断言条数与预期 {expected} 不一致：\n{out[-800:]}"
