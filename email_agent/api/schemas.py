from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ResolveReviewBody(BaseModel):
    folder_id: str
    folder_name: str
    category: Optional[str] = None
    user_note: Optional[str] = None


class UpdateDraftBody(BaseModel):
    body_html: str


class ApproveDraftBody(BaseModel):
    approved: bool = True


class TagStyleBody(BaseModel):
    tone_tag: str


class ConfigUpdateBody(BaseModel):
    # Free-form key/value strings persisted to SQLite config_kv table.
    values: dict[str, str]
