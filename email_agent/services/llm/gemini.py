"""Gemini (Google Generative Language API) provider — free tier friendly."""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import LLMError, LLMProvider

_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, embedding_model: str, timeout: float = 60.0):
        if not api_key:
            raise LLMError("GEMINI_API_KEY missing")
        self.api_key = api_key
        self.model = model
        self.embedding_model = embedding_model
        self._client = httpx.Client(timeout=timeout)

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        url = f"{_BASE}/models/{self.model}:generateContent"
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        r = self._client.post(url, params={"key": self.api_key}, json=body)
        if r.status_code >= 400:
            raise LLMError(f"Gemini {r.status_code}: {r.text[:400]}")
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected Gemini response: {json.dumps(data)[:400]}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Gemini embedContent supports one text per call. Free-tier: batch sequentially.
        out: list[list[float]] = []
        url = f"{_BASE}/models/{self.embedding_model}:embedContent"
        for t in texts:
            body = {"content": {"parts": [{"text": t}]}}
            r = self._client.post(url, params={"key": self.api_key}, json=body)
            if r.status_code >= 400:
                raise LLMError(f"Gemini embed {r.status_code}: {r.text[:400]}")
            out.append(r.json()["embedding"]["values"])
        return out
