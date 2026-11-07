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

## How it works

```
 Browser ─▶ Nginx gateway ─┬─▶ Next.js web UI
                           └─▶ FastAPI (stateless) ─▶ Postgres + pgvector
                                   │                        ▲
                                Redis                       │ bulk writes
                                                            │
 Your folder ─▶ Worker: extract → chunk → embed (Ollama) ───┘
```

- **Ingestion.** The worker scans your folder, skips files whose modification time is unchanged (no read needed), and only re-indexes files whose content hash changed. Text is split into ~500-token chunks that never cross a heading or page boundary, so every result points to one exact location.
- **Embedding reuse.** Each chunk's text is hashed. If identical text is already in the index (another copy of a document, or an unchanged section of an edited file), its vector is reused instead of calling the model again.
- **Hybrid search.** One SQL query runs a vector search (HNSW index) and a full-text search (GIN index) and merges the two rankings with [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf). Vectors find meaning ("raising the rent" ≈ "rent increase"); full-text finds exact tokens such as names, codes and acronyms that embeddings miss.
- **Ask mode (RAG).** The question runs through the same cached hybrid search; the top passages go to a language model with instructions to answer only from them and cite each one as [n]. The answer streams to the browser as server-sent events. Repeated questions are served from cache. The local default model is small (`llama3.2:3b`, ~2 GB) so it runs on a laptop; set an OpenAI or Anthropic key for better answers.
- **Caching.** Redis caches query embeddings (the slowest step, a model call) and whole search results. Result keys include an index version that the worker bumps after every sync, so new documents invalidate all cached results at once without per-key deletes. Responses carry an `X-Cache: HIT|MISS` header, and `/api/stats` reports the hit rate. If Redis is down, search keeps working uncached.
- **Read/write split.** Search runs on a read connection pool and ingestion on the primary, so the scaled setup can point reads at a Postgres replica without code changes.

Design decisions are recorded in [`docs/adr/`](docs/adr).

## API

| Endpoint | Description |
|---|---|
| `GET /api/search?q=...&limit=10` | Ranked chunks with document, path, page, heading path, highlighted snippet, and whether each matched semantically, by keyword, or both |
| `POST /api/ask` `{"question": "..."}` | Streamed answer (server-sent events): `sources`, then `token`s, then `done` with the citations used |
| `GET /api/documents?status=indexed` | Indexed documents with chunk counts and errors |
| `GET /api/documents/{id}` | Document text plus each chunk's position, for the preview |
| `GET /api/stats` | Document counts, chunk count, index version, embedding model, search cache hit rate |
| `GET /health` | Database and Redis health |

## Configuration

Everything is configured in `.env` (see [`.env.example`](.env.example)).

| Setting | Default | Notes |
|---|---|---|
| `LOCAL_DOCS_PATH` | `./sample-docs` | Folder to index |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` (local, free), `openai` (your own key), `fake` (tests) |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Changing it requires `make reindex` |
| `LLM_PROVIDER` / `LLM_MODEL` | `ollama` / `llama3.2:3b` | Model for Ask mode: `ollama`, `openai`, `anthropic`, `fake` |
| `COMPOSE_PROFILES` | `local-ai` | Remove to skip running Ollama when you use an API provider |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP_TOKENS` | `500` / `50` | Chunk size and overlap |

**Using an API key instead of local models:** set the provider and key in `.env`. Usage is billed to your own account. Vectors from different models can't be mixed, so the worker refuses to start if the configured model differs from the one the index was built with, and `make reindex` rebuilds it.

## Development

```bash
make up          # start the stack
make logs        # follow api + worker logs
make test-unit   # fast tests, no database needed
make test        # all tests, against a separate test database in the compose Postgres
make lint        # ruff
make smoke       # end-to-end check against the running stack
```

Working on the UI with hot reload: `cd web && npm install && API_URL=http://localhost:8080/api npm run dev`, then open <http://localhost:3000> (the rest of the stack keeps running in Docker).

Tests use a deterministic fake embedding provider, so they need no model download, network or API key.

### CI/CD

Every push and pull request runs lint, unit and integration tests against real Postgres + pgvector, a Docker build, an end-to-end smoke test of the full stack, and a secret scan. Merges to `main` publish the backend image to GitHub Container Registry.

## Roadmap

- [x] **Weekend 1 — Core search:** Docker Compose stack, PDF/DOCX/Markdown ingestion with page and heading tracking, embedding reuse, hybrid search with RRF, CI/CD
- [x] **Weekend 2 — Usable product:** Next.js UI with highlighted results and document preview, Ask mode (answers with citations), Redis caching
- [ ] **Weekend 3 — Google Drive + deadlines:** OAuth + incremental sync via the Changes API, Redis Streams job queue, deadline extraction
- [ ] **Weekend 4 — Prove it:** scaled profile (API/worker replicas, Postgres read replica), k6 load tests, benchmark results in this README

Later: email/iCal deadline reminders, Prometheus + Grafana, reranking and a search-quality eval set, more connectors, OCR.

## License

[MIT](LICENSE)
