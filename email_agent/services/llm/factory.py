"""Factory that builds an LLMProvider from AppConfig."""
from __future__ import annotations

from ...config import AppConfig
from .base import LLMError, LLMProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider


def build_provider(cfg: AppConfig) -> LLMProvider:
    name = cfg.llm_provider
    if name == "gemini":
        return GeminiProvider(
            api_key=cfg.get("llm", "gemini", "api_key", default=""),
            model=cfg.get("llm", "gemini", "model", default="gemini-1.5-flash"),
            embedding_model=cfg.get(
                "llm", "gemini", "embedding_model", default="text-embedding-004"
            ),
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=cfg.get("llm", "openai", "api_key", default=""),
            model=cfg.get("llm", "openai", "model", default="gpt-4o-mini"),
            embedding_model=cfg.get(
                "llm", "openai", "embedding_model", default="text-embedding-3-small"
            ),
        )
    if name == "ollama":
        return OllamaProvider(
            base_url=cfg.get("llm", "ollama", "base_url", default="http://localhost:11434"),
            model=cfg.get("llm", "ollama", "model", default="llama3"),
            embedding_model=cfg.get(
                "llm", "ollama", "embedding_model", default="nomic-embed-text"
            ),
        )
    raise LLMError(f"Unknown LLM provider: {name}")
