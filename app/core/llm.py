# -*- coding: utf-8 -*-
"""
大模型统一调用（阶段3迁移 + 阶段4新增流式变体）

改造点（阶段4方案 7.1）：
  - 保留同步 call_llm()：降级路径与健康探测用（阶段3已实测）
  - 新增 call_llm_stream()：httpx AsyncClient + stream=True，
    async generator 逐段 yield —— SSE 流式输出的数据源
  - mock 模式下把整段答案按 ~30 字切分逐段 yield（离线也能验证前端流式渲染）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from typing import AsyncIterator, Callable

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """大模型调用异常。所有上层业务只需捕获这一个类型。"""


# Prompt 中使用的结构化分隔符。用显眼标记而不是 Markdown 标题，
# 是为了让 mock provider 能可靠解析（真实模型也能理解这种结构）。
CTX_OPEN, CTX_CLOSE = "<<<CONTEXT>>>", "<<<END_CONTEXT>>>"
EV_OPEN, EV_CLOSE = "<<<EVIDENCE>>>", "<<<END_EVIDENCE>>>"


def _mock_llm(prompt: str) -> str:
    """
    离线模拟大模型（无 API Key 时使用）。

    ⚠️ 这不是"假装有大模型"，而是把"链路是否打通"与"模型是否可用"解耦：
       - 它严格只复述 Prompt 里已有的证据与检索片段，不引入任何新信息
       - 输出格式与真实模型完全一致，因此可以验证 Prompt 结构、解析逻辑、
         引用标注、以及"无依据时必须说不知道"的行为
       - 一旦配置了真实 API Key，这个分支永远不会被走到
    """
    banner = "【离线模拟输出｜未调用真实大模型，配置 LLM_API_KEY 后自动切换】\n"

    # ---- 分支 1：异常分析 ----
    if EV_OPEN in prompt:
        try:
            raw = prompt.split(EV_OPEN, 1)[1].split(EV_CLOSE, 1)[0]
            ev = json.loads(raw)
        except (IndexError, json.JSONDecodeError):
            return banner + "证据解析失败，无法生成分析。请人工核实。"

        etype = ev.get("异常类型", "未知")
        meter = ev.get("电表编号", "-")
        lines = [banner]
        lines.append("【异常现象】")
        lines.append(
            f"{ev.get('监测窗口', '')} 期间，电表 {meter}（所属楼栋 {ev.get('楼栋', '-')}）"
            f"触发「{etype}」规则。规则判定结果：{ev.get('规则判定', '')}"
        )
        lines.append("")
        lines.append("【可能原因】（以下为待核实假设，不是已确认事实）")
        for i, cause in enumerate(ev.get("候选原因", []), start=1):
            lines.append(f"{i}. {cause}  —— 可能性：待核实")
        lines.append("")
        lines.append("【相关知识依据】")
        refs = ev.get("知识引用", [])
        if refs:
            for i, ref in enumerate(refs, start=1):
                lines.append(f"[{i}] {ref}")
        else:
            lines.append("未检索到直接相关的知识条目，本条分析无知识库依据。")
        lines.append("")
        lines.append("【建议核查的数据或记录】")
        for item in ev.get("建议核查项", []):
            lines.append(f"- {item}")
        lines.append("")
        lines.append("【是否需要人工复核】")
        lines.append(f"需要。{ev.get('复核理由', '该结论依赖计量配置与现场情况，必须人工确认。')}")
        return "\n".join(lines)

    # ---- 分支 2：知识库问答 ----
    if CTX_OPEN in prompt:
        ctx = prompt.split(CTX_OPEN, 1)[1].split(CTX_CLOSE, 1)[0].strip()
        if not ctx or "未检索到" in ctx:
            return banner + (
                "知识库中没有检索到与该问题直接相关的内容，依据不足，无法给出结论。\n"
                "建议：补充相关管理制度文档，或转人工咨询能源经理。"
            )
        # 抽取每个上下文片段的来源与首句，拼成"带引用"的回答
        blocks = re.split(r"\n\[(\d+)\]\s", "\n" + ctx)
        pairs: list[tuple[str, str]] = []
        for i in range(1, len(blocks) - 1, 2):
            idx = blocks[i]
            body = blocks[i + 1].strip()
            head, _, rest = body.partition("\n")
            # head 形如 "来源：02_xxx.md / 标题 > 小节"，只留文件名，避免引用过长
            short = head.replace("来源：", "").strip().split("/")[-1].strip()
            text = (rest or head).strip().replace("\n", " ")
            sentence = text.split("。")[0][:110]
            pairs.append((f"[{idx}] {short}", sentence))

        lines = [banner, "根据园区能源管理知识库，可以按以下要点处理：", ""]
        for cite, snippet in pairs:
            lines.append(f"- {snippet}。 {cite}")
        lines.append("")
        lines.append("以上内容均来自知识库检索片段，具体执行请以现行有效版本制度为准。")
        return "\n".join(lines)

    return banner + "（模拟模式）已收到请求，但未识别的 Prompt 结构。"


def call_llm(
    prompt: str,
    cfg: Settings,
    model: str | None = None,
    provider: str | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    retries: int = 1,
) -> str:
    """
    大模型同步调用入口（降级路径 / 健康探测用；请求路径请用 call_llm_stream）。

    错误处理策略：
      - 超时、连接错误、5xx：指数退避重试
      - 401/403（鉴权失败）、400（参数错误）：立即失败，重试没有意义
      - 所有异常统一包装成 LLMError，调用方只需处理一种异常
    """
    provider = (provider or cfg.llm_provider or "openai").lower()
    model = model or cfg.llm_model
    temperature = cfg.llm_temperature if temperature is None else temperature
    max_tokens = cfg.llm_max_tokens if max_tokens is None else max_tokens
    timeout = timeout or cfg.llm_timeout

    if provider == "mock":
        return _mock_llm(prompt)

    if not cfg.llm_api_key:
        # 没有 Key 不报错终止，而是降级到模拟模式——保证系统在任何机器上可演示
        logger.warning("未配置 LLM_API_KEY，自动降级为离线模拟模式")
        return _mock_llm(prompt)

    import requests  # 同步客户端仅用于非请求路径
    url = f"{cfg.llm_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout as exc:
            last_exc = exc
            logger.warning("LLM 请求超时（第 %s 次，%ss）", attempt, timeout)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("LLM 网络异常（第 %s 次）：%s", attempt, exc)
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, ValueError) as exc:
                    raise LLMError(f"LLM 响应结构异常：{resp.text[:300]}") from exc
            if resp.status_code in (401, 403):
                raise LLMError(f"LLM 鉴权失败（{resp.status_code}），请检查 LLM_API_KEY")
            if resp.status_code == 404:
                raise LLMError(f"接口不存在（404）：{url}，请检查 LLM_BASE_URL 是否需要带 /v1")
            if 400 <= resp.status_code < 500:
                raise LLMError(f"LLM 请求被拒绝（{resp.status_code}）：{resp.text[:300]}")
            last_exc = LLMError(f"服务端错误 {resp.status_code}")
            logger.warning("LLM 服务端错误（第 %s 次）：%s", attempt, resp.status_code)

        if attempt <= retries:
            time.sleep(min(2 ** attempt, 4))     # 指数退避

    raise LLMError(f"LLM 调用最终失败：{last_exc}")


async def _check_stop(should_stop: Callable[[], object] | None) -> bool:
    """检查停止信号；兼容同步与异步（协程）的 should_stop 回调。

    Starlette 的 Request.is_disconnected() 在新版本是 async def，
    同步调用只会拿到协程对象（恒为真值），必须 await 才是真结果。
    """
    if should_stop is None:
        return False
    result = should_stop()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


async def call_llm_stream(
    prompt: str,
    cfg: Settings,
    system: str | None = None,
    should_stop: Callable[[], object] | None = None,
    chunk_size: int = 30,
) -> AsyncIterator[str]:
    """
    大模型流式调用（SSE token 事件的数据源）。

    - 真实模式：httpx.AsyncClient + stream=True，逐段解析 OpenAI 兼容 SSE 增量
    - mock / 无 Key：整段生成后按 ~30 字切分逐段 yield，模拟流式节奏
    - should_stop：每个片段之间检查一次（前端 abort / 断连时提前终止）
    - 鉴权失败等"重试无意义"错误直接抛 LLMError；网络类错误不在这里重试，
      由上层 chat_service 决定是否降级 mock（阶段4方案 C2）
    """
    provider = (cfg.llm_provider or "openai").lower()

    if provider == "mock" or not cfg.llm_api_key:
        text = _mock_llm(prompt)
        for i in range(0, len(text), chunk_size):
            if await _check_stop(should_stop):
                return
            yield text[i:i + chunk_size]
            await asyncio.sleep(0.03)   # 模拟流式节奏，便于前端观察逐字追加
        return

    url = f"{cfg.llm_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}", "Content-Type": "application/json"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": cfg.llm_model,
        "messages": messages,
        "temperature": cfg.llm_temperature,
        "max_tokens": cfg.llm_max_tokens,
        "stream": True,
    }

    try:
        timeout = httpx.Timeout(cfg.llm_timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if resp.status_code in (401, 403):
                        raise LLMError(f"LLM 鉴权失败（{resp.status_code}），请检查 LLM_API_KEY")
                    if resp.status_code == 404:
                        raise LLMError(f"接口不存在（404）：{url}，请检查 LLM_BASE_URL")
                    raise LLMError(f"LLM 服务返回 {resp.status_code}：{body[:300]}")

                # 解析 OpenAI 兼容 SSE：每帧 "data: {...}"，结尾为 "data: [DONE]"
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue   # 半截帧，留给下一个 chunk 拼接
                        delta = (obj.get("choices") or [{}])[0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            if await _check_stop(should_stop):
                                return
                            yield piece
    except httpx.TimeoutException as exc:
        raise LLMError(f"模型响应超时（{cfg.llm_timeout}s），请重试") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"模型网络异常：{exc}") from exc


async def call_llm_tools(
    messages: list[dict],
    tools: list[dict],
    cfg: Settings,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
) -> dict:
    """
    Function Calling 单轮调用（阶段6 Agent 循环用）。

    返回 {"content": str, "tool_calls": [{"id","name","arguments"(dict)}]}。
    无 Key / provider=mock 时抛 LLMError——由 Agent 循环回落到"剧本式确定性执行"，
    离线环境仍能完整演示工具链（这与阶段4 mock 降级是同一思路）。
    """
    provider = (cfg.llm_provider or "openai").lower()
    if provider == "mock" or not cfg.llm_api_key:
        raise LLMError("未配置可用大模型，Agent 使用剧本式确定性执行")

    url = f"{cfg.llm_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}",
               "Content-Type": "application/json"}
    payload = {
        "model": cfg.llm_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": cfg.llm_temperature if temperature is None else temperature,
        "max_tokens": cfg.llm_max_tokens if max_tokens is None else max_tokens,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout or cfg.llm_timeout) as client:
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Agent LLM 网络异常：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"Agent LLM 调用失败（{resp.status_code}）：{resp.text[:200]}")
    try:
        msg = resp.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Agent LLM 响应结构异常：{resp.text[:200]}") from exc

    calls: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            args = {"__raw__": raw_args}
        calls.append({"id": tc.get("id") or "", "name": fn.get("name") or "",
                      "arguments": args})
    return {"content": (msg.get("content") or "").strip(), "tool_calls": calls}
