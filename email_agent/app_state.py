"""Wires every component together and exposes them as a shared state object.

Kept intentionally small: the FastAPI router and the scheduler both pull
services from here, so reconfiguration (e.g. switching LLM provider from
the panel) is a single place to update.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .agents.classifier import ClassifierAgent
from .agents.coordinator import CoordinatorAgent
from .agents.responder import ResponderAgent
from .config import AppConfig
from .services.graph_client import GraphAuthError, GraphClient
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
            sent_from_iso=cfg.agent_sent_from_iso,
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
            inbox_from_iso=cfg.agent_inbox_from_iso,
        )
        self._folders: list[dict] = []
        self._graph_lock = threading.Lock()
        self._graph_bootstrapped = False
        self._graph_auth_pending = False
        self._graph_message = "Pulsa «Conectar Microsoft» para autorizar Graph."
        self._graph_error: str | None = None

    # --------------------------------------------------------- bootstrap
    def bootstrap(self, *, interactive: bool = False) -> None:
        """Attempt Graph authentication and seed the classifier."""
        try:
            self.graph.ensure_token(interactive=interactive)
            self._finish_graph_bootstrap()
        except Exception as exc:
            self._set_graph_error(str(exc))
            raise

    def try_bootstrap(self, *, interactive: bool = False) -> bool:
        try:
            self.bootstrap(interactive=interactive)
            return True
        except Exception:
            return False

    def _finish_graph_bootstrap(self) -> None:
        atendidos = self.graph.ensure_folder(self.atendidos_folder_name)
        self.responder.atendidos_folder_id = atendidos["id"]
        self._folders = self.graph.list_folders()
        self.classifier.seed_folders(self._folders)
        with self._graph_lock:
            self._graph_bootstrapped = True
            self._graph_auth_pending = False
            self._graph_message = "Graph conectado."
            self._graph_error = None
        log.info("Bootstrapped with %d folders.", len(self._folders))

    def graph_status(self) -> dict[str, object]:
        authenticated = self.graph.is_authenticated()
        with self._graph_lock:
            connected = authenticated and self._graph_bootstrapped
            return {
                "connected": connected,
                "pending": self._graph_auth_pending,
                "message": self._graph_message,
                "error": self._graph_error,
            }

    def start_graph_connect(self) -> dict[str, object]:
        if self.try_bootstrap(interactive=False):
            return self.graph_status()
        with self._graph_lock:
            pending = self._graph_auth_pending
        if pending:
            return self.graph_status()
        try:
            flow = self.graph.initiate_device_flow()
        except Exception as exc:
            self._set_graph_error(str(exc))
            return self.graph_status()
        with self._graph_lock:
            self._graph_auth_pending = True
            self._graph_message = flow["message"]
            self._graph_error = None
        threading.Thread(
            target=self._complete_graph_connect,
            args=(flow,),
            daemon=True,
            name="graph-device-flow",
        ).start()
        return self.graph_status()

    def _complete_graph_connect(self, flow: dict) -> None:
        try:
            self.graph.complete_device_flow(flow)
            self._finish_graph_bootstrap()
        except Exception as exc:
            self._set_graph_error(str(exc))

    def ensure_graph_ready(self) -> None:
        status = self.graph_status()
        if status["pending"]:
            raise GraphAuthError(
                "Completa el login de Microsoft que aparece en el panel y vuelve a intentarlo."
            )
        if not status["connected"]:
            raise GraphAuthError(
                str(status["error"] or "Graph no está conectado. Pulsa «Conectar Microsoft».")
            )

    def _set_graph_error(self, message: str) -> None:
        with self._graph_lock:
            self._graph_bootstrapped = False
            self._graph_auth_pending = False
            self._graph_message = "Graph desconectado."
            self._graph_error = message

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
