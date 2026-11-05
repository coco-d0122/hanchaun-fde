# ---- 构建阶段 ----
# 说明：faiss-cpu / numpy 在 slim 镜像里有可用 wheel，不需要编译工具链。
# 若换 arm64 或更冷门的平台，需确认 faiss-cpu 有对应 wheel，否则会被迫装编译器。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用层缓存：改代码不用重装依赖
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir \
    fastapi "uvicorn[standard]" sqlalchemy aiosqlite httpx pydantic pydantic-settings \
    python-dotenv PyJWT bcrypt jinja2 python-multipart pypdf numpy jieba rank-bm25 faiss-cpu

# 再拷代码与数据（.dockerignore 已排除 .venv/.git/本地库/缓存）
COPY app ./app
COPY knowledge ./knowledge
COPY data ./data
COPY scripts ./scripts
COPY eval ./eval

# 运行期数据目录（容器内不要写镜像层，统一挂卷）
RUN mkdir -p /app/uploads /app/.cache

# 非 root 运行：容器被突破时降低影响面
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 健康检查：/api/health 会返回知识库切片数与 LLM 就绪状态
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status==200 else 1)"

# 单 worker：应用内有限流与内存索引，多 worker 会各算各的（见 docs/deployment.md）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
