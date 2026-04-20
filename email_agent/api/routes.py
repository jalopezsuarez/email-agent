"""FastAPI routes used by the management panel."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from ..services.graph_client import GraphAuthError
from .schemas import (
    ApproveDraftBody,
    ConfigUpdateBody,
    ResolveReviewBody,
    TagStyleBody,
    UpdateDraftBody,
)

log = logging.getLogger(__name__)


def build_router(app_state) -> APIRouter:
    """Build the API router bound to the application state container."""
    router = APIRouter(prefix="/api")

    @router.get("/status")
    def status():
        stats = app_state.sqlite.decision_stats()
        graph = app_state.graph_status()
        return {
            "graph_connected": graph["connected"],
            "graph_pending": graph["pending"],
            "graph_message": graph["message"],
            "graph_error": graph["error"],
            "llm_provider": app_state.llm.name,
            "classifier_threshold": app_state.classifier.threshold,
            "personal_threshold": app_state.coordinator.personal_threshold,
            "port": app_state.cfg.port,
            "stats": stats,
            "style_samples": app_state.sqlite.style_samples_count(),
        }

    @router.post("/graph/connect")
    def connect_graph():
        return app_state.start_graph_connect()

    @router.post("/scan-now")
    def scan_now():
        try:
            app_state.ensure_graph_ready()
        except GraphAuthError as exc:
            raise HTTPException(409, str(exc))
        return app_state.coordinator.run_cycle()

    @router.get("/folders")
    def folders():
        return app_state.folders_cache()

    @router.get("/pending-reviews")
    def pending_reviews():
        return app_state.sqlite.list_pending_reviews()

    @router.post("/pending-reviews/{email_id}/resolve")
    def resolve_review(email_id: int, body: ResolveReviewBody):
        email = app_state.sqlite.get_email(email_id)
        if not email:
            raise HTTPException(404, "Email not found")
        # Move in Graph.
        try:
            app_state.graph.move_message(email["graph_id"], body.folder_id)
        except Exception as exc:
            raise HTTPException(502, f"Graph move failed: {exc}")
        app_state.sqlite.update_email(
            email_id,
            folder_id=body.folder_id,
            folder_name=body.folder_name,
            status="classified",
            category=body.category,
        )
        app_state.sqlite.add_feedback(
            email_id,
            correct_folder_id=body.folder_id,
            correct_folder_name=body.folder_name,
            correct_category=body.category,
            user_note=body.user_note,
        )
        app_state.sqlite.log_decision(
            email_id,
            agent="classifier",
            action="classify",
            target=body.folder_id,
            reviewed_by="human",
            notes=body.user_note or "",
        )
        # Feed the classifier's vector store with the corrected example.
        text = (
            f"Subject: {email.get('subject') or ''}\n"
            f"From: {email.get('from_name')} <{email.get('from_addr')}>\n"
            f"{email.get('body_snippet') or ''}"
        )
        try:
            app_state.classifier.ingest_feedback(
                folder_id=body.folder_id,
                folder_name=body.folder_name,
                text=text,
            )
        except Exception as exc:
            log.warning("ingest_feedback failed: %s", exc)
        return {"ok": True}

    @router.get("/drafts")
    def drafts():
        return app_state.sqlite.list_drafts()

    @router.put("/drafts/{draft_id}")
    def update_draft(draft_id: int, body: UpdateDraftBody):
        # Update the on-disk record AND the Outlook draft if we have a Graph id.
        drafts_all = {d["id"]: d for d in app_state.sqlite.list_drafts(limit=500)}
        record = drafts_all.get(draft_id)
        if not record:
            raise HTTPException(404, "Draft not found")
        if record.get("graph_draft_id"):
            try:
                app_state.graph.update_draft_body(record["graph_draft_id"], body.body_html)
            except Exception as exc:
                raise HTTPException(502, f"Graph update failed: {exc}")
        app_state.sqlite.update_draft(draft_id, body_html=body.body_html)
        return {"ok": True}

    @router.post("/drafts/{draft_id}/approve")
    def approve_draft(draft_id: int, body: ApproveDraftBody):
        # 'Approve' only marks the record as user-approved. It does NOT send.
        app_state.sqlite.update_draft(draft_id, approved=1 if body.approved else 0)
        return {"ok": True, "sent": False, "note": "Sending is disabled by design."}

    @router.get("/style-samples")
    def style_samples(recipient: str | None = None):
        return app_state.sqlite.list_style_samples_for(recipient, limit=100)

    @router.post("/style-samples/{sample_id}/tag")
    def tag_style(sample_id: int, body: TagStyleBody):
        app_state.sqlite.tag_style_sample(sample_id, body.tone_tag)
        return {"ok": True}

    @router.post("/train/style")
    def train_style():
        try:
            app_state.ensure_graph_ready()
        except GraphAuthError as exc:
            raise HTTPException(409, str(exc))
        new = app_state.responder.learn_from_sent_items(
            limit=app_state.cfg.get("responder", "sent_items_learning_batch", default=200)
        )
        return {"new_samples": new}

    @router.get("/config")
    def get_config():
        return {
            "file": app_state.cfg.raw,
            "runtime": app_state.sqlite.all_config(),
        }

    @router.put("/config")
    def put_config(body: ConfigUpdateBody):
        for k, v in body.values.items():
            app_state.sqlite.set_config(k, v)
        app_state.apply_runtime_config()
        return {"ok": True}

    return router
