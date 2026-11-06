# SRC 阶段交付包

> 版本：2026-11-06；对应仓库 main@`bf2789c`+SRC 提交；课程第 SRC 阶段交付。

## 文件清单

| 文件 | 内容 | 说明 |
|---|---|---|
| `mvp_source.zip` | 现有 MVP 全部源码（229 文件，2.0MB） | `git archive` 从仓库 HEAD 导出，**自动排除**：.git、.venv、`__pycache__`、`.cache`、`.jwt_secret`、`*.db`、运行日志等全部未跟踪/敏感文件（打包后逐项校验通过） |
| `initial_qa_assistant.zip` | 阶段 1 初始问答助手原型（15 文件） | `prototype/` 目录原样打包：单文件命令行原型（BM25+向量 RRF 融合检索、倍率/总分表规则检测、LLM 分析），含演示数据、知识库样本与演示输出 |

## 启动方法

### 初始原型（initial_qa_assistant.zip）
```bash
unzip initial_qa_assistant.zip && cd prototype
pip install -r requirements.txt
python energy_agent.py --demo                       # 非交互跑完全链路
python energy_agent.py --ask "电表倍率发生变化时应该如何处理？"
```

### MVP（mvp_source.zip）
```bash
unzip mvp_source.zip && cd hanchaun-fde
pip install -e ".[dev]"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # http://127.0.0.1:8000/
python -m app.init_admin --username admin --role admin        # 交互输入密码
python -m pytest -q                                           # 74 passed
```
LLM 未配置时自动 mock 离线运行（演示口径）；配置 `LLM_API_KEY` 后为真实模型。Docker：`docker compose up -d --build`（配置齐备，宿主机需自备 Docker）。

## 演示步骤（回放口径）

1. 登录 → 问答页提知识类问题（带引用）；
2. "异常分析"模式指定异常事件 → SSE 流式分析 → 生成报告草稿；
3. 点"确认提交"生成复核请求编号（HR-xxx），或"取消"仅保留草稿；
4. 管理后台：异常列表（行级范围过滤）→ 审计页追溯工具调用与证据 → 报告导出 md/html。

## 三个真实人工确认点（后端已生效，自测 19/19 通过）

| 审核点 | 后端机制 | 验证 |
|---|---|---|
| ① 数据异常复核 | `detect_force_review` 读确定性证据（不读模型措辞）：倍率异常/版本冲突/数据缺失/安全风险/归属不明 5 类强制挂起，**用户取消也保留风险请求**（自测 PASS：取消也建 HR-xxx） | scripts/confirmation_selftest.py |
| ② 报告提交确认 | confirm 四重校验（属主/状态/风险请求/证据哈希）：他人确认被拒、证据改动被拒（EVIDENCE_INVALID）、确认≠签核、重复确认幂等 | 同上 19 项全 PASS |
| ③ 最终业务审核 | 授权审批签核：复核记录（ReviewRecord）**系统内只读**，签核走线下授权流程（电气安全主管一票否决）；报告导出件页眉注明"待复核草稿，不代表已确认结论"。**系统内签核写入通道为待开发项，属设计边界而非缺陷** | tests/integration/test_report_export.py |

## 已实现功能

知识问答（RAG+引用+拒答）、能耗查询（行级数据范围）、六项数据校验、规则 A/B 异常检测（六态事件+证据落库）、Agent 双通道（8 只读工具、12 次上限/同类 3 次/180s 预算）、报告草稿+确认+导出、RBAC+CSRF+限流+LLM 日预算+Agent 停用开关、/api/metrics 监控、全链路审计、管理后台 10 页面、30 条评测（mock 口径 30/30）。

## 未实现（如实声明）

15 分钟实时采集调度（幂等导入通道已建）、系统内签核写入（线下签核）、人工修复队列工作台、真实 LLM 下评测/压测基线（现 mock 口径）。
