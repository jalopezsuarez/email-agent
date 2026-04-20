"""Pin the anti-send guarantee.

These tests fail loudly if someone ever wires up a code path that could
send an email from this application.
"""
from __future__ import annotations

import inspect

import pytest

from email_agent.services import graph_client
from email_agent.services.safety import SendingForbiddenError, assert_safe_path


class TestForbiddenPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "/me/sendMail",
            "/me/messages/abc123/send",
            "/me/messages/abc/replyAll",
            "/me/messages/abc/forward",
            "https://graph.microsoft.com/v1.0/me/sendMail",
        ],
    )
    def test_assert_safe_path_blocks(self, path: str):
        with pytest.raises(SendingForbiddenError):
            assert_safe_path("POST", path)

    @pytest.mark.parametrize(
        "path",
        [
            "/me/messages/abc/createReply",
            "/me/messages/abc/move",
            "/me/mailFolders",
            "/me/mailFolders/Inbox/messages",
        ],
    )
    def test_assert_safe_path_allows_reads_and_drafts(self, path: str):
        # Should not raise.
        assert_safe_path("GET", path)
        assert_safe_path("POST", path)


class TestGraphClientAPI:
    def test_no_send_method_is_exposed(self):
        """GraphClient must not expose any send-shaped method."""
        forbidden_names = {"send", "send_mail", "send_message", "reply_and_send"}
        for name, _ in inspect.getmembers(graph_client.GraphClient, predicate=inspect.isfunction):
            assert name.lower() not in forbidden_names, (
                f"GraphClient exposes a send-shaped method '{name}' — forbidden."
            )

    def test_source_does_not_reference_sendmail(self):
        """No code path builds a sendMail URL."""
        src = inspect.getsource(graph_client)
        # The only mentions allowed are in the safety guard module itself.
        assert "sendMail" not in src, "graph_client must not reference sendMail."


class TestScopesExcludeMailSend:
    def test_default_scopes_do_not_include_mail_send(self):
        from email_agent.config import AppConfig
        cfg = AppConfig.load("config.yaml")
        scopes = cfg.get("graph", "scopes", default=[])
        lowered = [s.lower() for s in scopes]
        assert "mail.send" not in lowered, (
            "Mail.Send must never appear in configured scopes."
        )
