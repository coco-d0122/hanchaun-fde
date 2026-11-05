# 瀚川工业园 · Web 后端与聊天界面（阶段4 + 阶段5第一/二批）

基于阶段3原型核心模块（检索 / 规则引擎 / Guardrail 原样迁移）构建的 FastAPI + SQLite + SSE 流式聊天系统，
含登录鉴权与 RBAC 权限（第一批）、管理后台与知识库文档管理（第二批）。

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
| GET | `/api/health` | 健康检查（知识库 / LLM 状态） |
| GET | `/login` + `/` | 登录页 + 聊天页（未登录 302 跳登录） |
| GET | `/admin` → `/admin/kb` | 管理后台（知识库管理页，未登录跳登录、无权限 403 页） |
| GET | `/admin/prompts` | 管理后台 · AI 提示词管理页 |

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

## 边界声明（当前明确不做）

真实计量系统接入、任何修改能耗数据或异常状态的写接口、多租户数据域、
WebSocket（SSE 够用且更简单）；六类业务角色与数据范围（scope）授权、
复核签核接口（`anomaly:review:submit` 不授予任何角色）——留给后续批次。
