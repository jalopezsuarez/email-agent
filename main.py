"""Email-Agent entry point.

Starts a FastAPI process hosting:
- the three cooperating agents (Coordinator, Classifier, Responder)
- a polling scheduler for the Microsoft 365 inbox
- the HTML management panel at `/`
- a REST API at `/api/...`

Never sends email. Drafts are stored in the "Atendidos IA" folder.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from email_agent.api.routes import build_router
from email_agent.app_state import AppState
from email_agent.config import AppConfig

log = logging.getLogger("email_agent")


def create_app(cfg_path: str = "config.yaml") -> tuple[FastAPI, AppState, BackgroundScheduler]:
    logging.basicConfig(
        level=os.environ.get("EMAIL_AGENT_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = AppConfig.load(cfg_path)
    state = AppState(cfg)

    app = FastAPI(title="Email-Agent", version="0.1.0")

    @app.on_event("startup")
    def _startup() -> None:
        if not state.try_bootstrap(interactive=False):
            log.warning(
                "Graph bootstrap skipped at startup. The panel will stay available "
                "and Graph can be connected later from the UI.",
            )

    app.include_router(build_router(state))

    web_dir = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/")
    def _index():
        return FileResponse(web_dir / "index.html")

    @app.get("/healthz")
    def _health():
        return {"ok": True}

    # Scheduler ------------------------------------------------------------
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _run_cycle_safely,
        "interval",
        seconds=state.polling_interval(),
        args=[state],
        id="inbox-poll",
        max_instances=1,
        coalesce=True,
    )

    @app.on_event("startup")
    def _start_scheduler() -> None:
        scheduler.start()
        log.info("Scheduler started (interval=%ds).", state.polling_interval())

    @app.on_event("shutdown")
    def _stop_scheduler() -> None:
        scheduler.shutdown(wait=False)

    return app, state, scheduler


def _run_cycle_safely(state: AppState) -> None:
    if not state.graph_status()["connected"]:
        log.info("Scheduled cycle skipped: Graph is not connected yet.")
        return
    try:
        summary = state.coordinator.run_cycle()
        log.info("Cycle: %s", summary)
    except Exception:
        log.exception("Scheduled cycle failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Email-Agent server")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    args = parser.parse_args()

    app, state, _scheduler = create_app(args.config)
    host = args.host or state.cfg.host
    port = args.port or state.cfg.port
    log.info("Serving Email-Agent on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
