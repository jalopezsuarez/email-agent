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
from typing import Any

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
    "expecting a written reply, even if the topic is work-related "
    "(friends, family, direct 1:1 requests, a colleague or partner asking "
    "for help, clarification, or a decision). "
    "'work' means work-related email that is still operational rather than a "
    "clear direct-reply request, such as status updates, routed threads, FYIs, "
    "or team/process traffic. "
    "Newsletters, receipts, automated alerts, calendar invites, job apps, "
    "and mass-marketing are NEVER personal. "
    "When historical outgoing replies are provided, treat them as evidence of "
    "human correspondence and the user's reply style, but not as proof of "
    "'personal': repeated replies to coworkers, vendors or support addresses "
    "can still be 'work' or 'transactional'."
)
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "outlook.es",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "yahoo.com",
    "yahoo.es",
    "proton.me",
    "protonmail.com",
    "gmx.com",
}
_DIRECT_REQUEST_PATTERNS = [
    r"\bme puedes\b",
    r"\bpuedes ayudarme\b",
    r"\bme ayudas\b",
    r"\bpodrias\b",
    r"\bpuedes\b",
    r"\bcan you\b",
    r"\bcould you\b",
    r"\bneed your help\b",
    r"\bhelp me\b",
    r"\bgracias\b",
    r"\bthanks\b",
]
_AUTOMATED_PATTERNS = [
    r"\bnoreply\b",
    r"\bno-reply\b",
    r"\bdo not reply\b",
    r"\bmailer-daemon\b",
    r"\bautomatic(?:o|a)?\b",
    r"\bautogenerad[oa]\b",
    r"\bnewsletter\b",
    r"\bsuscrib",
    r"\brecibo\b",
    r"\bfactura\b",
    r"\bnotification\b",
    r"\bplanner\b",
    r"\btask assigned\b",
]
_EXTERNAL_BANNER_MARKERS = [
    "REMITENTE EXTERNO",
    "EXTERNAL SENDER",
]


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
        inbox_from_iso: str | None = None,
    ):
        self.llm = llm
        self.graph = graph
        self.sqlite = sqlite
        self.classifier = classifier
        self.responder = responder
        self.inbox_batch_size = inbox_batch_size
        self.personal_threshold = personal_threshold
        self.inbox_from_iso = inbox_from_iso

    def run_cycle(self) -> dict:
        """One polling pass. Returns a summary dict for the panel."""
        messages = self._load_inbox_batch()
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
                    moved_message = self.graph.move_message(msg["id"], result.folder_id)
                    moved_graph_id = moved_message.get("id") or msg["id"]
                    self.sqlite.update_email(
                        email_id,
                        graph_id=moved_graph_id,
                        folder_id=result.folder_id,
                        folder_name=result.folder_name,
                        status="classified",
                        confidence=result.confidence,
                    )
                    email_row["graph_id"] = moved_graph_id
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
    def _load_inbox_batch(self) -> list[dict]:
        if not self.inbox_from_iso:
            since = self.sqlite.last_seen_received_at()
            return self.graph.list_inbox(since_iso=since, top=self.inbox_batch_size)
        selected: list[dict] = []
        for msg in self.graph.iter_inbox(
            since_iso=self.inbox_from_iso,
            page_size=max(25, min(self.inbox_batch_size, 50)),
        ):
            if self.sqlite.has_email_graph_id(msg["id"]):
                continue
            selected.append(msg)
            if len(selected) >= self.inbox_batch_size:
                break
        return selected

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
        outbound_style = self.sqlite.style_profile(limit=20)
        correspondence = self.sqlite.correspondence_reference(email_row.get("from_addr"), limit=3)
        clean_body = self._clean_body_preview(email_row.get("body_snippet") or "")
        user_prompt = (
            "== User reply style baseline ==\n"
            f"{self._style_reference_text(outbound_style)}\n\n"
            "== Historical outgoing reply reference ==\n"
            f"{self._correspondence_reference_text(correspondence)}\n\n"
            "== Incoming email ==\n"
            f"Subject: {email_row.get('subject') or ''}\n"
            f"From: {email_row.get('from_name')} <{email_row.get('from_addr')}>\n"
            f"Body preview:\n{clean_body}\n\n"
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
        fallback = self._fallback_personal_category(
            email_row,
            clean_body=clean_body,
            correspondence=correspondence,
        )
        if fallback and (cat == "notification" and conf <= 0.05):
            return fallback
        return cat, conf, reason

    @staticmethod
    def _style_reference_text(profile: dict) -> str:
        if not profile.get("sample_count"):
            return "No Sent Items samples are available yet."
        lines = [f"Stored Sent Items samples: {profile['sample_count']}"]
        if profile.get("avg_word_count") is not None:
            lines.append(f"Typical reply length: ~{profile['avg_word_count']} words.")
        if profile.get("greetings"):
            lines.append("Common greetings: " + ", ".join(profile["greetings"]))
        if profile.get("signoffs"):
            lines.append("Common sign-offs: " + ", ".join(profile["signoffs"]))
        if profile.get("tone_tags"):
            lines.append("Tagged tones: " + ", ".join(profile["tone_tags"]))
        return "\n".join(lines)

    @staticmethod
    def _correspondence_reference_text(reference: dict) -> str:
        if reference.get("match_scope") == "none":
            return "No previous outgoing replies found for this sender or domain."
        lines = [
            f"Match scope: {reference['match_scope']}",
            f"Exact replies to this sender: {reference['exact_reply_count']}",
            f"Replies to this sender domain: {reference['domain_reply_count']}",
        ]
        if reference.get("last_replied_at"):
            lines.append(f"Last reply sent at: {reference['last_replied_at']}")
        if reference.get("avg_word_count") is not None:
            lines.append(
                f"Average length of matching replies: ~{reference['avg_word_count']} words."
            )
        if reference.get("greetings"):
            lines.append("Observed greetings: " + ", ".join(reference["greetings"]))
        if reference.get("signoffs"):
            lines.append("Observed sign-offs: " + ", ".join(reference["signoffs"]))
        if reference.get("tone_tags"):
            lines.append("Observed tone tags: " + ", ".join(reference["tone_tags"]))
        if reference.get("recent_subjects"):
            lines.append("Recent outbound subjects: " + " | ".join(reference["recent_subjects"]))
        if reference.get("sample_recipients") and reference.get("match_scope") == "domain":
            lines.append(
                "Known recipients in this domain: "
                + ", ".join(reference["sample_recipients"])
            )
        examples = reference.get("example_replies") or []
        if examples:
            rendered = "\n\n".join(
                (
                    f"(To {example.get('recipient') or 'unknown'} | "
                    f"{example.get('sent_at') or 'unknown date'} | "
                    f"Subject: {example.get('subject') or '(no subject)'})\n"
                    f"{CoordinatorAgent._trim_text(example.get('body_text') or '', 350)}"
                )
                for example in examples
            )
            lines.append("Example outgoing replies:\n" + rendered)
        return "\n".join(lines)

    @staticmethod
    def _trim_text(text: str, limit: int) -> str:
        clean = " ".join((text or "").split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3].rstrip() + "..."

    @staticmethod
    def _clean_body_preview(body: str) -> str:
        if not body:
            return ""
        cleaned = body
        for marker in _EXTERNAL_BANNER_MARKERS:
            idx = cleaned.find(marker)
            if idx >= 0:
                cleaned = cleaned[:idx]
                break
        lines = []
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            if re.fullmatch(r"\S+@\S+\.\S+", stripped):
                continue
            lines.append(stripped)
        return "\n".join(lines).strip()

    def _fallback_personal_category(
        self,
        email_row: dict[str, Any],
        *,
        clean_body: str,
        correspondence: dict[str, Any],
    ) -> tuple[str, float, str] | None:
        sender_addr = (email_row.get("from_addr") or "").strip().lower()
        sender_name = (email_row.get("from_name") or "").strip().lower()
        if not sender_addr:
            return None
        _, parsed_addr = parseaddr(sender_addr)
        sender_addr = parsed_addr or sender_addr
        sender_domain = sender_addr.split("@", 1)[-1] if "@" in sender_addr else ""
        body = clean_body.lower()
        if not body:
            return None
        if any(re.search(pattern, sender_addr) or re.search(pattern, sender_name) or re.search(pattern, body) for pattern in _AUTOMATED_PATTERNS):
            return None

        score = 0.0
        reasons: list[str] = []
        if sender_domain in _PERSONAL_EMAIL_DOMAINS:
            score += 0.35
            reasons.append("personal_mailbox")
        if correspondence.get("exact_reply_count", 0) > 0:
            score += 0.35
            reasons.append("exact_reply_history")
        elif correspondence.get("domain_reply_count", 0) > 0:
            score += 0.15
            reasons.append("domain_reply_history")
        if any(re.search(pattern, body) for pattern in _DIRECT_REQUEST_PATTERNS):
            score += 0.35
            reasons.append("direct_request_language")
        if "?" in clean_body:
            score += 0.1
            reasons.append("question_mark")
        if len(clean_body.split()) <= 40:
            score += 0.05
            reasons.append("short_plain_request")
        if score < 0.65:
            return None
        confidence = min(0.95, round(score, 3))
        return "personal", confidence, "heuristic:" + ",".join(reasons)
