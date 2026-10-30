# Build from the repo root:  docker build -f docker/worker/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first so this layer is cached until the lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --only-group worker

COPY src/pr_reviewer/__init__.py src/pr_reviewer/__init__.py
COPY src/pr_reviewer/common src/pr_reviewer/common
COPY src/pr_reviewer/github src/pr_reviewer/github
COPY src/pr_reviewer/llm src/pr_reviewer/llm
COPY src/pr_reviewer/storage src/pr_reviewer/storage
COPY src/pr_reviewer/worker src/pr_reviewer/worker

RUN useradd --system --no-create-home app
USER app

# Cloud Run supplies $PORT (default 8080)
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn pr_reviewer.worker.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
