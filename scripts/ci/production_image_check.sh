#!/usr/bin/env bash
# Production-image test (ADR-007, ADR-011): the production target must not contain test
# code; the test target is the negative control that shows the probe can succeed.
#   scripts/ci/production_image_check.sh refund_api
set -euo pipefail

service="${1:?usage: production_image_check.sh <service>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
context="$root/services/$service"
testing_package="${service}_testing"

docker build --quiet --target production -t "$service:production-check" "$context" > /dev/null
docker build --quiet --target test -t "$service:test-check" "$context" > /dev/null

probe() { docker run --rm --entrypoint python "$1" -c "import $2" > /dev/null 2>&1; }

probe "$service:production-check" "$service" \
  || { echo "FAIL: production image can't import $service" >&2; exit 1; }
if probe "$service:production-check" "$testing_package"; then
  echo "FAIL: production image can import $testing_package" >&2
  exit 1
fi
probe "$service:test-check" "$testing_package" \
  || { echo "FAIL: control: test image can't import $testing_package" >&2; exit 1; }
echo "production image check passed: $testing_package absent from production, present in test"
