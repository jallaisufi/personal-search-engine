"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, type SearchResponse, type SearchResult, type Stats } from "@/lib/api";
import DocumentPreview from "./DocumentPreview";
import ResultItem from "./ResultItem";

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

  const [input, setInput] = useState(urlQuery);
  const [state, setState] = useState<State>({ kind: "idle" });
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<SearchResult | null>(null);
  const lastResponse = useRef<SearchResponse | null>(null);

  useEffect(() => {
    api.stats().then(setStats).catch(() => setStats(null));
  }, []);

  // Keep the URL in sync with the input (debounced), so searches can be shared and reloaded.
  useEffect(() => {
    const trimmed = input.trim();
    if (trimmed === urlQuery) return;
    const timer = setTimeout(() => {
      const next = trimmed ? `${pathname}?${new URLSearchParams({ q: trimmed })}` : pathname;
      router.replace(next, { scroll: false });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [input, urlQuery, pathname, router]);

  // Run the search whenever the query in the URL changes.
  useEffect(() => {
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
  }, [urlQuery]);

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
          const trimmed = input.trim();
          router.replace(trimmed ? `${pathname}?${new URLSearchParams({ q: trimmed })}` : pathname, {
            scroll: false,
          });
        }}
      >
        <label htmlFor="q" className="visually-hidden">
          Search your documents
        </label>
        <input
          id="q"
          className="search-input"
          type="search"
          autoComplete="off"
          autoFocus
          placeholder="Where did I read about…"
          value={input}
          onChange={(event) => setInput(event.target.value)}
        />
        <p className="search-meta" aria-live="polite">
          <SearchMeta state={state} stats={stats} />
        </p>
      </form>

      <div className={`workspace${selected ? " has-preview" : ""}`}>
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
