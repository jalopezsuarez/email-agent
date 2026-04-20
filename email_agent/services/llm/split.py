"""Composite provider: one model for text generation, another for embeddings."""
from __future__ import annotations

from .base import LLMProvider


class SplitProvider(LLMProvider):
    def __init__(self, completion: LLMProvider, embeddings: LLMProvider):
        self.completion = completion
        self.embeddings = embeddings
        self.name = (
            completion.name
            if completion.name == embeddings.name
            else f"{completion.name}/{embeddings.name}-embed"
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
        return self.completion.complete(
            system,
            user,
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embeddings.embed(texts)

    def health(self) -> dict:
        return {
            "provider": self.name,
            "completion_provider": self.completion.name,
            "embedding_provider": self.embeddings.name,
            "ok": True,
        }
