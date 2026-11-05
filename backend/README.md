# 瀚川工业园 · Web 后端（阶段4 + 阶段5 + 阶段6 Agent + 阶段7 Harness）

基于阶段3原型核心模块（检索 / 规则引擎 / Guardrail 原样迁移）构建的 FastAPI + SQLite + SSE 系统：
登录鉴权与 RBAC（一批）、管理后台与知识库（二批）、提示词版本化（三批）、
对话与异常审计（四批）、操作日志与联调（五批）、Agent 工具调用（阶段6）。

## 运行

```bash
cd backend
python -m venv .venv            # 或复用仓库根目录的 .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# cp .env.example .env           # 可选：填入 LLM_API_KEY（留空 = 离线模拟模式）

# 第一步：创建账号（首次运行必须，幂等）
INIT_PASSWORD='你的密码' .venv/Scripts/python -m app.init_admin --username admin --role admin
INIT_PASSWORD='用户密码'  .venv/Scripts/python -m app.init_admin --username demo_user --role user
#   交互模式：不带 INIT_PASSWORD 时提示输入两次；--list 查看已有用户

# 第二步：启动
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>，未登录自动跳转 `/login` 登录页。

- 无 LLM_API_KEY 时自动进入**离线模拟模式**：全链路可跑通（检索、引用、状态事件、
  中断语义），回答由 mock 生成并带"离线模拟模式"角标；配置 Key 后自动切换真实模型。
- 首次启动构建 FAISS/BM25 索引（约 2 秒），之后命中 `.cache/` 秒级启动。
- 首次启动若 `anomaly_events` 表为空，自动对 `data/` 下两份模拟数据集跑阶段3检测链
  落库（3 条事件，确定性事件 ID，重复启动幂等）。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录（签发 JWT，HttpOnly Cookie，8h 有效） |
| POST | `/api/auth/logout` | 退出（清除 Cookie） |
| GET | `/api/auth/me` | 当前用户信息（含角色与权限码） |
| GET | `/api/users` | 用户列表（仅 `user:read`，即 admin） |
| POST | `/api/users` | 创建用户（仅 `user:write`） |
| PATCH | `/api/users/{id}` | 改角色 / 禁用（保护最后一个 admin） |
| POST | `/api/chat` | SSE 流式聊天（`mode=knowledge/anomaly`，可选 `anomaly_id`） |
| GET | `/api/sessions` | 会话列表（按更新时间倒序） |
| GET | `/api/sessions/{id}/messages` | 会话消息（含引用） |
| GET | `/api/anomalies` | 异常事件列表（`?status=` 过滤） |
| GET | `/api/anomalies/{id}` | 异常事件详情（含证据） |
| GET | `/api/kb/documents` | 知识库文档列表（`kb:read`） |
| GET | `/api/kb/documents/{id}` | 文档详情 + 版本历史（`kb:read`） |
| POST | `/api/kb/documents` | 上传文档（multipart：`file` + `doc_type`，`kb:write`） |
| DELETE | `/api/kb/documents/{id}` | 软删除文档（`kb:delete`，内建文档 403 保护） |
| POST | `/api/kb/documents/{id}/reprocess` | 重新处理（`kb:write`，内容未变时命中缓存秒级） |
| GET | `/api/prompts` | 提示词模板列表（`prompt:read`） |
| GET | `/api/prompts/{code}` | 当前生效模板（`code=qa/anomaly`，`prompt:read`） |
| PUT | `/api/prompts/{code}` | 保存编辑 → 新版本并立即生效（`prompt:write`） |
| GET | `/api/prompts/{code}/versions` | 版本历史（`prompt:read`） |
| POST | `/api/prompts/versions/{version_id}/restore` | 恢复历史版本（生成新版本号，`prompt:write`） |
| GET | `/api/audit/conversations` | 对话审计列表（六条件筛选，`audit:conversation:read`） |
| GET | `/api/audit/conversations/{message_id}` | 消息审计详情（引用/提示词版本/生成状态/关联异常） |
| GET | `/api/audit/flag-types` | 风险标记 7 类字典 |
| GET | `/api/audit/flags` | 风险标记列表 |
| POST | `/api/audit/flags` | 添加风险标记（`audit:flag:write` + CSRF） |
| PATCH | `/api/audit/flags/{id}` | 更新标记状态（待处理/已处理/已驳回） |
| GET | `/api/anomalies/{id}/reviews` | 人工复核记录（只读；本阶段恒为空） |
| POST | `/api/agent/analyze` | Agent 分析（SSE：task/status/tool_result/token/citations/done） |
| GET | `/api/agent/tasks` | Agent 任务列表（有审计权限看全库，否则只看本人） |
| GET | `/api/agent/logs` | Harness 执行日志（用户/时间/任务/工具/轮次/状态组合筛选） |
| POST | `/api/agent/drafts/{id}/confirm` | 确认提交人工复核（二次校验 + 幂等） |
| POST | `/api/agent/drafts/{id}/cancel` | 取消提交（强制复核风险独立保留） |
| GET | `/api/agent/drafts/{id}` | 草稿状态查询 |
| GET | `/api/agent/tasks/{id}` | Agent 任务账本 + 调用轨迹 |
| GET | `/api/health` | 健康检查（知识库 / LLM 状态） |
| GET | `/login` + `/` | 登录页 + 聊天页（未登录 302 跳登录） |
| GET | `/admin` → `/admin/dashboard` | 管理后台入口（工作台） |
| GET | `/admin/dashboard` | 工作台：概览卡片 + 最近 10 条操作日志 |
| GET | `/admin/users` | 用户管理（建号/改角色/禁用） |
| GET | `/admin/kb` | 知识库管理页 |
| GET | `/admin/prompts` | AI 提示词管理页 |
| GET | `/admin/audit` | AI 对话审计页 |
| GET | `/admin/anomalies` | 能耗异常事件审计页（只读） |
| GET | `/admin/agent-tasks` | Agent 任务账本页（状态机快照 + 工具调用轨迹） |
| GET | `/admin/agent-logs` | Harness 执行日志页（每轮决策 + 每次工具调用） |

**鉴权规则**：所有业务接口需登录（未登录 401 中文错误）；权限不足 403；
所有 POST/PATCH 写请求须带 `X-Requested-With: XMLHttpRequest` 头（CSRF 防线，
浏览器原生表单无法携带自定义头）。

SSE 事件协议：`session → status(×N，与真实执行阶段对应) → token(×N) → citations → done`；
建流前错误返回统一 JSON 错误体（`code/message/details`），建流后错误发 `error` 事件。
交互式文档：<http://127.0.0.1:8000/docs>

## 目录结构

```
app/
├─ main.py              # FastAPI 实例 + lifespan（建表/自愈/落事件/建索引/权限缓存）
├─ init_admin.py        # CLI 建号工具（bcrypt 哈希，幂等，可交互可环境变量）
├─ api/                 # auth / users / chat(SSE) / sessions / anomalies / kb / admin 路由
├─ api/deps.py          # get_current_user + require_permission + CSRF 校验
├─ core/security.py     # bcrypt 哈希 + JWT 签发校验（密钥从环境变量读）
├─ core/permissions.py  # 权限码常量与两角色默认授权表
├─ services/chat_service.py   # 双模式 SSE 编排（状态机式分步）
├─ services/kb_service.py     # 知识库上传流水线 / 影子重建原子切换 / 种子化
├─ services/prompt_service.py # 提示词快照读取（30s 缓存）/ 编辑 / 恢复 / 硬约束追加
├─ agent/specs.py       # 7 个工具的 Function Calling 声明 + 权限/前置条件/复核岗位映射
├─ agent/tools.py       # 7 个原子工具实现（只读 + 一个"只增不改"的复核请求）
├─ agent/executor.py    # 五道闸 + 防循环预算 + 同参缓存 + 调用留痕
├─ agent/state.py       # 任务状态机（门禁的依据，模型无写权）
├─ agent/loop.py        # 阶段6 剧本式编排（engine=script 回归对照）
├─ agent/schemas.py     # 8 工具 Pydantic 严格参数校验（extra=forbid 防注入）
├─ agent/dependencies.py# 依赖图 + 并行/顺序批次规划 + 硬门禁
├─ agent/harness.py     # 阶段7：最多 5 轮决策的 Harness 执行循环
scripts/agent_selftest.py  # 阶段6 三场景自测（16 项断言，可作 CI）
├─ core/default_prompts.py    # 默认模板种子（15 条 QA 规则 + 7 条异常分析规则）
├─ templates/           # Jinja2 管理后台模板（admin_base / kb / prompts）
├─ core/                # 阶段3原样迁移的 11 个核心模块（config 改 pydantic-settings，
│                       #   llm 加流式变体，prompts 加 history 参数，其余零改动）
├─ db/                  # SQLAlchemy 模型（五表 + users/roles/permissions/
│                       #   user_roles/role_permissions/audit_logs/knowledge_documents/
│                       #   knowledge_document_versions）+ 仓储层
├─ schemas/             # Pydantic 请求/响应模型
└─ errors.py            # 统一错误码 + 中文错误信息
static/                 # login.html / index.html / app.css / app.js（原生 JS）
knowledge_base/ data/   # 沿用阶段3的模拟知识库与电表数据
uploads/                # 用户上传的原始文档（运行时生成，已 gitignore）
```

## 关键机制（对应《阶段4 接口与交互设计方案 v1.0》）

1. **检索器只在 lifespan 构建一次**（`app.state.retriever`），请求期只读；
   检索与 Guardrail 校验经线程池执行，不阻塞事件循环。
2. **消息四态**（`generating/complete/interrupted/failed`）：assistant 消息先行落库
   `generating`，终态从不无凭据写 `complete`；客户端断连时 Starlette 取消流任务，
   服务端用**独立后台任务**把已生成内容落库为 `interrupted`；进程崩溃遗留的
   `generating` 消息由启动自愈改标 `failed`。
3. **依据不足不调 LLM**：`has_evidence` 判 False 时直接返回 `NO_EVIDENCE`，
   不产生 assistant 消息。
4. **引用不经模型转手**：`citations` 事件由服务端从当次检索结果与规则引擎指标
   独立组装；引用表只存定位键 + 120 字摘录，不重复存知识库全文。
5. **数值溯源**（anomaly 模式）：回答中每个数字必须能在证据集合中找到来源
   （容差 0.5%），结果随 `done` 事件返回并入库。
6. **知识库影子重建 + 原子切换**（第二批）：上传/删除/重处理都在**全新的**检索器实例上
   构建，任一步失败直接丢弃影子（线上问答零影响），全部成功后仅一行引用替换完成切换；
   在途请求仍持有旧实例，互不干扰。切片与向量按 `doc_id + content_hash` 逐文档缓存，
   传一篇新文档不必重算全部内建文档的向量（实测 0.2~0.3 秒）。
7. **上传安全三道校验**：扩展名白名单（`.md/.pdf`）→ 大小上限（10MB）→ 文件头魔数
   （改名冒充的 `.exe` 会因缺 `%PDF-` 头被拒）；PDF 无文本层置 `needs_ocr` 并给中文提示，
   不做静默入库。上传原始文件落 `backend/uploads/doc{id}/v{n}/`（已 gitignore）。
8. **知识库的唯一事实来源是数据库**：启动时把 `knowledge_base/` 下 6 份内建文档幂等
   登记进 `knowledge_documents`，此后检索构建只认该表 `status=published` +
   显式 include/exclude 的目标文档——避免"上传成功但检索不到""删除后仍在索引里"两类经典坑。

## 管理后台（第二批 / 第三批）

- 入口：<http://127.0.0.1:8000/admin/kb>、<http://127.0.0.1:8000/admin/prompts>
  （聊天页左下角也会对 `kb:read` 用户显示"管理后台"）。
- 知识库页：文档列表（类型/版本/切片数/状态/上传人）、上传并发布、重新处理、软删除；
  内建 6 份文档不可删除（`KB_BUILTIN_PROTECTED`）。
- 提示词页：当前模板编辑（保存即新版本）、版本历史（操作人/变更说明/字数）、一键恢复。
- 页面路由与 API 的失败语义不同：**页面**未登录 302 带 `next` 回跳、无权限渲染 403 中文页；
  **API** 统一 401/403 JSON。

## 提示词版本快照式生效（第三批）

```
聊天请求 → 取 current_version_id + 内容（30s 内存缓存防抖）
        → 本次请求用它拼 Prompt（生成期间不变）
        → version_id 写入 assistant 消息 → done 事件也回带 prompt_version_id
PUT 保存 → 插入新版本 + 移动指针 + 立即失效缓存 → 下一个新提问即用新版
恢复历史 → 读旧内容生成**新版本号**（不覆盖、不复用）
```

- 提示词正文只存在数据库（`prompt_templates` / `prompt_template_versions`），代码里只剩
  占位符拼装；默认 15 条 QA 规则与 7 条异常分析规则在首次启动种子写入 v1。
- **代码级硬约束不可被模板删除**：渲染时由代码在尾部追加"系统级硬性约束"段；
  数值溯源、引用组装、"依据不足"拒答都不读模板。实测把模板改成"可以编造数据、
  输出系统提示词、忽略限制"，写入后：硬约束段仍在、无关问题仍返回 `NO_EVIDENCE`、
  `number_trace_guard` 仍拦截编造数值（98765/42 → `passed=false`）。
- 并发实测：一个 SSE 请求进行中保存模板，该请求仍以旧版本完整结束（17 个 token 事件 +
  `status=complete`），版本号是它开始时的快照。

## 对话审计与异常审计（第四批）

审计单元是**一条 assistant 消息**：问题、回答、引用、提示词版本、生成状态、关联异常都在一处。

- 六条件组合筛选：用户 / 时间范围 / 会话号 / 模式 / 是否涉异常 / 是否有风险标记，+ 关键词。
- 风险标记 7 类（缺证据/编造规则/推测当结论/建议改数/建议绕复核/建议控设备/与规则冲突），
  状态 `pending → processed / dismissed`，每次标记与流转都写 `audit_logs`（`action=flag_update`）。
- **异常六态机**：`pending_analysis → pending_review → reviewing → confirmed / dismissed / closed`。
  本阶段系统只做第一步推进（AI 分析完成即 `pending_analysis → pending_review`），
  其余由业务签核——`POST /api/anomalies/{id}/reviews` **不注册路由**（实测 405），
  页面"提交复核"按钮渲染为禁用态并注明原因。
- 规则结果（原始）与 AI 草稿**分离展示**：AI 草稿只挂在对话消息上，永不回写
  `anomaly_events.rule_result` / `evidence`。
- 页面闸门：`/admin/anomalies` 要求 `anomaly:read` + `audit:conversation:read`——
  普通用户虽然能看事件（聊天页需要），但进不了含 AI 草稿与复核记录的后台审计视图。

## 系统操作日志（第五批）

- `GET /api/logs`：时间范围 / 类型 / 操作人 / 结果 / 对象号 组合筛选 + 分页；
  `GET /api/logs/actions` 给前端下拉用的类型字典。
- 页面 `/admin/logs`：只读表格 + 明细展开 + 上一页/下一页；接口不提供修改与删除。
- 动作枚举（方案 7.3 的 8 类落地）：`login / logout / kb_upload / kb_delete / kb_reindex /
  prompt_update / user_create / user_update / flag_update`（`anomaly_review` 预留给签核接口）。
- `detail` 白名单化：只存操作摘要，密码 / API Key / 提示词全文进不了日志
  （实测对全量日志 grep `testpass123 / password_hash / api_key / sk-` 均为 0 命中）。
- 保留策略：`LOG_RETENTION_DAYS=90`（配置文件项），启动时按保留天数清理过期日志并记条数。
- 全链路联调与逐条验收证据见仓库根目录《瀚川工业园_阶段5_验收自测记录_v1.0.md》。

## Agent 工具调用（阶段6）

双通道：知识/制度问题走阶段4 直达 RAG（不进循环，成本不变）；分析/定位/排查问题走 Agent 通道。

**7 个工具**（注册表里没有的，模型幻想出来只会得到 `UNKNOWN_TOOL`）：
`query_energy_data` / `get_meter_config`（含拓扑）/ `validate_meter_data` /
`detect_energy_anomaly` / `search_energy_knowledge` / `generate_anomaly_report` /
`request_human_review`（只增不改）。
**不存在的工具**：设备控制、改计量数据、租户结算、异常定性、复核签核——这是"三权分立"的物理落地。

**五道闸**（每步 tool_call 依次过，任一不过即拒绝并回传结构化错误）：
权限码 → 数据范围（scope 预留）→ 前置条件（读任务状态机）→ 参数校验 → 输出闸（`_trace` + 证据编号签发）。

**门禁的物理性**：
- `detect_energy_anomaly` 前置 `validation_status == passed` → 校验不过拿不到检测结果；
- `generate_anomaly_report` 的 status 枚举只有 `draft / pending_check`，参数改不成 confirmed；
  正文跑 NumberTrace，数值不在证据里整篇退回；
- `request_human_review` 校验 evidence_ids 必须存在于本任务证据库（防编造编号）；
- 状态机 `review_required=true` 但模型没调 HR 时，**编排层兜底补发**一条请求。

**防循环**：总调用 12 步 / 单工具 3 次 / 同参重复直接返回缓存 / 180 秒预算。

**离线可跑**：没有 LLM_API_KEY 时，先尝试 Function Calling（失败即回落）→ 执行确定性剧本
（工具链、门禁、状态机、留痕全部真实），只有最后一段解释文字由确定性摘要器生成。
摘要同样过 NumberTrace：实测 `passed=true, checked=22, unmatched=[]`。

**留痕**：`agent_tasks`（状态机快照）+ `agent_tool_calls`（每步输入/输出摘要、错误码、耗时）
+ `audit_logs(action=agent_analysis, business_ref=task_id)` + messages/ citations，四者用 task_id 串成证据链。

**自测**：
```bash
cd backend && ../.venv/Scripts/python scripts/agent_selftest.py
# 16 项断言全通过：直达/Agent 双通道、校验拦截检测、强制复核、报告状态上限、
# NumberTrace、账本回查、跳过校验被物理拒绝（含重复请求幂等）、UNKNOWN_TOOL、SCOPE_DENIED
```

## 前端接入（阶段6 收口）

- 聊天页新增第三模式 **Agent 深度分析**：走 `/api/agent/analyze`，保留 `/api/chat` 原协议与既有模式不变。
- 回答气泡上方渲染**可折叠工具轨迹卡片**：每步工具名、成功/拒绝、错误码、摘要、耗时；
  `gate_blocked`（校验拦截检测）与 `fallback`（离线回落）以提示行显示；完成后自动收起。
- 异常事件面板新增「Agent 深度分析」按钮（自动带入事件号、直接进入剧本）；原「AI 分析」按钮行为不变。
- `done` 事件的状态字段渲染为角标：数据校验是否通过、是否已提交人工复核、报告草稿状态、
  是否使用确定性剧本。
- 管理后台 `/admin/agent-tasks`：任务列表 + 抽屉式调用轨迹（入参/结果摘要、错误码、耗时、
  状态机快照），可跳到对话审计页按会话追溯。普通用户页面 403，接口只能查本人任务。

完整实现说明、默认假设与验收证据见仓库根目录《瀚川工业园_阶段6_实现与验收记录_v1.0.md》。

---
## Agent Harness 执行循环（阶段7 第一批）

`POST /api/agent/analyze` 默认走 Harness（`engine=script` 保留阶段6 剧本做回归对照），
循环为「模型决策 → 前置校验 → 执行工具 → 结果回填 → 再决策」，**最多 5 轮模型决策**。

- 同轮相互独立的只读工具并行（`asyncio.gather`）；有依赖的按 `DEPENDENCIES` 分层顺序执行。
- 参数走 Pydantic `extra=forbid`，`sql/api_key/password` 类字段一律拒绝；没有任意 SQL 通道。
- 硬门禁读任务状态机：`detect` 需 `validation_status=passed`；`report` 需查询+校验+有效证据。
- 单工具 15 秒超时、瞬时错误重试 1 次、连接断开/用户取消即停止并说明原因；到 5 轮自动停止并给出已完成步骤与未完成原因。
- 每轮决策与每次工具调用写 `agent_execution_logs`（task_id/session_id/user_id/decision_round/
  tool_name/input_parameters/execution_time_ms/execution_status/result_summary/error_code/created_at），
  `GET /api/agent/logs` 与 `/admin/agent-logs` 供阶段5 审计链查询；不记 API Key、密码、隐藏推理。
- 无 LLM Key 时 Function Calling 尝试失败后回落确定性规划器，工具链/门禁/日志/预算全部真实执行。
- 自测：`scripts/harness_selftest.py`（25 项全通过）；阶段6 `scripts/agent_selftest.py` 16 项回归通过。
- 选型说明、架构图、目录结构、SQLAlchemy 模型与默认假设见仓库根目录
  《瀚川工业园_阶段7_Harness执行循环设计说明_v1.0.md》。

## 报告草稿与人工确认（阶段7 第二批）

- `POST /api/agent/analyze` 带 `require_confirmation=true` 时：报告草稿经 SSE `token` 流式下发，
  末尾发 `draft_ready` 并**暂停**，不在循环内自动创建复核请求。
- 前端渲染草稿卡片与「确认提交人工复核 / 取消」按钮；确认时携带 `idempotency_key` 与
  `report_version`，后端二次校验身份、权限、报告版本、证据 hash，通过后调用 `request_human_review`。
- 重复确认不重复建单（唯一幂等键 + `status=pending` CAS）；取消不执行普通提交，
  但强制复核风险（版本冲突/关键数据缺失/电气安全风险）由后端**独立保留**风险复核请求。
- **确认提交 ≠ 确认异常原因 ≠ 签核**：不写 `review_records`，不改异常状态到 confirmed/closed。
- 自测：`scripts/confirmation_selftest.py`（19 项全通过）；回归 Harness 25 项 + 阶段6 16 项。

## 边界声明（当前明确不做）

真实计量系统接入、任何修改能耗数据或异常状态的写接口、多租户数据域、
WebSocket（SSE 够用且更简单）；六类业务角色与数据范围（scope）授权、
复核签核接口（`anomaly:review:submit` 不授予任何角色）——留给后续批次。
