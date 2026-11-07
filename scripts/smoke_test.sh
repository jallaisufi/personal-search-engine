#!/usr/bin/env bash
# End-to-end check against a running stack: health → sample docs indexed → search works.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8080}"
EXPECTED_DOCS="${EXPECTED_DOCS:-$(find sample-docs -type f | wc -l | tr -d ' ')}"
TIMEOUT="${TIMEOUT_SECONDS:-180}"

wait_for() {  # wait_for <description> <command...>
  local desc="$1"; shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then echo "✗ timed out waiting for: $desc"; return 1; fi
    sleep 2
  done
  echo "✓ $desc"
}

all_indexed() {
  local n
  n=$(curl -fsS "$BASE/api/stats" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["documents"].get("indexed",0))')
  [ "$n" -ge "$EXPECTED_DOCS" ]
}

wait_for "API healthy" curl -fsS "$BASE/health"
wait_for "$EXPECTED_DOCS sample documents indexed" all_indexed

top=$(curl -fsS "$BASE/api/search?q=security%20deposit" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["results"]; print(r[0]["path"] if r else "")')
if [[ "$top" != "apartment/lease-agreement.md" ]]; then
  echo "✗ search returned '$top' as top result for 'security deposit'"
  exit 1
fi
echo "✓ search ranks the lease agreement first for 'security deposit'"

if curl -fsS -N -X POST "$BASE/api/ask" -H 'Content-Type: application/json' \
    -d '{"question":"How much is the security deposit?"}' | grep -q "^event: done"; then
  echo "✓ Ask mode streams an answer through the gateway"
else
  echo "✗ Ask mode did not finish an answer"
  exit 1
fi

if curl -fsS "$BASE/" | grep -q "Personal Search"; then
  echo "✓ web UI is served by the gateway"
else
  echo "✗ web UI did not load at $BASE/"
  exit 1
fi
echo "Smoke test passed."
