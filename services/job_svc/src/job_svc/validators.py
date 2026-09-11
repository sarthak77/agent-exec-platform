"""User-input validation for the RPC surface, collected on a single validator.

Validation of request fields lives here (and is invoked from the servicer)
rather than in the services/* layer, so every rejection of malformed caller
input happens at the edge, before any business logic or database write runs.
The services layer trusts its already-validated keyword arguments.

The enum validators double as mappers: a proto enum value is only valid if it
maps to a known domain string, so they return that string on success and raise
otherwise, keeping "is this a real status/type?" in one place.
"""

from __future__ import annotations

from job_svc.errors import ValidationError
from job_svc.mappers import STATUS_FROM_PROTO, TYPE_FROM_PROTO

# Bound is deliberately generous — it exists to reject an unbounded id list, not
# to enforce a business limit.
MAX_FILTER_IDS = 1_000


class JobValidator:
    """Stateless validators for the JobService gRPC surface.

    Methods raise :class:`ValidationError` on bad input (mapped to
    INVALID_ARGUMENT by the servicer) and otherwise return the normalized value
    the servicer should forward to the services layer.
    """

    MAX_FILTER_IDS = MAX_FILTER_IDS

    @staticmethod
    def validate_type(proto_type: int) -> str:
        job_type = TYPE_FROM_PROTO.get(proto_type)
        if job_type is None:
            raise ValidationError("type is required")
        return job_type

    @staticmethod
    def validate_status(proto_status: int) -> str:
        status = STATUS_FROM_PROTO.get(proto_status)
        if status is None:
            raise ValidationError("status is required")
        return status

    @staticmethod
    def validate_max_attempts(max_attempts: int | None) -> None:
        if max_attempts is not None and max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1")

    @staticmethod
    def validate_job_id(value: str) -> None:
        if not value or not value.strip():
            raise ValidationError("id must not be empty")

    @classmethod
    def validate_filter_ids(cls, ids: list[str]) -> None:
        if len(ids) > cls.MAX_FILTER_IDS:
            raise ValidationError(f"filter may contain at most {cls.MAX_FILTER_IDS} ids")
        for job_id in ids:
            if not job_id or not job_id.strip():
                raise ValidationError("filter ids must not be empty")

    @staticmethod
    def validate_filter_enums(values: list[int], mapping: dict[int, str], label: str) -> list[str]:
        result = []
        for v in values:
            mapped = mapping.get(v)
            if mapped is None:
                raise ValidationError(f"invalid {label} filter value {v}")
            result.append(mapped)
        return result
