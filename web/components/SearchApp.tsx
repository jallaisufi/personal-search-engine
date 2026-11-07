"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, type SearchResponse, type SearchResult, type Stats } from "@/lib/api";
import AskPanel from "./AskPanel";
import DocumentPreview from "./DocumentPreview";
import ResultItem from "./ResultItem";

type Mode = "search" | "ask";

type State =
  | { kind: "idle" }
  | { kind: "loading"; previous: SearchResponse | null }
  | { kind: "done"; response: SearchResponse }
  | { kind: "error"; message: string };

const DEBOUNCE_MS = 250;

export default function SearchApp() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const urlQuery = params.get("q") ?? "";
  const mode: Mode = params.get("mode") === "ask" ? "ask" : "search";

  const urlFor = useCallback(
    (q: string, m: Mode) => {
      const query = new URLSearchParams();
      if (m === "ask") query.set("mode", "ask");
      if (q) query.set("q", q);
      const qs = query.toString();
      return qs ? `${pathname}?${qs}` : pathname;
    },
    [pathname],
  );

  const [input, setInput] = useState(urlQuery);
  const [state, setState] = useState<State>({ kind: "idle" });
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<SearchResult | null>(null);
  const lastResponse = useRef<SearchResponse | null>(null);

  useEffect(() => {
    api.stats().then(setStats).catch(() => setStats(null));
  }, []);

  // In search mode, keep the URL in sync with the input (debounced) so results update as you
  // type and searches can be shared. Ask mode waits for Enter: each question runs a model.
  useEffect(() => {
    if (mode !== "search") return;
    const trimmed = input.trim();
    if (trimmed === urlQuery) return;
    const timer = setTimeout(() => {
      router.replace(urlFor(trimmed, "search"), { scroll: false });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [input, urlQuery, mode, router, urlFor]);

  // Run the search whenever the query in the URL changes.
  useEffect(() => {
    setSelected(null);
    if (mode !== "search") return;
    if (!urlQuery) {
      setState({ kind: "idle" });
      setSelected(null);
      return;
    }
    const controller = new AbortController();
    setState({ kind: "loading", previous: lastResponse.current });
    api
      .search(urlQuery, controller.signal)
      .then((response) => {
        lastResponse.current = response;
        setState({ kind: "done", response });
        setSelected(null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const message = error instanceof ApiError ? error.message : "Search failed unexpectedly.";
        setState({ kind: "error", message });
      });
    return () => controller.abort();
  }, [urlQuery, mode]);

  const closePreview = useCallback(() => setSelected(null), []);

  const response =
    state.kind === "done" ? state.response : state.kind === "loading" ? state.previous : null;

  return (
    <>
      <form
        className="search-form"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          router.replace(urlFor(input.trim(), mode), { scroll: false });
        }}
      >
        <div className="mode-switch" role="group" aria-label="Mode">
          <button
            type="button"
            aria-pressed={mode === "search"}
            onClick={() => router.replace(urlFor(input.trim(), "search"), { scroll: false })}
          >
            Find passages
          </button>
          <button
            type="button"
            aria-pressed={mode === "ask"}
            onClick={() => router.replace(urlFor(input.trim(), "ask"), { scroll: false })}
          >
            Ask a question
          </button>
        </div>
        <label htmlFor="q" className="visually-hidden">
          {mode === "ask" ? "Ask a question about your documents" : "Search your documents"}
        </label>
        <input
          id="q"
          className="search-input"
          type="search"
          autoComplete="off"
          autoFocus
          enterKeyHint={mode === "ask" ? "send" : "search"}
          placeholder={mode === "ask" ? "When is the design review?" : "Where did I read about…"}
          value={input}
          onChange={(event) => setInput(event.target.value)}
        />
        <p className="search-meta" aria-live="polite">
          {mode === "search" ? (
            <SearchMeta state={state} stats={stats} />
          ) : (
            urlQuery !== input.trim() && input.trim() && <>Press Enter to ask</>
          )}
        </p>
      </form>

      <div className={`workspace${selected ? " has-preview" : ""}`}>
        {mode === "ask" && urlQuery && (
          <AskPanel
            question={urlQuery}
            selectedChunk={selected?.chunk_id ?? null}
            onOpenSource={setSelected}
          />
        )}

        {mode === "ask" && !urlQuery && (
          <p className="message">
            Ask in your own words. The answer is written only from passages in your documents, with
            numbered links to each source.
          </p>
        )}

        {mode === "search" && (
          <section aria-label="Results">
            {state.kind === "error" && (
              <p className="message" role="alert">
                <strong>{state.message}</strong> Run <code>docker compose ps</code> to see which
                services are up.
              </p>
            )}

            {state.kind === "idle" && <IdleMessage stats={stats} />}

            {response && response.results.length === 0 && state.kind === "done" && (
              <p className="message">
                No passages match <strong>{response.query}</strong>. Try fewer words or describe the
                topic differently.
              </p>
            )}

            {response && response.results.length > 0 && (
              <ol className="results">
                {response.results.map((result) => (
                  <ResultItem
                    key={result.chunk_id}
                    result={result}
                    selected={selected?.chunk_id === result.chunk_id}
                    onOpen={() => setSelected(result)}
                  />
                ))}
              </ol>
            )}
          </section>
        )}

        {selected && <DocumentPreview hit={selected} onClose={closePreview} />}
      </div>
    </>
  );
}

function SearchMeta({ state, stats }: { state: State; stats: Stats | null }) {
  if (state.kind === "loading") return <>Searching…</>;
  if (state.kind === "done") {
    const n = state.response.results.length;
    return (
      <>
        {n} {n === 1 ? "passage" : "passages"} in {Math.round(state.response.took_ms)} ms
        {state.response.cached && ", from cache"}
      </>
    );
  }
  if (stats) {
    const docs = stats.documents.indexed ?? 0;
    return (
      <>
        Searching {docs} {docs === 1 ? "document" : "documents"} ({stats.chunks} passages)
      </>
    );
  }
  return null;
}

function IdleMessage({ stats }: { stats: Stats | null }) {
  if (stats && (stats.documents.indexed ?? 0) === 0) {
    return (
      <p className="message">
        Nothing is indexed yet. Put files in the folder set as <code>LOCAL_DOCS_PATH</code>; PDF,
        Word, Markdown and text files are picked up within a minute.
      </p>
    );
  }
  return (
    <p className="message">
      Search by meaning or by exact words. Each result shows the document, section and page where
      the passage appears.
    </p>
  );
}
