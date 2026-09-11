"""Runtime settings, loaded from a TOML config file (config.toml at the
service root by default; override the path with GATEWAY_CONFIG_FILE).
Mirrors mcp_svc/config.py's loading pattern.

The API key is never read from config.toml — only from a provider-specific
env var (OPENAI_API_KEY for "openai", GROQ_API_KEY for "groq") — so it never
ends up committed or logged. Groq is reached through its OpenAI-compatible
endpoint (base_url https://api.groq.com/openai/v1), so the same
chat-completions provider drives it; set model.provider = "groq" to use it.
"""

from __future__ import annotations

import functools
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"

# provider -> (api-key env var, default base_url). base_url None means the
# OpenAI SDK's own default (the OpenAI API); Groq shares the OpenAI wire
# protocol so it just needs a different base_url + key.
_PROVIDERS: dict[str, tuple[str, str | None]] = {
    "openai": ("OPENAI_API_KEY", None),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
}


@dataclass(frozen=True, slots=True)
class GrpcSettings:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: str
    name: str
    max_tokens: int
    temperature: float
    timeout_seconds: int
    # OpenAI-compatible endpoint; None uses the SDK default (the OpenAI API).
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class GuardrailSettings:
    max_input_chars: int
    blocklist: list[str]


@dataclass(frozen=True, slots=True)
class Settings:
    grpc: GrpcSettings
    model: ModelSettings
    guardrails: GuardrailSettings
    api_key: str


def _load(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)

    raw_model = dict(raw["model"])
    provider = raw_model.get("provider")
    if provider not in _PROVIDERS:
        raise RuntimeError(
            f"unsupported model.provider {provider!r} "
            f"(supported: {sorted(_PROVIDERS)})"
        )
    env_key, default_base_url = _PROVIDERS[provider]

    api_key = os.environ.get(env_key)
    if not api_key:
        raise RuntimeError(
            f"{env_key} env var must be set for provider {provider!r} "
            "(never read from config.toml)"
        )

    # An explicit base_url in config.toml wins; otherwise fall back to the
    # provider's default (None for OpenAI, the Groq endpoint for Groq).
    raw_model.setdefault("base_url", default_base_url)

    return Settings(
        grpc=GrpcSettings(**raw["grpc"]),
        model=ModelSettings(**raw_model),
        guardrails=GuardrailSettings(**raw["guardrails"]),
        api_key=api_key,
    )


@functools.cache
def get_settings() -> Settings:
    """Load settings once, on first use.

    Deferred (not module-level) so importing this module — and therefore
    importing the provider/servicer that depend on its dataclasses — does
    not require OPENAI_API_KEY or config.toml to be present. That keeps the
    rest of the package import-safe and unit-testable.
    """
    return _load(Path(os.environ.get("GATEWAY_CONFIG_FILE", _DEFAULT_CONFIG_PATH)))
