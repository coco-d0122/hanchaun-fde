# -*- coding: utf-8 -*-
"""
聊天服务（阶段4核心）：双模式 SSE 编排

  knowledge：历史 → 检索（线程池）→ 依据判定 → Prompt → LLM 流式 → 落库
  anomaly：  历史 → 事件加载 → 检索（结构化摘要）→ Prompt → LLM 流式 →
             数值溯源 → 引用（知识 + 数据证据）→ 落库

消息状态语义（方案 4.4，从不无凭据写 complete）：
  正常完成     → complete（引用一并入库）
  用户停止/断连 → interrupted（已累积内容入库）
  LLM 失败     → failed（error_info 入库）；无 token 时允许降级 mock 继续流
  进程崩溃     → generating 悬挂，启动自愈改标 failed
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

import anyio
from fastapi import Request

from app.core.config import Settings
from app.core.guardrail import number_trace_guard
from app.core.llm import LLMError, call_llm_stream
# 系统提示词正文已迁入 prompt_templates（阶段5第三批），这里只保留用户侧拼装函数
from app.core.prompts import build_anomaly_prompt, build_qa_prompt
from app.rag.retriever import HybridRetriever, Retrieved
from app.energy.anomaly_detector import AnomalyEvent
from app.db import repo
from app.db.models import AnomalyEventRow
from app.db.session import session_factory
from app.schemas.chat import ChatRequest

logger = logging.getLogger(__name__)

# 后台持久化任务的强引用集合（防止任务被 GC 提前回收）
_PERSIST_TASKS: set[asyncio.Task] = set()


async def _persist_interrupted(message_id: int, content: str) -> None:
    """在独立任务中把中断消息落库（与被取消的请求任务隔离，不会被连带取消）。"""
    try:
        async with session_factory() as db:
            async with db.begin():
                await repo.finish_message(db, message_id, "interrupted", content=content)
        logger.info("中断消息已落库：message_id=%s，内容 %s 字", message_id, len(content))
    except Exception:  # noqa: BLE001
        logger.exception("中断消息落库失败 message_id=%s（启动自愈可兜底）", message_id)

# ---- 状态文案（方案 4.1：与真实执行点一一对应，无固定延时模拟）----
STATUS_TEXT = {
    "understanding": "正在理解问题……",
    "retrieving": "正在检索能源管理知识库……",
    "loading_event": "正在读取异常事件……",
    "analyzing": "正在分析能耗异常……",
    "generating": "正在生成回答……",
}

# 依据不足提示（不调 LLM，把决定权留给用户换问法）
NO_EVIDENCE_MESSAGE = (
    "当前知识库中未找到相关规定，无法提供有依据的回答。"
    "建议换一种问法或补充制度文档后再问。"
)

# 从用户自然语言中提取电表号 / 事件号（E1 扩展的正则路径）
_METER_RE = re.compile(r"\b(?:METER[-_ ]?|M[-_]?)([A-Za-z0-9_-]{1,12})\b", re.IGNORECASE)
_EVENT_RE = re.compile(r"AN-\d{8}-\d{4}", re.IGNORECASE)


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """组装一帧 SSE：event + 单行 JSON data + 空行分隔。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def knowledge_citations(results: list[Retrieved], excerpt_len: int) -> list[dict[str, Any]]:
    """知识库引用（来自当次检索的真实返回，不经模型转手）。"""
    return [
        {
            "source_type": "knowledge",
            "source_file": r.chunk.source_file,
            "chunk_id": r.chunk.chunk_id,
            "excerpt": r.chunk.content[:excerpt_len],
        }
        for r in results
    ]


def anomaly_data_citations(event_row: AnomalyEventRow) -> list[dict[str, Any]]:
    """数据证据引用：由规则引擎计算结果直接组装（E3：source_type=anomaly_data）。"""
    items: list[dict[str, Any]] = []
    metrics = event_row.rule_result or {}
    if event_row.rule_code == "R04_RATIO_CHANGE":
        excerpt = (f"旧倍率 {metrics.get('旧倍率', '-')} → 新倍率 {metrics.get('新倍率', '-')}，"
                   f"变化 {metrics.get('变化倍数', '-')} 倍")
    else:
        excerpt = (f"总表电量 {metrics.get('总表电量_kWh', '-')} kWh，"
                   f"分表合计 {metrics.get('分表合计_kWh', '-')} kWh，"
                   f"差异率 {metrics.get('差异率', '-')}")
    items.append({
        "source_type": "anomaly_data",
        "source_file": "energy_readings",
        "chunk_id": f"{event_row.id}/evidence/{event_row.rule_code[:3]}",
        "excerpt": excerpt,
    })
    return items


def resolve_anomaly_id(question: str, rows: list[dict[str, Any]]) -> str | None:
    """
    anomaly 模式的事件定位（E1 扩展）：
      1. 优先精确匹配事件号（AN-YYYYMMDD-NNNN）
      2. 其次从自然语言中提取电表号（METER_001 / M-002 / MT-B1 等），命中唯一事件
      3. 都取不到返回 None → API 层返回 400 ANOMALY_NOT_SPECIFIED
    """
    m = _EVENT_RE.search(question.upper())
    if m:
        target = m.group(0)
        for r in rows:
            if r["anomaly_id"].upper() == target:
                return r["anomaly_id"]
        return None

    m = _METER_RE.search(question)
    if m:
        raw = m.group(0).upper()
        meter = re.sub(r"^METER[-_ ]?|^M[-_]?", "M-", raw) if raw.startswith(("METER", "M-")) else f"M-{raw}"
        # 先精确试 "M-xxx"，再试去掉前缀的裸编号（兼容 METER_001 与 MT-B1 两种写法）
        for r in rows:
            if r["meter_id"].upper() == meter:
                return r["anomaly_id"]
        for r in rows:
            if meter.lstrip("M-") and meter.lstrip("M-") in r["meter_id"].upper():
                return r["anomaly_id"]
    return None


async def chat_stream(
    req: ChatRequest,
    http_request: Request,
    retriever: HybridRetriever,
    cfg: Settings,
    user_id: int | None = None,
) -> AsyncIterator[str]:
    """
    SSE 主流程生成器。

    前置校验（会话存在、事件存在、KB 就绪）已在 API 层完成；
    anomaly 模式必须保证 req.anomaly_id 已解析为有效事件编号（API 层负责回填）。
    user_id：阶段5 会话归属，创建会话时落库。

    每次数据库操作使用独立短事务（WAL 模式下多读单写安全），
    避免流式生成期间长时间持有写事务。
    """

    def _status(stage: str) -> str:
        return sse_frame("status", {"stage": stage, "text": STATUS_TEXT[stage]})

    async def _stopped() -> bool:
        """用户停止生成 / 断线检测（每个 token 之间检查一次）。

        Starlette 新版的 is_disconnected() 是协程，必须 await。
        """
        try:
            return bool(await http_request.is_disconnected())
        except Exception:  # noqa: BLE001
            return False

    # ---------- Phase 1：会话确定 + 历史读取 + 用户消息落库 ----------
    # 提示词版本快照：在请求开始时取一次，整个生成期间沿用（改库不影响在途请求）
    from app.services.prompt_service import get_system_prompt
    prompt_code = "qa" if req.mode == "knowledge" else "anomaly"
    prompt_version_id, system_text = await get_system_prompt(prompt_code)

    async with session_factory() as db:
        async with db.begin():
            if req.session_id:
                conv = await repo.get_conversation(db, req.session_id)
            else:
                conv = None
            created = conv is None
            if created:
                conv = await repo.create_conversation(db, req.question[:30], user_id=user_id)
            session_id = conv.id
            history = await repo.load_history(db, session_id, cfg.history_rounds,
                                              cfg.history_char_budget)
            await repo.add_message(db, session_id, "user", req.question, req.mode, status=None)

    yield sse_frame("session", {"session_id": session_id, "created": created})
    yield _status("understanding")

    # ---------- Phase 2：按模式分流 ----------
    guardrail: dict[str, Any] = {"passed": True, "checked": 0, "unmatched": []}
    evidence_text = ""      # 数值溯源用：证据 JSON（anomaly 模式）
    context_text = ""       # 数值溯源用：知识片段正文
    citations: list[dict[str, Any]] = []

    if req.mode == "knowledge":
        yield _status("retrieving")
        try:
            # 检索是 CPU/网络混合操作，必须丢线程池，不能阻塞事件循环（方案 7.3）
            results = await anyio.to_thread.run_sync(
                lambda: retriever.retrieve(req.question, top_k=cfg.top_k))
        except Exception as exc:  # noqa: BLE001
            logger.exception("检索失败")
            yield sse_frame("error", {"code": "RETRIEVE_FAILED", "message": "知识检索失败，请重试",
                                      "details": type(exc).__name__})
            return

        # 依据不足：不调 LLM、不落 assistant 消息，直接终止流（方案 4.1）
        if not retriever.has_evidence(results, req.question):
            yield sse_frame("error", {"code": "NO_EVIDENCE", "message": NO_EVIDENCE_MESSAGE,
                                      "details": None})
            return

        prompt = build_qa_prompt(req.question, results, history)
        system = system_text          # 来自 prompt_templates 快照（含代码追加的硬约束）
        citations = knowledge_citations(results, cfg.citation_excerpt_len)
        context_text = "\n".join(r.chunk.content for r in results)
    else:
        # anomaly 模式（API 层已保证 req.anomaly_id 有效）
        yield _status("loading_event")
        async with session_factory() as db:
            async with db.begin():
                ev_row = await repo.get_anomaly_event(db, req.anomaly_id or "")
        if ev_row is None:  # 理论上到不了这里（前置校验过），双保险
            yield sse_frame("error", {"code": "ANOMALY_NOT_FOUND",
                                      "message": "异常事件不存在或已被删除", "details": None})
            return

        # 检索 query 用事件结构化摘要而不是用户原话（阶段3实测结论，方案 1.2 第 5 条）
        metrics = ev_row.rule_result or {}
        if ev_row.rule_code == "R04_RATIO_CHANGE":
            query = "电表倍率变化如何处理 倍率配置变更流程 生效时间"
        else:
            query = "总表与分表数据不一致的原因 平衡差核对规则 损耗 允许偏差"

        yield _status("retrieving")
        try:
            results = await anyio.to_thread.run_sync(
                lambda: retriever.retrieve(query, top_k=cfg.top_k))
        except Exception as exc:  # noqa: BLE001
            logger.exception("检索失败")
            yield sse_frame("error", {"code": "RETRIEVE_FAILED", "message": "知识检索失败，请重试",
                                      "details": type(exc).__name__})
            return

        yield _status("analyzing")
        event: AnomalyEvent = repo.row_to_event(ev_row)
        prompt = build_anomaly_prompt(event, results, history)
        system = system_text          # 同上
        # 数值溯源的"允许数值集合"= 证据 JSON + 知识片段
        from app.core.prompts import build_evidence_payload
        evidence_text = json.dumps(build_evidence_payload(event, results),
                                   ensure_ascii=False, default=str)
        context_text = "\n".join(r.chunk.content for r in results)
        citations = (anomaly_data_citations(ev_row)
                     + knowledge_citations(results, cfg.citation_excerpt_len))

    # ---------- Phase 3：assistant 消息先行落库（generating）----------
    yield _status("generating")
    async with session_factory() as db:
        async with db.begin():
            msg = await repo.add_message(db, session_id, "assistant", "", req.mode,
                                         status="generating",
                                         prompt_version_id=prompt_version_id,
                                         anomaly_id=(req.anomaly_id
                                                     if req.mode == "anomaly" else None))
            message_id = msg.id

    # ---------- Phase 4：LLM 流式生成 ----------
    pieces: list[str] = []
    # 无 Key / 显式 mock 时本来就是离线模式（C2 角标）；真实模式失败时中途改标
    llm_mode = "llm" if (cfg.llm_provider != "mock" and cfg.llm_api_key) else "mock_offline"
    try:
        async for piece in call_llm_stream(prompt, cfg, system=system, should_stop=_stopped):
            pieces.append(piece)
            yield sse_frame("token", {"delta": piece})
    except asyncio.CancelledError:
        # 客户端断连时 Starlette 会直接取消流生成器任务（不等 is_disconnected 轮询），
        # 且取消作用域内的 DB 写操作也会被连带取消（实测 aiosqlite 连接会被弄坏）。
        # 因此：把已收到的部分内容交给**独立后台任务**落库为 interrupted，
        # 该任务不在被取消的作用域内，可以安全完成；然后重新抛出取消信号。
        # 若后台任务也失败（极端情况），启动自愈会把 generating 改标 failed 兜底。
        content = "".join(pieces)
        task = asyncio.get_running_loop().create_task(_persist_interrupted(message_id, content))
        _PERSIST_TASKS.add(task)
        task.add_done_callback(_PERSIST_TASKS.discard)
        raise
    except LLMError as exc:
        if not pieces and cfg.llm_fallback_mock:
            # 首个请求就失败（尚未输出任何内容）→ 降级 mock 继续流（C2 默认）
            logger.warning("LLM 调用失败，降级为离线模拟输出：%s", exc)
            llm_mode = "mock_offline"
            from app.core.llm import _mock_llm
            fallback_prompt = prompt
            text = _mock_llm(fallback_prompt)
            step = 30
            for i in range(0, len(text), step):
                if await _stopped():
                    break
                piece = text[i:i + step]
                pieces.append(piece)
                yield sse_frame("token", {"delta": piece})
        else:
            # 中途失败（已有部分内容）或未启用降级 → error 事件 + failed 落库
            logger.warning("LLM 流式生成失败：%s", exc)
            content = "".join(pieces)
            async with session_factory() as db:
                async with db.begin():
                    await repo.finish_message(
                        db, message_id, "failed", content=content,
                        error_info=json.dumps({"code": "LLM_ERROR", "message": str(exc)[:300]},
                                              ensure_ascii=False))
            yield sse_frame("error", {"code": "LLM_ERROR",
                                      "message": f"回答生成失败：{exc}", "details": None})
            return

    content = "".join(pieces)

    # ---------- Phase 5：终态判定 ----------
    if await _stopped():
        # 用户停止 / 断线 → 已累积内容入库标 interrupted（方案 4.4）
        # 不发 done、不发 error——前端 abort 侧自行标注"已停止"
        async with session_factory() as db:
            async with db.begin():
                await repo.finish_message(db, message_id, "interrupted", content=content)
        return

    if not content:
        # 真实 LLM 正常返回但内容为空 → 按 failed 处理，不误标 complete
        async with session_factory() as db:
            async with db.begin():
                await repo.finish_message(
                    db, message_id, "failed", content="",
                    error_info=json.dumps(
                        {"code": "LLM_EMPTY", "message": "模型返回内容为空"},
                        ensure_ascii=False))
        yield sse_frame("error", {"code": "LLM_EMPTY",
                                  "message": "模型返回内容为空，请重试", "details": None})
        return

    # ---------- Phase 6：数值溯源（仅 anomaly 模式）----------
    if req.mode == "anomaly":
        guardrail = await anyio.to_thread.run_sync(
            lambda: number_trace_guard(content, evidence_text, context_text))

    # ---------- Phase 7：引用 + 终态落库 + done ----------
    yield sse_frame("citations", {"items": citations})

    async with session_factory() as db:
        async with db.begin():
            await repo.finish_message(
                db, message_id, "complete", content=content,
                guardrail_passed=(guardrail["passed"] if req.mode == "anomaly" else None))
            await repo.save_citations(db, message_id, cfg.citation_excerpt_len, citations)
            # 阶段5第四批：AI 分析完成 → 事件由 pending_analysis 推进到 pending_review
            # 只做这一个方向的推进，不写任何结论（最终结论属业务签核权）
            if req.mode == "anomaly" and req.anomaly_id:
                await repo.mark_anomaly_analyzed(db, req.anomaly_id)

    yield sse_frame("done", {
        "message_id": message_id,
        "status": "complete",
        "guardrail": guardrail if req.mode == "anomaly" else None,
        "llm_mode": llm_mode,
        "prompt_version_id": prompt_version_id,
    })
