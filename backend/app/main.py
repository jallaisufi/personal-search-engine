import logging
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from redis.asyncio import Redis

from app.api.deadlines import router as deadlines_router
from app.api.routes import router
from app.api.sources import router as sources_router
from app.cache import SearchCache
from app.config import get_settings
from app.connectors.gdrive import GoogleOAuth
from app.crypto import Cipher
from app.db import dispose_engines
from app.ingest.embed import make_provider
from app.llm import make_llm

HOSTNAME = socket.gethostname()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.embedder = make_provider(settings)
    app.state.llm = make_llm(settings)
    app.state.redis = Redis.from_url(settings.redis_url, socket_timeout=0.5)
    app.state.cache = SearchCache(app.state.redis, settings.search_cache_ttl_seconds)
    app.state.google = GoogleOAuth(
        settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri
    )
    app.state.cipher = Cipher(settings.encryption_key) if settings.encryption_key else None
    yield
    await app.state.embedder.close()
    await app.state.llm.close()
    await app.state.google.close()
    await app.state.redis.aclose()
    await dispose_engines()


app = FastAPI(
    title="Personal Search",
    version="0.1.0",
    description="Self-hosted hybrid search over your own documents.",
    lifespan=lifespan,
)


@app.middleware("http")
async def timing(request: Request, call_next) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Served-By"] = HOSTNAME  # which API replica answered
    return response


app.include_router(router)
app.include_router(sources_router)
app.include_router(deadlines_router)
