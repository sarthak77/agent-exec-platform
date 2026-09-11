"""Typed errors, mapped to gRPC status codes in servicer.py."""


class AppError(Exception):
    """Base for all typed service errors."""


class GuardrailRejected(AppError):
    """Input failed a guardrail check (empty, oversized, or blocklisted)."""


class ValidationError(AppError):
    """The request was malformed independent of guardrail policy (e.g. a tool
    parameter schema that isn't valid JSON, or a request the model provider
    itself rejected as invalid) -- the caller's fault, not an upstream outage."""


class ProviderError(AppError):
    """The underlying model call failed (connectivity, rate limit, or a
    provider-side error) -- not the caller's fault; retryable upstream."""
