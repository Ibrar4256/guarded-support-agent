#!/usr/bin/env bash
# The end-to-end crash tests (ADR-004, ADR-015). CI calls this; run it locally too, with the
# compose Postgres up and .env loaded:
#   set -a; . ./.env; set +a; scripts/ci/e2e_checks.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
e2e="$root/tests/e2e"
agent_venv="$root/services/agent/.venv/bin"

step() { printf '\n== e2e: %s\n' "$1"; }

for name in AGENT_DB_OWNER_URL AGENT_DB_RUNTIME_URL REFUND_DB_OWNER_URL REFUND_DB_RUNTIME_URL \
            POSTGRES_SUPERUSER POSTGRES_SUPERUSER_PASSWORD; do
  if [ -z "${!name:-}" ]; then
    if [ -n "${CI:-}" ]; then
      echo "FAIL: $name is not set; CI must run the crash tests" >&2
      exit 1
    fi
    echo "WARNING: $name is not set; the crash tests will be SKIPPED" >&2
  fi
done

step "uv sync --locked (e2e and both services)"
(cd "$e2e" && uv sync --locked)
(cd "$root/services/agent" && uv sync --locked)
(cd "$root/services/refund_api" && uv sync --locked)

step "ruff and mypy (the agent's tools, the e2e suite's packages)"
(cd "$e2e" && "$agent_venv/ruff" check . && "$agent_venv/ruff" format --check .)
(cd "$e2e" && "$agent_venv/mypy" --python-executable "$e2e/.venv/bin/python" ./*.py)

step "black-box check (ADR-015 Part 2)"
uv run --no-project --python 3.12 python "$root/scripts/ci/check_service_independence.py"

step "pytest"
(cd "$e2e" && .venv/bin/python -m pytest -q)
