# 系统架构说明

> 本文描述**已经实现的代码结构**，不是设计愿景。未实现的部分在文末"未实现能力"中单独列出。

## 1. 总体形态：模块化单体

单进程 FastAPI 应用 + 一个 SQLite 文件（可换 PostgreSQL）+ 一份本地知识库。
选单体的理由：试点只有两栋楼、240 块表、个位数并发；拆分服务只会增加部署与排障成本，
而这个系统的风险点在"AI 说了不该说的话、调了不该调的工具"，不在吞吐量。

```
浏览器
  │  HTML/CSS/JS（app/static，SSE 直连，无前端框架、无构建步骤）
  ▼
FastAPI（app/main.py，单 worker）
  ├─ 中间件        RateLimitMiddleware（用户级 + 系统级固定窗口）
  ├─ 认证层        Cookie(JWT, HS256, 8h) → require_login / require_permission
  ├─ 接口层        app/api/*        聊天 / Agent / 异常 / 管理后台 / 审计 / 日志
  ├─ 服务层        app/services/*   会话编排、知识库流水线、提示词版本
  ├─ Agent 层      app/agent/*      双通道（直达 RAG / Harness 循环）
  ├─ 领域层        app/rag, app/energy   检索与能耗规则（纯函数，可单测）
  ├─ 持久层        app/db/*         SQLAlchemy 2.x async + 仓储函数
  └─ 静态页        app/static, app/templates（Jinja2 管理后台，SSR）
```

## 2. 两条问答通道

| 通道 | 入口 | 行为 | 适用 |
|---|---|---|---|
| 直达 RAG | `POST /api/chat` | 检索 → 依据判定 → 流式回答 + 引用 | 制度问答（"倍率变更要核查什么"） |
| Harness | `POST /api/agent/analyze` | 5 轮模型决策 → 工具调用 → 证据/门禁 → 草稿/复核 | 数据分析（"M-002 为什么突增"） |

两条通道共用同一套检索器、同一套数据访问与同一套审计；
差别是 Harness 通道会真的调工具、会产生任务账本与人工复核请求。

## 3. Harness 执行循环（app/agent/harness.py）

```
用户问题
  → 上下文解析（app/agent/context.py：电表/楼栋/相对日期/代词/序数）
      ├─ 解析不出来 → 直接反问，不猜参数（不调任何工具）
      └─ 解析成功 ↓
  → 循环（最多 5 轮，MAX_DECISION_ROUNDS）
      ① 模型决策（有 Key）或确定性规划器（无 Key）产出 tool_calls
      ② 依赖编排（app/agent/dependencies.py）：拆批、可并行则 gather、缺前置则拒绝
      ③ 执行器五道闸（app/agent/guards.py）：
         身份权限 → 数据范围 → 前置状态 → 参数校验 → 输出（证据号/数值溯源）
      ④ 回填状态（app/agent/state.py）：只能由程序推进，模型无写权
  → 收敛：回答 / 报告草稿 / 人工复核请求 / 达轮次上限说明已完成与未完成
```

关键设计：**门禁在代码里，不在提示词里**。`detect_energy_anomaly` 要求
`validation_status == passed`；`generate_anomaly_report` 要求证据非空且正文每个数值
可溯源（NumberTrace）。模型再会说也过不去。

## 4. 领域层

| 模块 | 职责 | 为什么不放在 agent 里 |
|---|---|---|
| `app/rag/chunker.py` | 标题感知切片、重叠切分 | 切片参数直接影响引用质量，要能单独调参与单测 |
| `app/rag/loader.py` | Markdown/文本 → 带溯源元数据的切片 | 纯 IO，与检索策略解耦 |
| `app/rag/embeddings.py` | hash / API / 本地模型三种后端 | 无 Key 也要能跑，接口必须统一 |
| `app/rag/retriever.py` | BM25 + 向量 + RRF 融合 + 依据判定 | 检索质量与"是否敢回答"是两件事，前者在召回，后者在阈值 |
| `app/rag/index_manager.py` | FAISS 索引构建/持久化/加载 | 索引与检索算法分离，便于换库 |
| `app/energy/data_service.py` | 读数加载、15 分钟差分 × 倍率、窗口筛选 | 计量口径必须与 Agent 无关地可验证 |
| `app/energy/anomaly_detector.py` | 规则 A 倍率变化、规则 B 总分表平衡差 | 确定性基座：结论只能由这里算出 |
| `app/energy/meter_validator.py` | 六项数据质量校验 | 校验先于检测，是硬门禁的前置 |
| `app/energy/report_service.py` | 报告草稿（draft/pending_check） | 状态上限写死在枚举里，不给"已确认"留口 |

## 5. 数据模型（app/db/models.py）

| 表 | 用途 | 不可写性 |
|---|---|---|
| `users` / `roles` / `permissions` / `role_permissions` / `user_roles` | 账号与 RBAC | 仅管理员接口可写，且全部审计 |
| `conversations` / `messages` | 会话与消息（含引用、prompt 版本号） | 用户只能读自己的 |
| `anomaly_events` | 异常事件（六态机） | AI 只能推进到 `pending_analysis → pending_review` |
| `review_records` | 人工复核结论 | **只读**：代码里没有写入路径（签核通道未开放） |
| `knowledge_documents` / `knowledge_document_versions` | 知识库文档与版本 | 影子重建后原子切换 |
| `prompt_templates` / `prompt_template_versions` | 提示词与历史版本 | 每次改动留版本，可一键恢复 |
| `audit_logs` / `conversation_audit_flags` | 操作审计与对话风险标记 | 只增不改；90 天启动清理 |
| `agent_tasks` / `agent_tool_calls` / `agent_execution_logs` | Agent 任务账本、工具轨迹、执行日志 | 只增；用于事后追责 |
| `agent_report_drafts` | 报告草稿与确认状态 | 确认 ≠ 签核，`review_records` 不因此新增 |

## 6. 状态机

**异常事件**：`pending_analysis → pending_review → pending_check → confirmed / rejected / closed`。
AI 只能推进前两格；后四格需要人工签核通道（当前未开放，403/405）。

**Agent 任务**：`running → completed / failed / budget_exceeded / cancelled`。
没有永久 `running`：进程崩溃后由 `repo.get_latest_agent_task` 只认已结束任务，避免脏上下文。

**报告草稿**：`pending → confirmed → submitted`（或 `cancelled`）。
`submitted` 只是"已提交人工复核"，不是"已签核"。

## 7. 关键取舍

1. **没有真正的实时采集管道**：当前读 `data/samples/*.csv`。接真实数据需实现采集任务与增量入库。
2. **单 worker**：内存索引 + 进程内限流 + SQLite 单写者，多 worker 会各算各的。要扩需先换 PG + Redis。
3. **离线确定性规划器**：无 LLM Key 时用固定轨迹驱动 Harness。它证明门禁与账本可靠，**不证明**模型规划质量。
4. **前端无框架**：一个 `app.js`（约 900 行）足够，避免为演示引入构建链。
5. **SSE 而非 WebSocket**：单向流式、易穿透代理、断线由浏览器自动重连语义处理。

## 8. 未实现能力（不要在对外材料里说成已实现）

- 实时数据采集中台、15 分钟调度器、断点续采。
- 跨楼栋/跨租户的数据范围过滤（`scope` 字段已预留但未启用）。
- 业务签核通道（`POST /api/anomalies/{id}/reviews` 返回 405）。
- 租户结算、设备控制、原始计量数据修改（**刻意不做**）。
- 压测报告、监控告警接入、分布式限流。
- 真实 LLM 下的评测基线（当前所有评测都在离线规划器下运行）。
