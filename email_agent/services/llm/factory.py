"""Factory that builds an LLMProvider from AppConfig."""
from __future__ import annotations

from ...config import AppConfig
from .base import LLMError, LLMProvider
from .claude import ClaudeProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .split import SplitProvider


def build_provider(cfg: AppConfig) -> LLMProvider:
    completion_name = _canonical_provider_name(cfg.llm_provider)
    embedding_name = _canonical_provider_name(
        str(cfg.get("llm", "embedding_provider", default=completion_name))
    )
    completion = _build_single_provider(cfg, completion_name)
    embeddings = _build_single_provider(cfg, embedding_name)
    if completion.name == embeddings.name and not completion.supports_embeddings:
        raise LLMError(
            f"{completion.name} does not provide embeddings. Set llm.embedding_provider "
            "to openai, gemini, or ollama."
        )
    if not embeddings.supports_embeddings:
        raise LLMError(f"{embeddings.name} cannot be used as an embedding provider.")
    return SplitProvider(completion=completion, embeddings=embeddings)


def _canonical_provider_name(name: str) -> str:
    lowered = str(name).strip().lower()
    if lowered == "anthropic":
        return "claude"
    return lowered


def _build_single_provider(cfg: AppConfig, name: str) -> LLMProvider:
    if name == "gemini":
        output_dim = cfg.get("llm", "gemini", "embedding_output_dimensions", default=None)
        return GeminiProvider(
            api_key=cfg.get("llm", "gemini", "api_key", default=""),
            base_url=cfg.get(
                "llm",
                "gemini",
                "base_url",
                default="https://generativelanguage.googleapis.com/v1beta",
            ),
            model=cfg.get("llm", "gemini", "model", default="gemini-2.5-flash"),
            embedding_model=cfg.get(
                "llm", "gemini", "embedding_model", default="gemini-embedding-001"
            ),
            embedding_output_dimensions=int(output_dim) if output_dim not in (None, "") else None,
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=cfg.get("llm", "openai", "api_key", default=""),
            base_url=cfg.get("llm", "openai", "base_url", default="https://api.openai.com/v1"),
            model=cfg.get("llm", "openai", "model", default="gpt-4o-mini"),
            embedding_model=cfg.get(
                "llm", "openai", "embedding_model", default="text-embedding-3-small"
            ),
        )
    if name == "claude":
        return ClaudeProvider(
            api_key=cfg.get("llm", "claude", "api_key", default=""),
            base_url=cfg.get("llm", "claude", "base_url", default="https://api.anthropic.com/v1"),
            api_version=cfg.get("llm", "claude", "api_version", default="2023-06-01"),
            model=cfg.get("llm", "claude", "model", default="claude-sonnet-4-20250514"),
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
