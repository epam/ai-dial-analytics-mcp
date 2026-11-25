# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra otel

COPY src/ ./src/
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel


FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get upgrade --yes --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
RUN pip uninstall --yes pip setuptools wheel
ENV PATH="/app/.venv/bin:$PATH" \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=5000 \
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none \
    OTEL_RESOURCE_ATTRIBUTES=service.name=adas-mcp \
    OTEL_EXPORTER_PROMETHEUS_HOST=0.0.0.0

USER app
EXPOSE 5000 9464
ENTRYPOINT ["opentelemetry-instrument", "adas-mcp"]
CMD ["--transport", "http"]
