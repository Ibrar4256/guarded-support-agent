#!/usr/bin/env bash
# The eval replay gate (ADR-030). CI calls this; run it locally too, with the compose Postgres up
# and .env loaded:
#   set -a; . ./.env; set +a; scripts/ci/eval_replay.sh
#
# Validates services/agent/evals/pinned.json, then replays every pinned suite with no provider
# key and compares it with its pinned report. Exits 1 if any suite had a recording error, else 4
# if any suite differs from its pin, else 0; a refused pin exits 2. With EVAL_PIN_BASE set to a
# commit (CI: the PR's base, or the push's previous head), it first prints what a change to the
# pin moved; that printout never fails the job.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
agent="$root/services/agent"
step() { printf '\n== eval replay: %s\n' "$1"; }

# Replay makes no request; no provider credential may reach it, even by accident.
while IFS='=' read -r name _; do
  case "$name" in
    *_API_KEY | *_API_TOKEN | CLOUDFLARE_ACCOUNT_ID | OLLAMA_BASE_URL) unset "$name" ;;
  esac
done < <(env)

cd "$agent"
step "uv sync --locked"
uv sync --locked
export PYTHONPATH=testing

step "the pin"
pins="$(uv run python -m agent_testing.evals.pin list)"
echo "$pins"

step "what a change to the pin moved"
base="${EVAL_PIN_BASE:-}"
if [ -n "$base" ] && ! git cat-file -e "$base^{commit}" 2>/dev/null; then
  git fetch --no-tags --quiet --depth=1 origin "$base" 2>/dev/null || true
fi
if [ -n "$base" ] && git cat-file -e "$base^{commit}" 2>/dev/null; then
  before="$(mktemp -d)"
  trap 'rm -rf "$before"' EXIT
  if git show "$base:services/agent/evals/pinned.json" > "$before/pinned.json" 2>/dev/null; then
    for name in $(uv run python -c 'import json, sys; print(*json.load(open(sys.argv[1])).values())' "$before/pinned.json"); do
      git show "$base:services/agent/evals/reports/$name" > "$before/$name" 2>/dev/null || true
    done
  else
    rm -f "$before/pinned.json"
  fi
  uv run python -m agent_testing.evals.pin moved --base-pins "$before/pinned.json" --base-reports "$before"
else
  echo "no base commit to compare with: skipped"
fi

status=0
while read -r suite k model warrant_model report; do
  step "$suite (k=$k)"
  code=0
  uv run python -m agent_testing.evals --suite "$suite" --mode replay --model "$model" \
    --warrant-model "$warrant_model" --k "$k" --expect "$report" || code=$?
  if [ "$code" -eq 1 ] || { [ "$code" -ne 0 ] && [ "$status" -ne 1 ]; }; then
    status=$code
  fi
done <<< "$pins"

step "result"
case "$status" in
  0) echo "every pinned suite replays and matches its pin" ;;
  1) echo "FAIL: a recording error (a missing recording, another anchor, a real date, an invariant)" ;;
  4) echo "FAIL: a replay differs from its pinned report (listed above); re-pin only if the change is intended" ;;
  *) echo "FAIL: exit $status" ;;
esac
exit "$status"
