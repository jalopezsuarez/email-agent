"""OpenAI provider."""
from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        embedding_model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ):
        if not api_key:
            raise LLMError("OPENAI_API_KEY missing")
        self.api_key = api_key
        self.model = model
        self.embedding_model = embedding_model
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            base_url=base_url.rstrip("/"),
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
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self._client.post("/chat/completions", json=body)
        if r.status_code >= 400:
            raise LLMError(f"OpenAI {r.status_code}: {r.text[:400]}")
        return r.json()["choices"][0]["message"]["content"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = self._client.post(
            "/embeddings",
            json={"model": self.embedding_model, "input": texts},
        )
        if r.status_code >= 400:
            raise LLMError(f"OpenAI embed {r.status_code}: {r.text[:400]}")
        return [item["embedding"] for item in r.json()["data"]]
