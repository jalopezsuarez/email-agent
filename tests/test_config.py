from email_agent.config import AppConfig


def test_config_loads_and_exposes_defaults():
    cfg = AppConfig.load("config.yaml")
    assert cfg.llm_provider in {"gemini", "openai", "claude", "ollama"}
    assert cfg.port >= 1
    assert cfg.sqlite_path.endswith(".db")
    assert "lancedb" in cfg.lancedb_path


def test_scopes_block_mail_send_even_if_injected(tmp_path, monkeypatch):
    # If a user edits config.yaml to add Mail.Send, the GraphClient strips it.
    from email_agent.services.graph_client import GraphClient
    import email_agent.services.graph_client as graph_client_module

    class DummyPublicClientApplication:
        def __init__(self, *args, **kwargs):
            pass

        def get_accounts(self):
            return []

        def acquire_token_silent(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        graph_client_module.msal,
        "PublicClientApplication",
        DummyPublicClientApplication,
    )

    cache = tmp_path / "cache.bin"
    client = GraphClient(
        client_id="fake-client-id",
        tenant="consumers",
        scopes=["Mail.ReadWrite", "Mail.Send", "offline_access"],
        token_cache_path=str(cache),
    )
    assert "Mail.Send" not in client._scopes
    assert "mail.send" not in [s.lower() for s in client._scopes]
