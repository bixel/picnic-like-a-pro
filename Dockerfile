FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project 2>/dev/null || uv sync --no-dev --no-install-project

FROM python:3.11-slim-bookworm
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
