from __future__ import annotations

import re

import pandas as pd


# ---------------------------------------------------------------------
# General dataframe cleaning
# ---------------------------------------------------------------------

def clean_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize dataframe column names.

    Example:
        "Activity Code"                  -> "activity_code"
        "Approved Cost (in Rs.)"         -> "approved_cost_in_rs"
        "Gram Panchayat & Equivalent"    -> "gram_panchayat_equivalent"
    """
    out = df.copy()

    out.columns = (
        pd.Index(out.columns)
        .astype(str)
        .str.strip()
        .str.lower()
        .str.replace("&", "and", regex=False)
        .str.replace(r"[()/.-]", " ", regex=True)
        .str.replace(r"[^a-z0-9\s_]", "", regex=True)
        .str.replace(r"\s+", "_", regex=True)
        .str.replace(r"_+", "_", regex=True)
        .str.strip("_")
    )

    return out


def clean_strings(series: pd.Series) -> pd.Series:
    """
    Clean text while preserving the original wording.

    - trims leading/trailing whitespace
    - collapses repeated spaces
    - converts blank strings to missing values
    """
    s = series.astype("string")

    s = (
        s.str.strip()
        .str.replace(r"\s+", " ", regex=True)
    )

    return s.replace(
        {
            "": pd.NA,
            "nan": pd.NA,
            "None": pd.NA,
            "NULL": pd.NA,
            "null": pd.NA,
            "NA": pd.NA,
            "N/A": pd.NA,
            "-": pd.NA,
        }
    )


# ---------------------------------------------------------------------
# Identifier cleaning
# ---------------------------------------------------------------------

def clean_identifier(series: pd.Series) -> pd.Series:
    """
    Clean identifier-like columns while preserving them as strings.

    Useful for:
        activity_code
        gp_lgd_code
        plan_code
        voucher number components
        approval IDs

    Excel/CSV files sometimes turn identifiers into values such as:
        119598.0

    This converts that back to:
        "119598"
    """
    s = clean_strings(series)

    s = s.str.replace(
        r"^(-?\d+)\.0$",
        r"\1",
        regex=True,
    )

    return s


def clean_lgd_code(series: pd.Series) -> pd.Series:
    """
    Normalize LGD codes.

    LGD codes are identifiers, not numeric measures, so they are
    always stored as strings.
    """
    return clean_identifier(series)


def clean_activity_code(series: pd.Series) -> pd.Series:
    """
    Normalize eGramSwaraj activity codes as strings.
    """
    return clean_identifier(series)


# ---------------------------------------------------------------------
# Financial year cleaning
# ---------------------------------------------------------------------

def normalize_financial_year_value(value: object) -> str | pd.NA:
    """
    Normalize common financial-year representations to YYYY-YYYY.

    Examples:
        2025-26       -> 2025-2026
        2025-2026     -> 2025-2026
        2025 / 26     -> 2025-2026
        2025_2026     -> 2025-2026
        2025          -> 2025-2026
    """
    if pd.isna(value):
        return pd.NA

    text = str(value).strip()

    if not text:
        return pd.NA

    # Remove accidental .0 from numeric imports
    text = re.sub(r"\.0$", "", text)

    # Replace common separators
    text = re.sub(r"[/_–—]", "-", text)
    text = re.sub(r"\s+", "", text)

    # YYYY-YYYY
    match = re.fullmatch(r"(\d{4})-(\d{4})", text)

    if match:
        start = int(match.group(1))
        end = int(match.group(2))

        if end == start + 1:
            return f"{start:04d}-{end:04d}"

        return pd.NA

    # YYYY-YY
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)

    if match:
        start = int(match.group(1))
        short_end = int(match.group(2))

        expected_end = start + 1

        if expected_end % 100 != short_end:
            return pd.NA

        return f"{start:04d}-{expected_end:04d}"

    # Single year
    match = re.fullmatch(r"\d{4}", text)

    if match:
        start = int(text)
        return f"{start:04d}-{start + 1:04d}"

    return pd.NA


def clean_financial_year(series: pd.Series) -> pd.Series:
    """
    Normalize a financial-year column to YYYY-YYYY.
    """
    return series.map(
        normalize_financial_year_value
    ).astype("string")


# ---------------------------------------------------------------------
# Numeric cleaning
# ---------------------------------------------------------------------

def clean_numeric(
    series: pd.Series,
    *,
    allow_negative: bool = True,
) -> pd.Series:
    """
    Convert numeric-looking values to numbers.

    Handles:
        "1,25,000"     -> 125000
        "₹25,000"      -> 25000
        "Rs. 10,500"   -> 10500
        ""             -> NaN
    """
    s = series.astype("string").str.strip()

    s = (
        s.str.replace(",", "", regex=False)
        .str.replace("₹", "", regex=False)
        .str.replace(r"(?i)\brs\.?\b", "", regex=True)
        .str.strip()
    )

    values = pd.to_numeric(
        s,
        errors="coerce",
    )

    if not allow_negative:
        values = values.where(values >= 0)

    return values


def clean_amount(series: pd.Series) -> pd.Series:
    """
    Clean monetary amount columns.

    Negative values are preserved because accounting data may
    legitimately contain adjustments/reversals.
    """
    return clean_numeric(
        series,
        allow_negative=True,
    )


def clean_nonnegative_amount(series: pd.Series) -> pd.Series:
    """
    Clean amounts that should never be negative, such as
    planned/estimated costs.
    """
    return clean_numeric(
        series,
        allow_negative=False,
    )


def clean_integer(series: pd.Series) -> pd.Series:
    """
    Convert integer-like values to pandas nullable Int64.
    """
    values = clean_numeric(series)

    values = values.where(
        values.isna() | (values % 1 == 0)
    )

    return values.astype("Int64")


# ---------------------------------------------------------------------
# Date cleaning
# ---------------------------------------------------------------------

def clean_date(
    series: pd.Series,
    *,
    dayfirst: bool = True,
) -> pd.Series:
    """
    Convert date-like values to pandas datetime.

    eGramSwaraj exports commonly contain DD/MM/YYYY values,
    therefore dayfirst=True is the default.
    """
    s = clean_strings(series)

    return pd.to_datetime(
        s,
        errors="coerce",
        dayfirst=dayfirst,
    )


# ---------------------------------------------------------------------
# Boolean cleaning
# ---------------------------------------------------------------------

TRUE_VALUES = {
    "yes",
    "y",
    "true",
    "1",
    "available",
    "present",
}

FALSE_VALUES = {
    "no",
    "n",
    "false",
    "0",
    "not available",
    "absent",
}


def clean_boolean(series: pd.Series) -> pd.Series:
    """
    Convert common boolean representations to pandas boolean dtype.
    """
    s = (
        clean_strings(series)
        .str.lower()
    )

    result = pd.Series(
        pd.NA,
        index=series.index,
        dtype="boolean",
    )

    result.loc[s.isin(TRUE_VALUES)] = True
    result.loc[s.isin(FALSE_VALUES)] = False

    return result


# ---------------------------------------------------------------------
# Empty-value handling
# ---------------------------------------------------------------------

def normalize_nulls(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize common textual null representations across a dataframe.

    This is deliberately conservative and does not modify numeric/date
    columns beyond replacing obvious blank text.
    """
    out = df.copy()

    null_values = [
        "",
        " ",
        "nan",
        "NaN",
        "None",
        "NONE",
        "null",
        "NULL",
        "N/A",
        "NA",
    ]

    return out.replace(
        null_values,
        pd.NA,
    )


# ---------------------------------------------------------------------
# Duplicate helpers
# ---------------------------------------------------------------------

def remove_exact_duplicates(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Remove rows that are completely identical.
    """
    return (
        df
        .drop_duplicates()
        .reset_index(drop=True)
    )


def deduplicate_by_key(
    df: pd.DataFrame,
    key_columns: list[str],
    *,
    keep: str = "last",
) -> pd.DataFrame:
    """
    Remove duplicate records using a defined business key.

    This will be useful later when we export schema tables and want
    repeated source files/runs to avoid creating repeated records.
    """
    missing = [
        column
        for column in key_columns
        if column not in df.columns
    ]

    if missing:
        raise KeyError(
            f"Cannot deduplicate. Missing key column(s): {missing}"
        )

    return (
        df
        .drop_duplicates(
            subset=key_columns,
            keep=keep,
        )
        .reset_index(drop=True)
    )
