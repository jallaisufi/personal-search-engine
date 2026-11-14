"""Google Drive connector: OAuth, incremental change listing, downloads.

Read-only by design: the only scope requested is drive.readonly, and nothing here
writes to Drive. Sync is incremental: after the first full listing, only the
Changes API is called, which returns just the files that changed since the saved
page token.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"

GOOGLE_DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Drive MIME type → file suffix the extractor understands.
SUPPORTED = {
    "application/pdf": ".pdf",
    DOCX: ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    GOOGLE_DOC: ".md",  # exported as Markdown, which keeps the headings
}
FILE_FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,webViewLink,size,trashed"


class GoogleError(RuntimeError):
    pass


class ReconnectRequired(GoogleError):
    """Google no longer accepts the stored refresh token; the user has to connect again."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str | None
    md5: str | None
    web_url: str | None
    size: int | None
    trashed: bool

    @classmethod
    def from_api(cls, data: dict) -> "DriveFile":
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            mime_type=data.get("mimeType", ""),
            modified_time=data.get("modifiedTime"),
            md5=data.get("md5Checksum"),
            web_url=data.get("webViewLink"),
            size=int(data["size"]) if data.get("size") is not None else None,
            trashed=bool(data.get("trashed", False)),
        )

    @property
    def supported(self) -> bool:
        return self.mime_type in SUPPORTED

    @property
    def content_hash(self) -> str:
        # Binary files have an md5; Google Docs don't, so their modified time stands in.
        return self.md5 or f"modified:{self.modified_time}"


@dataclass(frozen=True)
class Change:
    file_id: str
    removed: bool
    file: DriveFile | None


class GoogleOAuth:
    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = httpx.AsyncClient(transport=transport, timeout=60)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def auth_url(self, state: str) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # ...and get one even when reconnecting
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(query)}"

    async def _token_request(self, form: dict[str, str]) -> dict:
        form = {
            **form,
            "client_id": self.client_id or "",
            "client_secret": self.client_secret or "",
        }
        try:
            response = await self.http.post(TOKEN_URL, data=form)
        except httpx.TransportError as exc:
            raise GoogleError("Google's sign-in service isn't reachable.") from exc
        if response.status_code == 400 and response.json().get("error") == "invalid_grant":
            raise ReconnectRequired(
                "Google no longer accepts the saved sign-in. Connect Google Drive again."
            )
        if response.is_error:
            raise GoogleError(f"Google sign-in failed ({response.status_code}).")
        return response.json()

    async def exchange_code(self, code: str) -> dict:
        return await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        )

    async def refresh(self, refresh_token: str) -> tuple[str, float]:
        """A fresh access token and the time (monotonic) at which to stop using it."""
        data = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        return data["access_token"], time.monotonic() + int(data.get("expires_in", 3600)) - 60

    async def close(self) -> None:
        await self.http.aclose()


class DriveClient:
    """Read-only Drive API calls for one connected account."""

    def __init__(self, oauth: GoogleOAuth, refresh_token: str):
        self.oauth = oauth
        self.refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def _token(self, force: bool = False) -> str:
        if force or self._access_token is None or time.monotonic() >= self._expires_at:
            self._access_token, self._expires_at = await self.oauth.refresh(self.refresh_token)
        return self._access_token

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with auth. Refreshes the token once on 401; backs off on rate limits and 5xx."""
        refreshed = False
        delay = 1.0
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            try:
                response = await self.oauth.http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise GoogleError("Google Drive isn't reachable.") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self._token(force=True)  # expired early or revoked: refresh, retry once
                continue
            if response.status_code in (429, 500, 502, 503) and attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return response
        raise GoogleError("Google Drive kept failing; will try again on the next sync.")

    async def _json(self, url: str, params: dict | None = None) -> dict:
        response = await self._get(url, params)
        if response.is_error:
            raise GoogleError(f"Google Drive returned an error ({response.status_code}).")
        return response.json()

    async def account_email(self) -> str:
        data = await self._json(f"{API}/about", {"fields": "user(emailAddress)"})
        return data["user"]["emailAddress"]

    async def start_page_token(self) -> str:
        return (await self._json(f"{API}/changes/startPageToken"))["startPageToken"]

    async def list_files(self) -> AsyncIterator[DriveFile]:
        """Every supported, non-trashed file. Used once, for the first sync."""
        types = " or ".join(f"mimeType = '{mime}'" for mime in SUPPORTED)
        params = {
            "q": f"trashed = false and ({types})",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": 200,
        }
        while True:
            data = await self._json(f"{API}/files", params)
            for item in data.get("files", []):
                yield DriveFile.from_api(item)
            if not data.get("nextPageToken"):
                return
            params = {**params, "pageToken": data["nextPageToken"]}

    async def changes(self, page_token: str) -> tuple[list[Change], str]:
        """Everything that changed since `page_token`, and the token for next time."""
        changes: list[Change] = []
        params = {
            "pageToken": page_token,
            "fields": "nextPageToken,newStartPageToken,"
            f"changes(fileId,removed,file({FILE_FIELDS}))",
            "pageSize": 200,
            "includeRemoved": "true",
        }
        while True:
            data = await self._json(f"{API}/changes", params)
            for item in data.get("changes", []):
                file = DriveFile.from_api(item["file"]) if item.get("file") else None
                changes.append(Change(item["fileId"], bool(item.get("removed")), file))
            if data.get("newStartPageToken"):
                return changes, data["newStartPageToken"]
            params = {**params, "pageToken": data["nextPageToken"]}

    async def download(self, file_id: str, mime_type: str, dest: Path) -> bool:
        """Save the file's content to `dest`. Returns False if it no longer exists."""
        if mime_type == GOOGLE_DOC:
            response = await self._get(
                f"{API}/files/{file_id}/export", {"mimeType": "text/markdown"}
            )
        else:
            response = await self._get(f"{API}/files/{file_id}", {"alt": "media"})
        if response.status_code == 404:
            return False
        if response.is_error:
            raise GoogleError(f"Downloading from Google Drive failed ({response.status_code}).")
        await asyncio.to_thread(dest.write_bytes, response.content)
        return True
