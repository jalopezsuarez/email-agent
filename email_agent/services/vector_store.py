"""LanceDB-backed vector store for semantic retrieval.

Tables
------
folder_embeddings : one row per (folder, example) with high weight for feedback.
email_embeddings  : one row per ingested email (for nearest-folder lookups).
style_embeddings  : one row per style sample for per-recipient retrieval.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # LanceDB is an optional heavy dep; keep import guarded so tests can stub it.
    import lancedb
    import pyarrow as pa
except Exception:  # pragma: no cover - only hit when the optional dep is missing
    lancedb = None  # type: ignore
    pa = None  # type: ignore


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class VectorStore:
    def __init__(self, path: str, dim: int = 768):
        if lancedb is None:
            raise RuntimeError(
                "lancedb is not installed. Run `pip install lancedb pyarrow`."
            )
        Path(path).mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(path)
        self._dim = dim
        self._folders = self._open_or_create(
            "folder_embeddings",
            {
                "folder_id": "string",
                "folder_name": "string",
                "text": "string",
                "source": "string",  # 'name' | 'example' | 'feedback'
                "weight": "float",
                "created_at": "string",
            },
        )
        self._emails = self._open_or_create(
            "email_embeddings",
            {
                "email_id": "int64",
                "subject": "string",
                "text": "string",
                "created_at": "string",
            },
        )
        self._style = self._open_or_create(
            "style_embeddings",
            {
                "sample_id": "int64",
                "recipient": "string",
                "recipient_domain": "string",
                "text": "string",
                "tone_tag": "string",
                "created_at": "string",
            },
        )

    # ------------------------------------------------------------------ util
    def _schema(self, extra: dict[str, str]) -> Any:
        fields = [pa.field("vector", pa.list_(pa.float32(), self._dim))]
        type_map = {
            "string": pa.string(),
            "int64": pa.int64(),
            "float": pa.float32(),
        }
        for name, typ in extra.items():
            fields.append(pa.field(name, type_map[typ]))
        return pa.schema(fields)

    def _open_or_create(self, name: str, extra_cols: dict[str, str]):
        if name in self._db.table_names():
            return self._db.open_table(name)
        return self._db.create_table(name, schema=self._schema(extra_cols))

    # -------------------------------------------------------------- folders
    def add_folder_example(
        self,
        folder_id: str,
        folder_name: str,
        text: str,
        vector: list[float],
        *,
        source: str = "example",
        weight: float = 1.0,
    ) -> None:
        self._folders.add(
            [
                {
                    "vector": vector,
                    "folder_id": folder_id,
                    "folder_name": folder_name,
                    "text": text,
                    "source": source,
                    "weight": float(weight),
                    "created_at": _utcnow(),
                }
            ]
        )

    def nearest_folders(self, vector: list[float], top_k: int = 6) -> list[dict]:
        """Return aggregated folder scores using weighted similarity."""
        try:
            results = self._folders.search(vector).limit(top_k * 6).to_list()
        except Exception:
            return []
        agg: dict[str, dict[str, Any]] = {}
        for r in results:
            fid = r["folder_id"]
            dist = r.get("_distance", 1.0)
            sim = max(0.0, 1.0 - float(dist))
            score = sim * float(r.get("weight", 1.0))
            if fid not in agg or score > agg[fid]["score"]:
                agg[fid] = {
                    "folder_id": fid,
                    "folder_name": r["folder_name"],
                    "score": score,
                    "sample_text": r["text"],
                }
        ranked = sorted(agg.values(), key=lambda x: x["score"], reverse=True)
        return ranked[:top_k]

    # ---------------------------------------------------------------- emails
    def add_email(self, email_id: int, subject: str, text: str, vector: list[float]) -> None:
        self._emails.add(
            [
                {
                    "vector": vector,
                    "email_id": email_id,
                    "subject": subject or "",
                    "text": text or "",
                    "created_at": _utcnow(),
                }
            ]
        )

    # ----------------------------------------------------------------- style
    def add_style(
        self,
        sample_id: int,
        recipient: str,
        recipient_domain: str,
        text: str,
        vector: list[float],
        tone_tag: str | None = None,
    ) -> None:
        self._style.add(
            [
                {
                    "vector": vector,
                    "sample_id": sample_id,
                    "recipient": recipient or "",
                    "recipient_domain": recipient_domain or "",
                    "text": text or "",
                    "tone_tag": tone_tag or "",
                    "created_at": _utcnow(),
                }
            ]
        )

    def nearest_style(
        self,
        vector: list[float],
        recipient: str | None = None,
        top_k: int = 3,
    ) -> list[dict]:
        try:
            q = self._style.search(vector).limit(top_k * 4)
            if recipient:
                q = q.where(f"recipient = '{recipient}'")
            return q.to_list()[:top_k]
        except Exception:
            return []
