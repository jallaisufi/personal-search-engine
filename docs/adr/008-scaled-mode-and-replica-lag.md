# ADR 008: Scaled mode and reading from a lagging replica

**Status:** accepted · 2026-11

## Context

A personal search engine for one user needs one API process and one database. The project should still show how the same code scales out, and be honest about what that means on one machine.

Scaling reads with a Postgres streaming replica introduces replication lag: the replica applies the primary's write-ahead log (WAL) a moment later. Two things can go wrong for search:

1. **Read-your-writes:** a document was just indexed (the UI says "Searchable"), but a search hits the replica before it has the new rows.
2. **Cache poisoning:** search results are cached under the index version ([ADR 003](003-versioned-search-cache.md)). If a search for version N runs on a replica that only has version N-1's data, stale results are cached under N and served until they expire.

## Decision

**Two Compose files.** `compose.yaml` is the lite default. `compose.scaled.yaml` is an override that sets 3 API replicas, 4 workers, adds a `postgres-replica` service and points the API's read pool at it. Nothing in the application code differs between modes; only environment variables do.

- **Load balancing:** Nginx resolves the `api` service name to all replica addresses and round-robins. The API keeps no state in process (sessions, caches and the job queue live in Redis), so any replica can answer any request.
- **Replica bootstrap:** on first start the replica runs `pg_basebackup -R` against the primary and then follows its WAL. The primary gets one extra client-authentication rule (replication connections) through a mounted `pg_hba.conf`, so switching an existing lite installation to scaled mode needs no re-initialisation.
- **Schema changes:** migrations run once, on the primary, in a one-shot container before any API or worker starts; the replica receives them through replication.

**Lag-aware search (`ReplicaGate`).** After each ingest batch the worker commits, reads the primary's current WAL position (LSN) and publishes it to Redis together with the new index version. For each new version, an API process asks its replica once: "have you replayed up to this LSN?"

- Not yet → this search reads the primary. Correct, slightly more load on the primary for a moment.
- Yes → the replica is used, and the answer is remembered for that version, so steady-state searches pay nothing for the check.

This fixes both problems above with one integer comparison per version per process, instead of a lag check on every request or a fixed "read the primary for N seconds after a write" timer that is either too short or wasteful.

**Failure handling:** if the replica can't be reached, reads fall back to the primary. Search stays available with a replica outage; `/stats` reports `replica_streaming: false`.

## Alternatives considered

- **Synchronous replication** removes lag but makes every ingest commit wait for the replica, and stalls writes when the replica is down. Wrong trade-off: ingestion is the write-heavy path.
- **PgBouncer / pgpool for routing.** Adds a service; the application already knows which statements are reads.
- **Kubernetes manifests.** The right tool for several machines, but out of scope for a project that must start with one command on a laptop. Listed as future work.

## Consequences

- On one machine, replicas share CPU cores and disk. More API processes help because Python runs CPU work one request at a time per process; the read replica mostly isolates searches from ingestion I/O and locks rather than adding raw capacity. The benchmarks in the README are measured, not assumed, and state their hardware.
- Lists and statistics (`/documents`, `/stats`) read the replica without the gate and can be a moment behind. That is acceptable for those views.
- Without a replication slot, a replica that is offline for longer than the primary keeps WAL (`wal_keep_size=512MB`) must be re-created (`docker compose rm -sv postgres-replica`, remove its volume, start again). A slot would avoid that but lets WAL grow without bound on the primary when a replica is removed, which is the worse failure for a self-hosted setup.
- Nginx resolves replica addresses at startup, so the gateway is restarted after changing `API_REPLICAS`.
