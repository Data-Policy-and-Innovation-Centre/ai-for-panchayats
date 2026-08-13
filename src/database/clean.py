"""Shared cleaning rules.

These conventions are applied identically everywhere, because a code cleaned
one way in one table and another way in another table produces joins that
silently return nothing.
"""

from __future__ import annotations

import re

import pandas as pd

# XVFC/2025-26/P/143 -> the voucher's own fiscal year
_VOUCHER_YEAR = re.compile(r"/(\d{4})-(\d{2})/")


def to_code(series: pd.Series) -> pd.Series:
    """Any int/float/text code to a clean string: 119598, never 119598.0.

    Codes must not stay numeric. pandas reads them as float when a column has
    any null, and 0119598 would lose its leading zero on the way to CSV.
    """
    return (series.astype("string").str.strip()
            .str.replace(r"\.0$", "", regex=True))


def to_fiscal_year(series: pd.Series) -> pd.Series:
    """2020 -> "2020-2021". An already-hyphenated value is left alone."""
    def convert(value):
        if pd.isna(value):
            return pd.NA
        text = str(value).strip()
        if "-" in text:
            return text
        start = int(float(text))
        return f"{start}-{start + 1}"

    return series.map(convert).astype("string")


def year_from_voucher_no(voucher_no) -> str | float:
    """Fiscal year encoded in a voucher number.

    The voucher's own year, not the plan's: a plan year is when the activity
    was budgeted, while payment often falls in a later year. Matching the
    bridge on the plan year is what left most rows unmatched.
    """
    match = _VOUCHER_YEAR.search(str(voucher_no))
    if not match:
        return pd.NA
    start = int(match.group(1))
    return f"{start}-{start + 1}"


def strip_leading_zeros(series: pd.Series) -> pd.Series:
    """01 and 1 are the same sequence number."""
    return series.str.lstrip("0").replace("", "0")


def first_coordinate(series: pd.Series) -> pd.Series:
    """21.371,21.372 -> 21.371. Plain numbers pass through unchanged."""
    return pd.to_numeric(
        series.astype("string").str.split(",").str[0].str.strip(),
        errors="coerce")


def count_coordinates(series: pd.Series) -> pd.Series:
    """How many captures a cell holds, so a multi-capture row stays visible."""
    return (series.astype("string").str.count(",").fillna(0).astype(int) + 1)
