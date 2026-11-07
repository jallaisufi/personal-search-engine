"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, headingTrail, type DocumentDetail, type SearchResult } from "@/lib/api";

export default function DocumentPreview({
  hit,
  onClose,
}: {
  hit: SearchResult;
  onClose: () => void;
}) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const passageRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    api
      .document(hit.document_id, controller.signal)
      .then((d) => setDoc(d))
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        setError(e instanceof ApiError ? e.message : "The document couldn't be loaded.");
      });
    return () => controller.abort();
  }, [hit.document_id]);

  // Bring the passage into view once the text is rendered.
  useEffect(() => {
    if (doc?.id === hit.document_id) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      passageRef.current?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    }
  }, [doc, hit.chunk_id, hit.document_id]);

  useEffect(() => {
    closeRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const trail = headingTrail(hit.heading_path);
  const shown = doc && doc.id === hit.document_id ? doc : null;

  return (
    <aside className="preview" aria-label={`Preview of ${hit.title}`}>
      <div className="preview-head">
        <div>
          <h2>{hit.title}</h2>
          <p>
            {[trail.join(" › "), hit.page !== null ? `Page ${hit.page}` : null, hit.path]
              .filter(Boolean)
              .join(", ")}
          </p>
          {hit.web_url && (
            <p>
              <a href={hit.web_url} target="_blank" rel="noreferrer">
                Open original
              </a>
            </p>
          )}
        </div>
        <button ref={closeRef} type="button" className="close-button" onClick={onClose}>
          Close
        </button>
      </div>

      <div className="preview-body">
        {error && <p className="message">{error}</p>}
        {!error && !shown && <p className="message">Loading document…</p>}
        {shown && (
          <p className="preview-text">
            {shown.text.slice(0, hit.char_start)}
            <mark ref={passageRef} className="passage">
              {shown.text.slice(hit.char_start, hit.char_end)}
            </mark>
            {shown.text.slice(hit.char_end)}
          </p>
        )}
      </div>
    </aside>
  );
}
