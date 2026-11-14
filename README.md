# Personal Search

[![CI](https://github.com/jallaisufi/personal-search-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/jallaisufi/personal-search-engine/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A self-hosted search engine for your own documents. Ask *"where did I read about the security deposit?"* and get the exact document, page, section and a highlighted snippet — across PDFs, Word files, Markdown and text.

It runs entirely on your machine with one command. No API keys, no cloud, no cost by default.

> **Status:** weekend 1 of 4 — core indexing and hybrid search are working. See the [roadmap](#roadmap).

## Quick start

Requirements: Docker with Compose v2.24+.

```bash
git clone https://github.com/jallaisufi/personal-search-engine.git
cd personal-search-engine
cp .env.example .env
docker compose up -d
```

The first start downloads the embedding model (~270 MB) and a small chat model for Ask mode (~2 GB), and indexes the files in `sample-docs/`. Then open **<http://localhost:8080>** and search, for example, *security deposit* or *when is the design review*.

Each result shows the document, section and page, plus a location bar showing where in the document the passage sits. Click a result to open the document scrolled to that passage.

Switch to **Ask a question** to get a written answer instead, for example *how long is the notice period?* The answer streams in as the model writes it, and every claim links to a numbered source passage you can open. It's written only from your documents; if they don't contain the answer, it says so.

The API is available too:

```bash
curl "http://localhost:8080/api/search?q=security%20deposit"
```

Interactive API docs: <http://localhost:8080/docs>

To index your own files, set `LOCAL_DOCS_PATH` in `.env` to a folder on your machine and run `docker compose up -d` again. The folder is mounted read-only.

## Deadlines

The **Deadlines** page lists dates in your documents by which something has to be done ("design review by 20 November", "free cancellation until 31 March"), soonest first. Each one shows the sentence it comes from and opens the document at that sentence. **Remove** hides a deadline that is done or was misread; it stays hidden when the document is indexed again.

Deadlines appear a moment after a document is indexed. On the very first start they wait for the chat model to finish downloading.

## Connect Google Drive (optional)

Search your Drive next to your local files: PDFs, Word files, text and Markdown files, and Google Docs. Access is read-only, and after the first sync only changed files are fetched.

Because this is self-hosted, you create your own Google OAuth client once (free, about 10 minutes):

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. Under **APIs & Services → Library**, enable the **Google Drive API**.
3. Under **Google Auth Platform**, fill in the app name and your email, choose audience **External**, and add your own Google address as a **test user**.
4. Under **Clients**, create an OAuth client of type **Web application** with this authorised redirect URI:
   `http://localhost:8080/api/sources/gdrive/callback`
5. Put the client ID and secret into `.env`, together with an encryption key for the stored token:
   ```bash
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ENCRYPTION_KEY=   # python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
6. Run `docker compose up -d`, open **Documents**, and click **Connect Google Drive**.

Things to know:

- Google shows a "Google hasn't verified this app" screen, because the client is your own. Continue under *Advanced*.
- While the app's publishing status is **Testing**, Google expires the sign-in after about 7 days; the Documents page then shows **Reconnect**. To avoid that, set the status to **In production** under *Audience* (see [Manage App Audience](https://support.google.com/cloud/answer/15549945)).
- **Disconnect** removes the account, its stored token and its documents from the index. Nothing is ever changed in Drive.
- If you use a different host or port, change `GOOGLE_REDIRECT_URI` and the redirect URI in the Google client to match.

## Scaled mode

The default ("lite") runs one of everything, which is right for one person on a laptop. Scaled mode runs the same code as a small cluster:

```bash
docker compose -f compose.yaml -f compose.scaled.yaml up -d    # or: make up-scaled
```

```
                 ┌──▶ api 1 ─┐          ┌──▶ Postgres replica   (searches read here)
 Nginx gateway ──┼──▶ api 2 ─┼──────────┤          ▲ WAL streaming
 (round robin)   └──▶ api 3 ─┘          └──▶ Postgres primary   (ingestion writes here)
                                                   ▲
 Scheduler ─▶ Redis job queue ─▶ 4 workers ────────┘
```

| | Lite | Scaled |
|---|---|---|
| API | 1 container | 3 behind the Nginx load balancer (`API_REPLICAS`) |
| Workers | 1 | 4 sharing the job queue (`WORKER_REPLICAS`) |
| Postgres | primary | primary + streaming read replica |
| Searches read from | primary | replica, once it has caught up with the latest ingest |

To make it the default, set `COMPOSE_FILE=compose.yaml:compose.scaled.yaml` in `.env` (on Windows use `;` instead of `:`). If your `.env` still has `WORKER_REPLICAS=1` from an earlier version, remove that line to get four workers.

What this demonstrates, and what it doesn't: the API and workers are stateless, so they scale by adding containers, and the database scales reads by adding replicas. On a single machine all of them share the same CPU cores and disk, so the gain is limited by the hardware. The architecture is what carries over to several machines.

`GET /api/stats` shows whether searches read from a replica and how far behind it is; every response carries `X-Served-By` with the API replica that answered. `make smoke-scaled` verifies load balancing, replication and reads from the replica.

## Benchmarks

Load tests use [k6](https://k6.io/) and run with `make bench` (see [`bench/README.md`](bench/README.md)).

Measured in CI on a shared GitHub-hosted runner (`ubuntu-latest`), 20 concurrent users for 20 seconds, the six sample documents, and the deterministic test embedding provider. So these numbers cover the gateway, API, cache and database path, not the embedding model, and the index is tiny. Treat them as a direction, and run `make bench` on your own hardware and documents for real figures.

| Scenario | Mode | Requests/s | p50 | p95 | p99 | Cache hits | Failed |
|---|---|---:|---:|---:|---:|---:|---:|
| cold search (every query unique) | lite | 516 | 36.0 ms | 58.0 ms | 75.0 ms | 0% | 0.00% |
| cold search (every query unique) | scaled | 550 | 32.0 ms | 67.6 ms | 118.0 ms | 0% | 0.00% |
| warm search (repeated queries) | lite | 1838 | 10.4 ms | 13.3 ms | 15.9 ms | 100% | 0.00% |
| warm search (repeated queries) | scaled | 2476 | 7.0 ms | 15.8 ms | 24.6 ms | 100% | 0.00% |

What they show:

- **Caching:** a cached search is served about 3.5× faster than an uncached one (p50 36 ms → 10 ms in lite mode), with 3.6× the throughput. With a real embedding model the gap is larger, because a cache hit also skips the model call.
- **Scaling on one machine:** three API replicas raise cached throughput by about a third (1838 → 2476 requests/s) and uncached throughput by 7%, while tail latency (p99) gets worse. That is the expected result when 3 API containers, 2 databases and the load generator share the same few CPU cores: more processes help until the cores are full, then they compete. The replicas pay off when they run on separate machines.

## How it works

```
 Browser ─▶ Nginx gateway ─┬─▶ Next.js web UI
                           └─▶ FastAPI (stateless) ─▶ Postgres + pgvector
                                   │ cache                  ▲
                                 Redis                      │ bulk writes
                                   ▲ job queue              │
 Your folder ─▶ Scheduler ─────────┴─▶ Workers ─────────────┘
                                       (extract → chunk → embed)
```

- **Ingestion.** The scheduler scans your folder, skips files whose modification time is unchanged (no read needed), and queues a job only for files whose content hash changed. Text is split into ~500-token chunks that never cross a heading or page boundary, so every result points to one exact location.
- **Google Drive sync.** The first sync lists the Drive once; after that the scheduler asks Drive's Changes API only for what changed since the saved cursor, so a sync costs the same whether the Drive holds 50 files or 50,000. Renames and views don't trigger re-indexing (the content checksum is compared). Google Docs are exported as Markdown so headings survive. The refresh token is stored encrypted, and only the read-only scope is requested.
- **Job queue.** Jobs go through a Redis Streams consumer group, so any number of workers share the work and each document is indexed by exactly one of them (`WORKER_REPLICAS=3` in `.env`). A failed document is retried with exponential backoff and ends in a dead-letter list after three attempts; a job whose worker crashed is picked up by another worker. `/api/stats` shows how many jobs are waiting, retrying and dead.
- **Embedding reuse.** Each chunk's text is hashed. If identical text is already in the index (another copy of a document, or an unchanged section of an edited file), its vector is reused instead of calling the model again.
- **Hybrid search.** One SQL query runs a vector search (HNSW index) and a full-text search (GIN index) and merges the two rankings with [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf). Vectors find meaning ("raising the rent" ≈ "rent increase"); full-text finds exact tokens such as names, codes and acronyms that embeddings miss.
- **Deadline extraction in two stages.** Stage 1 is plain code: it parses dates (English and German, with or without a year) and keeps only sentences that contain a date and a cue word such as "due", "by" or "Frist". Only those passages go to the language model in stage 2, which decides whether each date is a real deadline and words the task. The model may only pick from the dates stage 1 parsed, so it can describe a deadline but never invent or shift its date. On the sample documents most passages never reach the model; `/api/stats` reports the share. Extraction runs as its own queue job after indexing, so a slow model never delays search.
- **Ask mode (RAG).** The question runs through the same cached hybrid search; the top passages go to a language model with instructions to answer only from them and cite each one as [n]. The answer streams to the browser as server-sent events. Repeated questions are served from cache. The local default model is small (`llama3.2:3b`, ~2 GB) so it runs on a laptop; set an OpenAI or Anthropic key for better answers.
- **Caching.** Redis caches query embeddings (the slowest step, a model call) and whole search results. Result keys include an index version that the worker bumps after every sync, so new documents invalidate all cached results at once without per-key deletes. Responses carry an `X-Cache: HIT|MISS` header, and `/api/stats` reports the hit rate. If Redis is down, search keeps working uncached.
- **Read/write split and replica lag.** Search reads from a separate connection pool; in scaled mode that pool points at a streaming replica while ingestion writes to the primary. A replica is always a moment behind, so each ingest batch records the primary's WAL position, and search uses the replica only once it has replayed past it (until then it reads the primary). If the replica is down, reads fall back to the primary.

Design decisions are recorded in [`docs/adr/`](docs/adr).

## API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=...&limit=10` | Ranked chunks with document, path, page, heading path, highlighted snippet, and whether each matched semantically, by keyword, or both |
| `POST /api/ask` `{"question": "..."}` | Streamed answer (server-sent events): `sources`, then `token`s, then `done` with the citations used |
| `GET /api/documents?status=indexed` | Indexed documents with chunk counts and errors |
| `GET /api/documents/{id}` | Document text plus each chunk's position, for the preview |
| `GET /api/deadlines?include_past=false` | Deadlines with task, date, days left, source sentence and its exact position; `POST /api/deadlines/{id}/dismiss` hides one |
| `GET /api/sources` | Connected sources with document counts, last sync and sync errors |
| `GET /api/sources/gdrive/connect` | Starts the Google sign-in; `DELETE /api/sources/{id}` disconnects an account |
| `GET /api/stats` | Document counts, chunk count, index version, embedding model, search cache hit rate, indexing queue depth, share of passages sent to the model for deadlines |
| `GET /health` | Database and Redis health |

## Configuration

Everything is configured in `.env` (see [`.env.example`](.env.example)).

| Setting | Default | Notes |
|---|---|---|
| `LOCAL_DOCS_PATH` | `./sample-docs` | Folder to index |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` (local, free), `openai` (your own key), `fake` (tests) |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Changing it requires `make reindex` |
| `LLM_PROVIDER` / `LLM_MODEL` | `ollama` / `llama3.2:3b` | Model for Ask mode: `ollama`, `openai`, `anthropic`, `fake` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ENCRYPTION_KEY` | unset | Enable Google Drive (see above) |
| `WORKER_REPLICAS` | `1` | Number of indexing workers sharing the job queue |
| `COMPOSE_PROFILES` | `local-ai` | Remove to skip running Ollama when you use an API provider |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP_TOKENS` | `500` / `50` | Chunk size and overlap |

**Using an API key instead of local models:** set the provider and key in `.env`. Usage is billed to your own account. Vectors from different models can't be mixed, so the worker refuses to start if the configured model differs from the one the index was built with, and `make reindex` rebuilds it.

## Development

```bash
make up          # start the stack
make logs        # follow api, scheduler and worker logs
make test-unit   # fast tests, no database needed
make test        # all tests, against a separate test database in the compose Postgres
make lint        # ruff
make smoke       # end-to-end check against the running stack
make up-scaled   # API replicas, more workers, Postgres read replica
make bench       # load test the running stack
```

Working on the UI with hot reload: `cd web && npm install && API_URL=http://localhost:8080/api npm run dev`, then open <http://localhost:3000> (the rest of the stack keeps running in Docker).

Tests use a deterministic fake embedding provider, so they need no model download, network or API key.

### CI/CD

Every push and pull request runs lint, unit and integration tests against real Postgres + pgvector, a Docker build, an end-to-end smoke test of the full stack, and a secret scan. Merges to `main` publish the backend image to GitHub Container Registry.

## Roadmap

- [x] **Weekend 1 — Core search:** Docker Compose stack, PDF/DOCX/Markdown ingestion with page and heading tracking, embedding reuse, hybrid search with RRF, CI/CD
- [x] **Weekend 2 — Usable product:** Next.js UI with highlighted results and document preview, Ask mode (answers with citations), Redis caching
- [x] **Weekend 3 — Google Drive + deadlines:** Redis Streams job queue with retries and dead letters, Google Drive OAuth + incremental sync via the Changes API, two-stage deadline extraction with a deadlines page
- [x] **Weekend 4 — Prove it:** scaled mode (load-balanced API replicas, more workers, Postgres read replica with lag-aware reads), k6 load tests

Later: email/iCal deadline reminders, Prometheus + Grafana, reranking and a search-quality eval set, more connectors, OCR.

## License

[MIT](LICENSE)
