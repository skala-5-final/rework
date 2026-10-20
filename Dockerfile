# api · worker · business-mcp · rag-mcp가 같은 이미지를 쓰고, compose에서 실행 명령만 다르게 준다.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# 의존성 레이어를 먼저 만들어 소스만 바뀌면 다시 설치하지 않는다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
