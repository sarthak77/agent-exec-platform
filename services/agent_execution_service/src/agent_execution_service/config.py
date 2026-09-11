"""Runtime settings, loaded from a TOML config file (config.toml at the
service root by default; override the path with AEP_CONFIG_FILE). Uses
tomllib from the stdlib rather than adding a YAML dependency for a basic
impl's needs.

Any individual field may be overridden by an environment variable named
``AEP_<SECTION>_<FIELD>`` (e.g. ``AEP_POSTGRES_PASSWORD``), so secrets can be
supplied by the environment and kept out of the committed TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

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
class JobSvcSettings:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class Settings:
    grpc: GrpcSettings
    postgres: PostgresSettings
    job_svc: JobSvcSettings


_T = TypeVar("_T")


def _section(cls: type[_T], section: str, raw: dict[str, Any]) -> _T:
    """Build a settings dataclass from the TOML section, letting
    ``AEP_<SECTION>_<FIELD>`` env vars override individual fields (coerced to
    the field's annotated type).
    """
    values = dict(raw)
    for name, typ in get_type_hints(cls).items():
        override = os.environ.get(f"AEP_{section}_{name}".upper())
        if override is not None:
            values[name] = typ(override)
    return cls(**values)


def _load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(
        grpc=_section(GrpcSettings, "grpc", raw["grpc"]),
        postgres=_section(PostgresSettings, "postgres", raw["postgres"]),
        job_svc=_section(JobSvcSettings, "job_svc", raw["job_svc"]),
    )


settings = _load(Path(os.environ.get("AEP_CONFIG_FILE", _DEFAULT_CONFIG_PATH)))
