"""Wires every component together and exposes them as a shared state object.

Kept intentionally small: the FastAPI router and the scheduler both pull
services from here, so reconfiguration (e.g. switching LLM provider from
the panel) is a single place to update.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .agents.classifier import ClassifierAgent
from .agents.coordinator import CoordinatorAgent
from .agents.responder import ResponderAgent
from .config import AppConfig
from .services.graph_client import GraphClient
from .services.llm import build_provider
from .services.sqlite_store import SQLiteStore
from .services.vector_store import VectorStore

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.sqlite = SQLiteStore(cfg.sqlite_path)

        # Overlay runtime overrides from SQLite into the raw config dict.
        self._apply_overrides()

        self.llm = build_provider(cfg)
        # Probe a single embedding to discover the vector dimension.
        dim = self._detect_dim()
        self.vectors = VectorStore(cfg.lancedb_path, dim=dim)
        self.graph = GraphClient(
            client_id=cfg.get("graph", "client_id", default=""),
            tenant=cfg.get("graph", "tenant", default="consumers"),
            scopes=cfg.get("graph", "scopes", default=["Mail.ReadWrite"]),
            token_cache_path=cfg.get(
                "graph", "token_cache_path", default="data/msal_cache.bin"
            ),
        )
        self.atendidos_folder_name = cfg.get(
            "graph", "atendidos_folder_name", default="Atendidos IA"
        )
        self.classifier = ClassifierAgent(
            llm=self.llm,
            sqlite=self.sqlite,
            vectors=self.vectors,
            threshold=float(cfg.get("classifier", "confidence_threshold", default=0.9)),
            top_k=int(cfg.get("classifier", "top_k_candidate_folders", default=6)),
        )
        self.responder = ResponderAgent(
            llm=self.llm,
            graph=self.graph,
            sqlite=self.sqlite,
            vectors=self.vectors,
            atendidos_folder_id="",  # resolved on bootstrap
            language_hint=cfg.get("responder", "draft_language", default="auto"),
        )
        self.coordinator = CoordinatorAgent(
            llm=self.llm,
            graph=self.graph,
            sqlite=self.sqlite,
            classifier=self.classifier,
            responder=self.responder,
            inbox_batch_size=int(cfg.get("polling", "inbox_batch_size", default=25)),
            personal_threshold=float(
                cfg.get("responder", "personal_confidence_threshold", default=0.8)
            ),
        )
        self._folders: list[dict] = []

    # --------------------------------------------------------- bootstrap
    def bootstrap(self) -> None:
        """Attempt Graph authentication and seed the classifier."""
        self.graph.ensure_token(interactive=True)
        atendidos = self.graph.ensure_folder(self.atendidos_folder_name)
        self.responder.atendidos_folder_id = atendidos["id"]
        self._folders = self.graph.list_folders()
        self.classifier.seed_folders(self._folders)
        log.info("Bootstrapped with %d folders.", len(self._folders))

    def folders_cache(self) -> list[dict]:
        if not self._folders:
            try:
                self._folders = self.graph.list_folders()
            except Exception:
                return []
        return self._folders

    # --------------------------------------------------------- helpers
    def _detect_dim(self) -> int:
        try:
            vec = self.llm.embed(["probe"])[0]
            return len(vec)
        except Exception as exc:
            log.warning("Could not probe embedding dim, defaulting to 768: %s", exc)
            return 768

    def _apply_overrides(self) -> None:
        overrides = self.sqlite.all_config()
        for key, value in overrides.items():
            parts = key.split(".")
            cur = self.cfg.raw
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = _coerce(value)

    def apply_runtime_config(self) -> None:
        """Re-apply SQLite-stored overrides and rebuild dependent agents."""
        self._apply_overrides()
        self.classifier.threshold = float(
            self.cfg.get("classifier", "confidence_threshold", default=self.classifier.threshold)
        )
        self.classifier.top_k = int(
            self.cfg.get(
                "classifier", "top_k_candidate_folders", default=self.classifier.top_k
            )
        )
        self.coordinator.personal_threshold = float(
            self.cfg.get(
                "responder",
                "personal_confidence_threshold",
                default=self.coordinator.personal_threshold,
            )
        )

    def polling_interval(self) -> int:
        return int(self.cfg.get("polling", "interval_seconds", default=120))


def _coerce(value: str):
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        return value
