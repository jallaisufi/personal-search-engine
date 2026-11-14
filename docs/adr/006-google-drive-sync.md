# ADR 006: Google Drive as a read-only, incrementally synced source

**Status:** accepted · 2026-11

## Context

People keep documents in Google Drive. Indexing it must not require re-reading the whole Drive on every sync, must not be able to change anything there, and must not leave a usable Google credential in a database dump.

## Decision

- **Each self-hoster brings their own OAuth client.** There is no shared client secret to ship in a public repo, and Google's verification process for restricted scopes doesn't fit a self-hosted tool. The cost is a one-time setup, documented in the README.
- **Scope: `drive.readonly` only.** The connector has no code path that writes to Drive.
- **Authorisation-code flow with a single-use `state`** kept in Redis for 10 minutes, so a callback can't be forged or replayed (CSRF).
- **Only the refresh token is stored, encrypted** (Fernet, key derived from `ENCRYPTION_KEY`). Access tokens live in memory and are refreshed when they expire or Drive answers 401.
- **Incremental sync with the Changes API.** On first sync the scheduler takes a start page token *before* listing all files (so nothing changed during the listing is missed), then stores the token as the source's cursor. Later syncs fetch only changes after the cursor. The cursor is saved last, after jobs are queued: a failure in between repeats work instead of skipping it.
- **Content decides, not the change feed.** The feed also reports renames, moves and views. A job is queued only when the file's checksum differs from the indexed one (Google Docs have no checksum, so their modified time is used).
- **Google Docs are exported as Markdown**, which keeps headings, so results show the section just like local Markdown files.
- **Drive documents go through the same queue and pipeline** as local files: the job says which connector it came from, the worker downloads the file to a temporary folder and runs the normal extract → chunk → embed path. Retries, dead letters and embedding reuse apply unchanged.
- **Failures are per account and visible.** If Google rejects the stored token (revoked, or expired in Testing mode), the error is recorded on the source and shown with a Reconnect button; other sources keep syncing.

## Consequences

- Sync cost is proportional to what changed. The first sync of a large Drive is the only expensive one, and the queue spreads it over all workers.
- In Google's Testing publishing status, refresh tokens for this scope expire after about a week. The README explains how to switch the app to production status for personal use.
- Supported types are PDF, Word, text, Markdown and Google Docs. Sheets, Slides and scanned PDFs (OCR) are future work; files over `DRIVE_MAX_FILE_MB` are skipped.
- The app itself has no login yet, so anyone who can open the UI can connect or disconnect accounts. That is acceptable for a single-user instance bound to localhost; multi-user mode (future work) changes this.
- Rotating `ENCRYPTION_KEY` invalidates stored tokens; the UI then asks to reconnect.
