"""User-input validation for the RPC surface.

Validation of request fields lives here (and is invoked from the servicer)
rather than in the services/* layer, so every rejection of malformed caller
input happens at the edge, before any business logic or database/job_svc call
runs. The services layer trusts its already-validated keyword arguments.
"""

from __future__ import annotations

from agent_execution_service.errors import ValidationError

# Bounds are deliberately generous — they exist to reject obviously abusive
# input (empty strings, unbounded id lists), not to enforce business limits.
MAX_INPUT_LEN = 10_000
MAX_FILTER_IDS = 1_000


def validate_task_input(value: str) -> None:
    if not value or not value.strip():
        raise ValidationError("input must not be empty")
    if len(value) > MAX_INPUT_LEN:
        raise ValidationError(f"input must be at most {MAX_INPUT_LEN} characters")


def validate_task_id(value: str) -> None:
    if not value or not value.strip():
        raise ValidationError("task_id must not be empty")


def validate_filter_ids(ids: list[str]) -> None:
    if len(ids) > MAX_FILTER_IDS:
        raise ValidationError(f"filter may contain at most {MAX_FILTER_IDS} ids")
    for task_id in ids:
        if not task_id or not task_id.strip():
            raise ValidationError("filter ids must not be empty")
