"""Chat model providers behind one streaming interface, chosen by LLM_PROVIDER.

Ollama (local, default), OpenAI and Anthropic (your own key), and a deterministic
fake for tests. Each yields the answer as text pieces while the model writes it.
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class LLMProvider(Protocol):
    model: str

    def stream(self, system: str, user: str) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...


class LLMError(RuntimeError):
    """The model call failed; the message is safe to show to the user."""


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield the `data:` payloads of a server-sent-events response."""
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()


class OllamaLLM:
    def __init__(self, settings: Settings):
        self.model = settings.llm_model
        self.client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=300)

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.1},
        }
        try:
            async with self.client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code == 404:
                    raise LLMError(
                        f"The model '{self.model}' isn't downloaded yet. "
                        "It is pulled on first start; check `docker compose logs ollama-pull`."
                    )
                response.raise_for_status()
                async for line in response.aiter_lines():  # newline-delimited JSON
                    if not line:
                        continue
                    event = json.loads(line)
                    if piece := event.get("message", {}).get("content"):
                        yield piece
                    if event.get("done"):
                        break
        except httpx.TransportError as exc:
            raise LLMError("The local model service (Ollama) isn't reachable.") from exc

    async def close(self) -> None:
        await self.client.aclose()


class OpenAILLM:
    def __init__(self, settings: Settings):
        self.model = settings.llm_model
        self.client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=120,
        )

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            async with self.client.stream("POST", "/chat/completions", json=body) as response:
                if response.is_error:
                    raise LLMError(f"OpenAI returned an error ({response.status_code}).")
                async for data in _sse_data(response):
                    if data == "[DONE]":
                        break
                    choices = json.loads(data).get("choices") or [{}]
                    if piece := choices[0].get("delta", {}).get("content"):
                        yield piece
        except httpx.TransportError as exc:
            raise LLMError("OpenAI isn't reachable.") from exc

    async def close(self) -> None:
        await self.client.aclose()


class AnthropicLLM:
    def __init__(self, settings: Settings):
        self.model = settings.llm_model
        self.client = httpx.AsyncClient(
            base_url="https://api.anthropic.com/v1",
            headers={
                "x-api-key": settings.anthropic_api_key or "",
                "anthropic-version": "2023-06-01",
            },
            timeout=120,
        )

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "max_tokens": 1024,
            "stream": True,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            async with self.client.stream("POST", "/messages", json=body) as response:
                if response.is_error:
                    raise LLMError(f"Anthropic returned an error ({response.status_code}).")
                async for data in _sse_data(response):
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        if piece := event.get("delta", {}).get("text"):
                            yield piece
                    elif event.get("type") == "message_stop":
                        break
        except httpx.TransportError as exc:
            raise LLMError("Anthropic isn't reachable.") from exc

    async def close(self) -> None:
        await self.client.aclose()


class FakeLLM:
    """Answers with the first sentence of source [1], cited. No model, no network."""

    model = "fake-llm"
    _first_source = re.compile(r"\[1\][^\n]*\n(.+?(?:[.!?](?=\s)|$))", re.DOTALL)

    async def stream(self, system: str, user: str) -> AsyncIterator[str]:
        match = self._first_source.search(user)
        if not match:
            yield "I couldn't find this in your documents."
            return
        for word in f"{' '.join(match.group(1).split())} [1]".split(" "):
            yield word + " "

    async def close(self) -> None:
        return None


def make_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai":
        return OpenAILLM(settings)
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings)
    if settings.llm_provider == "fake":
        return FakeLLM()
    return OllamaLLM(settings)
