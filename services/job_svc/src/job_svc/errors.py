"""Typed errors, mapped to gRPC status codes in servicer.py."""


class AppError(Exception):
    """Base for all typed service errors."""


class NotFoundError(AppError):
    pass


class ValidationError(AppError):
    pass


class AuthenticationError(AppError):
    """No tenant identity established at all (missing metadata)."""


class ConflictError(AppError):
    pass


class StateError(AppError):
    pass


class DependencyError(AppError):
    """A downstream dependency (e.g. the orchestrator) failed. Raised by the
    runner's clients; treated as a retryable job failure rather than surfaced on
    the RPC edge."""
