# 瀚川工业园 · 阶段4 Web 后端与聊天界面

基于阶段3原型核心模块（检索 / 规则引擎 / Guardrail 原样迁移）构建的 FastAPI + SQLite + SSE 流式聊天系统。

## 运行

```bash
cd backend
python -m venv .venv            # 或复用仓库根目录的 .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# cp .env.example .env           # 可选：填入 LLM_API_KEY（留空 = 离线模拟模式）

.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>。

- 无 LLM_API_KEY 时自动进入**离线模拟模式**：全链路可跑通（检索、引用、状态事件、
  中断语义），回答由 mock 生成并带"离线模拟模式"角标；配置 Key 后自动切换真实模型。
- 首次启动构建 FAISS/BM25 索引（约 2 秒），之后命中 `.cache/` 秒级启动。
- 首次启动若 `anomaly_events` 表为空，自动对 `data/` 下两份模拟数据集跑阶段3检测链
  落库（3 条事件，确定性事件 ID，重复启动幂等）。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | SSE 流式聊天（`mode=knowledge/anomaly`，可选 `anomaly_id`） |
| GET | `/api/sessions` | 会话列表（按更新时间倒序） |
| GET | `/api/sessions/{id}/messages` | 会话消息（含引用） |
| GET | `/api/anomalies` | 异常事件列表（`?status=` 过滤） |
| GET | `/api/anomalies/{id}` | 异常事件详情（含证据） |
| GET | `/api/health` | 健康检查（知识库 / LLM 状态） |
| GET | `/` + `/static/*` | 聊天页面与静态资源 |

SSE 事件协议：`session → status(×N，与真实执行阶段对应) → token(×N) → citations → done`；
建流前错误返回统一 JSON 错误体（`code/message/details`），建流后错误发 `error` 事件。
交互式文档：<http://127.0.0.1:8000/docs>

## 目录结构

```
app/
├─ main.py              # FastAPI 实例 + lifespan（建表/自愈/落事件/建索引/LLM探测）
├─ api/                 # chat(SSE) / sessions / anomalies 路由
├─ services/chat_service.py   # 双模式 SSE 编排（状态机式分步）
├─ core/                # 阶段3原样迁移的 11 个核心模块（config 改 pydantic-settings，
│                       #   llm 加流式变体，prompts 加 history 参数，其余零改动）
├─ db/                  # SQLAlchemy 模型（五表）+ 仓储层 + async 会话工厂
├─ schemas/             # Pydantic 请求/响应模型
└─ errors.py            # 统一错误码 + 中文错误信息
static/                 # index.html / app.css / app.js（原生 JS，无框架）
knowledge_base/ data/   # 沿用阶段3的模拟知识库与电表数据
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

## 边界声明（本阶段明确不做）

用户登录与权限（阶段5）、真实计量系统接入、任何修改能耗数据或异常状态的写接口、
多租户数据域、WebSocket（SSE 够用且更简单）。
