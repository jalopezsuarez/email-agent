from __future__ import annotations

from textwrap import dedent

import pytest

from email_agent.config import AppConfig
from email_agent.services.llm.base import LLMError
from email_agent.services.llm.factory import build_provider


@pytest.fixture(autouse=True)
def clear_service_env(monkeypatch):
    keys = [
        "SERVICE_LLM_PROVIDER",
        "SERVICE_LLM_BASEURL",
        "SERVICE_LLM_BASE_URL",
        "SERVICE_LLM_APIKEY",
        "SERVICE_LLM_API_KEY",
        "SERVICE_LLM_API_VERSION",
        "SERVICE_LLM_MODEL",
        "SERVICE_EMBEDDINGS_PROVIDER",
        "SERVICE_EMBEDDINGS_BASEURL",
        "SERVICE_EMBEDDINGS_BASE_URL",
        "SERVICE_EMBEDDINGS_APIKEY",
        "SERVICE_EMBEDDINGS_API_KEY",
        "SERVICE_EMBEDDINGS_MODEL",
        "SERVICE_EMBEDINGS_PROVIDER",
        "SERVICE_EMBEDINGS_BASEURL",
        "SERVICE_EMBEDINGS_BASE_URL",
        "SERVICE_EMBEDINGS_APIKEY",
        "SERVICE_EMBEDINGS_API_KEY",
        "SERVICE_EMBEDINGS_MODEL",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_build_provider_supports_split_claude_and_gemini(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        dedent(
            """
            llm:
              provider: claude
              embedding_provider: gemini
              claude:
                api_key: test-anthropic
                model: claude-sonnet-4-20250514
              gemini:
                api_key: test-gemini
                model: gemini-2.5-flash
                embedding_model: gemini-embedding-001
            """
        ),
        encoding="utf-8",
    )
    cfg = AppConfig.load(cfg_path)
    provider = build_provider(cfg)
    assert provider.name == "claude/gemini-embed"


def test_build_provider_rejects_claude_without_embedding_provider(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        dedent(
            """
            llm:
              provider: claude
              claude:
                api_key: test-anthropic
                model: claude-sonnet-4-20250514
            """
        ),
        encoding="utf-8",
    )
    cfg = AppConfig.load(cfg_path)
    with pytest.raises(LLMError, match="does not provide embeddings"):
        build_provider(cfg)


def test_build_provider_supports_generic_service_env_overrides(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        dedent(
            """
            llm:
              provider: claude
              embedding_provider: openai
              claude:
                api_key: ignored
                model: ignored
              openai:
                api_key: ignored
                embedding_model: ignored
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SERVICE_LLM_APIKEY", "test-anthropic")
    monkeypatch.setenv("SERVICE_LLM_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setenv("SERVICE_EMBEDDINGS_APIKEY", "test-openai")
    monkeypatch.setenv("SERVICE_EMBEDDINGS_MODEL", "text-embedding-3-small")

    cfg = AppConfig.load(cfg_path)
    provider = build_provider(cfg)
    assert provider.name == "claude/openai-embed"
