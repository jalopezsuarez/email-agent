from __future__ import annotations

from textwrap import dedent

import pytest

from email_agent.config import AppConfig
from email_agent.services.llm.base import LLMError
from email_agent.services.llm.factory import build_provider


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
