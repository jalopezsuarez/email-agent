"""Local Ollama provider."""
from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, embedding_model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embedding_model = embedding_model
        self._client = httpx.Client(timeout=timeout, base_url=self.base_url)

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
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            body["format"] = "json"
        r = self._client.post("/api/chat", json=body)
        if r.status_code >= 400:
            raise LLMError(f"Ollama {r.status_code}: {r.text[:400]}")
        return r.json()["message"]["content"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            r = self._client.post(
                "/api/embeddings", json={"model": self.embedding_model, "prompt": t}
            )
            if r.status_code >= 400:
                raise LLMError(f"Ollama embed {r.status_code}: {r.text[:400]}")
            out.append(r.json()["embedding"])
        return out
