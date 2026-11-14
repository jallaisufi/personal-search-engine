#!/usr/bin/env bash
# Checks what only exists in scaled mode: load balancing across API replicas, streaming
# replication, searches reading from the replica, and new documents becoming searchable
# through primary → replica → API.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8080}"
COMPOSE="docker compose -f compose.yaml -f compose.scaled.yaml"
TIMEOUT="${TIMEOUT_SECONDS:-120}"

wait_for() {
  local desc="$1"; shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then echo "✗ timed out waiting for: $desc"; return 1; fi
    sleep 2
  done
  echo "✓ $desc"
}

json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

# 1. Load balancing: requests are answered by more than one API replica.
replicas=$(for _ in $(seq 1 40); do
  curl -fsS -o /dev/null -D - "$BASE/api/stats" | tr -d '\r' | awk 'tolower($1)=="x-served-by:" {print $2}'
done | sort -u | wc -l)
if (( replicas < 2 )); then echo "✗ only $replicas API replica answered 40 requests"; exit 1; fi
echo "✓ gateway spreads requests over $replicas API replicas"

# 2. The primary has one streaming replica.
streaming=$($COMPOSE exec -T postgres psql -U app -d search -Atc \
  "SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming'")
if [[ "$streaming" != "1" ]]; then echo "✗ expected 1 streaming replica, found $streaming"; exit 1; fi
echo "✓ primary is streaming to 1 replica"

# 3. The API reads from a database that is in recovery, i.e. the replica.
replica_ok() { [[ "$(curl -fsS "$BASE/api/stats" | json "d['database']['read_replica'] and d['database']['replica_streaming']")" == "True" ]]; }
wait_for "API reads from the streaming replica" replica_ok

# 4. Writes are refused on the replica (it really is read-only).
if $COMPOSE exec -T postgres-replica psql -U app -d search -c "CREATE TABLE smoke_should_fail (x int)" >/dev/null 2>&1; then
  echo "✗ the replica accepted a write"; exit 1
fi
echo "✓ replica is read-only"

# 5. A new document becomes searchable: worker → primary → replica → API.
marker="zebrafinch$(date +%s)"
doc="sample-docs/_smoke_scaled.md"
trap 'rm -f "$doc"' EXIT
printf '# Smoke test\n\nThe %s migrates in spring.\n' "$marker" > "$doc"
found() { [[ "$(curl -fsS "$BASE/api/search?q=$marker" | json "d['results'][0]['path'] if d['results'] else ''")" == "_smoke_scaled.md" ]]; }
wait_for "a new document is searchable through the replica" found

echo "Scaled smoke test passed."
