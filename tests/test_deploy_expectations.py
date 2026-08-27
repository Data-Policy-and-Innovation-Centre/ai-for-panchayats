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


def test_a_fractional_tolerance_survives_json_loading():
    """parse_float=Decimal yields a Decimal tolerance; it must be accepted."""
    payload = (
        '{"schema_version": 1, "relation_row_counts": {}, "known_answer_queries": '
        '[{"name": "t", "sql": "SELECT 1", "expected": [[1.0]], "tolerance": 0.1}]}'
    )
    query = loads(payload).queries[0]

    assert isinstance(query.tolerance, Decimal)
    assert query.tolerance == Decimal("0.1")

    # And it still bounds drift once loaded.
    _check([(1.05,)], ((1.0,),), tolerance=query.tolerance)
    with pytest.raises(KnownAnswerError):
        _check([(1.5,)], ((1.0,),), tolerance=query.tolerance)


def test_textual_results_keep_leading_zeros():
    """Identifiers here are VARCHAR with meaningful leading zeros."""
    with pytest.raises(KnownAnswerError, match="probe"):
        _check([("001",)], (("1",),))

    _check([("001",)], (("001",),))


def test_a_numeric_result_still_accepts_an_exact_decimal_string():
    """The string form exists because JSON cannot carry this exactly."""
    _check([(Decimal("455046197982.4700"),)], (("455046197982.47",),))

    with pytest.raises(KnownAnswerError, match="probe"):
        _check([(Decimal("455046197982.4700"),)], (("455046197982.48",),))


def test_a_numeric_result_against_non_numeric_text_is_a_mismatch():
    with pytest.raises(KnownAnswerError, match="probe"):
        _check([(Decimal("1"),)], (("not-a-number",),))


def test_an_expectations_object_defining_no_checks_is_rejected():
    """An empty contract would let verify() run no loops and publish anyway."""
    with pytest.raises(SnapshotManifestError, match="define no checks"):
        Expectations(relation_row_counts={})

    with pytest.raises(SnapshotManifestError, match="define no checks"):
        loads('{"schema_version": 1}')

    with pytest.raises(SnapshotManifestError, match="define no checks"):
        loads('{"schema_version": 1, "relation_row_counts": {}, "known_answer_queries": []}')


def test_a_misspelled_expectations_key_is_rejected():
    """Silently ignoring it would drop every check it was meant to carry."""
    with pytest.raises(SnapshotManifestError, match="unexpected fields: relation_row_count"):
        loads('{"schema_version": 1, "relation_row_count": {"plan": 3}}')
