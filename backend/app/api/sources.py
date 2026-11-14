"""Connected sources: list them, connect Google Drive (OAuth), disconnect."""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.connectors.gdrive import DriveClient, GoogleError, GoogleOAuth
from app.db import read_conn, write_conn
from app.sync import publish_changes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sources")

STATE_TTL_SECONDS = 600
AFTER_CONNECT = "/documents?drive="  # back to the UI with the outcome


class SourceOut(BaseModel):
    id: int
    kind: str
    name: str
    documents: int
    last_synced: datetime | None
    error: str | None  # set when the last sync failed, e.g. access was revoked


class SourcesResponse(BaseModel):
    google_drive_configured: bool
    sources: list[SourceOut]


def _state_key(state: str) -> str:
    return f"oauth:state:{state}"


@router.get("", response_model=SourcesResponse)
async def list_sources(request: Request) -> SourcesResponse:
    async with read_conn() as conn:
        rows = await conn.execute(
            text("""
            SELECT s.id, s.kind, s.name, s.last_synced, s.config->>'error' AS error,
                   count(d.id) AS documents
            FROM sources s LEFT JOIN documents d ON d.source_id = s.id
            GROUP BY s.id ORDER BY s.kind DESC, s.name
            """)
        )
        sources = [SourceOut(**r._mapping) for r in rows]
    return SourcesResponse(
        google_drive_configured=request.app.state.google.configured, sources=sources
    )


@router.get("/gdrive/connect")
async def connect_gdrive(request: Request) -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    oauth: GoogleOAuth = request.app.state.google
    if not oauth.configured:
        raise HTTPException(
            status_code=404,
            detail="Google Drive isn't set up. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "ENCRYPTION_KEY to .env (see the README).",
        )
    # The state value ties the callback to this request (CSRF protection); single use.
    state = secrets.token_urlsafe(32)
    await request.app.state.redis.set(_state_key(state), 1, ex=STATE_TTL_SECONDS)
    return RedirectResponse(oauth.auth_url(state))


@router.get("/gdrive/callback")
async def gdrive_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Google redirects here after consent. Store the encrypted refresh token."""
    oauth: GoogleOAuth = request.app.state.google
    redis = request.app.state.redis

    if error or not code or not state:
        return RedirectResponse(AFTER_CONNECT + "declined")
    if not await redis.delete(_state_key(state)):  # unknown, expired or already used
        return RedirectResponse(AFTER_CONNECT + "expired")

    try:
        tokens = await oauth.exchange_code(code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise GoogleError("Google didn't return a refresh token")
        email = await DriveClient(oauth, refresh_token).account_email()
    except GoogleError:
        log.exception("connecting Google Drive failed")
        return RedirectResponse(AFTER_CONNECT + "failed")

    credentials = request.app.state.cipher.encrypt({"refresh_token": refresh_token})
    async with write_conn() as conn:
        await conn.execute(
            text("""
            INSERT INTO sources (kind, name, credentials) VALUES ('gdrive', :name, :credentials)
            ON CONFLICT (kind, name) DO UPDATE SET
                credentials = EXCLUDED.credentials, config = sources.config - 'error'
            """),
            {"name": email, "credentials": credentials},
        )
    log.info("connected Google Drive account %s", email)
    return RedirectResponse(AFTER_CONNECT + "connected")


@router.delete("/{source_id}", status_code=204)
async def disconnect(request: Request, source_id: int) -> None:
    """Remove a Google Drive account, its stored token and its documents from the index."""
    async with write_conn() as conn:
        deleted = await conn.execute(
            text("DELETE FROM sources WHERE id = :id AND kind = 'gdrive' RETURNING id"),
            {"id": source_id},
        )
        if deleted.first() is None:
            raise HTTPException(status_code=404, detail="No such Google Drive account")
    await publish_changes(request.app.state.redis)  # retire cached results from that account
