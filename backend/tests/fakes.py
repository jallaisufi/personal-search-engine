"""Small in-memory stand-ins for external services used in tests."""


class InMemoryRedis:
    """The subset of redis.asyncio.Redis the app uses. Values are stored as bytes, like Redis."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    @staticmethod
    def _bytes(value: object) -> bytes:
        return value if isinstance(value, bytes) else str(value).encode()

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        self.data[key] = self._bytes(value)
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def incr(self, key: str) -> int:
        value = int(self.data.get(key, b"0")) + 1
        self.data[key] = self._bytes(value)
        return value

    async def mget(self, *keys: str) -> list[bytes | None]:
        return [self.data.get(k) for k in keys]

    async def aclose(self) -> None:
        return None


class BrokenRedis:
    """Every call fails, like a Redis that is down."""

    def __getattr__(self, name: str):
        async def fail(*args: object, **kwargs: object) -> None:
            raise ConnectionError("redis is down")

        return fail


class FakeGoogle:
    """An in-memory Google: OAuth token endpoint and the parts of the Drive API the app uses.

    Use `httpx.MockTransport(fake.handle)` as the transport of a GoogleOAuth.
    """

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}  # id → {"meta": {...}, "content": bytes}
        self.change_log: list[dict] = []
        self.revoked = False
        self.expire_access_tokens = False  # answer 401 once, to exercise the refresh path
        self.requests: list[str] = []
        self._issued = 0

    # ── test helpers ─────────────────────────────────────────────────────
    def put(self, file_id: str, name: str, mime_type: str, content: bytes, **meta: object) -> None:
        """Create or update a file and record the change."""
        import hashlib

        version = len(self.change_log) + 1
        entry = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": f"2026-11-14T10:{version:02d}:00.000Z",
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            "size": str(len(content)),
            "trashed": False,
            **meta,
        }
        if not mime_type.startswith("application/vnd.google-apps"):
            entry["md5Checksum"] = hashlib.md5(content).hexdigest()  # noqa: S324
        self.files[file_id] = {"meta": entry, "content": content}
        self.change_log.append({"fileId": file_id, "removed": False, "file": entry})

    def touch(self, file_id: str) -> None:
        """A metadata-only change (opened, renamed): content and checksum stay the same."""
        self.change_log.append(
            {"fileId": file_id, "removed": False, "file": self.files[file_id]["meta"]}
        )

    def remove(self, file_id: str) -> None:
        del self.files[file_id]
        self.change_log.append({"fileId": file_id, "removed": True})

    # ── the fake server ──────────────────────────────────────────────────
    def handle(self, request):
        import httpx

        path = request.url.path
        self.requests.append(f"{request.method} {path}")

        if request.url.host == "oauth2.googleapis.com":
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self._issued += 1
            body = {"access_token": f"access-{self._issued}", "expires_in": 3600}
            if b"grant_type=authorization_code" in request.content:
                body["refresh_token"] = "refresh-token-1"
            return httpx.Response(200, json=body)

        token = request.headers.get("Authorization", "")
        if self.expire_access_tokens and token == "Bearer access-1":
            return httpx.Response(401, json={"error": "expired"})
        if not token.startswith("Bearer access-"):
            return httpx.Response(401, json={"error": "unauthorized"})

        params = request.url.params
        if path == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": "me@example.com"}})
        if path == "/drive/v3/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.change_log))})
        if path == "/drive/v3/changes":
            start = int(params["pageToken"])
            return httpx.Response(
                200,
                json={
                    "changes": self.change_log[start:],
                    "newStartPageToken": str(len(self.change_log)),
                },
            )
        if path == "/drive/v3/files":
            files = [f["meta"] for f in self.files.values() if not f["meta"]["trashed"]]
            return httpx.Response(200, json={"files": files})
        if path.startswith("/drive/v3/files/"):
            file_id = path.split("/")[4]
            if file_id not in self.files:
                return httpx.Response(404, json={"error": "notFound"})
            return httpx.Response(200, content=self.files[file_id]["content"])
        return httpx.Response(404, json={"error": f"unexpected request {path}"})
