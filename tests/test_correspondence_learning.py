from __future__ import annotations

from email_agent.agents.classifier import ClassificationResult
from email_agent.agents.coordinator import CoordinatorAgent
from email_agent.agents.responder import DraftResult
from email_agent.agents.responder import ResponderAgent
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


class BrokenCategoriserLLM(StubLLM):
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
        return "not-json"


class StubGraph:
    def __init__(self, *, inbox_pages=None, sent_calls=None) -> None:
        self.inbox_pages = inbox_pages or []
        self.sent_calls = sent_calls or {}
        self.sent_requests: list[tuple[str | None, str | None, int]] = []
        self.list_inbox_messages: list[dict] = []
        self.move_result = {"id": "moved-1"}

    def iter_inbox(self, since_iso: str | None = None, page_size: int = 50):
        for page in self.inbox_pages:
            for msg in page:
                yield msg

    def list_inbox(self, since_iso: str | None = None, top: int = 25):
        return list(self.list_inbox_messages)

    def iter_sent(
        self,
        *,
        since_iso: str | None = None,
        before_iso: str | None = None,
        page_size: int = 100,
    ):
        self.sent_requests.append((since_iso, before_iso, page_size))
        for msg in self.sent_calls.get((since_iso, before_iso), []):
            yield msg

    def list_sent(self, top: int = 100):
        return []

    def move_message(self, message_id: str, destination_folder_id: str):
        return dict(self.move_result)


class StubVectors:
    def __init__(self) -> None:
        self.added_style: list[dict] = []

    def add_style(
        self,
        *,
        sample_id: int,
        recipient: str,
        recipient_domain: str,
        text: str,
        vector: list[float],
        tone_tag: str | None = None,
    ) -> None:
        self.added_style.append(
            {
                "sample_id": sample_id,
                "recipient": recipient,
                "recipient_domain": recipient_domain,
                "text": text,
                "tone_tag": tone_tag,
            }
        )


class StubClassifier:
    def classify(self, email: dict) -> ClassificationResult:
        return ClassificationResult(
            folder_id="folder-1",
            folder_name="Folder 1",
            confidence=0.95,
            reason="fit",
            candidates=[],
            auto_applied=True,
        )


class StubResponderDraft:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def draft_reply(self, email_row: dict) -> DraftResult:
        self.rows.append(dict(email_row))
        return DraftResult(
            graph_draft_id="draft-1",
            body_html="<p>reply</p>",
            used_samples=1,
            reason="ok",
        )


def _inbox_msg(graph_id: str) -> dict:
    return {
        "id": graph_id,
        "subject": "Test",
        "from": {"emailAddress": {"address": "sender@example.com", "name": "Sender"}},
        "toRecipients": [{"emailAddress": {"address": "user@example.com"}}],
        "bodyPreview": "Preview",
        "receivedDateTime": "2026-04-20T09:00:00Z",
        "parentFolderId": "inbox",
    }


def _sent_msg(graph_id: str, recipient: str, sent_at: str, subject: str) -> dict:
    return {
        "id": graph_id,
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": recipient}}],
        "sentDateTime": sent_at,
        "body": {"contentType": "text", "content": "Hola\nGracias"},
        "bodyPreview": "Hola Gracias",
        "conversationId": f"conv-{graph_id}",
    }


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


def test_coordinator_falls_back_to_personal_for_direct_short_human_request(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    coordinator = CoordinatorAgent(
        llm=BrokenCategoriserLLM(),
        graph=object(),
        sqlite=store,
        classifier=object(),
        responder=object(),
    )

    category, confidence, reason = coordinator._categorise(
        {
            "subject": "ayuda proveedor",
            "from_name": "Jose Antonio Lopez",
            "from_addr": "jalopezsuarez@gmail.com",
            "body_snippet": (
                "Me puedes ayudar con el tema del provedor de IA?\n"
                "Gracias\n"
                "Jose Antonio Lopez\n"
                "jalopezsuarez@gmail.com\n\n"
                "REMITENTE EXTERNO Este correo proviene desde una direccion externa."
            ),
        }
    )

    assert category == "personal"
    assert confidence >= 0.8
    assert "heuristic:" in reason


def test_coordinator_backfills_inbox_from_date_skipping_known_messages(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    store.upsert_email(
        {
            "graph_id": "known-1",
            "received_at": "2026-04-20T10:00:00Z",
            "subject": "Known",
            "from_addr": "known@example.com",
            "from_name": "Known",
            "to_addrs": ["user@example.com"],
            "body_snippet": "Known",
            "folder_id": "inbox",
        }
    )
    coordinator = CoordinatorAgent(
        llm=StubLLM(),
        graph=StubGraph(
            inbox_pages=[
                [_inbox_msg("known-1"), _inbox_msg("known-2")],
                [_inbox_msg("new-3"), _inbox_msg("new-4")],
            ]
        ),
        sqlite=store,
        classifier=object(),
        responder=object(),
        inbox_batch_size=2,
        inbox_from_iso="2026-04-19T00:00:00Z",
    )

    batch = coordinator._load_inbox_batch()

    assert [msg["id"] for msg in batch] == ["known-2", "new-3"]


def test_sent_learning_expands_backward_when_agent_sent_from_moves_earlier(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    store.insert_style_sample(
        _style_row(
            "existing-1",
            "ana@example.com",
            "2026-02-10T09:00:00Z",
            "Seguimiento",
            "Hola Ana,\nTe digo algo.\nGracias",
        )
    )
    store.insert_style_sample(
        _style_row(
            "existing-2",
            "ana@example.com",
            "2026-03-12T09:00:00Z",
            "Plan",
            "Hola Ana,\nTe confirmo luego.\nGracias",
        )
    )
    graph = StubGraph(
        sent_calls={
            ("2025-01-01T00:00:00Z", "2026-02-10T09:00:00Z"): [
                _sent_msg("older-1", "maria@example.com", "2025-11-05T09:00:00Z", "Historico")
            ],
            ("2026-03-12T09:00:00Z", None): [
                _sent_msg("existing-2", "ana@example.com", "2026-03-12T09:00:00Z", "Plan"),
                _sent_msg("newer-1", "ana@example.com", "2026-04-18T09:00:00Z", "Nuevo"),
            ],
        }
    )
    vectors = StubVectors()
    responder = ResponderAgent(
        llm=StubLLM(),
        graph=graph,
        sqlite=store,
        vectors=vectors,
        atendidos_folder_id="folder",
        sent_from_iso="2025-01-01T00:00:00Z",
    )

    learned = responder.learn_from_sent_items(limit=80)

    assert learned == 2
    assert graph.sent_requests == [
        ("2025-01-01T00:00:00Z", "2026-02-10T09:00:00Z", 80),
        ("2026-03-12T09:00:00Z", None, 80),
    ]
    assert {row["recipient"] for row in vectors.added_style} == {
        "maria@example.com",
        "ana@example.com",
    }


def test_coordinator_uses_new_graph_id_after_move_when_drafting(tmp_path):
    store = SQLiteStore(str(tmp_path / "agent.db"))
    graph = StubGraph()
    graph.list_inbox_messages = [_inbox_msg("old-graph-id")]
    graph.move_result = {"id": "new-graph-id"}
    responder = StubResponderDraft()
    coordinator = CoordinatorAgent(
        llm=StubLLM(),
        graph=graph,
        sqlite=store,
        classifier=StubClassifier(),
        responder=responder,
        inbox_batch_size=5,
    )

    summary = coordinator.run_cycle()

    assert summary["drafted"] == 1
    assert responder.rows[0]["graph_id"] == "new-graph-id"
    email = store.get_email(1)
    assert email is not None
    assert email["graph_id"] == "new-graph-id"


def test_responder_suggests_formal_tone_for_formal_sample(tmp_path):
    responder = ResponderAgent(
        llm=StubLLM(),
        graph=StubGraph(),
        sqlite=SQLiteStore(str(tmp_path / "agent.db")),
        vectors=StubVectors(),
        atendidos_folder_id="folder",
    )

    tone = responder.suggest_tone_tag(
        {
            "greeting": "Estimado Juan,",
            "signoff": "Atentamente",
            "body_text": "Estimado Juan,\nAdjunto la documentacion solicitada.\nQuedo a su disposicion.\nAtentamente",
            "word_count": 12,
        }
    )

    assert tone == "formal"
