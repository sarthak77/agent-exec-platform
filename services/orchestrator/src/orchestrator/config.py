"""Runtime settings, loaded from a TOML config file (config.toml at the
service root by default; override the path with ORCHESTRATOR_CONFIG_FILE).
Mirrors agent_execution_service/config.py's and mcp_svc/config.py's loading
pattern.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"


@dataclass(frozen=True, slots=True)
class GrpcSettings:
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
class GatewaySettings:
    host: str
    port: int
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class McpSettings:
    url: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ChatSettings:
    max_messages: int


@dataclass(frozen=True, slots=True)
class Settings:
    grpc: GrpcSettings
    postgres: PostgresSettings
    gateway: GatewaySettings
    mcp: McpSettings
    chat: ChatSettings


def _load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(
        grpc=GrpcSettings(**raw["grpc"]),
        postgres=PostgresSettings(**raw["postgres"]),
        gateway=GatewaySettings(**raw["gateway"]),
        mcp=McpSettings(**raw["mcp"]),
        chat=ChatSettings(**raw["chat"]),
    )


settings = _load(Path(os.environ.get("ORCHESTRATOR_CONFIG_FILE", _DEFAULT_CONFIG_PATH)))
