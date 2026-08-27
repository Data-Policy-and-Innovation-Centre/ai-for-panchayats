"""Numeric-comparison contract for known-answer checks.

These are the cases where a *wrong* snapshot could pass the gate. They exist
because the comparison is the last thing standing between a substituted
artifact and a plausible-looking answer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.deploy.errors import KnownAnswerError, SnapshotManifestError
from src.deploy.expectations import Expectations, KnownAnswerQuery, loads, verify


class FakeConn:
    """Returns canned rows so the comparison can be tested without a database."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self._last = sql
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


def _check(actual_rows, expected_rows, tolerance=None):
    verify(
        FakeConn(actual_rows),
        Expectations(
            relation_row_counts={},
            queries=(
                KnownAnswerQuery(
                    name="probe", sql="SELECT 1", expected=expected_rows, tolerance=tolerance
                ),
            ),
        ),
    )


def test_exact_comparison_does_not_collapse_large_integers():
    """float() maps both of these to the same binary64 value."""
    with pytest.raises(KnownAnswerError, match="probe"):
        _check([(9007199254740993,)], ((9007199254740992,),))

    _check([(9007199254740993,)], ((9007199254740993,),))


def test_exact_comparison_preserves_decimal_precision():
    with pytest.raises(KnownAnswerError, match="probe"):
        _check([(Decimal("123456789012345678.0001"),)], ((Decimal("123456789012345678.0002"),),))

    # Trailing zeros are numerically equal, and must stay equal.
    _check([(Decimal("455046197982.4700"),)], ((Decimal("455046197982.47"),),))


def test_a_json_number_survives_loading_without_binary64_rounding():
    payload = (
        '{"schema_version": 1, "relation_row_counts": {}, "known_answer_queries": '
        '[{"name": "total", "sql": "SELECT 1", "expected": [[455046197982.47]], '
        '"tolerance": null}]}'
    )
    expectations = loads(payload)
    (value,) = expectations.queries[0].expected[0]

    assert isinstance(value, Decimal)
    assert value == Decimal("455046197982.47")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_results_are_rejected_even_with_a_tolerance(bad):
    """abs(NaN - x) > tol is False, so a NaN would otherwise pass silently."""
    with pytest.raises(KnownAnswerError, match="non-finite"):
        _check([(bad,)], ((5.0,),), tolerance=0.1)


def test_a_tolerance_still_bounds_drift():
    _check([(5.05,)], ((5.0,),), tolerance=0.1)
    with pytest.raises(KnownAnswerError, match=r"> 0.01"):
        _check([(5.05,)], ((5.0,),), tolerance=0.01)


def test_non_numeric_values_compare_directly():
    _check([("2025-2026",)], (("2025-2026",),))
    with pytest.raises(KnownAnswerError, match="probe"):
        _check([("2025-2026",)], (("2025-26",),))


def test_tolerance_must_be_finite_and_non_negative():
    for bad in (-1, float("nan"), float("inf")):
        with pytest.raises(SnapshotManifestError, match="finite and non-negative"):
            KnownAnswerQuery(name="p", sql="SELECT 1", expected=((1,),), tolerance=bad)
