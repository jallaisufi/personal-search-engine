"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type SourcesResponse } from "@/lib/api";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const CONNECT_URL = "/api/sources/gdrive/connect";

// Outcome of the Google sign-in, passed back in the URL as ?drive=...
const OUTCOME: Record<string, string> = {
  connected: "Google Drive is connected. Your files appear below as they are indexed.",
  declined: "Google Drive wasn't connected because access wasn't granted.",
  expired: "That sign-in link expired. Connect Google Drive again.",
  failed: "Google Drive couldn't be connected. Check the API logs, then try again.",
};

export default function SourcesPanel() {
  const [data, setData] = useState<SourcesResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const outcome = new URLSearchParams(window.location.search).get("drive");
    if (outcome && OUTCOME[outcome]) {
      setNotice(OUTCOME[outcome]);
      window.history.replaceState(null, "", window.location.pathname);
    }
    api
      .sources()
      .then(setData)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : "Sources couldn't be loaded."),
      );
  }, []);

  async function disconnect(id: number, name: string) {
    const ok = window.confirm(
      `Disconnect ${name}? Its documents are removed from search. Nothing changes in Google Drive.`,
    );
    if (!ok) return;
    try {
      await api.disconnectSource(id);
      window.location.assign("/documents");
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.message : "The account couldn't be disconnected.");
    }
  }

  if (!data && !error) return null;
  const accounts = data?.sources.filter((source) => source.kind === "gdrive") ?? [];

  return (
    <section className="sources" aria-label="Google Drive">
      <h2>Google Drive</h2>
      {notice && <p className="notice" role="status">{notice}</p>}
      {error && <p className="message" role="alert">{error}</p>}

      {data && !data.google_drive_configured && (
        <p className="path">
          Not set up yet. Add <code>GOOGLE_CLIENT_ID</code>, <code>GOOGLE_CLIENT_SECRET</code> and{" "}
          <code>ENCRYPTION_KEY</code> to <code>.env</code>; the README has the steps.
        </p>
      )}

      {accounts.length > 0 && (
        <ul className="accounts">
          {accounts.map((account) => (
            <li key={account.id}>
              <div>
                <div>{account.name}</div>
                <div className={account.error ? "path status-failed" : "path"}>
                  {account.error ??
                    `${account.documents} ${account.documents === 1 ? "document" : "documents"}` +
                      (account.last_synced
                        ? `, last checked ${dateFormat.format(new Date(account.last_synced))}`
                        : ", first sync in progress")}
                </div>
              </div>
              <div className="account-actions">
                {account.error && (
                  <a className="button" href={CONNECT_URL}>
                    Reconnect
                  </a>
                )}
                <button
                  type="button"
                  className="close-button"
                  onClick={() => disconnect(account.id, account.name)}
                >
                  Disconnect
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {data?.google_drive_configured && (
        <p>
          <a className="button" href={CONNECT_URL}>
            {accounts.length > 0 ? "Connect another account" : "Connect Google Drive"}
          </a>{" "}
          <span className="path">Read-only access. Files are never changed.</span>
        </p>
      )}
    </section>
  );
}
