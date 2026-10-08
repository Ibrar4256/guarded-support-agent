#!/usr/bin/env bash
# Write a .env with random throwaway passwords for CI or a fresh checkout.
# Refuses to overwrite an existing .env, so it can never clobber real values.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
target="$root/.env"
if [ -e "$target" ]; then
  echo "refusing to overwrite $target" >&2
  exit 1
fi

port="${POSTGRES_HOST_PORT:-5433}"
pw() { python3 -c 'import secrets; print(secrets.token_urlsafe(18))'; }
su=$(pw); ao=$(pw); ar=$(pw); ro=$(pw); rr=$(pw)

umask 077
cat > "$target" <<ENV
POSTGRES_HOST_PORT=$port
POSTGRES_SUPERUSER=postgres
POSTGRES_SUPERUSER_PASSWORD=$su
AGENT_DB_OWNER_PASSWORD=$ao
AGENT_DB_RUNTIME_PASSWORD=$ar
REFUND_DB_OWNER_PASSWORD=$ro
REFUND_DB_RUNTIME_PASSWORD=$rr
AGENT_DB_OWNER_URL=postgresql+psycopg://agent_owner:$ao@localhost:$port/agent
AGENT_DB_RUNTIME_URL=postgresql+psycopg://agent_runtime:$ar@localhost:$port/agent
REFUND_DB_OWNER_URL=postgresql+psycopg://refund_api_owner:$ro@localhost:$port/refund_api
REFUND_DB_RUNTIME_URL=postgresql+psycopg://refund_api_runtime:$rr@localhost:$port/refund_api
ENV
echo "wrote $target"
