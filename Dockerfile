# PyKnora 运行镜像：api 与 worker 共用（命令不同）。
# 依赖用 uv 锁定版本安装；源码在构建阶段就复制，因为 uv sync 会安装本项目本身。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# 直连 pypi.org 在部分网络下会超时，构建期可覆盖镜像源：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" "uv==0.12.24"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY db ./db

RUN UV_DEFAULT_INDEX="${PIP_INDEX_URL}" uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "pyknora.main:app", "--host", "0.0.0.0", "--port", "8000"]
