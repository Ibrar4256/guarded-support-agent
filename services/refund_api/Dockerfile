# refund-api (ADR-007, ADR-011). Build context: this directory only.
#   docker build --target production -t refund-api .
#   docker build --target test -t refund-api-test .
# The production target never contains testing/ (refund_api_testing), so the
# fault-injection build can't be started from it; CI checks that.

# uv pinned to the same version as [tool.uv] required-version and CI (ADR-007).
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv

FROM python:3.12-slim AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# Dependencies first, so code changes don't invalidate this layer.
RUN uv sync --locked --no-dev --no-install-project

FROM base AS production
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --system --uid 10001 refund && rm /usr/local/bin/uv
USER refund
EXPOSE 8000
CMD ["uvicorn", "--factory", "refund_api.app:app_from_env", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
COPY src ./src
RUN uv sync --locked
COPY alembic.ini ./
COPY migrations ./migrations
COPY testing ./testing
COPY scripts ./scripts
COPY tests ./tests
ENV PYTHONPATH=/app/testing
EXPOSE 8000
# Fault plans live in process memory: exactly one worker (ADR-010 Part 3, ADR-011).
CMD ["uvicorn", "--factory", "refund_api_testing.app:fault_app_from_env", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
