"""EmailResponderAgent.

Drafts reply bodies that imitate the user's own style, learned from their
Sent Items. It never sends — drafts are stored in the 'Atendidos IA' folder
for the user to review and send manually from Outlook.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..services.graph_client import GraphClient
from ..services.llm import LLMProvider
from ..services.sqlite_store import SQLiteStore
from ..services.vector_store import VectorStore

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are drafting an email reply in the user's own voice. You are given "
    "their writing style guide and a few of their recent authentic sent "
    "emails as reference. Match their greeting, sign-off, tone, language, "
    "formality, typical sentence length and punctuation style. "
    "IMPORTANT: You are drafting only. Never include phrases implying the "
    "mail was already sent. Output ONLY the HTML body of the reply, without "
    "preamble, subject line or explanations. Keep any previous quoted "
    "thread untouched — only write the new reply on top."
)


@dataclass
class DraftResult:
    graph_draft_id: str | None
    body_html: str
    used_samples: int
    reason: str


class ResponderAgent:
    def __init__(
        self,
        llm: LLMProvider,
        graph: GraphClient,
        sqlite: SQLiteStore,
        vectors: VectorStore,
        atendidos_folder_id: str,
        language_hint: str = "auto",
    ):
        self.llm = llm
        self.graph = graph
        self.sqlite = sqlite
        self.vectors = vectors
        self.atendidos_folder_id = atendidos_folder_id
        self.language_hint = language_hint

    # --------------------------------------------------------- learning
    def learn_from_sent_items(self, limit: int = 200) -> int:
        """Analyse the user's Sent Items and persist style fingerprints.

        Returns the number of new samples ingested.
        """
        messages = self.graph.list_sent(top=limit)
        new = 0
        for msg in messages:
            row = self._message_to_style_row(msg)
            if not row:
                continue
            sample_id = self.sqlite.insert_style_sample(row)
            if sample_id is None:
                continue  # already stored
            text = (
                f"To: {row['recipient']}\nSubject: {row['subject']}\n\n{row['body_text']}"
            )
            try:
                vec = self.llm.embed([text])[0]
            except Exception as exc:  # embedding failure shouldn't abort training
                log.warning("Embedding failed for style sample: %s", exc)
                continue
            self.vectors.add_style(
                sample_id=sample_id,
                recipient=row["recipient"] or "",
                recipient_domain=row["recipient_domain"] or "",
                text=row["body_text"] or "",
                vector=vec,
                tone_tag=row.get("tone_tag"),
            )
            new += 1
        return new

    # --------------------------------------------------------- drafting
    def draft_reply(self, email_row: dict) -> DraftResult:
        """Create a reply draft in Graph and move it to 'Atendidos IA'."""
        # Retrieve style exemplars: try recipient-specific, then global.
        query_text = (email_row.get("body_snippet") or email_row.get("subject") or "")[:1000]
        try:
            vec = self.llm.embed([query_text])[0]
        except Exception:
            vec = None
        samples: list[dict] = []
        if vec is not None:
            samples = self.vectors.nearest_style(
                vec, recipient=email_row.get("from_addr"), top_k=3
            )
        if not samples:
            samples = [
                {
                    "text": s["body_text"] or "",
                    "recipient": s["recipient"] or "",
                }
                for s in self.sqlite.list_style_samples_for(
                    email_row.get("from_addr"), limit=3
                )
            ]
        voice_profile = self._build_voice_profile()

        examples_text = "\n\n---\n\n".join(
            f"(Example to {s.get('recipient', '')})\n{s.get('text', '')[:800]}"
            for s in samples
        ) or "(no prior samples; mirror the original email's register)"

        user_prompt = (
            f"== Voice profile ==\n{voice_profile}\n\n"
            f"== Recent authentic samples ==\n{examples_text}\n\n"
            f"== Incoming email ==\n"
            f"From: {email_row.get('from_name')} <{email_row.get('from_addr')}>\n"
            f"Subject: {email_row.get('subject')}\n"
            f"Body:\n{email_row.get('body_snippet') or ''}\n\n"
            f"Language: {self.language_hint}.\n"
            "Write the reply HTML body now."
        )
        body_html = self.llm.complete(_SYSTEM, user_prompt, temperature=0.5, max_tokens=900)
        body_html = body_html.strip()
        if not body_html.lower().startswith("<"):
            body_html = f"<p>{body_html}</p>"

        # Create the draft in Outlook and move it to Atendidos IA.
        draft = self.graph.create_reply_draft(email_row["graph_id"], body_html)
        draft_id = draft.get("id")
        if draft_id and self.atendidos_folder_id:
            try:
                moved = self.graph.move_draft(draft_id, self.atendidos_folder_id)
                draft_id = moved.get("id", draft_id)
            except Exception as exc:
                log.warning("Could not move draft to Atendidos IA: %s", exc)
        return DraftResult(
            graph_draft_id=draft_id,
            body_html=body_html,
            used_samples=len(samples),
            reason="ok",
        )

    # --------------------------------------------------------- helpers
    def _build_voice_profile(self) -> str:
        profile = self.sqlite.style_profile(limit=30)
        if not profile["sample_count"]:
            return "No style samples yet. Be polite, concise and match the sender's register."
        profile_lines = []
        if profile["greetings"]:
            profile_lines.append("Typical greetings: " + ", ".join(profile["greetings"]))
        if profile["signoffs"]:
            profile_lines.append("Typical sign-offs: " + ", ".join(profile["signoffs"]))
        if profile["avg_word_count"] is not None:
            profile_lines.append(f"Average reply length: ~{int(profile['avg_word_count'])} words.")
        if profile["tone_tags"]:
            profile_lines.append("Typical tone tags: " + ", ".join(profile["tone_tags"]))
        return "\n".join(profile_lines)

    @staticmethod
    def _message_to_style_row(msg: dict) -> dict | None:
        to_list = msg.get("toRecipients") or []
        if not to_list:
            return None
        first = to_list[0].get("emailAddress", {})
        recipient = first.get("address") or ""
        if not recipient:
            return None
        domain = recipient.split("@", 1)[-1] if "@" in recipient else ""
        body = msg.get("body") or {}
        if body.get("contentType", "").lower() == "html":
            text = _html_to_text(body.get("content") or "")
        else:
            text = body.get("content") or msg.get("bodyPreview") or ""
        text = (text or "").strip()
        if not text:
            return None
        words = text.split()
        greeting = _extract_greeting(text)
        signoff = _extract_signoff(text)
        return {
            "graph_id": msg["id"],
            "sent_at": msg.get("sentDateTime"),
            "recipient": recipient,
            "recipient_domain": domain,
            "subject": msg.get("subject"),
            "body_text": text[:4000],
            "greeting": greeting,
            "signoff": signoff,
            "word_count": len(words),
            "tone_tag": None,
        }


def _html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text("\n", strip=True)
    except Exception:
        return html


def _extract_greeting(text: str) -> str | None:
    first_line = text.splitlines()[0].strip() if text else ""
    if not first_line:
        return None
    if re.match(
        r"^(hi|hey|hola|buenas|buenos d[ií]as|estimado|dear|hello)[\s,!]",
        first_line,
        re.IGNORECASE,
    ):
        return first_line[:60]
    return None


def _extract_signoff(text: str) -> str | None:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    tail = lines[-2] if len(lines) >= 2 else lines[-1]
    if re.match(
        r"^(saludos|un saludo|gracias|regards|best|cheers|cordialmente|atentamente)",
        tail,
        re.IGNORECASE,
    ):
        return tail[:60]
    return None
