"""EmailCoordinatorAgent.

Orchestrates the Classifier and Responder. On each polling cycle it pulls
fresh inbox messages, persists them, asks the classifier what to do, and if
the email looks personal, asks the responder to draft a reply.

Sending emails is categorically disabled — the responder only creates
drafts in the 'Atendidos IA' folder.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr

from ..services.graph_client import GraphClient
from ..services.llm import LLMProvider
from ..services.sqlite_store import SQLiteStore
from .classifier import ClassifierAgent
from .responder import ResponderAgent

log = logging.getLogger(__name__)

_CATEGORY_SYSTEM = (
    "You are an email triage assistant. Decide the pragmatic category of an "
    "email and return JSON only: "
    '{"category": "personal|work|transactional|marketing|notification", '
    '"confidence": 0.0-1.0, "reason": "short"}. '
    "'personal' means a human being writing to the user individually and "
    "expecting a written reply (friends, family, direct 1:1 messages). "
    "Newsletters, receipts, automated alerts, calendar invites, job apps, "
    "and mass-marketing are NEVER personal."
)


class CoordinatorAgent:
    def __init__(
        self,
        llm: LLMProvider,
        graph: GraphClient,
        sqlite: SQLiteStore,
        classifier: ClassifierAgent,
        responder: ResponderAgent,
        *,
        inbox_batch_size: int = 25,
        personal_threshold: float = 0.8,
    ):
        self.llm = llm
        self.graph = graph
        self.sqlite = sqlite
        self.classifier = classifier
        self.responder = responder
        self.inbox_batch_size = inbox_batch_size
        self.personal_threshold = personal_threshold

    def run_cycle(self) -> dict:
        """One polling pass. Returns a summary dict for the panel."""
        since = self.sqlite.last_seen_received_at()
        messages = self.graph.list_inbox(since_iso=since, top=self.inbox_batch_size)
        processed = 0
        classified = 0
        pending = 0
        drafted = 0
        errors: list[str] = []
        for msg in messages:
            try:
                email_id = self._ingest(msg)
                if email_id is None:
                    continue
                email_row = self.sqlite.get_email(email_id)
                if email_row is None:
                    continue
                processed += 1
                # Step 1 — classify to a folder.
                result = self.classifier.classify(email_row)
                self.sqlite.log_decision(
                    email_id,
                    agent="classifier",
                    action="classify" if result.auto_applied else "flag_review",
                    target=result.folder_id,
                    confidence=result.confidence,
                    notes=result.reason,
                )
                if result.auto_applied and result.folder_id:
                    self.graph.move_message(msg["id"], result.folder_id)
                    self.sqlite.update_email(
                        email_id,
                        folder_id=result.folder_id,
                        folder_name=result.folder_name,
                        status="classified",
                        confidence=result.confidence,
                    )
                    classified += 1
                else:
                    self.sqlite.update_email(
                        email_id,
                        status="pending_review",
                        confidence=result.confidence,
                        folder_id=result.folder_id,
                        folder_name=result.folder_name,
                    )
                    pending += 1

                # Step 2 — personal? draft (but only if we already classified,
                # so a pending-review email is handled later by the user).
                if result.auto_applied:
                    category, cat_conf, cat_reason = self._categorise(email_row)
                    self.sqlite.update_email(email_id, category=category)
                    self.sqlite.log_decision(
                        email_id,
                        agent="coordinator",
                        action="categorise",
                        target=category,
                        confidence=cat_conf,
                        notes=cat_reason,
                    )
                    if category == "personal" and cat_conf >= self.personal_threshold:
                        try:
                            draft = self.responder.draft_reply(email_row)
                            self.sqlite.insert_draft(
                                email_id=email_id,
                                graph_draft_id=draft.graph_draft_id,
                                body_html=draft.body_html,
                            )
                            self.sqlite.update_email(email_id, status="drafted")
                            self.sqlite.log_decision(
                                email_id,
                                agent="responder",
                                action="draft",
                                target=draft.graph_draft_id,
                                confidence=cat_conf,
                                notes=f"used_samples={draft.used_samples}",
                            )
                            drafted += 1
                        except Exception as exc:
                            errors.append(f"draft failed for {email_id}: {exc}")
                            log.exception("draft_reply failed")
            except Exception as exc:  # keep cycle running despite per-message errors
                errors.append(str(exc))
                log.exception("Coordinator cycle error")
        return {
            "polled": len(messages),
            "processed": processed,
            "classified": classified,
            "pending_review": pending,
            "drafted": drafted,
            "errors": errors,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------ helpers
    def _ingest(self, msg: dict) -> int | None:
        from_field = (msg.get("from") or {}).get("emailAddress") or {}
        to_addrs = [
            (r.get("emailAddress") or {}).get("address")
            for r in msg.get("toRecipients") or []
        ]
        return self.sqlite.upsert_email(
            {
                "graph_id": msg["id"],
                "received_at": msg.get("receivedDateTime"),
                "subject": msg.get("subject"),
                "from_addr": from_field.get("address"),
                "from_name": from_field.get("name"),
                "to_addrs": [a for a in to_addrs if a],
                "body_snippet": msg.get("bodyPreview"),
                "folder_id": msg.get("parentFolderId"),
            }
        )

    def _categorise(self, email_row: dict) -> tuple[str, float, str]:
        user_prompt = (
            f"Subject: {email_row.get('subject') or ''}\n"
            f"From: {email_row.get('from_name')} <{email_row.get('from_addr')}>\n"
            f"Body preview:\n{email_row.get('body_snippet') or ''}\n\n"
            "Return the JSON described in the system prompt."
        )
        raw = self.llm.complete(_CATEGORY_SYSTEM, user_prompt, json_mode=True, temperature=0.0)
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw or "", re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        cat = (data.get("category") or "notification").lower()
        if cat not in {"personal", "work", "transactional", "marketing", "notification"}:
            cat = "notification"
        conf = float(data.get("confidence") or 0.0)
        reason = str(data.get("reason") or "")
        return cat, conf, reason
