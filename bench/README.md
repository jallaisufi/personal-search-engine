# Load tests

[k6](https://k6.io/) runs inside the Compose network, so nothing needs installing.

```bash
# 1. Lite mode
API_RATE_LIMIT=100000r/s docker compose up -d
make bench LABEL=lite

# 2. Scaled mode
API_RATE_LIMIT=100000r/s docker compose -f compose.yaml -f compose.scaled.yaml up -d
make bench LABEL=scaled

# 3. Markdown table of everything measured so far
make bench-report
```

`API_RATE_LIMIT` lifts the gateway's per-client rate limit (20 requests/s by default), which
would otherwise reject most of a load test coming from one address. Start the stack normally
again afterwards.

Without `make`:

```bash
docker compose --profile bench run --rm -e LABEL=lite -e SCENARIO=cold k6 run /bench/search.js
docker compose --profile bench run --rm -e LABEL=lite -e SCENARIO=warm k6 run /bench/search.js
python3 bench/report.py
```

## Scenarios

| Scenario | What it measures |
|---|---|
| `cold` | Every query is unique, so the result cache never hits: query embedding plus the hybrid SQL query |
| `warm` | 20 queries repeated: the Redis result cache |

Settings: `VUS` (concurrent users, default 20) and `DURATION` (default `30s`).

## Reading the numbers

- Compare `cold` with `warm` in the same mode to see what the cache saves.
- Compare `lite` with `scaled` in `cold` to see what API replicas and the read replica add.
  On a single machine all replicas share the same CPU cores and disk, so the gain is limited
  by the hardware: expect more from extra API processes (Python runs one request per process
  at a time on the CPU) than from the read replica, whose benefit shows when the primary is
  busy with ingestion or when replicas run on separate machines.
- With the default local embedding model, `cold` is dominated by embedding the query in
  Ollama. To measure the database path alone, run with `EMBEDDING_PROVIDER=fake`.
