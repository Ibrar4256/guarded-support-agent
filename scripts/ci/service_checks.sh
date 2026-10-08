#!/usr/bin/env bash
# Every check for one service (ADR-007 CI scope). CI calls this; run it locally too:
#   scripts/ci/service_checks.sh agent
# Integration tests run when the database variables are set (see write_test_env.sh).
set -euo pipefail

service="${1:?usage: service_checks.sh <agent|refund_api>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root/services/$service"

step() { printf '\n== %s: %s\n' "$service" "$1"; }

step "uv sync --locked"
uv sync --locked

step "ruff"
uv run ruff check .
uv run ruff format --check .

step "mypy (src and tests)"
uv run mypy
uv run mypy tests

if [ -f scripts/check_core_purity.py ]; then
  step "core purity"
  uv run python scripts/check_core_purity.py
fi

step "import contracts"
PYTHONPATH=testing uv run lint-imports --no-cache

if [ -f alembic.ini ] && [ -n "${AGENT_DB_OWNER_URL:-}" ] && [ "$service" = "agent" ]; then
  step "migrations: upgrade head + drift check"
  uv run alembic upgrade head
  uv run alembic check
fi

step "pytest"
uv run pytest -q
