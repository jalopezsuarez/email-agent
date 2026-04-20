"""Configuration loader. Reads config.yaml, expands ${ENV_VAR} placeholders."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class AppConfig:
    raw: dict
    path: Path

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "AppConfig":
        p = Path(path)
        load_dotenv(dotenv_path=p.with_name(".env"), override=False)
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return cls(raw=_expand(data), path=p)

    # Convenience accessors -------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def port(self) -> int:
        env_port = os.environ.get("EMAIL_AGENT_PORT")
        if env_port:
            return int(env_port)
        return int(self.get("server", "port", default=8765))

    @property
    def host(self) -> str:
        return str(self.get("server", "host", default="127.0.0.1"))

    @property
    def llm_provider(self) -> str:
        return str(self.get("llm", "provider", default="gemini"))

    @property
    def sqlite_path(self) -> str:
        return str(self.get("storage", "sqlite_path", default="data/email_agent.db"))

    @property
    def lancedb_path(self) -> str:
        return str(self.get("storage", "lancedb_path", default="data/lancedb"))

    @property
    def agent_inbox_from_iso(self) -> str | None:
        return _parse_agent_from_date("AGENT_INBOX_FROM")

    @property
    def agent_sent_from_iso(self) -> str | None:
        return _parse_agent_from_date("AGENT_SENT_FROM")


def _parse_agent_from_date(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{8}", raw):
        raise ValueError(f"{name} must use YYYYMMDD, got {raw!r}.")
    dt = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")
