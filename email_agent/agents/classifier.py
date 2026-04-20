"""EmailClassifierAgent.

Classifies inbox messages into one of the user's existing mail folders.

Signals combined
----------------
1. Semantic similarity between the message and the folder embeddings stored
   in LanceDB (built from folder names and any accumulated feedback).
2. An LLM ranker that, given the top-K candidate folders, picks one and
   self-reports a confidence score in [0,1].

The final confidence is ``0.5 * llm_confidence + 0.5 * vector_similarity``.
If ``final < threshold`` the message is flagged as ``pending_review`` for
human training via the web panel. Default threshold is deliberately high
(0.9); operators relax it from the panel as trust grows.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..services.llm import LLMProvider
from ..services.sqlite_store import SQLiteStore
from ..services.vector_store import VectorStore

log = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    folder_id: str | None
    folder_name: str | None
    confidence: float
    reason: str
    candidates: list[dict]
    auto_applied: bool


_SYSTEM = (
    "You are an email triage assistant. Given an email and a shortlist of "
    "candidate folders from the user's mailbox, you pick the single best "
    "destination folder. If none of the candidates fit well, return "
    '{"folder_id": null, "folder_name": null, "confidence": 0.0, '
    '"reason": "no good match"}. '
    'Otherwise return JSON: {"folder_id": "...", "folder_name": "...", '
    '"confidence": 0.0-1.0, "reason": "short explanation"}.'
)


class ClassifierAgent:
    def __init__(
        self,
        llm: LLMProvider,
        sqlite: SQLiteStore,
        vectors: VectorStore,
        threshold: float = 0.9,
        top_k: int = 6,
    ):
        self.llm = llm
        self.sqlite = sqlite
        self.vectors = vectors
        self.threshold = threshold
        self.top_k = top_k
        self._folder_cache: list[dict] = []

    # ---------------------------------------------------- training
    def seed_folders(self, folders: list[dict]) -> None:
        """Ingest folder names so the vector store has something to match against.

        Called once per startup (or after the user adds folders). Feedback
        samples accumulate on top with higher weight.
        """
        self._folder_cache = folders
        texts = [f"Folder: {f['full_name']}" for f in folders]
        if not texts:
            return
        vectors = self.llm.embed(texts)
        for f, vec, text in zip(folders, vectors, texts):
            if f.get("well_known_name") in {"inbox", "sentitems", "drafts", "deleteditems"}:
                continue
            self.vectors.add_folder_example(
                folder_id=f["id"],
                folder_name=f["full_name"],
                text=text,
                vector=vec,
                source="name",
                weight=0.5,
            )

    def ingest_feedback(
        self,
        folder_id: str,
        folder_name: str,
        text: str,
    ) -> None:
        """Push a human-confirmed example into the vector store with high weight."""
        vec = self.llm.embed([text])[0]
        self.vectors.add_folder_example(
            folder_id=folder_id,
            folder_name=folder_name,
            text=text,
            vector=vec,
            source="feedback",
            weight=2.0,
        )

    # ---------------------------------------------------- inference
    def classify(self, email: dict) -> ClassificationResult:
        """Return classification decision for the given email dict.

        ``email`` is expected to be the row from SQLite (already persisted).
        """
        text = self._email_to_text(email)
        vec = self.llm.embed([text])[0]
        candidates = self.vectors.nearest_folders(vec, top_k=self.top_k)
        if not candidates:
            return ClassificationResult(
                folder_id=None,
                folder_name=None,
                confidence=0.0,
                reason="vector store has no folder examples yet",
                candidates=[],
                auto_applied=False,
            )

        # Ask the LLM to rank the shortlist.
        candidate_text = "\n".join(
            f"- {c['folder_name']} (id={c['folder_id']}, score={c['score']:.2f})"
            for c in candidates
        )
        user_prompt = (
            f"Email subject: {email.get('subject') or '(no subject)'}\n"
            f"From: {email.get('from_name')} <{email.get('from_addr')}>\n"
            f"Body preview:\n{email.get('body_snippet') or ''}\n\n"
            f"Candidate folders:\n{candidate_text}\n\n"
            "Respond ONLY with the JSON object described in the system prompt."
        )
        raw = self.llm.complete(_SYSTEM, user_prompt, json_mode=True, temperature=0.1)
        llm_choice = _parse_json(raw)
        llm_folder_id = llm_choice.get("folder_id")
        llm_conf = float(llm_choice.get("confidence") or 0.0)
        reason = str(llm_choice.get("reason") or "")

        top = candidates[0]
        # Vector-based confidence is the raw top similarity clamped to [0,1].
        vec_conf = max(0.0, min(1.0, float(top["score"])))

        if llm_folder_id and any(c["folder_id"] == llm_folder_id for c in candidates):
            folder = next(c for c in candidates if c["folder_id"] == llm_folder_id)
            final = 0.5 * llm_conf + 0.5 * vec_conf
        else:
            # LLM declined or suggested unknown -> lean on vector with low llm weight.
            folder = top
            final = 0.3 * llm_conf + 0.4 * vec_conf
            reason = reason or "llm declined; falling back to top vector match"

        auto = final >= self.threshold
        return ClassificationResult(
            folder_id=folder["folder_id"],
            folder_name=folder["folder_name"],
            confidence=round(final, 3),
            reason=reason,
            candidates=candidates,
            auto_applied=auto,
        )

    # ---------------------------------------------------- helpers
    @staticmethod
    def _email_to_text(email: dict) -> str:
        parts = [
            f"Subject: {email.get('subject') or ''}",
            f"From: {email.get('from_name') or ''} <{email.get('from_addr') or ''}>",
            (email.get("body_snippet") or "")[:2000],
        ]
        return "\n".join(parts)


def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract a JSON object from surrounding noise.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        log.warning("Classifier: could not parse LLM JSON: %s", raw[:200])
        return {}
