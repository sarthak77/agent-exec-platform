"""Runtime settings, loaded from a TOML config file (config.toml at the
service root by default; override the path with JOB_SVC_CONFIG_FILE). Uses
tomllib from the stdlib rather than adding a YAML dependency for a basic
impl's needs.

Any individual field may be overridden by an environment variable named
``JOB_SVC_<SECTION>_<FIELD>`` (e.g. ``JOB_SVC_POSTGRES_PASSWORD`` or
``JOB_SVC_JOBS_DEFAULT_MAX_ATTEMPTS``), so secrets and per-environment tuning
can come from the environment and stay out of the committed TOML.
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
class JobsSettings:
    # Applied to a CreateJob that omits max_attempts. Config-driven so the retry
    # budget can be tuned per environment without a code change.
    default_max_attempts: int
    # Applied to a CreateJob that omits max_retries -- the separate,
    # user/manual retry budget consumed by RetryJob (see services/jobs.py).
    default_max_retries: int


@dataclass(frozen=True, slots=True)
class PollerSettings:
    # The startup poller that claims queued jobs and hands them to a runner.
    enabled: bool
    interval_seconds: float
    batch_size: int
    # How long a claim lease is valid. A running job whose lease is older than
    # this is assumed abandoned (the pod that claimed it died) and requeued by
    # the reaper. Set comfortably above the longest expected job runtime so a
    # slow-but-alive run is not reaped out from under its worker.
    lease_seconds: float


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    # The orchestrator service the runner calls to decompose/execute prompts.
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class Settings:
    grpc: GrpcSettings
    postgres: PostgresSettings
    jobs: JobsSettings
    poller: PollerSettings
    orchestrator: OrchestratorSettings


_T = TypeVar("_T")


def _coerce(typ: type, value: str) -> Any:
    # bool(str) is truthy for any non-empty string, so interpret common truthy
    # spellings explicitly rather than letting "false" read as True.
    if typ is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return typ(value)


def _section(cls: type[_T], section: str, raw: dict[str, Any]) -> _T:
    """Build a settings dataclass from the TOML section, letting
    ``JOB_SVC_<SECTION>_<FIELD>`` env vars override individual fields (coerced to
    the field's annotated type).
    """
    values = dict(raw)
    for name, typ in get_type_hints(cls).items():
        override = os.environ.get(f"JOB_SVC_{section}_{name}".upper())
        if override is not None:
            values[name] = _coerce(typ, override)
    return cls(**values)


def _load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(
        grpc=_section(GrpcSettings, "grpc", raw["grpc"]),
        postgres=_section(PostgresSettings, "postgres", raw["postgres"]),
        jobs=_section(JobsSettings, "jobs", raw["jobs"]),
        poller=_section(PollerSettings, "poller", raw["poller"]),
        orchestrator=_section(OrchestratorSettings, "orchestrator", raw["orchestrator"]),
    )


settings = _load(Path(os.environ.get("JOB_SVC_CONFIG_FILE", _DEFAULT_CONFIG_PATH)))
