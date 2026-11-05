# 部署与运维说明

> 目标环境：工业园区内网，单实例，两栋楼试点。公网暴露不在本期范围。

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+ | 3.13 已验证；3.10 及以下不支持（用到 `X | None` 语法） |
| 内存 | ≥1.5 GB | FAISS 索引 + 240 表读数在内存里 |
| 磁盘 | ≥2 GB | 依赖约 700 MB，其余是日志与上传文件 |
| 网络 | 出网可选 | 无外网时自动降级为离线模式（规则与门禁照常） |

## 2. 本地启动（5 分钟）

```bash
git clone https://github.com/coco-d0122/hanchaun-fde.git
cd hanchaun-fde

python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # 不填任何 Key 也能跑（离线模拟模式）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 → 自动跳 `/login`。

首次启动做三件事（幂等，重复启动不会重复写入）：

1. 建表（SQLAlchemy `create_all`）。
2. 种子数据：角色/权限、内置提示词模板、6 份知识库文档登记、异常事件。
3. 构建检索索引（知识库 → 切片 → 向量 + BM25），缓存到 `.cache/kb_<manifest>`。

### 创建首个管理员

```bash
# 密码不落命令行（会被 shell history 记录）：用交互输入，或非 TTY 环境给 INIT_PASSWORD
python -m app.init_admin --username admin --role admin
# CI / 脚本里：
INIT_PASSWORD='ChangeMe12345' python -m app.init_admin --username admin --role admin

python -m app.init_admin --list      # 查看现有账号
```

密码只存 bcrypt 哈希（cost=12），系统不存在任何默认口令。

### 演示账号（仅本地/演示）

如需快速体验，可自行用上面的命令建两个账号，一个 `user`、一个 `admin`。
仓库**不包含任何预置密码**，也没有默认口令。

## 3. 环境变量

完整字段与注释见 `.env.example`。必须知道的几个：

| 变量 | 默认 | 影响 |
|---|---|---|
| `LLM_API_KEY` | 空 | 空 = 离线模拟模式；配置后走真实模型 |
| `DB_URL` | `sqlite+aiosqlite:///<root>/hanchuan.db` | 换 PG 只改这里 |
| `KB_DIR` | `knowledge/samples` | 知识库目录（相对根目录或绝对路径） |
| `DATA_DIR` | `data/samples` | 读数目录；真实接入时替换 |
| `LOG_RETENTION_DAYS` | 90 | 审计日志保留天数 |
| `RATE_LIMIT_PER_MINUTE` | 240 | 单用户每分钟请求数 |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | 1200 | 全系统每分钟请求数 |
| `EVAL_CLOCK_DATE` | 空 | 固定"今天"，仅评测/回归用 |

## 4. Docker

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps            # 等待 healthy
curl -s localhost:8000/api/health | python -m json.tool
```

- 端口只绑 `127.0.0.1`，对外由反向代理暴露。
- 数据卷 `hanchuan-data` 挂 `/data`：SQLite、索引缓存、上传文件都在里面。
- **单 worker**：应用内限流与 FAISS 索引都在进程内存，多 worker 会各算各的。

### 反向代理（Nginx 片段）

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    # SSE 必须关缓冲，否则 token 会被攒到最后一次性吐出
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
}
```

## 5. 备份与恢复

```bash
# 备份（SQLite 必须用 .backup，直接拷文件在 WAL 模式下可能不一致）
sqlite3 hanchuan.db ".backup 'backup/hanchuan-$(date +%F).db'"
cp -r .cache backup/cache-$(date +%F)          # 索引缓存（可重建，可选）
cp -r uploads backup/uploads-$(date +%F) 2>/dev/null

# 恢复
docker compose down
cp backup/hanchuan-2026-11-06.db hanchuan.db
docker compose up -d
curl -s localhost:8000/api/health             # 确认 kb.chunks > 0
```

知识库与提示词都在数据库里，所以**备份数据库 = 备份了知识库清单、版本与提示词历史**；
原始 md/pdf 文件在 `knowledge/samples` 与 `uploads/`，需一并备份。

## 6. 升级与回滚

```bash
# 升级
git pull
docker compose build && docker compose up -d
curl -s localhost:8000/api/health

# 回滚（镜像打标签）
docker tag hanchuan-energy-agent:1.0.0 hanchuan-energy-agent:rollback
docker compose down && docker run -d --name hanchuan-agent ... hanchuan-energy-agent:rollback
```

**数据库结构变更**：当前无迁移框架（`create_all` 只建不改）。上线后如需改表，
必须先备份、在副本上演练、再执行手工 DDL；这也是阶段9 清单 D5-02 的要求。

## 7. 健康检查与监控

`GET /api/health` 返回：

```json
{"status":"ok","kb":{"chunks":23,"backend":"faiss","cache_hit":true},
 "llm":{"provider":"mock","model":"mock(offline)","ready":true}}
```

- `kb.chunks == 0` → 索引没建起来，问答会全部失败，属于 P0 告警。
- `llm.ready == false` → 仍在离线模式（不是故障，但要知道）。

建议接入的告警（当前**未实现**，需在试点前补）：

1. `/api/health` 连续 3 次非 200；
2. `kb.chunks` 为 0；
3. 5xx 比例 >1%（5 分钟窗口）；
4. `agent_tasks` 中 `running` 超过 10 分钟的任务数 >0；
5. 429 比例突增（可能是有人在刷，也可能是阈值过紧）。

## 8. 限流的容量含义

进程内固定窗口，重启即清零；单用户 240/分钟对"一个人反复点"够用，
但**挡不住分布式压测**，也不做队列。真实并发上量前必须：

1. 用 `scripts/` 下压测脚本（待补）跑出 30 并发基线；
2. 换 PostgreSQL + Redis（限流与缓存外置）；
3. 反向代理层再加一层连接数与速率限制。

## 9. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面能开但问答报 `NO_EVIDENCE` | 问题确实在知识库外，或索引没建好 | 先看 `/api/health` 的 `kb.chunks` |
| 上传文档后检索不到 | 索引未重建完成（返回 processing） | 等重建结束或点"重新处理" |
| 回答变成"离线确定性摘要" | 没配 `LLM_API_KEY` 或调用失败降级 | 配 Key；看后端日志的 `LLM` 行 |
| 429 频繁 | 阈值过紧或有人刷 | 调 `RATE_LIMIT_*`，并查审计日志 |
| 时间相关查询查不到数 | 数据只有 2026-10-23，相对日期按数据日+1 | 用绝对日期，或配 `EVAL_CLOCK_DATE` |
| 容器 healthy 但页面 502 | 反向代理缓冲/超时 | 关 `proxy_buffering`，放大 `proxy_read_timeout` |
