# ---------------------------------------------------------------------------
# Stage 1: resolve production dependencies
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project 2>/dev/null || uv sync --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2: test image (dev dependencies + test suite)
#
# Built explicitly with `--target test`; a plain `docker build` must keep
# yielding the production runtime stage below, so this stage stays in front.
#   docker build --target test -t picnic-test .
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS test
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --group dev --no-install-project 2>/dev/null || uv sync --group dev --no-install-project
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src:/app" \
    PICNIC_MOCK=true
RUN mkdir -p /app/data

ENTRYPOINT []
CMD ["pytest", "--cov=picnic_meal_planner", "--cov-report=term-missing", "--cov-report=html", "-v"]

# ---------------------------------------------------------------------------
# Stage 3: production runtime (default build target — must remain last)
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

ENV PATH="/app/.venv/bin:$PATH"
RUN mkdir -p /app/data

ENTRYPOINT ["uv", "run"]
CMD ["bot"]
