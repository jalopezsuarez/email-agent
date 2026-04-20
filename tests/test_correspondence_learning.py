from __future__ import annotations

from email_agent.agents.coordinator import CoordinatorAgent
from email_agent.services.llm.base import LLMProvider
from email_agent.services.sqlite_store import SQLiteStore


def _style_row(
    graph_id: str,
    recipient: str,
    sent_at: str,
    subject: str,
    body_text: str,
    *,
    greeting: str | None = None,
    signoff: str | None = None,
    tone_tag: str | None = None,
    word_count: int | None = None,
) -> dict:
    return {
        "graph_id": graph_id,
        "sent_at": sent_at,
        "recipient": recipient,
        "recipient_domain": recipient.split("@", 1)[-1],
        "subject": subject,
        "body_text": body_text,
        "greeting": greeting,
        "signoff": signoff,
        "word_count": word_count if word_count is not None else len(body_text.split()),
        "tone_tag": tone_tag,
    }


class StubLLM(LLMProvider):
    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        self.calls.append((system, user))
        return '{"category": "personal", "confidence": 0.92, "reason": "reply history"}'

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_correspondence_reference_prefers_exact_sender_history(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    store.insert_style_sample(
        _style_row(
            "1",
            "ana@example.com",
            "2026-04-18T09:00:00+00:00",
            "Cena",
            "Hola Ana,\nClaro, quedamos manana.\nUn abrazo",
            greeting="Hola Ana,",
            signoff="Un abrazo",
            tone_tag="warm",
        )
    )
    store.insert_style_sample(
        _style_row(
            "2",
            "ana@example.com",
            "2026-04-19T09:00:00+00:00",
            "Plan",
            "Hola Ana,\nTe confirmo luego.\nGracias",
            greeting="Hola Ana,",
            signoff="Gracias",
            tone_tag="friendly",
        )
    )
    store.insert_style_sample(
        _style_row(
            "3",
            "ventas@example.com",
            "2026-04-17T09:00:00+00:00",
            "Factura",
            "Buenos dias,\nRecibido.\nGracias",
            greeting="Buenos dias,",
            signoff="Gracias",
            tone_tag="formal",
        )
    )

    reference = store.correspondence_reference("ana@example.com", limit=2)

    assert reference["match_scope"] == "exact"
    assert reference["exact_reply_count"] == 2
    assert reference["domain_reply_count"] == 3
    assert reference["recent_subjects"] == ["Plan", "Cena"]
    assert reference["greetings"] == ["Hola Ana,"]
    assert reference["sample_recipients"] == ["ana@example.com"]
    assert len(reference["example_replies"]) == 2


def test_correspondence_reference_falls_back_to_domain_history(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    store.insert_style_sample(
        _style_row(
            "1",
            "maria@acme.com",
            "2026-04-18T09:00:00+00:00",
            "Kickoff",
            "Hola Maria,\nPerfecto.\nGracias",
        )
    )
    store.insert_style_sample(
        _style_row(
            "2",
            "soporte@acme.com",
            "2026-04-19T09:00:00+00:00",
            "Ticket",
            "Buenas,\nLo reviso.\nUn saludo",
        )
    )

    reference = store.correspondence_reference("nuevo@acme.com", limit=2)

    assert reference["match_scope"] == "domain"
    assert reference["exact_reply_count"] == 0
    assert reference["domain_reply_count"] == 2
    assert reference["sample_recipients"] == ["soporte@acme.com", "maria@acme.com"]


def test_coordinator_uses_sent_history_as_personal_reference(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    store.insert_style_sample(
        _style_row(
            "1",
            "ana@example.com",
            "2026-04-19T09:00:00+00:00",
            "Plan",
            "Hola Ana,\nTe confirmo luego.\nGracias",
            greeting="Hola Ana,",
            signoff="Gracias",
            tone_tag="friendly",
        )
    )
    llm = StubLLM()
    coordinator = CoordinatorAgent(
        llm=llm,
        graph=object(),
        sqlite=store,
        classifier=object(),
        responder=object(),
    )

    category, confidence, reason = coordinator._categorise(
        {
            "subject": "Cena este viernes",
            "from_name": "Ana",
            "from_addr": "ANA@EXAMPLE.COM",
            "body_snippet": "Te apetece que cenemos el viernes?",
        }
    )

    assert category == "personal"
    assert confidence == 0.92
    assert reason == "reply history"
    _, prompt = llm.calls[0]
    assert "User reply style baseline" in prompt
    assert "Historical outgoing reply reference" in prompt
    assert "Exact replies to this sender: 1" in prompt
    assert "Hola Ana," in prompt
    assert "Te confirmo luego." in prompt
