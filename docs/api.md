# API 接口说明

- 基础地址：`http://127.0.0.1:8000`
- 认证：登录后下发 `hc_token`（HttpOnly Cookie，JWT HS256，8 小时）
- 写操作必须带请求头 `X-Requested-With: XMLHttpRequest`（CSRF 防线）
- 错误结构统一为 `{"code": "...", "message": "中文说明", "details": ...}`
- 交互式文档：`/docs`（Swagger）、`/redoc`

## 1. 认证

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| POST | `/api/auth/login` | 登录，成功下发 Cookie | 公开 |
| POST | `/api/auth/logout` | 退出，清 Cookie | 登录 |
| GET | `/api/auth/me` | 当前用户、角色、权限码 | 登录 |

```bash
curl -c jar -H 'X-Requested-With: XMLHttpRequest' -H 'Content-Type: application/json' \
  -d '{"username":"demo_user","password":"DemoPass12345"}' \
  http://127.0.0.1:8000/api/auth/login
```

登录失败统一返回 401 + "用户名或密码错误"（不区分账号不存在，防枚举）。

## 2. 聊天（直达 RAG 通道）

`POST /api/chat` → `text/event-stream`

请求：

```json
{"question": "电表倍率变更后需要核查哪些信息？", "session_id": null, "mode": "chat", "anomaly_id": null}
```

`mode`：`chat`（默认）| `anomaly`（异常解读，需能定位事件）。

SSE 事件序列：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `session` | `{session_id, created}` | 会话建立（新建或复用） |
| `status` | `{stage, text}` | 阶段提示（检索/生成） |
| `token` | `{delta}` | 增量文本 |
| `citations` | `{items:[{source_file, heading_path, excerpt, ...}]}` | 引用来源 |
| `error` | `{code, message}` | 业务错误（如 `NO_EVIDENCE`） |
| `done` | `{...}` | 结束 |

无依据时返回 `error.code = NO_EVIDENCE`，`message` 固定为"当前知识库中未找到相关规定…"，
**不输出任何编造条款或数值**。

## 3. Agent 深度分析（Harness 通道）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/agent/analyze` | 流式执行（SSE） |
| GET | `/api/agent/tasks` | 任务列表（本人） |
| GET | `/api/agent/tasks/{task_id}` | 任务详情 + 工具轨迹 |
| GET | `/api/agent/logs` | 执行日志（管理员） |
| GET | `/api/agent/drafts/{draft_id}` | 草稿详情 |
| POST | `/api/agent/drafts/{draft_id}/confirm` | 用户确认提交人工复核 |
| POST | `/api/agent/drafts/{draft_id}/cancel` | 取消草稿 |

请求：

```json
{"question": "帮我分析 M-002 为什么用电量突然增加，并生成异常分析报告",
 "anomaly_id": "AN-20261023-0001", "engine": "harness", "require_confirmation": true}
```

SSE 事件：`task` → `status` → `round` → `tool_call` → `tool_result` → `token`
→（`draft_ready`）→ `done`；异常时 `error`。

`require_confirmation=true` 时，报告草稿以 `draft_ready` 下发并暂停，
必须走确认端点（四重校验：身份/权限/草稿版本/证据 hash）才会创建复核请求。

**确认 ≠ 签核**：确认只产生 `pending` 的复核请求，不写 `review_records`，不改异常状态。

## 4. 异常事件

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/anomalies` | 列表（支持楼栋/状态/规则筛选） |
| GET | `/api/anomalies/{anomaly_id}` | 详情 + 证据 |
| GET | `/api/anomalies/{anomaly_id}/reviews` | 复核记录（只读） |
| POST | `/api/anomalies/{anomaly_id}/reviews` | **405：签核通道未开放** |

## 5. 知识库管理（管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/kb/documents` | 文档列表（含切片数、状态） |
| POST | `/api/kb/documents` | 上传（`.md`/`.pdf`，≤10MB，扩展名+魔数双重校验） |
| GET | `/api/kb/documents/{doc_id}` | 详情与版本 |
| DELETE | `/api/kb/documents/{doc_id}` | 软删除（内建文档 403） |
| POST | `/api/kb/documents/{doc_id}/reprocess` | 重新处理（含 OCR 缺失分支） |

上传走"影子重建 + 原子切换"：新索引建好并验证后才替换，构建期间旧索引持续可查。

## 6. 提示词管理（管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/prompts` | 模板列表 |
| GET | `/api/prompts/{code}` | 当前版本 |
| PUT | `/api/prompts/{code}` | 保存（自动生成新版本，30s 缓存立即失效） |
| GET | `/api/prompts/{code}/versions` | 历史版本 |
| POST | `/api/prompts/versions/{version_id}/restore` | 恢复历史版本 |

模板可改，但**"系统级硬性约束"段由代码在渲染时追加**：把模板改成"可以编造数据"也删不掉。

## 7. 用户与权限（管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/users` | 用户列表 |
| POST | `/api/users` | 创建用户 |
| PATCH | `/api/users/{user_id}` | 改角色 / 禁用启用 |
| GET | `/api/users/{user_id}/scope` | 查看数据范围（阶段11 行级权限） |
| PUT | `/api/users/{user_id}/scope` | 设置数据范围（`scope_type=all/buildings`，管理员自身不可限权） |

权限码 13 个、角色 2 个（`user` / `admin`）。`anomaly:review:submit`（业务签核）
**不授予任何角色**，包括管理员。

## 8. 审计与日志（管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audit/conversations` | 对话审计列表 |
| GET | `/api/audit/conversations/{message_id}` | 单条消息详情 |
| GET | `/api/audit/flags` | 风险标记列表 |
| POST | `/api/audit/flags` | 添加标记（7 类） |
| PATCH | `/api/audit/flags/{flag_id}` | 更新标记 |
| GET | `/api/audit/flag-types` | 标记类型枚举 |
| GET | `/api/logs` | 操作日志（动作枚举与方案 7.3 对齐） |
| GET | `/api/logs/actions` | 可选动作列表 |

日志保留 90 天（`LOG_RETENTION_DAYS`），启动时清理超期记录。

## 9. 会话与健康检查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 我的会话列表 |
| GET | `/api/sessions/{session_id}/messages` | 会话消息（他人会话按不存在处理） |
| GET | `/api/health` | 健康检查：知识库切片数、LLM 就绪、缓存状态 |
| GET | `/api/metrics` | Prometheus 指标（阶段11）：请求量/5xx/耗时/工具失败率/当日 token，需 `log:read` |

```bash
curl -s http://127.0.0.1:8000/api/health | python -m json.tool
```

## 10. 限流响应

超过阈值（默认 240 次/分钟/用户、1200 次/分钟/系统）返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 37

{"code":"RATE_LIMITED","message":"请求过于频繁，请在 37 秒后重试。…","details":{"retry_after":37}}
```

健康检查、静态资源、登录页不计入限流。

## 11. 管理后台页面（SSR）

`/admin/dashboard`、`/admin/anomalies`、`/admin/kb`、`/admin/prompts`、`/admin/users`、
`/admin/audit`、`/admin/logs`、`/admin/agent-tasks`、`/admin/agent-logs`。
均为服务端渲染，未登录 302 到 `/login`。

> 路径参数名以代码为准（如 `{anomaly_id}`、`{doc_id}`）：本文档与 `app.openapi()` 逐个路径核对一致，
> 只有参数命名差异，没有缺漏接口。

## 12. Agent 报告导出（阶段11）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/tasks/{task_id}/report` | 导出任务最新报告草稿，`?format=md\|html`（默认 md），附件下载；本人可导，他人需 admin；无草稿返回 404 `REPORT_NOT_FOUND`。导出件页眉注明"待复核草稿，不代表已确认结论" |

## 13. LLM 日预算（阶段11）

单用户当日（UTC）LLM token 累计超过 `LLM_DAILY_TOKEN_BUDGET`（默认 500000，0=不限）时，
`POST /api/chat` 与 `POST /api/agent/analyze` 建流前返回：

```http
HTTP/1.1 429 Too Many Requests

{"code":"LLM_BUDGET_EXCEEDED","message":"今日 AI 调用额度已用完，请稍后再试或联系管理员。","details":null}
```

## 14. 行级数据范围（阶段11）

`user_roles.scope_type/scope_values` 正式启用：限权用户（scope_type=buildings）
只看到授权楼栋的异常事件；访问范围外楼栋的详情/Agent 分析/显式楼栋筛选一律
403 `FORBIDDEN`（不返回空列表假装无数据）。管理员恒为全部数据。

