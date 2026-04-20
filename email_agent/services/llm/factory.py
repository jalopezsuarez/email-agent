"""Factory that builds an LLMProvider from AppConfig."""
from __future__ import annotations

import os

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
    completion = _build_single_provider(cfg, completion_name, role="llm")
    embeddings = _build_single_provider(cfg, embedding_name, role="embeddings")
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


def _build_single_provider(cfg: AppConfig, name: str, *, role: str) -> LLMProvider:
    if name == "gemini":
        output_dim = _provider_value(
            cfg, role, "gemini", "embedding_output_dimensions", default=None
        )
        return GeminiProvider(
            api_key=_provider_value(cfg, role, "gemini", "api_key", default=""),
            base_url=_provider_value(
                cfg,
                role,
                "gemini",
                "base_url",
                default="https://generativelanguage.googleapis.com/v1beta",
            ),
            model=_provider_value(cfg, "llm", "gemini", "model", default="gemini-2.5-flash"),
            embedding_model=_provider_value(
                cfg,
                "embeddings",
                "gemini",
                "embedding_model",
                default="gemini-embedding-001",
            ),
            embedding_output_dimensions=int(output_dim) if output_dim not in (None, "") else None,
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=_provider_value(cfg, role, "openai", "api_key", default=""),
            base_url=_provider_value(
                cfg, role, "openai", "base_url", default="https://api.openai.com/v1"
            ),
            model=_provider_value(cfg, "llm", "openai", "model", default="gpt-4o-mini"),
            embedding_model=_provider_value(
                cfg,
                "embeddings",
                "openai",
                "embedding_model",
                default="text-embedding-3-small",
            ),
        )
    if name == "claude":
        return ClaudeProvider(
            api_key=_provider_value(cfg, role, "claude", "api_key", default=""),
            base_url=_provider_value(
                cfg, role, "claude", "base_url", default="https://api.anthropic.com/v1"
            ),
            api_version=_provider_value(
                cfg, role, "claude", "api_version", default="2023-06-01"
            ),
            model=_provider_value(
                cfg, "llm", "claude", "model", default="claude-sonnet-4-20250514"
            ),
        )
    if name == "ollama":
        return OllamaProvider(
            base_url=_provider_value(
                cfg, role, "ollama", "base_url", default="http://localhost:11434"
            ),
            model=_provider_value(cfg, "llm", "ollama", "model", default="llama3"),
            embedding_model=_provider_value(
                cfg,
                "embeddings",
                "ollama",
                "embedding_model",
                default="nomic-embed-text",
            ),
        )
    raise LLMError(f"Unknown LLM provider: {name}")


def _provider_value(
    cfg: AppConfig,
    role: str,
    provider: str,
    field: str,
    *,
    default: str | None,
):
    env_key = _field_env_key(field, role)
    env_value = _role_env(role, env_key)
    if env_value not in (None, ""):
        return env_value
    return cfg.get("llm", provider, field, default=default)


def _field_env_key(field: str, role: str) -> str:
    if field == "api_key":
        return "api_key"
    if field == "base_url":
        return "base_url"
    if field == "model":
        return "model"
    if field == "embedding_model":
        return "model"
    if field == "api_version":
        return "api_version"
    if field == "embedding_output_dimensions":
        return "output_dimensions"
    return field


def _role_env(role: str, key: str) -> str | None:
    names: list[str]
    if role == "llm":
        names = [f"SERVICE_LLM_{key.upper()}", f"SERVICE_LLM_{key.upper().replace('_', '')}"]
    else:
        names = [
            f"SERVICE_EMBEDDINGS_{key.upper()}",
            f"SERVICE_EMBEDDINGS_{key.upper().replace('_', '')}",
            f"SERVICE_EMBEDINGS_{key.upper()}",
            f"SERVICE_EMBEDINGS_{key.upper().replace('_', '')}",
        ]
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return None
