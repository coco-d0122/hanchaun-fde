/* ================================================================
 * 瀚川工业园能耗异常定位智能体 · 前端逻辑（原生 JS，无框架）
 *
 * 核心机制：
 *  1. POST /api/chat 用 fetch + ReadableStream 手工分帧解析 SSE
 *     （不用 EventSource：它只支持 GET）
 *  2. 状态条文案由后端 status 事件驱动，与真实执行阶段一一对应
 *  3. 停止生成 = abortController.abort()；后端把已生成内容标 interrupted
 *  4. 引用卡片只渲染服务端 citations 事件里的内容（不经模型转手）
 *  5. AI 回答渲染前先 HTML 转义，再套简易 Markdown（防注入）
 * ================================================================ */
"use strict";

/* ---------------- DOM 引用 ---------------- */
const $ = (id) => document.getElementById(id);
const els = {
  banner: $("net-banner"),
  sidebar: $("sidebar"),
  sidebarToggle: $("sidebar-toggle"),
  btnNew: $("btn-new"),
  sessionList: $("session-list"),
  chatTitle: $("chat-title"),
  modeTag: $("mode-tag"),
  messages: $("messages"),
  emptyHint: $("empty-hint"),
  input: $("input"),
  btnSend: $("btn-send"),
  btnStop: $("btn-stop"),
  charCount: $("char-count"),
  anomalyMask: $("anomaly-mask"),
  anomalyPanel: $("anomaly-panel"),
  apClose: $("ap-close"),
  apList: $("ap-list"),
  btnAnomalyPanel: $("btn-anomaly-panel"),
};

/* ---------------- 全局状态 ---------------- */
const state = {
  sessionId: null,        // 当前会话
  generating: false,      // 是否正在生成
  abortCtrl: null,        // 当前请求的 AbortController
  lastAnomalyId: null,    // 异常面板选中事件（AI 分析按钮填入）
  sessions: [],
};

/* ================================================================
 * 工具函数
 * ================================================================ */

/** HTML 转义：渲染 AI 输出前的第一步，防止 LLM 输出注入脚本 */
function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** 简易 Markdown 渲染（标题/加粗/列表/代码块/表格）。先转义再格式化。 */
function renderMarkdown(text) {
  const safe = escapeHtml(text);
  const lines = safe.split("\n");
  const out = [];
  let inCode = false;
  let inList = null; // "ul" | "ol"
  let tableBuf = [];

  const closeList = () => {
    if (inList) { out.push(`</${inList}>`); inList = null; }
  };
  const flushTable = () => {
    if (tableBuf.length) {
      const rows = tableBuf.map((r) => r.map((c) => `<td>${c}</td>`).join(""));
      out.push(`<table><tbody>${rows.map((r) => `<tr>${r}</tr>`).join("")}</tbody></table>`);
      tableBuf = [];
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();

    // ---- 代码块 ----
    if (line.trim().startsWith("```")) {
      flushTable(); closeList();
      if (inCode) { out.push("</code></pre>"); inCode = false; }
      else { out.push("<pre><code>"); inCode = true; }
      continue;
    }
    if (inCode) { out.push(rawLine); continue; }

    // ---- 表格行（| a | b |） ----
    if (/^\|.*\|$/.test(line.trim())) {
      closeList();
      const cells = line.trim().slice(1, -1).split("|").map((c) => c.trim());
      // 跳过分隔行 |---|---|
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      tableBuf.push(cells);
      continue;
    }
    flushTable();

    // ---- 标题 ----
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }

    // ---- 列表 ----
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.、]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (inList !== want) { closeList(); out.push(`<${want}>`); inList = want; }
      out.push(`<li>${inline(ul ? ul[1] : ol[1])}</li>`);
      continue;
    }
    closeList();

    // ---- 空行 / 普通段落 ----
    if (!line.trim()) { out.push(""); continue; }
    // 【xxx】开头的段落渲染为小节标题（AI 分析报告的固定格式）
    if (/^【.+?】/.test(line.trim())) {
      out.push(`<p><strong>${line.trim()}</strong></p>`);
      continue;
    }
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); flushTable();
  if (inCode) out.push("</code></pre>");
  return out.join("\n");
}

/** 行内格式：加粗 / 行内代码 */
function inline(s) {
  return String(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function showBanner(show) {
  els.banner.classList.toggle("hidden", !show);
  if (show) setTimeout(() => els.banner.classList.add("hidden"), 5000);
}

function scrollBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

/* ================================================================
 * 消息渲染
 * ================================================================ */

function buildMsgEl(role) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  wrap.appendChild(bubble);
  const time = document.createElement("div");
  time.className = "msg-time";
  wrap.appendChild(time);
  els.messages.appendChild(wrap);
  els.emptyHint && els.emptyHint.remove();
  return { wrap, bubble, time };
}

/** 状态条（status 事件驱动） */
function setStatusBubble(wrap, text) {
  let line = wrap.querySelector(".status-line");
  if (!line) {
    line = document.createElement("div");
    line.className = "status-line";
    wrap.insertBefore(line, wrap.querySelector(".bubble"));
  }
  line.textContent = text;
}

function clearStatusBubble(wrap) {
  const line = wrap.querySelector(".status-line");
  if (line) line.remove();
}

/** 引用卡片（区分 📄 知识库依据 与 ⚡ 数据证据；可展开收起） */
function renderCitations(wrap, items) {
  const box = document.createElement("div");
  box.className = "citations";

  const title = document.createElement("div");
  title.className = "citations-title";
  title.textContent = `参考依据（${items.length}）▾ 点击展开/收起`;
  box.appendChild(title);

  const list = document.createElement("div");
  list.className = "citation-list hidden";
  title.addEventListener("click", () => {
    list.classList.toggle("hidden");
    title.textContent = title.textContent.replace("▾", "▾").replace("▴", "▾");
    if (!list.classList.contains("hidden")) {
      title.textContent = title.textContent.replace("▾", "▴");
    }
  });

  for (const c of items || []) {
    const item = document.createElement("div");
    item.className = `citation-item ${c.source_type} collapsed`;
    const isData = c.source_type === "anomaly_data";
    const head = document.createElement("div");
    head.className = "citation-head";
    head.innerHTML =
      `<span>${isData ? "⚡" : "📄"}</span>` +
      `<span class="ci-file">${escapeHtml(c.source_file)}</span>` +
      `<span class="ci-type">${isData ? "数据证据" : "知识库依据"}</span>`;
    const excerpt = document.createElement("div");
    excerpt.className = "citation-excerpt";
    excerpt.textContent = c.excerpt || "";
    item.appendChild(head);
    item.appendChild(excerpt);
    head.addEventListener("click", () => item.classList.toggle("collapsed"));
    list.appendChild(item);
  }
  box.appendChild(list);
  wrap.appendChild(box);
  return box;
}

/** 消息状态标注（中断 / 失败，与后端 messages.status 严格一致） */
function renderMsgFlag(wrap, kind, reason) {
  const flag = document.createElement("div");
  flag.className = `msg-flag ${kind}`;
  flag.textContent = kind === "interrupted" ? "已停止，内容不完整" : `生成失败：${reason || "未知原因"}`;
  wrap.appendChild(flag);
}

function renderBadge(wrap, cls, text) {
  const b = document.createElement("span");
  b.className = `badge ${cls}`;
  b.textContent = text;
  wrap.appendChild(b);
}

/* ================================================================
 * 会话列表
 * ================================================================ */

async function loadSessions() {
  try {
    const resp = await fetch("/api/sessions");
    if (resp.status === 401) { location.href = "/login"; return; }
    const data = await resp.json();
    state.sessions = data.items || [];
    els.sessionList.innerHTML = "";
    for (const s of state.sessions) {
      const item = document.createElement("div");
      item.className = "session-item" + (s.session_id === state.sessionId ? " active" : "");
      const t = document.createElement("div");
      t.className = "session-title";
      t.textContent = s.title || "新会话";
      const meta = document.createElement("div");
      meta.className = "session-meta";
      const upd = new Date(s.updated_at);
      const today = new Date();
      const same = upd.toDateString() === today.toDateString();
      meta.innerHTML =
        `<span>${same ? fmtTime(s.updated_at) : `${upd.getMonth() + 1}-${upd.getDate()}`}</span>` +
        `<span class="${s.mode === "anomaly" ? "m-mode-anomaly" : ""}">` +
        `${s.mode === "anomaly" ? "异常分析" : "知识问答"}</span>` +
        `<span>${s.message_count} 条</span>`;
      item.appendChild(t);
      item.appendChild(meta);
      item.addEventListener("click", () => openSession(s.session_id));
      els.sessionList.appendChild(item);
    }
  } catch (e) {
    showBanner(true);
  }
}

/** 打开历史会话：加载消息并渲染（含引用与状态标注） */
async function openSession(sessionId) {
  if (state.generating) return;
  try {
    const resp = await fetch(`/api/sessions/${sessionId}/messages`);
    if (!resp.ok) { alert("会话不存在或已过期"); return; }
    const data = await resp.json();
    state.sessionId = sessionId;
    els.messages.innerHTML = "";
    let lastMode = "knowledge";
    for (const m of data.items) {
      const { wrap, bubble, time } = buildMsgEl(m.role);
      time.textContent = fmtTime(m.created_at);
      if (m.role === "user") {
        bubble.textContent = m.content;
      } else {
        bubble.innerHTML = renderMarkdown(m.content);
        lastMode = m.mode || lastMode;
        if (m.status === "interrupted") renderMsgFlag(wrap, "interrupted");
        if (m.status === "failed") renderMsgFlag(wrap, "failed", "生成中断");
        if (m.guardrail_passed === false) renderBadge(wrap, "guard-bad", "数值溯源未通过");
        else if (m.guardrail_passed === true) renderBadge(wrap, "guard-ok", "数值溯源通过");
        if (m.citations && m.citations.length) renderCitations(wrap, m.citations);
        if (m.mode === "anomaly") {
          const note = document.createElement("div");
          note.className = "review-note";
          note.textContent = "⚠ AI 分析草稿，未经人工复核不构成业务结论";
          wrap.appendChild(note);
        }
      }
    }
    els.chatTitle.textContent = (state.sessions.find((s) => s.session_id === sessionId) || {}).title || "历史会话";
    setModeTag(lastMode);
    if (window.innerWidth <= 768) els.sidebar.classList.remove("open");
    scrollBottom();
    loadSessions();
  } catch (e) {
    showBanner(true);
  }
}

function setModeTag(mode) {
  els.modeTag.textContent = mode === "anomaly" ? "● 异常分析"
    : mode === "agent" ? "● Agent 深度分析" : "● 知识问答";
  const radios = document.querySelectorAll('input[name="mode"]');
  radios.forEach((r) => { r.checked = r.value === mode; });
}

function newSession() {
  if (state.generating) return;
  state.sessionId = null;
  state.lastAnomalyId = null;
  els.messages.innerHTML = "";
  els.messages.appendChild(els.emptyHint = buildEmptyHint());
  els.chatTitle.textContent = "新会话";
  setModeTag("knowledge");
  els.input.focus();
}

function buildEmptyHint() {
  const div = document.createElement("div");
  div.className = "empty-hint";
  div.id = "empty-hint";
  div.innerHTML =
    '<div class="eh-title">向智能体提问</div>' +
    '<div class="eh-item">📄 能源管理知识问答：制度、规范、排查流程</div>' +
    '<div class="eh-item">⚡ 能耗异常分析：选择左侧异常事件，或直接问"为什么 M-002 用电量突然增加？"</div>';
  return div;
}

/* ================================================================
 * 发送与 SSE 流解析（核心）
 * ================================================================ */

async function sendQuestion() {
  if (state.generating) return;
  const question = els.input.value.trim();
  if (!question) return;
  const mode = document.querySelector('input[name="mode"]:checked').value;

  // 阶段6：Agent 通道走独立 SSE 端点（7 工具 + 五道闸），不走 /api/chat
  if (mode === "agent") {
    els.input.value = "";
    updateCharCount();
    await sendAgentQuestion(question);
    return;
  }

  els.input.value = "";
  updateCharCount();
  showBanner(false);

  // 用户消息立即上屏
  const userEl = buildMsgEl("user");
  userEl.bubble.textContent = question;
  userEl.time.textContent = fmtTime(new Date().toISOString());

  // AI 消息骨架 + 状态条
  const aiEl = buildMsgEl("assistant");
  setStatusBubble(aiEl.wrap, "正在理解问题……");

  state.generating = true;
  setBusy(true);
  state.abortCtrl = new AbortController();

  let streamOk = false;
  let gotDone = false;
  let citations = [];
  let aborted = false;
  let httpError = null;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",   // CSRF 校验头（写操作必带）
      },
      body: JSON.stringify({
        session_id: state.sessionId,
        question,
        mode,
        anomaly_id: mode === "anomaly" ? (state.lastAnomalyId || null) : null,
      }),
      signal: state.abortCtrl.signal,
    });

    if (resp.status === 401) { location.href = "/login"; return; }

    if (!resp.ok) {
      // 建流之前的错误：统一 JSON 错误体
      const err = await resp.json().catch(() => ({ message: `HTTP ${resp.status}` }));
      httpError = err;
      throw new Error(err.message || `HTTP ${resp.status}`);
    }

    streamOk = true;
    const sessionId = await readSseStream(resp, aiEl, (ev, data) => {
      switch (ev) {
        case "session":
          if (data.session_id) state.sessionId = data.session_id;
          if (data.created) els.chatTitle.textContent = question.slice(0, 30);
          break;
        case "status":
          setStatusBubble(aiEl.wrap, data.text);
          break;
        case "token":
          clearStatusBubble(aiEl.wrap);
          aiEl.bubble.innerHTML += renderMarkdown(data.delta)
            .replace(/<p>(.*)<\/p>/, "$1");  // 行内追加不强制分段
          scrollBottom();
          break;
        case "citations":
          citations = data.items || [];
          break;
        case "done":
          gotDone = true;
          clearStatusBubble(aiEl.wrap);
          finishAssistant(aiEl, mode, citations, data);
          break;
        case "error":
          clearStatusBubble(aiEl.wrap);
          renderMsgFlag(aiEl.wrap, "failed", data.message);
          if (data.code === "NO_EVIDENCE") {
            aiEl.bubble.innerHTML = `<p class="no-evidence">${escapeHtml(data.message)}</p>`;
          }
          break;
      }
    });
  } catch (e) {
    if (e.name === "AbortError") {
      aborted = true;
      clearStatusBubble(aiEl.wrap);
      // 用户主动停止：后端把已收到内容标 interrupted；前端同样标注
      renderMsgFlag(aiEl.wrap, "interrupted");
    } else {
      clearStatusBubble(aiEl.wrap);
      if (!streamOk) {
        // HTTP 错误（建流前）：展示统一中文错误
        renderMsgFlag(aiEl.wrap, "failed", httpError ? httpError.message : "网络异常");
        showBanner(true);
      } else {
        renderMsgFlag(aiEl.wrap, "failed", "连接中断");
      }
    }
  } finally {
    state.generating = false;
    setBusy(false);
    state.abortCtrl = null;
    if (!gotDone && !aborted && aiEl.bubble.innerHTML === "" && !streamOk) {
      // 请求彻底失败时移除空骨架，避免留一个空气泡
      aiEl.wrap.remove();
    }
    if (!gotDone) {
      // 收尾：中断/失败也保留已生成内容与标注
    }
    loadSessions();
    scrollBottom();
  }
}

/**
 * 手工分帧解析 SSE：fetch + ReadableStream（EventSource 只支持 GET，用不了）
 * 返回处理过程中拿到的 session_id（若有）
 */
async function readSseStream(resp, aiEl, handler) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let currentEvent = null;
  let sessionId = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith("data: ")) {
          let data = null;
          try { data = JSON.parse(line.slice(6)); } catch (e) { data = {}; }
          if (currentEvent === "session" && data.session_id) sessionId = data.session_id;
          handler(currentEvent, data);
        }
      }
    }
  }
  return sessionId;
}

/* ================================================================
 * 阶段6：Agent 通道（/api/agent/analyze）+ 工具轨迹可视化
 * 协议扩展：task / status(tool_calling|gate_blocked|fallback) / tool_result /
 *           token / citations / done
 * 轨迹卡片只是"可解释性展示"，不承担安全职责：门禁在服务端执行器里。
 * ================================================================ */

/** 建一个可折叠的 Agent 工具轨迹卡片，插在回答气泡之前 */
function buildAgentTrace(wrap) {
  const box = document.createElement("details");
  box.className = "agent-trace";
  box.open = true;                       // 生成中默认展开，用户可随时收起
  const head = document.createElement("summary");
  head.className = "trace-head";
  head.innerHTML = '<span class="trace-robot">🤖</span> '
    + '<span class="trace-title">Agent 工具轨迹</span> '
    + '<span class="trace-count">0 步</span>';
  const body = document.createElement("div");
  body.className = "trace-steps";
  box.appendChild(head);
  box.appendChild(body);
  wrap.insertBefore(box, wrap.querySelector(".bubble"));
  return { box, head, body };
}

function setTraceMeta(trace, taskId, finalText) {
  const title = trace.head.querySelector(".trace-title");
  if (taskId) title.textContent = `Agent 工具轨迹 · ${taskId}`;
  const count = trace.head.querySelector(".trace-count");
  const steps = trace.body.querySelectorAll(".trace-step").length;
  count.textContent = finalText || `${steps} 步`;
}

function appendTraceStep(trace, d) {
  const row = document.createElement("div");
  row.className = "trace-step" + (d.ok ? " ok" : " fail");
  const icon = d.ok ? "✓" : "⛔";
  const err = d.error_code ? `<span class="trace-code">${escapeHtml(d.error_code)}</span>` : "";
  const ms = d.duration_ms != null ? `<span class="trace-ms">${d.duration_ms}ms</span>` : "";
  const cached = d.cached ? '<span class="trace-code">缓存命中</span>' : "";
  row.innerHTML = `<span class="trace-icon">${icon}</span>`
    + `<span class="trace-tool">${escapeHtml(d.tool || "")}</span>`
    + `<span class="trace-summary">${escapeHtml(d.summary || "")}</span>`
    + err + cached + ms;
  trace.body.appendChild(row);
  trace.body.scrollTop = trace.body.scrollHeight;
}

function appendTraceNote(trace, text, kind) {
  const row = document.createElement("div");
  row.className = "trace-step note" + (kind ? ` ${kind}` : "");
  row.textContent = text;
  trace.body.appendChild(row);
}

/** 发送 Agent 深度分析（独立 SSE 流，保留 /api/chat 原有协议不变） */
async function sendAgentQuestion(question, anomalyIdOverride) {
  if (state.generating) return;
  const anomalyId = anomalyIdOverride || state.lastAnomalyId || null;

  const userEl = buildMsgEl("user");
  userEl.bubble.textContent = question;
  userEl.time.textContent = fmtTime(new Date().toISOString());

  const aiEl = buildMsgEl("assistant");
  setStatusBubble(aiEl.wrap, "正在规划分析步骤……");
  const trace = buildAgentTrace(aiEl.wrap);

  state.generating = true;
  setBusy(true);
  state.abortCtrl = new AbortController();

  let citations = [];
  let gotDone = false;
  let streamOk = false;
  let httpError = null;
  let taskId = null;

  try {
    const resp = await fetch("/api/agent/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({
        session_id: state.sessionId,
        question,
        anomaly_id: anomalyId,
        require_confirmation: true,   // 报告草稿流式下发后暂停，等待用户明确确认
      }),
      signal: state.abortCtrl.signal,
    });
    if (resp.status === 401) { location.href = "/login"; return; }
    if (!resp.ok) {
      httpError = await resp.json().catch(() => ({ message: `HTTP ${resp.status}` }));
      throw new Error(httpError.message || `HTTP ${resp.status}`);
    }
    streamOk = true;
    await readSseStream(resp, aiEl, (ev, data) => {
      switch (ev) {
        case "session":
          if (data.session_id) state.sessionId = data.session_id;
          if (data.created) els.chatTitle.textContent = question.slice(0, 30);
          break;
        case "task":
          taskId = data.task_id;
          setTraceMeta(trace, taskId);
          appendTraceNote(trace, "任务账本已建立，工具调用将逐步记录", "sys");
          break;
        case "status":
          setStatusBubble(aiEl.wrap, data.text || "正在执行…");
          if (data.stage === "gate_blocked") appendTraceNote(trace, data.text, "warn");
          if (data.stage === "fallback") appendTraceNote(trace, data.text, "sys");
          break;
        case "tool_result":
          appendTraceStep(trace, data);
          setTraceMeta(trace, taskId);
          break;
        case "token":
          clearStatusBubble(aiEl.wrap);
          aiEl.bubble.innerHTML += renderMarkdown(data.delta)
            .replace(/<p>(.*)<\/p>/, "$1");
          scrollBottom();
          break;
        case "citations":
          citations = data.items || [];
          break;
        case "done":
          gotDone = true;
          clearStatusBubble(aiEl.wrap);
          setTraceMeta(trace, taskId, `完成 · ${data.tool_calls || 0} 步`);
          trace.box.open = false;        // 生成完毕自动收起，避免遮挡回答
          finishAssistant(aiEl, "anomaly", citations, data);
          if (data.llm_mode === "offline_scripted") {
            renderBadge(aiEl.wrap, "mock", "确定性剧本执行（未调用真实大模型）");
          }
          renderBadge(aiEl.wrap,
            data.validation_status === "passed" ? "guard-ok" : "guard-bad",
            `数据校验：${data.validation_status === "passed" ? "通过" : "发现问题，检测已拦截"}`);
          if (data.review_status === "pending") {
            renderBadge(aiEl.wrap, "guard-bad", "已提交人工复核（本次为 AI 草稿）");
          }
          if (data.report_status) {
            renderBadge(aiEl.wrap, "mock", `报告草稿：${data.report_status}`);
          }
          break;
        case "draft_ready":
          renderDraftCard(aiEl.wrap, data);
          break;
        case "error":
          clearStatusBubble(aiEl.wrap);
          renderMsgFlag(aiEl.wrap, "failed", data.message);
          break;
      }
    });
  } catch (e) {
    if (e.name === "AbortError") {
      clearStatusBubble(aiEl.wrap);
      renderMsgFlag(aiEl.wrap, "interrupted");
    } else {
      clearStatusBubble(aiEl.wrap);
      renderMsgFlag(aiEl.wrap, "failed",
        httpError ? httpError.message : (streamOk ? "连接中断" : "网络异常"));
      if (!streamOk) showBanner(true);
    }
  } finally {
    state.generating = false;
    setBusy(false);
    state.abortCtrl = null;
    if (!gotDone && aiEl.bubble.innerHTML === "") aiEl.wrap.remove();
    loadSessions();
    scrollBottom();
  }
}

/** 阶段7：报告草稿人工确认卡片（确认≠签核；取消时强制复核风险独立保留） */
function renderDraftCard(wrap, d) {
  const box = document.createElement("div");
  box.className = "draft-card";
  const key = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID() : `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  box.innerHTML =
    `<div class="draft-title">📝 异常分析报告草稿（等待人工确认）</div>` +
    `<div class="draft-meta">草稿 ${escapeHtml(d.draft_id)} · 版本 v${d.report_version} · `
    + `证据 ${d.evidence_count} 条${d.force_review ? ' · <b>存在强制复核风险</b>' : ''}</div>` +
    `<div class="draft-note">${escapeHtml(d.note || '')}</div>` +
    `<div class="draft-actions">` +
    `<button class="btn-draft confirm">确认提交人工复核</button>` +
    `<button class="btn-draft cancel">取消</button>` +
    `</div><div class="draft-result"></div>`;

  const confirmBtn = box.querySelector(".confirm");
  const cancelBtn = box.querySelector(".cancel");
  const result = box.querySelector(".draft-result");

  async function decide(action) {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;      // 一次点击即锁定，防重复确认重复建单
    result.textContent = "正在提交…";
    const endpoint = `/api/agent/drafts/${encodeURIComponent(d.draft_id)}/${action}`;
    try {
      const resp = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify({ idempotency_key: key, report_version: d.report_version }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        result.className = "draft-result err";
        result.textContent = data.message || "操作失败";
        confirmBtn.disabled = false;
        cancelBtn.disabled = false;
        return;
      }
      result.className = "draft-result ok";
      result.textContent = data.message
        + (data.review_request_id ? `（复核请求：${data.review_request_id}）` : "");
      if (data.risk_retained) {
        result.textContent += `　⚠ 风险复核已独立保留：${data.risk_request_id}`;
      }
    } catch (e) {
      result.className = "draft-result err";
      result.textContent = "网络异常，请重试";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  }
  confirmBtn.addEventListener("click", () => decide("confirm"));
  cancelBtn.addEventListener("click", () => decide("cancel"));
  wrap.appendChild(box);
  scrollBottom();
}

/** done 事件收尾：定稿渲染（完整 Markdown + 引用卡片 + 角标） */
function finishAssistant(aiEl, mode, citations, doneData) {
  // 重新整体渲染（流式期间是行内追加，最终统一格式化一次）
  aiEl.bubble.innerHTML = renderMarkdown(aiEl.bubble.textContent || "");
  if (citations.length) renderCitations(aiEl.wrap, citations);
  if (mode === "anomaly" && doneData && doneData.guardrail) {
    renderBadge(aiEl.wrap, doneData.guardrail.passed ? "guard-ok" : "guard-bad",
      `数值溯源${doneData.guardrail.passed ? "通过" : "未通过"}` +
      `（${doneData.guardrail.checked} 个数字）`);
  }
  if (doneData && doneData.llm_mode === "mock_offline") {
    renderBadge(aiEl.wrap, "mock", "离线模拟模式（未调用真实大模型）");
  }
  if (mode === "anomaly") {
    const note = document.createElement("div");
    note.className = "review-note";
    note.textContent = "⚠ AI 分析草稿，未经人工复核不构成业务结论";
    aiEl.wrap.appendChild(note);
  }
}

/* ================================================================
 * 异常事件面板
 * ================================================================ */

async function openAnomalyPanel() {
  els.anomalyMask.classList.remove("hidden");
  els.anomalyPanel.classList.remove("hidden");
  if (!els.apList.dataset.loaded) {
    els.apList.innerHTML = '<div class="ap-loading">正在加载异常事件…</div>';
    try {
      const resp = await fetch("/api/anomalies");
      const data = await resp.json();
      els.apList.innerHTML = "";
      for (const a of data.items || []) {
        els.apList.appendChild(buildAnomalyItem(a));
      }
      els.apList.dataset.loaded = "1";
    } catch (e) {
      els.apList.innerHTML = '<div class="ap-loading">加载失败，请重试</div>';
    }
  }
}

function closeAnomalyPanel() {
  els.anomalyMask.classList.add("hidden");
  els.anomalyPanel.classList.add("hidden");
}

function buildAnomalyItem(a) {
  const item = document.createElement("div");
  item.className = "ap-item";
  const detected = new Date(a.detected_at);
  const p = (n) => String(n).padStart(2, "0");
  const ts = `${detected.getMonth() + 1}-${p(detected.getDate())} ${p(detected.getHours())}:${p(detected.getMinutes())}`;
  item.innerHTML =
    `<div class="ap-line1">` +
    `<span class="ap-id">${escapeHtml(a.anomaly_id)}</span>` +
    `<span class="ap-sev ${a.severity}">${a.severity === "high" ? "高" : a.severity === "medium" ? "中" : "低"}</span>` +
    `</div>` +
    `<div class="ap-type">${escapeHtml(a.anomaly_type)}</div>` +
    `<div class="ap-meta">设备 ${escapeHtml(a.meter_id)} · 楼栋 ${escapeHtml(a.building_id || "-")} · ${ts}<br>` +
    `${escapeHtml((a.summary || "").slice(0, 80))}…</div>`;
  const btn = document.createElement("button");
  btn.className = "btn-ai";
  btn.textContent = "AI 分析";
  btn.addEventListener("click", () => {
    closeAnomalyPanel();
    state.lastAnomalyId = a.anomaly_id;
    setModeTag("anomaly");
    els.input.value = `请分析异常事件 ${a.anomaly_id}（${a.anomaly_type}）：给出可能原因、知识依据与建议核查事项。`;
    updateCharCount();
    els.input.focus();
  });
  item.appendChild(btn);

  // 阶段6：异常面板直达 Agent 通道（工具调用 + 门禁 + 轨迹可视化）
  const agentBtn = document.createElement("button");
  agentBtn.className = "btn-ai agent";
  agentBtn.textContent = "Agent 深度分析";
  agentBtn.title = "进入 Agent 循环：取数 → 查配置 → 校验 → 检索 → 报告草稿 → 人工复核";
  agentBtn.addEventListener("click", () => {
    if (state.generating) return;
    closeAnomalyPanel();
    state.lastAnomalyId = a.anomaly_id;
    setModeTag("agent");
    const q = `请分析异常事件 ${a.anomaly_id}（${a.anomaly_type}）：`
      + "给出可能原因、知识依据与建议核查事项。";
    sendAgentQuestion(q, a.anomaly_id);
  });
  item.appendChild(agentBtn);
  return item;
}

/* ================================================================
 * UI 状态与事件绑定
 * ================================================================ */

function setBusy(busy) {
  els.btnSend.classList.toggle("hidden", busy);
  els.btnStop.classList.toggle("hidden", !busy);
  els.btnSend.disabled = busy;
  els.btnNew.disabled = busy;
}

function updateCharCount() {
  els.charCount.textContent = `${els.input.value.length}/500`;
}

function bindEvents() {
  els.btnSend.addEventListener("click", sendQuestion);
  els.btnStop.addEventListener("click", () => {
    if (state.abortCtrl) state.abortCtrl.abort();
  });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuestion();
    }
  });
  els.input.addEventListener("input", updateCharCount);
  els.btnNew.addEventListener("click", newSession);
  els.sidebarToggle.addEventListener("click", () => els.sidebar.classList.toggle("open"));
  els.btnAnomalyPanel.addEventListener("click", openAnomalyPanel);
  els.apClose.addEventListener("click", closeAnomalyPanel);
  els.anomalyMask.addEventListener("click", closeAnomalyPanel);
  document.querySelectorAll(".quick-entry[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setModeTag(btn.dataset.mode);
      els.input.focus();
      if (window.innerWidth <= 768) els.sidebar.classList.remove("open");
    });
  });
}

/* ---------------- 启动 ---------------- */
bindEvents();
newSession();
loadSessions();
loadCurrentUser();

/** 加载当前登录用户：侧栏底部显示用户名与角色，提供退出入口 */
async function loadCurrentUser() {
  try {
    const resp = await fetch("/api/auth/me");
    if (resp.status === 401) { location.href = "/login"; return; }
    const me = await resp.json();
    const foot = document.querySelector(".side-foot");
    if (foot && me.username) {
      const roles = (me.roles || []).join("/") || "-";
      // 管理后台入口只对拥有知识库读权限的角色显示（无权限时页面会 403，不制造死链）
      const canAdmin = (me.permissions || []).includes("kb:read");
      const adminLink = canAdmin
        ? `<a href="/admin/kb" style="margin-left:8px;color:#1e6fff;">管理后台</a>`
        : "";
      foot.innerHTML =
        `<span>${escapeHtml(me.username)}（${escapeHtml(roles)}）</span>` +
        adminLink +
        `<a href="javascript:void(0)" id="btn-logout" style="margin-left:8px;color:#1e6fff;">退出</a>`;
      const logoutBtn = document.getElementById("btn-logout");
      logoutBtn && logoutBtn.addEventListener("click", async () => {
        try {
          await fetch("/api/auth/logout", {
            method: "POST",
            headers: { "X-Requested-With": "XMLHttpRequest" },
          });
        } catch (e) { /* 忽略网络错误，反正要跳转 */ }
        location.href = "/login";
      });
    }
  } catch (e) { /* 静默：不阻断聊天界面 */ }
}
