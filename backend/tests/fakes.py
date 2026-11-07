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
