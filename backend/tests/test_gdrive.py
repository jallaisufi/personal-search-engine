"""The Drive client against a fake Google (no network)."""

import httpx
import pytest

from app.connectors.gdrive import (
    GOOGLE_DOC,
    SCOPE,
    DriveClient,
    GoogleOAuth,
    ReconnectRequired,
)
from tests.fakes import FakeGoogle


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def oauth(google) -> GoogleOAuth:
    return GoogleOAuth(
        "client-id",
        "client-secret",
        "http://localhost:8080/api/sources/gdrive/callback",
        transport=httpx.MockTransport(google.handle),
    )


def test_auth_url_asks_for_read_only_offline_access(oauth):
    url = httpx.URL(oauth.auth_url("state-123"))
    assert url.host == "accounts.google.com"
    assert url.params["scope"] == SCOPE and SCOPE.endswith("drive.readonly")
    assert url.params["access_type"] == "offline"
    assert url.params["state"] == "state-123"


def test_not_configured_without_credentials():
    assert GoogleOAuth(None, None, "http://x").configured is False


async def test_code_exchange_returns_a_refresh_token(oauth):
    tokens = await oauth.exchange_code("auth-code")
    assert tokens["refresh_token"] == "refresh-token-1"


async def test_first_sync_lists_files_and_changes_report_only_what_changed(google, oauth):
    google.put("f1", "Lease.pdf", "application/pdf", b"%PDF lease")
    google.put("f2", "Trip plan", GOOGLE_DOC, b"# Trip\n\nFlight at 9am.")
    drive = DriveClient(oauth, "refresh-token-1")

    cursor = await drive.start_page_token()
    files = {f.id: f async for f in drive.list_files()}
    assert set(files) == {"f1", "f2"}
    assert files["f1"].content_hash == files["f1"].md5  # binary: checksum
    assert files["f2"].content_hash.startswith("modified:")  # Google Doc: no checksum

    assert await drive.changes(cursor) == ([], cursor)  # nothing new

    google.put("f2", "Trip plan", GOOGLE_DOC, b"# Trip\n\nFlight at 11am.")
    google.remove("f1")
    changes, next_cursor = await drive.changes(cursor)
    assert [(c.file_id, c.removed) for c in changes] == [("f2", False), ("f1", True)]
    assert next_cursor != cursor


async def test_download_exports_google_docs_and_reports_missing_files(google, oauth, tmp_path):
    google.put("doc", "Notes", GOOGLE_DOC, b"# Notes\n\nHello.")
    drive = DriveClient(oauth, "refresh-token-1")

    assert await drive.download("doc", GOOGLE_DOC, tmp_path / "doc.md") is True
    assert (tmp_path / "doc.md").read_bytes() == b"# Notes\n\nHello."
    assert "GET /drive/v3/files/doc/export" in google.requests

    assert await drive.download("gone", "application/pdf", tmp_path / "gone.pdf") is False


async def test_access_token_is_reused_and_refreshed_after_a_401(google, oauth):
    drive = DriveClient(oauth, "refresh-token-1")
    await drive.account_email()
    await drive.account_email()
    assert google.requests.count("POST /token") == 1  # cached between calls

    google.expire_access_tokens = True  # Google now rejects the first token
    assert await drive.account_email() == "me@example.com"
    assert google.requests.count("POST /token") == 2  # refreshed once, then retried


async def test_revoked_access_asks_the_user_to_reconnect(google, oauth):
    google.revoked = True
    with pytest.raises(ReconnectRequired):
        await DriveClient(oauth, "refresh-token-1").account_email()
