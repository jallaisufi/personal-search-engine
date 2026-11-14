# ADR 005: Ingestion through a Redis Streams job queue

**Status:** accepted · 2026-11

## Context

Until now one worker scanned the folder and indexed changed files in the same loop. That couples the cheap part (listing files, comparing hashes) to the expensive part (parsing, embedding), allows only one worker, and has no retry: a file that failed once stayed failed until it changed.

Google Drive sync and the scaled setup both need several workers sharing the work.

## Decision

Split ingestion into a **scheduler** (exactly one) and **workers** (any number), connected by a queue in the Redis instance the stack already runs.

- **Queue: Redis Streams with a consumer group.** `XREADGROUP` hands each job to exactly one worker and keeps it pending until `XACK`. Compared with a Redis list (`BRPOP`), a job is not lost when a worker dies after taking it; compared with RabbitMQ or Kafka, there is no extra service to run for a self-hosted app.
- **Retries with backoff:** a failed job goes to a sorted set scored by its due time (30 s, 2 min, 8 min, ...). Workers move due jobs back onto the stream; `ZREM` decides which worker wins, so a retry is never duplicated.
- **Dead letters:** after `INGEST_MAX_ATTEMPTS` (3) the job is copied to a capped dead-letter stream and the document is marked failed with its error, visible on the Documents page. It is not retried until the file changes.
- **Crash recovery:** jobs pending longer than 15 minutes are reclaimed with `XAUTOCLAIM` and sent through the failure path, so a document that keeps crashing workers uses up its attempts instead of looping forever.
- **No duplicate jobs:** the scheduler sets a key per document version (`SET NX`) before adding a job, so repeated scans don't queue the same version twice while it waits or is in flight.
- **One cache invalidation per batch:** a worker bumps the index version once after each batch of jobs, not once per document.
- **Durability:** Redis runs with an append-only file, so queued jobs survive a restart. If Redis data is lost anyway, documents still marked pending are queued again by the next scan.

## Consequences

- Indexing throughput scales by adding workers (`WORKER_REPLICAS`); the benchmark in weekend 4 measures 1 vs 4 workers.
- Delivery is **at-least-once**, so a job can occasionally run twice (for example a very slow document reclaimed after 15 minutes while its first worker is still busy). That is safe because indexing is idempotent: a document's chunks are replaced in one transaction.
- Two versions of the same document processed at the same moment by different workers could commit in either order; workers read the file's current content when they run the job, so both write the latest version.
- The scheduler is a single instance by design. It holds no state, so restarting it is safe; running two would only cause duplicate scans, which the dedup key absorbs.
