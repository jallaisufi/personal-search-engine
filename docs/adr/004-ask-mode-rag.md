# ADR 004: Ask mode as retrieval-augmented generation with streamed, cited answers

**Status:** accepted · 2026-11

## Context

Search returns passages; people often want the answer itself ("how long is the notice period?"). A language model can write that answer, but on its own it invents things and can't see the user's files.

## Decision

- **Retrieve, then generate.** The question goes through the same hybrid search and caches as normal search. The top 6 passages (`ASK_SOURCES`) are numbered and sent to the model with a system prompt that allows only those sources, requires a `[n]` citation after each claim, and requires an explicit "I couldn't find this" when the sources don't answer it.
- **Stream over server-sent events.** `POST /ask` returns `sources` first (so the UI can show them immediately), then `token` events as the model writes, then `done` with the citation numbers actually used. SSE is one-way HTTP, works through the nginx gateway with buffering off, and needs no WebSocket infrastructure.
- **Pluggable providers.** Ollama (default, local, free), OpenAI and Anthropic behind one streaming interface; a deterministic fake for tests and CI.
- **Small local default.** `llama3.2:3b` (~2 GB) runs on an ordinary laptop. Answer quality is the trade-off; the README explains how to switch to a larger local model or an API key.
- **Answer cache.** Answers are cached under the same versioned keys as search results (`ask:v{index_version}:…`), keyed also by model, so a re-index or model change never serves an old answer.
- **Rate limit.** The gateway allows 30 questions per minute per client, far below search, because each question occupies the model.

## Consequences

- Citations make answers checkable: every number opens the exact passage in the document preview.
- Answers can only be as good as retrieval: if the right passage isn't in the top 6, the model should say it couldn't find the answer. Reranking (future work) raises that ceiling.
- Small local models sometimes cite loosely. `cited_numbers` drops citations to sources that don't exist, and the UI only links valid ones.
