"""Typed errors."""


class AppError(Exception):
    """Base for all typed service errors."""


class AuthenticationError(AppError):
    """No tenant identity established at all (missing header)."""
