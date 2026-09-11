"""Unit tests for the servicer-side input validators."""

from __future__ import annotations

import pytest

from agent_execution_service.errors import ValidationError
from agent_execution_service.validators import (
    MAX_FILTER_IDS,
    MAX_INPUT_LEN,
    validate_agent_id,
    validate_agent_tool_ids,
    validate_filter_ids,
    validate_instructions,
    validate_name,
    validate_task_id,
    validate_task_input,
    validate_tool_id,
)


def test_valid_input_passes() -> None:
    validate_task_input("do the thing")


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_empty_or_blank_input_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="input must not be empty"):
        validate_task_input(value)


def test_input_at_limit_passes_over_limit_rejected() -> None:
    validate_task_input("x" * MAX_INPUT_LEN)
    with pytest.raises(ValidationError, match="at most"):
        validate_task_input("x" * (MAX_INPUT_LEN + 1))


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_task_id_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="task_id must not be empty"):
        validate_task_id(value)


def test_valid_task_id_passes() -> None:
    validate_task_id("abc-123")


def test_empty_filter_id_list_is_allowed() -> None:
    validate_filter_ids([])


def test_blank_filter_id_rejected() -> None:
    with pytest.raises(ValidationError, match="filter ids must not be empty"):
        validate_filter_ids(["ok", ""])


def test_too_many_filter_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="at most"):
        validate_filter_ids(["x"] * (MAX_FILTER_IDS + 1))


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_agent_id_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="agent_id must not be empty"):
        validate_agent_id(value)


def test_valid_agent_id_passes() -> None:
    validate_agent_id("abc-123")


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_tool_id_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="tool_id must not be empty"):
        validate_tool_id(value)


def test_valid_tool_id_passes() -> None:
    validate_tool_id("abc-123")


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_name_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="name must not be empty"):
        validate_name(value)


def test_valid_name_passes() -> None:
    validate_name("agent")


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_instructions_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="instructions must not be empty"):
        validate_instructions(value)


def test_valid_instructions_passes() -> None:
    validate_instructions("do the thing")


def test_empty_agent_tool_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="tool_config.ids must not be empty"):
        validate_agent_tool_ids([])


def test_non_empty_agent_tool_ids_passes() -> None:
    validate_agent_tool_ids(["tool-1"])
