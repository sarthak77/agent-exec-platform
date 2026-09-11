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
