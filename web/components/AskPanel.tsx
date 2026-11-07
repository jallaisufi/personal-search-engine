"use client";

import { Fragment, useEffect, useState } from "react";
import { ApiError, askStream, type Source } from "@/lib/api";
import ResultItem from "./ResultItem";

type Status = "thinking" | "writing" | "done" | "error";

export default function AskPanel({
  question,
  selectedChunk,
  onOpenSource,
}: {
  question: string;
  selectedChunk: number | null;
  onOpenSource: (source: Source) => void;
}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [answer, setAnswer] = useState("");
  const [status, setStatus] = useState<Status>("thinking");
  const [meta, setMeta] = useState<{ took_ms: number; cached: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setSources([]);
    setAnswer("");
    setStatus("thinking");
    setMeta(null);
    setError(null);

    askStream(
      question,
      (e) => {
        if (e.event === "sources") setSources(e.data);
        else if (e.event === "token") {
          setStatus("writing");
          setAnswer((a) => a + e.data.text);
        } else if (e.event === "done") {
          setStatus("done");
          setMeta({ took_ms: e.data.took_ms, cached: e.data.cached });
        } else if (e.event === "error") {
          setStatus("error");
          setError(e.data.message);
        }
      },
      controller.signal,
    ).catch((e: unknown) => {
      if (controller.signal.aborted) return;
      setStatus("error");
      setError(e instanceof ApiError ? e.message : "The answer couldn't be loaded.");
    });

    return () => controller.abort();
  }, [question]);

  const byNumber = new Map(sources.map((s) => [s.n, s]));

  return (
    <div className="ask">
      <div className="answer" aria-live="polite" aria-busy={status === "thinking" || status === "writing"}>
        {status === "thinking" && <p className="message">Reading your documents…</p>}
        {answer && (
          <p className="answer-text">
            {renderWithCitations(answer, byNumber, onOpenSource)}
            {status === "writing" && <span className="cursor" aria-hidden="true" />}
          </p>
        )}
        {error && (
          <p className="message" role="alert">
            <strong>{error}</strong>
          </p>
        )}
        {meta && (
          <p className="search-meta">
            Answered in {(meta.took_ms / 1000).toFixed(1)} s{meta.cached && ", from cache"}. Answers
            come only from the passages below; check the sources before relying on them.
          </p>
        )}
      </div>

      {sources.length > 0 && (
        <section aria-label="Sources">
          <h2 className="sources-heading">Sources</h2>
          <ol className="results">
            {sources.map((source) => (
              <ResultItem
                key={source.chunk_id}
                number={source.n}
                result={source}
                selected={selectedChunk === source.chunk_id}
                onOpen={() => onOpenSource(source)}
              />
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

/** Turn "[2]" markers into buttons that open that source. */
function renderWithCitations(
  answer: string,
  sources: Map<number, Source>,
  onOpen: (source: Source) => void,
) {
  return answer.split(/(\[\d{1,2}\])/g).map((part, i) => {
    const match = /^\[(\d{1,2})\]$/.exec(part);
    const source = match ? sources.get(Number(match[1])) : undefined;
    if (!source) return <Fragment key={i}>{part}</Fragment>;
    return (
      <button
        key={i}
        type="button"
        className="citation"
        aria-label={`Source ${source.n}: ${source.title}`}
        onClick={() => onOpen(source)}
      >
        {source.n}
      </button>
    );
  });
}
