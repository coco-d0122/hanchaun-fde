# -*- coding: utf-8 -*-
"""多轮上下文解析：电表号/楼栋/相对日期/代词继承。纯函数，无需数据库。"""

from __future__ import annotations

import pytest

from app.agent.context import _clock_date, _extract_buildings, _extract_meters, _is_followup
from app.core.config import get_settings


@pytest.mark.parametrize("text,expected", [
    ("M-002的倍率从10变成100", ["M-002"]),
    ("检查M-003在2026-10-23是否存在缺口", ["M-003"]),      # 中文紧邻，无词边界
    # 需求场景写 METER_001（倍率 10→100），模拟数据里对应 M-002：别名表把两者对齐
    ("METER_001昨天的能耗", ["M-002"]),
    ("MT-B1总表与分表差多少", ["MT-B1"]),
    ("比较M-001和M-002昨天用电", ["M-001", "M-002"]),
    ("这笔单子金额是1000", []),                            # 不能把数字当电表
])
def test_extract_meters(text, expected):
    assert _extract_meters(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("B1楼昨天用电量", ["B1"]),
    # 需求用"A栋/B栋"，模拟数据用 B1/B2：别名必须双向认得出，否则串楼栋
    ("A栋楼今天的用电量", ["B1"]),
    ("B2 的空调回路", ["B2"]),
])
def test_extract_buildings(text, expected):
    assert _extract_buildings(text) == expected


@pytest.mark.parametrize("text", ["它的倍率是多少？", "那昨天呢？", "这个需要人工复核吗？",
                                  "把第二块表的配置查一下"])
def test_followup_detected(text):
    assert _is_followup(text) is True


@pytest.mark.parametrize("text", ["查询 M-002 昨天用电量", "电表倍率变更需要核查哪些信息"])
def test_not_followup(text):
    assert _is_followup(text) is False


def test_clock_defaults_to_data_day_plus_one():
    """
    相对日期的基准日必须落在模拟数据上，否则"昨天"永远查不到数。

    当前模拟数据覆盖 2026-10-23，因此默认基准日是 2026-10-24；
    配置了 EVAL_CLOCK_DATE 时以配置为准（评测可复现）。
    """
    cfg = get_settings()
    saved = cfg.eval_clock_date
    try:
        cfg.eval_clock_date = ""
        assert _clock_date(cfg).isoformat() == "2026-10-24"
        cfg.eval_clock_date = "2026-10-24"
        assert _clock_date(cfg).isoformat() == "2026-10-24"
    finally:
        cfg.eval_clock_date = saved
