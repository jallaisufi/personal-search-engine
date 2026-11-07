"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type DocumentSummary } from "@/lib/api";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const STATUS_LABEL: Record<DocumentSummary["status"], string> = {
  indexed: "Searchable",
  pending: "Waiting",
  failed: "Couldn't read",
};

export default function DocumentsTable() {
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .documents()
      .then(setDocs)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : "Documents couldn't be loaded."),
      );
  }, []);

  if (error) return <p className="message" role="alert">{error}</p>;
  if (!docs) return <p className="message">Loading documents…</p>;
  if (docs.length === 0) {
    return (
      <p className="message">
        No documents yet. Set <code>LOCAL_DOCS_PATH</code> in <code>.env</code> to the folder you
        want to search.
      </p>
    );
  }

  return (
    <div className="table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">Document</th>
            <th scope="col">Status</th>
            <th scope="col">Passages</th>
            <th scope="col">Last changed</th>
          </tr>
        </thead>
        <tbody>
          {docs.map((doc) => (
            <tr key={doc.id}>
              <td>
                <div>{doc.title}</div>
                <div className="path">{doc.path}</div>
              </td>
              <td className={doc.status === "failed" ? "status-failed" : undefined}>
                {STATUS_LABEL[doc.status]}
                {doc.error && <div className="path">{doc.error}</div>}
              </td>
              <td className="number">{doc.chunks}</td>
              <td>{doc.modified_at ? dateFormat.format(new Date(doc.modified_at)) : "Unknown"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
