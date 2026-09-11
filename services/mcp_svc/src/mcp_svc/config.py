"""Runtime settings, loaded from a TOML config file (config.toml at the
service root by default; override the path with MCP_SVC_CONFIG_FILE).
Mirrors agent_execution_service/config.py's loading pattern.

Postgres connection fields can be overridden per-field from the environment
(MCP_SVC_POSTGRES_PASSWORD, MCP_SVC_POSTGRES_HOST, ...), so the committed
config.toml can hold dev defaults while real deployments keep credentials
out of source control.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"


@dataclass(frozen=True, slots=True)
class HttpSettings:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True, slots=True)
class ToolExecutionSettings:
    """Bounds for the code-defined tool handlers (see handlers.py). Optional in
    config.toml — the defaults are sensible for the built-in http_request tool."""

    timeout_seconds: float = 30.0
    max_response_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    """Host/Origin allowlists for the streamable-HTTP transport. When both are
    empty, DNS-rebinding/Origin protection is left off (the service is expected
    to sit behind a trusted edge); populate either to turn protection on.
    """

    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Settings:
    http: HttpSettings
    postgres: PostgresSettings
    security: SecuritySettings
    tool_execution: ToolExecutionSettings


def _postgres(raw: dict[str, Any]) -> PostgresSettings:
    env = os.environ.get
    return PostgresSettings(
        host=env("MCP_SVC_POSTGRES_HOST", raw["host"]),
        port=int(env("MCP_SVC_POSTGRES_PORT", raw["port"])),
        user=env("MCP_SVC_POSTGRES_USER", raw["user"]),
        password=env("MCP_SVC_POSTGRES_PASSWORD", raw["password"]),
        database=env("MCP_SVC_POSTGRES_DATABASE", raw["database"]),
    )


def _load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    security = raw.get("security", {})
    tool_execution = raw.get("tool_execution", {})
    return Settings(
        http=HttpSettings(**raw["http"]),
        postgres=_postgres(raw["postgres"]),
        security=SecuritySettings(
            allowed_hosts=tuple(security.get("allowed_hosts", ())),
            allowed_origins=tuple(security.get("allowed_origins", ())),
        ),
        tool_execution=ToolExecutionSettings(**tool_execution),
    )


settings = _load(Path(os.environ.get("MCP_SVC_CONFIG_FILE", _DEFAULT_CONFIG_PATH)))
