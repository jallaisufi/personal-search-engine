"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, headingTrail, type Deadline, type SearchResult } from "@/lib/api";
import DocumentPreview from "./DocumentPreview";

const dayFormat = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
});
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

/** The document preview takes a search hit; a deadline has the same location fields. */
function asHit(deadline: Deadline): SearchResult {
  return { ...deadline, snippet_html: "", score: 0, matched_by: [] };
}

export default function DeadlinesList() {
  const [deadlines, setDeadlines] = useState<Deadline[] | null>(null);
  const [includePast, setIncludePast] = useState(false);
  const [selected, setSelected] = useState<Deadline | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    api
      .deadlines(includePast)
      .then((items) => current && setDeadlines(items))
      .catch((e: unknown) => {
        if (current) setError(e instanceof ApiError ? e.message : "Deadlines couldn't be loaded.");
      });
    return () => {
      current = false;
    };
  }, [includePast]);

  const closePreview = useCallback(() => setSelected(null), []);

  async function dismiss(deadline: Deadline) {
    try {
      await api.dismissDeadline(deadline.id);
      setDeadlines((items) => items?.filter((item) => item.id !== deadline.id) ?? null);
      if (selected?.id === deadline.id) setSelected(null);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "The deadline couldn't be removed.");
    }
  }

  return (
    <>
      <label className="toggle">
        <input
          type="checkbox"
          checked={includePast}
          onChange={(event) => setIncludePast(event.target.checked)}
        />
        Show past deadlines
      </label>

      <div className={`workspace${selected ? " has-preview" : ""}`}>
        <section aria-label="Deadlines">
          {error && <p className="message" role="alert">{error}</p>}
          {!error && !deadlines && <p className="message">Loading deadlines…</p>}
          {deadlines?.length === 0 && (
            <p className="message">
              No {includePast ? "" : "upcoming "}deadlines found. They appear here a moment after a
              document with a dated task is indexed, for example “submit the form by 31 March”.
            </p>
          )}

          {deadlines && deadlines.length > 0 && (
            <ol className="deadlines">
              {deadlines.map((deadline) => {
                const trail = headingTrail(deadline.heading_path);
                const soon = deadline.days_left >= 0 && deadline.days_left <= 7;
                return (
                  <li key={deadline.id} className="deadline">
                    <div className="deadline-when">
                      <span className="deadline-date">
                        {dayFormat.format(new Date(`${deadline.due_on}T00:00:00`))}
                      </span>
                      <span className={soon ? "deadline-soon" : "path"}>
                        {relative.format(deadline.days_left, "day")}
                      </span>
                    </div>
                    <div className="deadline-what">
                      <button
                        type="button"
                        className="result-open"
                        aria-current={selected?.id === deadline.id}
                        aria-label={`Open ${deadline.title} at this deadline`}
                        onClick={() => setSelected(deadline)}
                      >
                        <span className="result-title">{deadline.task}</span>
                        <span className="result-snippet">{deadline.quote}</span>
                        <span className="result-where">
                          <span>{deadline.title}</span>
                          {trail.length > 0 && <span>{trail[trail.length - 1]}</span>}
                          {deadline.page !== null && <span>Page {deadline.page}</span>}
                        </span>
                      </button>
                      <button
                        type="button"
                        className="close-button"
                        onClick={() => dismiss(deadline)}
                      >
                        Remove
                      </button>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        {selected && <DocumentPreview hit={asHit(selected)} onClose={closePreview} />}
      </div>
    </>
  );
}
