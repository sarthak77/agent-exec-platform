"""Unit tests for participant-slug generation (groupchat._slugify)."""

from __future__ import annotations

from orchestrator.groupchat import _slugify


def test_plain_name_passes_through() -> None:
    assert _slugify("Planner", set()) == "Planner"


def test_non_word_chars_become_underscores_and_are_trimmed() -> None:
    assert _slugify("My Agent!", set()) == "My_Agent"


def test_collisions_get_numeric_suffixes() -> None:
    taken: set[str] = set()
    assert _slugify("agent", taken) == "agent"
    assert _slugify("agent", taken) == "agent_2"
    assert _slugify("agent", taken) == "agent_3"


def test_all_non_word_falls_back_to_agent() -> None:
    assert _slugify("!!!", set()) == "agent"


def test_leading_digit_gets_letter_prefix() -> None:
    assert _slugify("7 wonders", set()) == "a_7_wonders"
