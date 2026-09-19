"""Tests for `src/job_hunter/rootutil.py`'s shared argparse helpers not already covered by
`tests/test_project_argument.py` (which owns `add_project_argument`/`resolve_project_root`)."""

import argparse

import pytest

from job_hunter.rootutil import nonneg_int


@pytest.mark.parametrize("value", ["0", "1", "42", "1000000"])
def test_nonneg_int_accepts_nonnegative_values(value):
    assert nonneg_int(value) == int(value)


@pytest.mark.parametrize("value", ["-1", "-42", "-1000000"])
def test_nonneg_int_rejects_negative_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        nonneg_int(value)


@pytest.mark.parametrize("value", ["abc", "", "1.5", "1,000"])
def test_nonneg_int_rejects_non_integer_values(value):
    """`int()`'s own `ValueError` is what a caller actually gets for these -- argparse treats
    that identically to `ArgumentTypeError` (both produce a clean "invalid ... value" CLI error),
    so this only needs to confirm it raises, not which exact exception type."""
    with pytest.raises(ValueError):
        nonneg_int(value)
