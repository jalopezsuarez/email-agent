"""Anthropic Claude provider."""
from __future__ import annotations

import json

import httpx

from .base import LLMError, LLMProvider

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_API_VERSION = "2023-06-01"


class ClaudeProvider(LLMProvider):
    name = "claude"
    supports_embeddings = False

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
        api_version: str = _DEFAULT_API_VERSION,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY missing")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(
            timeout=timeout,
            base_url=base_url.rstrip("/"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": api_version,
                "content-type": "application/json",
            },
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        if json_mode:
            system = (
                f"{system}\n\n"
                "Return a single valid JSON object and no surrounding prose or markdown."
            )
        body = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        r = self._client.post("/messages", json=body)
        if r.status_code >= 400:
            raise LLMError(f"Claude {r.status_code}: {r.text[:400]}")
        data = r.json()
        try:
            blocks = data["content"]
            return "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Unexpected Claude response: {json.dumps(data)[:400]}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise LLMError(
            "Claude does not provide embeddings. Configure llm.embedding_provider "
            "to openai, gemini, or ollama."
        )
