# syntax=docker/dockerfile:1
# PyKnora API 镜像：多阶段，仅复制必要产物。
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app

# 依赖层：只依赖 manifest 与锁文件，源码变化不失效
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# 源码层
COPY src ./src
COPY db ./db
RUN uv sync --frozen --no-dev

# 非 root 运行
RUN useradd --create-home --uid 10001 pyknora \
    && chown -R pyknora:pyknora /app
USER pyknora

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "pyknora.main:app", "--host", "0.0.0.0", "--port", "8000"]
