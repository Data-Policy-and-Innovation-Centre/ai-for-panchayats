from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import config


# =====================================================================
# Business keys
# =====================================================================


TABLE_KEYS: dict[str, list[str]] = {
    "gram_panchayat": [
        "gp_lgd_code",
    ],

    "plan": [
        "plan_code",
    ],

    "planned_activity": [
        "activity_code",
    ],

    "activity_asset": [
        "activity_code",
    ],

    "activity_fund": [
        "activity_code",
    ],

    "activity_training": [
        "activity_code",
    ],

    "activity_delegation": [
        "activity_code",
    ],

    "activity_community_service": [
        "activity_code",
    ],

    "activity_nsap": [
        "activity_code",
        "category",
        "age_band",
        "gender",
    ],

    "activity_expenditure": [
        "expenditure_id",
    ],

    "voucher": [
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
    ],

    # ---------------------------------------------------------
    # IMPORTANT:
    # voucher_line_no preserves separate occurrences of the
    # same voucher within one expenditure activity.
    # ---------------------------------------------------------
    "activity_voucher": [
        "expenditure_id",
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
        "voucher_line_no",
    ],

    "admin_approval": [
        "row_id",
    ],

    "admin_approval_scheme": [
        "row_id",
    ],

    "technical_approval": [
        "row_id",
    ],

    "physical_progress": [
        "row_id",
    ],

    "dim_code": [
        "variable",
        "code",
    ],

    "dim_welfare_scheme": [
        "scheme_code",
    ],

    "dim_lsdg_theme": [
        "focus_area_name",
    ],
}


# =====================================================================
# Export result
# =====================================================================


@dataclass
class ExportResult:
    """
    Summary of one table export.
    """

    table_name: str
    path: Path

    existing_rows: int
    incoming_rows: int

    inserted_rows: int
    updated_rows: int

    final_rows: int


# =====================================================================
# Read existing table
# =====================================================================


def _read_existing(
    path: Path,
) -> pd.DataFrame:
    """
    Read an existing schema table if present.
    """

    if not path.exists():
        return pd.DataFrame()

    if path.stat().st_size == 0:
        return pd.DataFrame()

    return pd.read_csv(
        path,
        low_memory=True,
    )


# =====================================================================
# Key normalization
# =====================================================================


def _normalize_key_values(
    df: pd.DataFrame,
    key_columns: list[str],
) -> pd.DataFrame:
    """
    Normalize business-key columns.
    """

    out = df.copy()

    for column in key_columns:

        if column not in out.columns:

            raise KeyError(
                f"Business key column '{column}' "
                f"is missing from dataframe."
            )

        out[column] = (
            out[column]
            .astype("string")
            .str.strip()
        )

        out[column] = (
            out[column]
            .str.replace(
                r"\.0$",
                "",
                regex=True,
            )
        )

    return out


def _build_key_series(
    df: pd.DataFrame,
    key_columns: list[str],
) -> pd.Series:
    """
    Build comparable composite business keys.
    """

    normalized = _normalize_key_values(
        df,
        key_columns,
    )

    key_frame = normalized[
        key_columns
    ].fillna(
        "<NULL>"
    )

    return (
        key_frame
        .astype(
            "string"
        )
        .agg(
            "||".join,
            axis=1,
        )
    )


# =====================================================================
# Merge incoming + existing
# =====================================================================


def merge_table(
    *,
    table_name: str,
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    int,
    int,
]:
    """
    Incrementally merge incoming rows into existing schema table.

    New keys:
        inserted

    Existing keys:
        replaced by incoming row

    Duplicate incoming keys:
        final occurrence retained
    """

    if table_name not in TABLE_KEYS:

        raise KeyError(
            f"No business key configured for table: "
            f"{table_name}"
        )

    key_columns = TABLE_KEYS[
        table_name
    ]

    incoming = incoming.copy()

    missing_columns = [
        column
        for column in key_columns
        if column not in incoming.columns
    ]

    if missing_columns:

        raise KeyError(
            f"{table_name}: missing business-key columns: "
            f"{missing_columns}"
        )

    if incoming.empty:

        return (
            existing.copy(),
            0,
            0,
        )

    incoming = _normalize_key_values(
        incoming,
        key_columns,
    )

    incoming = (
        incoming
        .drop_duplicates(
            subset=key_columns,
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    if existing.empty:

        return (
            incoming,
            len(incoming),
            0,
        )

    missing_existing_columns = [
        column
        for column in key_columns
        if column not in existing.columns
    ]

    if missing_existing_columns:

        raise KeyError(
            f"{table_name}: existing file is missing "
            f"business-key columns: "
            f"{missing_existing_columns}"
        )

    existing = _normalize_key_values(
        existing,
        key_columns,
    )

    existing = (
        existing
        .drop_duplicates(
            subset=key_columns,
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    existing_keys = set(
        _build_key_series(
            existing,
            key_columns,
        )
    )

    incoming_key_series = _build_key_series(
        incoming,
        key_columns,
    )

    incoming_keys = set(
        incoming_key_series
    )

    updated_rows = len(
        existing_keys
        & incoming_keys
    )

    inserted_rows = len(
        incoming_keys
        - existing_keys
    )

    existing_key_series = _build_key_series(
        existing,
        key_columns,
    )

    keep_existing = (
        ~existing_key_series.isin(
            incoming_keys
        )
    )

    existing_remaining = existing[
        keep_existing
    ].copy()

    all_columns = list(
        dict.fromkeys(
            [
                *existing_remaining.columns,
                *incoming.columns,
            ]
        )
    )

    for column in all_columns:

        if column not in existing_remaining.columns:
            existing_remaining[column] = pd.NA

        if column not in incoming.columns:
            incoming[column] = pd.NA

    existing_remaining = (
        existing_remaining[
            all_columns
        ]
    )

    incoming = incoming[
        all_columns
    ]

    merged = pd.concat(
        [
            existing_remaining,
            incoming,
        ],
        ignore_index=True,
    )

    merged = (
        merged
        .drop_duplicates(
            subset=key_columns,
            keep="last",
        )
        .reset_index(
            drop=True
        )
    )

    return (
        merged,
        inserted_rows,
        updated_rows,
    )


# =====================================================================
# Export one table
# =====================================================================


def export_table(
    table_name: str,
    incoming: pd.DataFrame,
) -> ExportResult:
    """
    Export one schema table incrementally.
    """

    if table_name not in config.SCHEMA_TABLES:

        raise KeyError(
            f"Unknown schema table: "
            f"{table_name}"
        )

    if table_name not in TABLE_KEYS:

        raise KeyError(
            f"No business key configured for table: "
            f"{table_name}"
        )

    path = config.TABLE_PATHS[
        table_name
    ]

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    existing = _read_existing(
        path
    )

    existing_rows = len(
        existing
    )

    incoming_rows = len(
        incoming
    )

    (
        merged,
        inserted_rows,
        updated_rows,
    ) = merge_table(
        table_name=table_name,
        existing=existing,
        incoming=incoming,
    )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    merged.to_csv(
        temporary_path,
        index=False,
    )

    temporary_path.replace(
        path
    )

    return ExportResult(
        table_name=table_name,
        path=path,
        existing_rows=existing_rows,
        incoming_rows=incoming_rows,
        inserted_rows=inserted_rows,
        updated_rows=updated_rows,
        final_rows=len(merged),
    )


# =====================================================================
# Export one or more tables
# =====================================================================


def export_tables(
    tables: dict[str, pd.DataFrame],
) -> list[ExportResult]:
    """
    Export only supplied schema tables.

    Supports source-specific builders.
    """

    results: list[
        ExportResult
    ] = []

    if not tables:
        return results

    unknown_tables = (
        set(
            tables.keys()
        )
        - set(
            config.SCHEMA_TABLES
        )
    )

    if unknown_tables:

        raise KeyError(
            "Unknown schema table(s): "
            + ", ".join(
                sorted(
                    unknown_tables
                )
            )
        )

    for table_name, dataframe in tables.items():

        if not isinstance(
            dataframe,
            pd.DataFrame,
        ):

            raise TypeError(
                f"{table_name}: expected pandas DataFrame, "
                f"got {type(dataframe).__name__}"
            )

        result = export_table(
            table_name,
            dataframe,
        )

        results.append(
            result
        )

    return results


# =====================================================================
# Validation report
# =====================================================================


def export_validation_report(
    report: pd.DataFrame,
) -> Path:

    config.QUALITY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = config.VALIDATION_REPORT_PATH

    report.to_csv(
        path,
        index=False,
    )

    return path


# =====================================================================
# Quarantine
# =====================================================================


def export_quarantine(
    quarantine: pd.DataFrame,
) -> Path:

    config.QUALITY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = config.QUARANTINE_PATH

    quarantine.to_csv(
        path,
        index=False,
    )

    return path


# =====================================================================
# Export report
# =====================================================================


def export_export_report(
    results: list[ExportResult],
) -> Path:

    config.QUALITY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    rows = []

    for result in results:

        rows.append(
            {
                "table_name":
                    result.table_name,

                "path":
                    str(
                        result.path
                    ),

                "existing_rows":
                    result.existing_rows,

                "incoming_rows":
                    result.incoming_rows,

                "inserted_rows":
                    result.inserted_rows,

                "updated_rows":
                    result.updated_rows,

                "final_rows":
                    result.final_rows,
            }
        )

    report = pd.DataFrame(
        rows,
        columns=[
            "table_name",
            "path",
            "existing_rows",
            "incoming_rows",
            "inserted_rows",
            "updated_rows",
            "final_rows",
        ],
    )

    path = config.EXPORT_REPORT_PATH

    report.to_csv(
        path,
        index=False,
    )

    return path


# =====================================================================
# Console summary
# =====================================================================


def print_export_summary(
    results: list[ExportResult],
) -> None:

    if not results:

        print(
            "\nNo tables exported.\n"
        )

        return

    print(
        "\nExport summary\n"
    )

    header = (
        f"{'table':<32}"
        f"{'existing':>12}"
        f"{'incoming':>12}"
        f"{'inserted':>12}"
        f"{'updated':>12}"
        f"{'final':>12}"
    )

    print(
        header
    )

    print(
        "-" * len(
            header
        )
    )

    for result in results:

        print(
            f"{result.table_name:<32}"
            f"{result.existing_rows:>12,}"
            f"{result.incoming_rows:>12,}"
            f"{result.inserted_rows:>12,}"
            f"{result.updated_rows:>12,}"
            f"{result.final_rows:>12,}"
        )