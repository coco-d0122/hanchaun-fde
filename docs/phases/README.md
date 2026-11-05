# 阶段文档（历史记录）

这里的文档按阶段冻结，记录当时的方案、取舍与验收证据，**不做追溯修改**。

因此文中出现的路径是**阶段10 代码整理之前**的路径。对照关系：

| 文档中的旧路径 | 现在的位置 |
|---|---|
| `backend/app/...` | `app/...` |
| `backend/app/agent/executor.py` | `app/agent/guards.py`（五道闸，职责不变） |
| `backend/app/core/rules.py` | `app/energy/data_service.py` + `app/energy/anomaly_detector.py` |
| `backend/app/core/loader.py` | `app/rag/chunker.py` + `app/rag/loader.py` |
| `backend/app/core/retriever.py` / `embedder.py` / `vector_store.py` | `app/rag/retriever.py` / `embeddings.py` / `index_manager.py` |
| `backend/app/core/default_prompts.py` | `app/agent/prompts.py` |
| `backend/scripts/...` | `scripts/...` |
| `backend/knowledge_base/` | `knowledge/samples/` |
| `backend/data/` | `data/samples/` |
| `backend/static/` | `app/static/` |
| 仓库根目录的 `瀚川工业园_*.md` | `docs/phases/` |

运行命令也变了：不再需要 `cd backend`，从仓库根目录直接执行
（例如 `python scripts/run_agent_eval.py`）。

行为层面的变更以代码与 `docs/architecture.md` 为准；阶段文档里"待实现/已知缺陷"的条目，
若未在本目录后续文档中说明已修复，则视为**仍未解决**。
