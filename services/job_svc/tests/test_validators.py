"""Unit tests for the servicer-side input validator (JobValidator)."""

from __future__ import annotations

import pytest

from aep.job.v1 import service_pb2
from job_svc.errors import ValidationError
from job_svc.mappers import STATUS_FROM_PROTO, TYPE_FROM_PROTO
from job_svc.validators import JobValidator


# --- validate_type ---------------------------------------------------------


def test_valid_type_returns_domain_string() -> None:
    assert JobValidator.validate_type(service_pb2.JOB_TYPE_AGENT_EXECUTION) == "agent_execution"


def test_unspecified_type_rejected() -> None:
    with pytest.raises(ValidationError, match="type is required"):
        JobValidator.validate_type(service_pb2.JOB_TYPE_UNSPECIFIED)


def test_unknown_type_value_rejected() -> None:
    with pytest.raises(ValidationError, match="type is required"):
        JobValidator.validate_type(9999)


# --- validate_status -------------------------------------------------------


def test_valid_status_returns_domain_string() -> None:
    assert JobValidator.validate_status(service_pb2.JOB_STATUS_RUNNING) == "running"


def test_unspecified_status_rejected() -> None:
    with pytest.raises(ValidationError, match="status is required"):
        JobValidator.validate_status(service_pb2.JOB_STATUS_UNSPECIFIED)


def test_unknown_status_value_rejected() -> None:
    with pytest.raises(ValidationError, match="status is required"):
        JobValidator.validate_status(9999)


# --- validate_max_attempts -------------------------------------------------


def test_none_max_attempts_is_allowed() -> None:
    JobValidator.validate_max_attempts(None)


@pytest.mark.parametrize("value", [1, 3, 100])
def test_positive_max_attempts_allowed(value: int) -> None:
    JobValidator.validate_max_attempts(value)


@pytest.mark.parametrize("value", [0, -1, -5])
def test_non_positive_max_attempts_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="max_attempts must be >= 1"):
        JobValidator.validate_max_attempts(value)


# --- validate_job_id -------------------------------------------------------


@pytest.mark.parametrize("value", ["", "  ", "\n\t"])
def test_blank_job_id_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="id must not be empty"):
        JobValidator.validate_job_id(value)


def test_valid_job_id_allowed() -> None:
    JobValidator.validate_job_id("abc-123")


# --- validate_filter_ids ---------------------------------------------------


def test_empty_filter_id_list_is_allowed() -> None:
    JobValidator.validate_filter_ids([])


def test_blank_filter_id_rejected() -> None:
    with pytest.raises(ValidationError, match="filter ids must not be empty"):
        JobValidator.validate_filter_ids(["ok", ""])


def test_too_many_filter_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="at most"):
        JobValidator.validate_filter_ids(["x"] * (JobValidator.MAX_FILTER_IDS + 1))


def test_filter_ids_at_limit_allowed() -> None:
    JobValidator.validate_filter_ids(["x"] * JobValidator.MAX_FILTER_IDS)


# --- validate_filter_enums -------------------------------------------------


def test_filter_enums_maps_each_value() -> None:
    result = JobValidator.validate_filter_enums(
        [service_pb2.JOB_STATUS_QUEUED, service_pb2.JOB_STATUS_DEAD], STATUS_FROM_PROTO, "status"
    )
    assert result == ["queued", "dead"]


def test_filter_enums_empty_is_allowed() -> None:
    assert JobValidator.validate_filter_enums([], TYPE_FROM_PROTO, "type") == []


def test_filter_enums_unknown_value_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid status filter value"):
        JobValidator.validate_filter_enums([9999], STATUS_FROM_PROTO, "status")
