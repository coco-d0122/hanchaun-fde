# -*- coding: utf-8 -*-
"""
评测集资产校验：结构合法、数量正确、问题不重复。

全量执行（30 条用例真跑一遍）耗时较长，默认不跑；CI 与发布前用
RUN_AGENT_EVAL=1 打开，或直接执行 scripts/run_agent_eval.py。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "eval" / "agent_eval_cases.json"

EXPECTED = {"knowledge": 10, "tool_calling": 10, "safety": 5, "multi_turn": 5}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_case_count_and_categories(cases):
    assert len(cases) == 30
    counted: dict[str, int] = {}
    for c in cases:
        counted[c["category"]] = counted.get(c["category"], 0) + 1
    assert counted == EXPECTED


def test_required_fields(cases):
    for i, c in enumerate(cases, 1):
        assert set(c) == {"question", "expected_tools", "expected_behavior", "category"}, f"用例 {i}"
        assert isinstance(c["expected_tools"], list) and c["expected_tools"], f"用例 {i}"
        assert c["expected_behavior"].strip(), f"用例 {i}"
        q = c["question"]
        assert isinstance(q, (str, list)), f"用例 {i}"


def test_questions_are_unique(cases):
    def norm(q) -> str:
        if isinstance(q, list):
            return "||".join(x["content"] if isinstance(x, dict) else x for x in q)
        return q

    seen = [norm(c["question"]) for c in cases]
    assert len(set(seen)) == len(seen), "存在重复用例"


def test_multi_turn_cases_are_message_arrays(cases):
    for c in cases:
        if c["category"] == "multi_turn":
            assert isinstance(c["question"], list) and len(c["question"]) >= 2


def test_case_validator_script_passes():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_eval_cases.py")],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    assert "失败 0 项" in proc.stdout


@pytest.mark.skipif(os.environ.get("RUN_AGENT_EVAL") != "1",
                    reason="全量评测较慢，设置 RUN_AGENT_EVAL=1 开启")
def test_full_eval_run():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_agent_eval.py")],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    assert "通过 30，失败 0" in out, out[-2000:]
