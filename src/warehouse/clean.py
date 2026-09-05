"""Shared, pure cleaning conventions for the warehouse transform layer.

Every function here takes a pandas Series and returns a pandas Series; none
touch a database or the filesystem, so each is testable on a two-row fixture.

Money is parsed straight from its textual representation into
``decimal.Decimal`` and never round-tripped through a binary ``float``.  A
float64 column cannot represent 19.99 exactly, and summing many such columns
compounds the error; Decimal keeps every rupee-and-paisa amount exact from
the source value to the DuckDB ``DECIMAL`` column that stores it.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import pandas as pd

from .load_common import MONEY_GROUPED

_TRAILING_DOT_ZERO = re.compile(r"^(-?\d+)\.0$")
# A trailing \b after an optional literal "." never matches ("." and the
# following space/digit are both non-word characters, so there is no
# word/non-word transition there): "Rs. 1,25,000" would then strip only
# "Rs", leaving a stray "." that breaks Decimal parsing. The leading \b is
# enough to avoid matching "rs" mid-word.
_CURRENCY_NOISE = re.compile(r"(?i)\brs\.?|₹")


def ungroup_digits(text: str) -> str | None:
    """Strip digit-group commas, or return None if they are malformed.

    Stripping unconditionally turns bad input into a plausible number rather
    than a null: ``"1,2"`` becomes 12 and ``"12,,34"`` becomes 1234, so a
    typo in an expenditure amount arrives in the warehouse as a number
    nobody can tell is wrong (#127).

    ``MONEY_GROUPED`` comes from ``load_common``, which already had to
    answer this for the CSV loaders and documents the reasoning: both Indian
    and Western grouping are accepted, because 1,00,000 is one lakh and not
    a malformed 100,000, and a validator that only knew three-digit groups
    would reject Odisha financial data wholesale.
    """

    if "," not in text:
        return text
    if not MONEY_GROUPED.match(text):
        return None
    return text.replace(",", "")


def to_code(series: pd.Series) -> pd.Series:
    """Any int/float/text identifier to a clean string.

    Identifiers are never numeric: a float round-trip silently drops a
    leading zero (e.g. ``0119598`` -> ``119598.0``), and a bare code is never
    treated as a globally unique key on its own -- callers combine it with
    ``source_system``/``source_run_id`` or another business dimension.
    """

    cleaned = series.astype("string").str.strip()
    cleaned = cleaned.str.replace(_TRAILING_DOT_ZERO, r"\1", regex=True)
    return cleaned.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})


def to_decimal_money(series: pd.Series, *, places: int = 2) -> pd.Series:
    """Parse a monetary column into exact ``decimal.Decimal`` values.

    Accepts plain numbers, comma-grouped strings, and rupee-symbol/``Rs.``
    prefixed strings. A value that cannot be parsed becomes ``None`` rather
    than raising, so one malformed cell does not fail an entire table load;
    callers that must reject such rows do so explicitly via quarantine.
    """

    quantum = Decimal(1).scaleb(-places)

    def parse(value: object) -> Decimal | None:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        if isinstance(value, Decimal):
            return value.quantize(quantum)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return Decimal(value).quantize(quantum)
        if isinstance(value, float):
            # Route even native floats through their repr rather than the
            # Decimal(float) constructor, which would faithfully reproduce
            # the binary approximation instead of the intended value.
            text = repr(value)
        else:
            text = str(value)
        text = text.strip()
        if not text or text.lower() in {"nan", "none", "null", "na", "n/a", "-"}:
            return None
        text = _CURRENCY_NOISE.sub("", text).strip()
        if not text:
            return None
        ungrouped = ungroup_digits(text)
        if ungrouped is None:
            return None
        try:
            return Decimal(ungrouped).quantize(quantum)
        except InvalidOperation:
            return None

    return series.map(parse)


def to_int(series: pd.Series) -> pd.Series:
    """Convert integer-like values to pandas nullable Int64."""

    # Same grouping rule as to_decimal_money above: a count is not money, but
    # "1,2" -> 12 is the identical silent rewrite, and leaving one spelling
    # of the defect fixed and its neighbour broken is worse than either.
    # ungroup_digits returns None for a malformed value, which to_numeric
    # then reads as NA instead of inventing a number.
    text = series.astype("string").str.strip()
    # .astype("string") after .map is load-bearing, not tidiness: map returns
    # an object-dtype Series, pd.to_numeric then produces a numpy dtype, and
    # .astype("Int64") *raises* on values it used to coerce -- an out-of-range
    # count would abort the build instead of becoming NA.
    ungrouped = text.map(
        lambda value: value if not isinstance(value, str) else ungroup_digits(value)
    ).astype("string")
    numeric = pd.to_numeric(ungrouped, errors="coerce")
    return numeric.round().astype("Int64")


def strip_leading_zeros(series: pd.Series) -> pd.Series:
    """01 and 1 are the same sequence number."""

    cleaned = series.astype("string")
    stripped = cleaned.str.lstrip("0")
    return stripped.mask(cleaned.notna() & (stripped == ""), "0")


def to_datetime(series: pd.Series, *, dayfirst: bool = False) -> pd.Series:
    """Best-effort timestamp parsing; unparseable values become null."""

    return pd.to_datetime(series, errors="coerce", dayfirst=dayfirst, format="mixed")
