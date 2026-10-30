# Build from the repo root:  docker build -f docker/receiver/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first so this layer is cached until the lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --only-group receiver

COPY src/pr_reviewer/__init__.py src/pr_reviewer/__init__.py
COPY src/pr_reviewer/receiver src/pr_reviewer/receiver

RUN useradd --system --no-create-home app
USER app

# Cloud Run supplies $PORT (default 8080)
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn pr_reviewer.receiver.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
