# 瀚川工业园 · 园区能耗监测与异常定位智能体

面向工业园区（试点：A/B 两栋楼、约 240 块电表、15 分钟采集周期）的能耗异常定位系统。
给运维与能源管理人员一个"能查、能问、能追责"的工作台：制度问答有依据、能耗数据只读查询、
异常可以定位、结论必须人工复核。

> ## ⚠️ 数据与范围声明（请先读这一段）
>
> - 仓库内的**电表读数、设备台账、租户信息、6 份管理制度全部是模拟数据**，不代表瀚川工业园的真实记录或已确认制度。
> - 代码中的阈值（总分表偏差 3%、倍率变化 1.5 倍、>15 分钟缺口等）是**测试参数**，不构成合同验收口径。
> - 系统**只读**接入能耗数据：不修改原始计量记录、不控制工业设备、不做租户结算，也不具备这三类能力。
> - 本项目是**试点候选版本**，未完成压测、真实数据接入与业务签核流程；上线前须按
>   [`docs/acceptance/阶段9_上线前检查清单与试点验收方案_v1.0.md`](docs/acceptance/阶段9_上线前检查清单与试点验收方案_v1.0.md) 逐项验收。

---

## 一、项目简介

园区 240 块电表的抄表数据每 15 分钟一条，人工排查一次异常平均要 3 天：先导出数据、再对总分表、
再翻制度确认口径、最后找计量和电气的人确认。本系统把这条链路压缩成一次对话，并且：

- **答案必须有依据**：知识库答不出就明确说"未找到相关规定"，不编条款、不编数值；
- **分析必须有工具痕迹**：每个结论对应可回查的工具调用、证据编号与时间窗；
- **定性必须由人做**：AI 只能把异常推进到"待复核"，签核永远在线下完成。

## 二、核心功能

| 功能 | 说明 |
|---|---|
| 制度问答（RAG） | 6 份制度文档、23 个切片，BM25 + 向量 + RRF 融合；回答带来源文件与片段，依据不足直接拒答 |
| 能耗查询 | 单表/整栋、15min/小时/天粒度，返回电量、采样点、缺失点、倍率与数据来源 |
| 数据质量校验 | 六项确定性校验：完整性、时间戳单调性、单位一致性、倍率一致性、配置版本冲突、总分表平衡 |
| 异常检测 | 规则 A（倍率变化待核查）、规则 B（总分表平衡差），输出事件与证据，不做故障定性 |
| Agent 深度分析 | Harness 循环最多 5 轮决策，8 个只读工具，依赖门禁 + 参数校验 + 防重复调用 |
| 报告草稿 | 只到 `draft/pending_check`，正文每个数值必须可溯源，否则整篇退回 |
| 人工确认与复核 | 草稿确认 → 四重校验 → 生成 `pending` 复核请求；确认 ≠ 签核（`review_records` 只读） |
| 多轮上下文 | 代词/序数/楼栋别名/相对日期的确定性解析；解析不出就反问，不猜参数 |
| 管理后台 | 用户与角色、知识库上传/重处理、提示词版本与恢复、对话审计、系统日志、Agent 任务与执行日志 |
| 审计与限流 | 全量操作审计（90 天保留）、7 类对话风险标记、用户级与系统级限流 |

## 三、技术架构

```
浏览器（原生 JS + SSE，无构建步骤）
   │
FastAPI 单进程（限流中间件 → JWT 认证 → RBAC）
   ├── /api/chat            直达 RAG：检索 → 依据判定 → 流式回答 + 引用
   └── /api/agent/analyze   Harness：5 轮决策 → 工具 → 证据 → 草稿/复核
          │
   app/rag/     切片 · 加载 · 向量/BM25 · RRF · 依据覆盖率判定
   app/energy/  读数差分×倍率 · 六项校验 · 规则 A/B · 报告草稿
   app/agent/   上下文 · 依赖编排 · 五道闸 · 状态机 · 确认
   app/db/      SQLAlchemy 2.x async（SQLite / 可换 PG）
```

详细说明见 [docs/architecture.md](docs/architecture.md)。三条设计底线：

1. **门禁在代码里**：`detect` 必须有通过的校验，`report` 必须有可溯源证据；提示词改不动这些。
2. **状态机单一写者**：异常状态与报告状态由程序推进，模型只能发起工具调用。
3. **可追责**：`agent_tool_calls` / `agent_tasks` / `audit_logs` 记录"谁、何时、对什么做了什么"。

## 四、目录说明

```text
.
├── app/
│   ├── main.py                 应用装配：lifespan 建表/种子/索引、中间件、路由、健康检查
│   ├── core/                   config / security / permissions / errors / prompts / guardrail / ratelimit
│   ├── api/                    auth chat agent anomalies admin kb prompts users audit logs sessions
│   ├── agent/                  harness（5 轮循环）· dependencies（依赖图）· guards（五道闸）
│   │                           state（状态机）· context（多轮解析）· confirmation（草稿确认）
│   │                           tools（工具注册与只读实现）· specs / schemas（声明与校验）· prompts
│   ├── rag/                    chunker / loader / embeddings / retriever / index_manager / bm25 / text
│   ├── energy/                 data_service / meter_validator / anomaly_detector / report_service
│   ├── db/                     models（表定义）· repo（仓储函数）· session（连接与 WAL）
│   ├── schemas/                Pydantic 请求/响应模型
│   ├── services/               chat_service / kb_service / prompt_service / audit_service
│   ├── templates/              管理后台 Jinja2 模板（SSR）
│   └── static/                 聊天页、登录页、app.js、app.css
├── knowledge/samples/          6 份模拟制度文档（Markdown）
├── data/samples/               2 份模拟读数（异常集 / 常规集）
├── eval/                       30 条评测用例 + 最近一次执行结果（机读 JSON）
├── scripts/                    4 套自测 + 评测执行器 + 用例校验（可单独运行）
├── tests/                      unit / integration / agent / evaluation（pytest）
├── docs/                       架构 / 接口 / 部署 / 安全 + 各阶段设计记录 + 验收材料
├── prototype/                  阶段3 命令行原型（历史保留，不参与运行）
├── Dockerfile · docker-compose.yml · pyproject.toml · .env.example · .github/workflows/ci.yml
```

## 五、本地开发环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11+（CI 用 3.11，本地 3.13 已验证） |
| 内存 | ≥1.5 GB（FAISS 索引 + 读数常驻） |
| 操作系统 | Windows / Linux / macOS 均可；SQLite 单文件，无需外部数据库 |
| 可选 | 一个 OpenAI 兼容的 LLM Key（不配则运行在离线模拟模式，功能与门禁照常） |

## 六、安装与启动

```bash
git clone https://github.com/coco-d0122/hanchaun-fde.git
cd hanchaun-fde

python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # 不填 Key 也能跑
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问 http://127.0.0.1:8000 → 跳转 `/login`。首次启动自动建表、写入种子数据（角色/权限、
提示词模板、知识库登记、异常事件）并构建检索索引。

创建账号（无默认口令）：

```bash
python -m app.init_admin --username admin --role admin     # 交互输入密码
python -m app.init_admin --username demo --role user
```

## 七、环境变量配置

完整清单见 [`.env.example`](.env.example)，关键项：

| 变量 | 默认 | 作用 |
|---|---|---|
| `LLM_API_KEY` | 空 | 空 = 离线模拟模式（确定性摘要，页面标注"离线模拟模式"） |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | 60 / 1400 | 超时与单次回复上限 |
| `DB_URL` | SQLite 文件 | 换 PostgreSQL 只改连接串 |
| `KB_DIR` / `DATA_DIR` | `knowledge/samples` / `data/samples` | 支持绝对路径（容器挂载） |
| `LOG_RETENTION_DAYS` | 90 | 审计日志保留天数 |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_GLOBAL_PER_MINUTE` | 240 / 1200 | 用户级 / 系统级限流 |
| `AGENT_ENABLED` | true | 紧急停用 Agent 分析（人工功能保留） |
| `EVAL_CLOCK_DATE` | 空 | 固定"今天"，仅评测/回归使用 |

## 八、数据库初始化

无需手工执行 SQL：应用启动时 `Base.metadata.create_all` 建表，随后幂等种子化（角色权限、
提示词模板、知识库文档登记、异常事件）。

```bash
# 查看表与数据量（SQLite）
sqlite3 hanchuan.db ".tables"
sqlite3 hanchuan.db "select count(*) from agent_tool_calls;"
```

> 注意：**没有迁移框架**。上线后改表必须"备份 → 副本演练 → 手工 DDL"，见
> [docs/deployment.md](docs/deployment.md) 第 6 节。切换 PostgreSQL 时需自行建库，表结构由 `create_all` 生成。

## 九、RAG 知识库初始化

1. 启动时把 `knowledge/samples/*.md` 登记进 `knowledge_documents`（`uploaded_by=NULL`，标记为内建）。
2. 读取已发布文档 → 标题感知切片（`CHUNK_SIZE=500`、`CHUNK_OVERLAP=50`）→ 向量 + BM25。
3. 索引缓存到 `.cache/kb_<manifest>/`，manifest 由文件名与内容哈希组成：**改文档即自动失效**。
4. 管理后台上传/删除文档走**影子重建 + 原子切换**：新索引验证通过才替换，构建期间旧索引继续服务。

```bash
# 强制重建：删缓存后重启（会自动重建）
rm -rf .cache && python -m uvicorn app.main:app
```

## 十、Agent 工具调用说明

8 个工具，全部只读（唯一"写"是创建人工复核请求，且只增不改）：

| 工具 | 作用 | 前置依赖 |
|---|---|---|
| `query_energy_data` | 查电量、点数、缺失点、倍率 | — |
| `get_meter_config` | 倍率、单位、配置版本与生效时间 | — |
| `get_device_relationship` | 父子表关系、设备与租户归属 | — |
| `validate_meter_data` | 六项确定性校验 | query + config |
| `detect_energy_anomaly` | 规则 A/B 检测 | validate **通过** |
| `search_energy_knowledge` | 制度检索（带依据判定） | — |
| `generate_anomaly_report` | 报告草稿（≤ pending_check） | query + validate + 证据非空 |
| `request_human_review` | 创建 pending 复核请求 | — |

执行约束：最多 5 轮决策；单工具最多 3 次；同参数重复调用走缓存；单次任务 180 秒预算；
工具超时 15 秒、失败重试 1 次；`extra=forbid` 拒绝未知参数（`sql`、`api_key` 之类）。

```bash
# 一次完整分析（人工确认模式）
curl -b jar -H 'X-Requested-With: XMLHttpRequest' -H 'Content-Type: application/json' \
  -d '{"question":"帮我分析 M-002 为什么用电量突然增加，并生成异常分析报告",
       "anomaly_id":"AN-20261023-0001","require_confirmation":true}' \
  http://127.0.0.1:8000/api/agent/analyze
```

## 十一、API 接口说明

完整接口、SSE 事件序列与错误结构见 [docs/api.md](docs/api.md)。常用：

| 接口 | 说明 |
|---|---|
| `POST /api/auth/login` | 登录（下发 HttpOnly Cookie） |
| `POST /api/chat` | 制度问答（SSE） |
| `POST /api/agent/analyze` | Agent 深度分析（SSE） |
| `GET /api/anomalies` | 异常事件列表 |
| `GET /api/agent/tasks/{id}` | 任务详情 + 工具轨迹 |
| `POST /api/agent/drafts/{id}/confirm` | 确认草稿并创建复核请求 |
| `GET /api/health` | 健康检查（知识库切片数、LLM 状态） |
| `/docs` · `/redoc` | OpenAPI 文档 |

写操作必须带 `X-Requested-With: XMLHttpRequest`，否则 403。

## 十二、测试运行方式

```bash
# 静态检查（CI 第一步；期望 All checks passed）
ruff check app scripts tests

# 全量（约 50 秒，含 30 条评测）
pytest -q

# 分层
pytest tests/unit -q           # 上下文解析 / 依据判定 / 安全 / 限流
pytest tests/integration -q    # 401 / 403 / CSRF / 405 / 拒答 / 门禁顺序 / 停用开关
pytest tests/agent -q          # 驱动 4 套深度自测（83 项断言）
pytest tests/evaluation -q     # 用例结构校验（全量执行需 RUN_AGENT_EVAL=1）

# 也可以直接跑脚本（输出逐条 PASS/FAIL，便于看证据）
python scripts/harness_scenarios.py
python scripts/run_agent_eval.py            # 30 条用例，退出码非 0 即失败
python scripts/run_agent_eval.py --only 8,9,10
```

当前基线：**ruff 0 error、单元 + 集成 49 项、Agent 深度回归 83 项、评测 30 条用例 149 项契约断言，全部通过**。
CI（`.github/workflows/ci.yml`）在每次 push/PR 跑同一套；CI 失败不代表验收通过，只代表不能合并。

## 十三、Docker 启动方式

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps                 # 等到 healthy
curl -s localhost:8000/api/health | python -m json.tool
```

镜像以非 root 运行，数据卷 `hanchuan-data` 挂 `/data`（SQLite、索引缓存、上传文件）。
**单 worker**：内存索引与进程内限流不做跨进程共享；要横向扩容先换 PG + Redis。
反向代理必须关闭 `proxy_buffering`，否则 SSE 会攒成一次性输出（见 deployment.md）。

## 十四、已实现功能与待开发功能

### 已实现（有代码、有测试）

- 制度问答（RAG + 依据判定 + 引用）、能耗查询、六项数据校验、规则 A/B 异常检测
- Agent 双通道、8 工具、依赖门禁、五道闸、5 轮上限、防重复调用、执行日志
- 报告草稿 + 用户确认 + 四重校验 + 复核请求（pending）+ 报告导出（MD/HTML）
- 多轮上下文（代词/序数/楼栋别名/相对日期/追问），歧义反问
- RBAC + JWT + CSRF + 会话归属校验 + 限流 + Agent 停用开关
- 行级数据范围（楼栋 scope）+ LLM 日预算熔断（429 中文提示）+ Prometheus 指标端点
- 管理后台 9 个页面、知识库上传/重处理/软删除、提示词版本与恢复、审计与日志
- 30 条评测用例 + 执行器、4 套自测、pytest 分层、GitHub Actions（含 pip-audit）
- 采集批次幂等落库通道（meter_readings 唯一键）、压测与备份恢复演练脚本

### 待开发（未实现，勿在对外材料中声称已完成）

| 项 | 影响 | 参考 |
|---|---|---|
| 实时数据采集调度（15 分钟周期） | 查询链路仍读 CSV；幂等落库通道已就绪（`scripts/ingest_meter_batch.py`），缺调度器与 db 数据源切换 | 阶段9 D2-05 / A-03 |
| 业务签核通道（`review_records` 写入） | 签核只能线下 | 阶段9 D3-05 / A-04 |
| 真实 LLM 下的评测与压测基线 | 现有评测与压测均在离线规划器下 | 阶段8 报告"残留风险" |
| OCR、报告 PDF 导出、外部通知 | 可接受（当前 MD/HTML 导出已够线下流转） | 阶段10 N-12/N-13 |
| Docker 镜像构建验证、CI 运行观测 | 环境限制（本机守护进程/runner 不可用） | 阶段10 N-10/N-11 |
| 多实例部署（PG + Redis + 外部向量库） | 试点单机够用 | 阶段10 N-15 |

阶段11 补齐记录（含压测数据、恢复演练、漏洞扫描结果）：
[docs/acceptance/阶段11_未完成项补齐记录_v1.0.md](docs/acceptance/阶段11_未完成项补齐记录_v1.0.md)

## 十五、工业安全边界与使用限制

1. **只读**：系统不修改原始计量记录、不覆盖读数、不下发任何设备控制指令、不做租户结算。
   工具注册表中不存在此类能力，强行调用返回 `UNKNOWN_TOOL` 并留痕。
2. **不定性**：倍率变化只输出"待核查"，不判定"电表故障"；异常结论一律 `pending_review`。
3. **不签核**：`anomaly:review:submit` 不授予任何角色（含管理员）。确认报告 ≠ 完成签核。
4. **不编造**：知识库无依据时说"未找到相关规定"；数据缺失时说"未取到数据/数据不完整"，
   不用旧数据冒充实时数据，不推测限额与比例。
5. **不做安全决策**：涉及停产、限电、设备操作的请求一律拒绝并给出替代路径
   （校验 → 异常标记 → 人工复核）。
6. **适用范围**：仅限试点（两栋楼）的辅助分析与制度查询；不得作为计费、结算、
   安全联锁或事故定责的唯一依据。

## 文档与验收材料

- [docs/README.md](docs/README.md) 文档索引
- [docs/architecture.md](docs/architecture.md) · [api.md](docs/api.md) · [deployment.md](docs/deployment.md) · [security.md](docs/security.md)
- [docs/acceptance/阶段9_上线前检查清单与试点验收方案_v1.0.md](docs/acceptance/阶段9_上线前检查清单与试点验收方案_v1.0.md)
- [docs/acceptance/阶段10_自检与发布记录_v1.0.md](docs/acceptance/阶段10_自检与发布记录_v1.0.md)
- [docs/acceptance/阶段11_未完成项补齐记录_v1.0.md](docs/acceptance/阶段11_未完成项补齐记录_v1.0.md)
- 各阶段设计与验收记录：[docs/phases/](docs/phases/)

## 许可

内部试点项目，保留所有权利，未授予开源许可。详见 [LICENSE](LICENSE)。
