# -*- coding: utf-8 -*-
"""
多轮会话上下文解析（阶段8，为"多轮对话与历史上下文"补齐 Agent 通道）

背景：阶段4 的 knowledge 通道把最近 6 轮历史塞进 Prompt，模型自己看上下文；
但 Agent 通道是"工具参数必须精确"的通道——模型（或离线规划器）必须拿到
**具体的 meter_id / building_id / 时间窗**才能调工具。因此这里做一层确定性解析：

    用户原话（"它的倍率是多少？" / "那昨天呢？" / "第二块为什么更高？"）
        → 继承最近 6 轮里出现过的电表、楼栋、时间范围
        → 解析成明确参数；解析不出来就标记 ambiguous，由编排层反问，绝不在缺失参数时臆造

设计红线（与评测标准对齐）：
  1. 只继承**用户明确说过**的实体；不从回答正文里猜编号（回答是 AI 生成物，不能当事实源）；
  2. 指代有歧义 → 反问，不默认取第一块表；
  3. 相对日期按"评测时钟"解析（eval_clock_date），没有配置就用今天——
     这样"昨天"到底是哪一天是可复现的，而不是随运行时间漂移；
  4. 继承只影响"参数解析"，不影响门禁：validate/detect 的前置条件仍由任务状态机判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.core.config import Settings
from app.db import repo
from app.db.session import session_factory

# ---- 别名表（演示数据与需求场景的写法差异，集中一处，便于业务确认后改）----
METER_ALIAS: dict[str, str] = {
    "METER_001": "M-002",     # 需求场景写的 METER_001（倍率 10→100）对应模拟数据里的 M-002
    "METER_002": "M-003",
    "METER_003": "M-004",
}
BUILDING_ALIAS: dict[str, str] = {
    "A栋": "B1", "A楼": "B1", "A座": "B1", "一号楼": "B1", "1号楼": "B1",
    "B栋": "B2", "B楼": "B2", "B座": "B2", "二号楼": "B2", "2号楼": "B2",
}

# 结尾不能用 \b：中文紧跟在编号后面时（"M-002的倍率""M-003在2026"）不存在词边界，
# 用 \b 会漏匹配，导致问题里的电表号被忽略、退回"默认那块表"——这是实测踩过的坑。
_METER_RE = re.compile(r"(MT-[A-Za-z]\d{1,2})(?![0-9])|(METER[_-]?(\d{3}))(?![0-9])"
                       r"|(M[-_](\d{3}))(?![0-9])", re.IGNORECASE)
_BUILDING_RE = re.compile(r"([A-Za-z]\d?)\s*(?:栋|楼|座)|([一二三]号楼)|\b(B\d)\b")
_EXPLICIT_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")

# 指代与序数
_PRONOUN_RE = re.compile(r"(它的?|这个表|这块表|该表|这只表|这台表|这个|那个|该设备)")
_ORDINAL_MAP = {"第一": 0, "首个": 0, "首块": 0, "前一": 0,
                "第二": 1, "次块": 1, "后一": 1,
                "第三": 2, "第四": 3}

# 相对时间词（按优先级从具体到宽泛）
_REL_DAY = (("前天", 2), ("昨天", 1), ("昨日", 1), ("今天", 0), ("今日", 0),
            ("当天", 0), ("本日", 0))

DEFAULT_DAY_START = "00:00"
DEFAULT_DAY_END = "23:45"


@dataclass
class Resolution:
    """一次问题解析的结果：明确参数 + 继承来源 + 歧义说明。"""

    meter_id: str | None = None
    building_id: str | None = None
    window: tuple[str, str] | None = None
    compare_meters: list[str] = field(default_factory=list)
    meter_explicit: bool = False                          # 电表号是否直接写在问题上
    inherited: list[str] = field(default_factory=list)   # 人类可读的继承说明（进任务账本）
    ambiguous: str | None = None                          # 非空 = 必须反问
    followup_of_task: str | None = None                   # 追问指向的上一轮任务


@dataclass
class ConversationContext:
    """最近若干轮会话的实体索引（只从 user 消息里取，回答正文不作为事实源）。"""

    messages: list[dict] = field(default_factory=list)
    prior_task: dict | None = None
    clock: date = field(default_factory=date.today)
    meters: list[str] = field(default_factory=list)       # 按出现顺序（去重）
    buildings: list[str] = field(default_factory=list)
    last_window: tuple[str, str] | None = None

    # ---------------- 载入 ----------------
    @classmethod
    async def load(cls, session_id: str | None, cfg: Settings,
                   question: str = "") -> "ConversationContext":
        clock = _clock_date(cfg)
        if not session_id:
            return cls(clock=clock)
        async with session_factory() as db:
            async with db.begin():
                history = await repo.load_history(db, session_id, cfg.history_rounds,
                                                  cfg.history_char_budget)
                prior = await repo.get_latest_agent_task(db, session_id)

        # 当前这轮用户消息已由 API 落库，解析历史时要排除它，否则"它"会指到它自己
        msgs = list(history)
        if msgs and msgs[-1]["role"] == "user" and question \
                and msgs[-1]["content"].strip() == question.strip():
            msgs = msgs[:-1]

        ctx = cls(messages=msgs, prior_task=prior, clock=clock)
        for m in msgs:
            if m["role"] != "user":
                continue
            for meter in _extract_meters(m["content"]):
                if meter not in ctx.meters:
                    ctx.meters.append(meter)
            for b in _extract_buildings(m["content"]):
                if b not in ctx.buildings:
                    ctx.buildings.append(b)
        if prior and prior.get("meter_id"):
            # 上一轮任务实际分析过哪块表：比"用户提到过"更强，放在最后作为兜底
            if prior["meter_id"] not in ctx.meters:
                ctx.meters.append(prior["meter_id"])
        return ctx

    # ---------------- 解析 ----------------
    def resolve(self, question: str) -> Resolution:
        q = question or ""
        res = Resolution()
        explicit_meters = _extract_meters(q)

        if len(explicit_meters) >= 2:
            res.compare_meters = explicit_meters
            res.meter_id = explicit_meters[-1]
            res.meter_explicit = True
            res.inherited.append("问题中出现多块电表，按比较任务处理：" + "、".join(explicit_meters))
        elif explicit_meters:
            res.meter_id = explicit_meters[0]
            res.meter_explicit = True
        else:
            # 序数指代："第二块" → 历史里第 2 块表
            ordinal = next((idx for k, idx in _ORDINAL_MAP.items() if k in q), None)
            if ordinal is not None:
                if len(self.meters) > ordinal:
                    res.meter_id = self.meters[ordinal]
                    res.inherited.append(f"序数指代继承：第 {ordinal + 1} 块 → {res.meter_id}")
                else:
                    res.ambiguous = ("问题里的“第几块表”没有可对应的历史记录，"
                                     "请说明具体电表编号")
                    return res
            elif _PRONOUN_RE.search(q):
                if self.meters:
                    res.meter_id = self.meters[-1]
                    res.inherited.append(f"代词指代继承：{res.meter_id}（最近一轮提到的电表）")
                else:
                    res.ambiguous = ("问题里的指代（它/这块表）没有可继承的电表，"
                                     "请补充电表编号或楼栋")
                    return res

        # 楼栋
        explicit_buildings = _extract_buildings(q)
        if explicit_buildings:
            res.building_id = explicit_buildings[0]
        elif res.meter_id and not explicit_meters:
            # 追问里没说楼栋：不额外推断，电表已足够定位
            res.building_id = None
        elif not res.meter_id and self.buildings:
            res.building_id = self.buildings[-1]
            res.inherited.append(f"楼栋继承：{res.building_id}")

        # 时间窗
        res.window, time_note = self._resolve_window(q)
        if time_note:
            res.inherited.append(time_note)

        # 追问指向的上一轮任务
        if self.prior_task and (res.meter_id or res.building_id) and _is_followup(q):
            res.followup_of_task = self.prior_task.get("task_id")
        return res

    def _resolve_window(self, q: str) -> tuple[tuple[str, str] | None, str | None]:
        dates = _EXPLICIT_DATE_RE.findall(q)
        if dates:
            if len(dates) >= 2:
                start = date(int(dates[0][0]), int(dates[0][1]), int(dates[0][2]))
                end = date(int(dates[-1][0]), int(dates[-1][1]), int(dates[-1][2]))
                return ((f"{start:%Y-%m-%d} {DEFAULT_DAY_START}",
                         f"{end:%Y-%m-%d} {DEFAULT_DAY_END}"),
                        f"时间窗按问题中的日期：{start}~{end}")
            d = date(int(dates[0][0]), int(dates[0][1]), int(dates[0][2]))
            return ((f"{d:%Y-%m-%d} {DEFAULT_DAY_START}", f"{d:%Y-%m-%d} {DEFAULT_DAY_END}"),
                    f"时间窗按问题中的日期：{d}")

        for word, delta in _REL_DAY:
            if word in q:
                d = self.clock - timedelta(days=delta)
                label = "今天" if delta == 0 else f"{word}（{delta} 天前）"
                return ((f"{d:%Y-%m-%d} {DEFAULT_DAY_START}", f"{d:%Y-%m-%d} {DEFAULT_DAY_END}"),
                        f"时间窗按评测时钟解析：{label} = {d}")
        if any(w in q for w in ("最近", "近期", "这几天")):
            d = self.clock
            return ((f"{d:%Y-%m-%d} {DEFAULT_DAY_START}", f"{d:%Y-%m-%d} {DEFAULT_DAY_END}"),
                    f"时间窗按评测时钟解析：最近 = {d}")

        if _is_followup(q) and self.last_window:
            return self.last_window, "时间窗继承上一轮"
        return None, None


def _clock_date(cfg: Settings) -> date:
    """
    相对日期的基准日（"今天"是哪天）。

    优先级：配置 eval_clock_date > 演示数据时钟 > 系统日期。

    为什么默认用"数据日 + 1 天"而不是系统日期：
      本系统的能耗数据是固定的模拟样本（2026-10-23 一个完整日）。若按系统日期解析
      "昨天"，任何相对时间问题都会落到 2026-11-05 而查不到任何读数，
      演示与回归会自动全部失败。模拟数据既然是"最近一个完整日"，把"今天"设为它的
      次日才符合业务语义；接入真实数据源后系统日期自然接管（数据集最新日期=今天）。
    """
    raw = (getattr(cfg, "eval_clock_date", "") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        from app.agent.tools import load_dataset

        readings = load_dataset(cfg)
        return max(r.ts for r in readings).date() + timedelta(days=1)
    except Exception:  # noqa: BLE001  数据源不可用时退回系统日期
        return date.today()


def _extract_meters(text: str) -> list[str]:
    """从文本中提取电表编号（含 METER_001 别名），按出现顺序去重。"""
    out: list[str] = []
    for m in _METER_RE.finditer(text or ""):
        raw = m.group(0).upper().replace("_", "-")
        if raw.startswith("METER"):
            key = raw.replace("-", "_")
            meter = METER_ALIAS.get(key) or METER_ALIAS.get(raw.replace("-", "_"))
            meter = meter or "M-" + key.split("_")[-1]
        elif raw.startswith("MT-"):
            meter = raw
        else:
            meter = "M-" + raw.split("-")[-1]
        if meter not in out:
            out.append(meter)
    return out


def _extract_buildings(text: str) -> list[str]:
    """从文本中提取楼栋（支持 A栋/A楼/一号楼/B1 等写法）。"""
    out: list[str] = []
    for word, target in BUILDING_ALIAS.items():
        if word in (text or "") and target not in out:
            out.append(target)
    for m in _BUILDING_RE.finditer(text or ""):
        token = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if not token:
            continue
        target = BUILDING_ALIAS.get(token) or BUILDING_ALIAS.get(token + "栋") or token
        if target and target not in out:
            out.append(target)
    return out


def _is_followup(q: str) -> bool:
    """是否是"接着上一轮说"的追问（指代/序数/时间省略/提交复核等）。"""
    if _PRONOUN_RE.search(q) or any(k in q for k in _ORDINAL_MAP):
        return True
    if any(w in q for w in ("那昨天", "那今天", "呢？", "呢?", "上一轮", "刚才", "这个需要")):
        return True
    return any(w in q for w in ("提交审核", "提交人工复核", "提交复核"))
