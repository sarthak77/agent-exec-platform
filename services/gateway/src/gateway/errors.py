"""Typed errors, mapped to gRPC status codes in servicer.py."""


class AppError(Exception):
    """Base for all typed service errors."""


class GuardrailRejected(AppError):
    """Input failed a guardrail check (empty, oversized, or blocklisted)."""


class ProviderError(AppError):
    """The underlying model call failed."""
