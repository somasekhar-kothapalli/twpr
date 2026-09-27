"""Tests for the shared helpers — the env guard and the EIA week calculation."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from common import env, week_ending  # noqa: E402


# --- env(): placeholder values must read as unset -------------------------


def test_missing_variable_returns_the_default(monkeypatch):
    monkeypatch.delenv("TWPR_TEST_VAR", raising=False)
    assert env("TWPR_TEST_VAR") is None
    assert env("TWPR_TEST_VAR", "fallback") == "fallback"


def test_a_real_value_passes_through(monkeypatch):
    monkeypatch.setenv("TWPR_TEST_VAR", "abc123")
    assert env("TWPR_TEST_VAR") == "abc123"


def test_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.setenv("TWPR_TEST_VAR", "  abc123  ")
    assert env("TWPR_TEST_VAR") == "abc123"


@pytest.mark.parametrize(
    "leaked",
    [
        "# free at eia.gov/opendata",
        "   # optional -- falls back to JSON files",
        "#",
    ],
)
def test_a_leaked_dotenv_comment_reads_as_unset(monkeypatch, leaked):
    """python-dotenv keeps an inline comment as the value when the value is empty.

    Regression: `.env.example` had `EIA_API_KEY=   # free at eia.gov/opendata`,
    so the key became the comment text and the EIA API answered 403 — which looks
    like a bad key rather than bad config.
    """
    monkeypatch.setenv("TWPR_TEST_VAR", leaked)
    assert env("TWPR_TEST_VAR") is None
    assert env("TWPR_TEST_VAR", "fallback") == "fallback"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_value_reads_as_unset(monkeypatch, blank):
    monkeypatch.setenv("TWPR_TEST_VAR", blank)
    assert env("TWPR_TEST_VAR") is None


def test_a_hash_inside_a_value_is_kept(monkeypatch):
    """Only a leading # marks a placeholder — a token may legitimately contain one."""
    monkeypatch.setenv("TWPR_TEST_VAR", "abc#123")
    assert env("TWPR_TEST_VAR") == "abc#123"


def test_shipped_env_example_has_no_leaked_comments():
    """Every value in .env.example must be usable or genuinely empty."""
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).parent.parent / ".env.example")
    leaked = {k: v for k, v in values.items() if v and v.lstrip().startswith("#")}
    assert leaked == {}, f"inline comments became values: {leaked}"


# --- week_ending() --------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 9, 4), "2026-09-04"),   # the Friday itself
        (date(2026, 9, 7), "2026-09-04"),   # Monday
        (date(2026, 9, 8), "2026-09-04"),   # Tuesday -- consensus day
        (date(2026, 9, 9), "2026-09-04"),   # Wednesday -- release day
        (date(2026, 9, 10), "2026-09-04"),  # Thursday
        (date(2026, 9, 11), "2026-09-11"),  # the next Friday
    ],
)
def test_week_ending_is_the_friday_the_report_covers(day, expected):
    assert week_ending(day) == expected
