from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from schema_tables import config
from schema_tables.export import TABLE_KEYS


# =====================================================================
# Logging
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)

logger = logging.getLogger(__name__)


# =====================================================================
# Tables
# =====================================================================

TABLES = [
    "gram_panchayat",
    "plan",
    "planned_activity",
    "activity_asset",
    "activity_fund",
    "activity_training",
    "activity_delegation",
    "activity_community_service",
    "activity_nsap",
    "activity_expenditure",
    "voucher",
    "activity_voucher",
    "admin_approval",
    "admin_approval_scheme",
    "technical_approval",
    "physical_progress",
    "dim_code",
    "dim_welfare_scheme",
    "dim_lsdg_theme",
]


# =====================================================================
# Foreign-key relationships
# =====================================================================

FOREIGN_KEYS = [
    # ---------------------------------------------------------
    # Core hierarchy
    # ---------------------------------------------------------
    {
        "child_table": "plan",
        "child_columns": ["gp_lgd_code"],
        "parent_table": "gram_panchayat",
        "parent_columns": ["gp_lgd_code"],
    },
    {
        "child_table": "planned_activity",
        "child_columns": ["plan_code"],
        "parent_table": "plan",
        "parent_columns": ["plan_code"],
    },
    {
        "child_table": "planned_activity",
        "child_columns": ["gp_lgd_code"],
        "parent_table": "gram_panchayat",
        "parent_columns": ["gp_lgd_code"],
    },

    # ---------------------------------------------------------
    # Planning satellites -> planned_activity
    # ---------------------------------------------------------
    {
        "child_table": "activity_asset",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_fund",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_training",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_delegation",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_community_service",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_nsap",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },

    # ---------------------------------------------------------
    # Expenditure
    # ---------------------------------------------------------
    {
        "child_table": "activity_expenditure",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "activity_expenditure",
        "child_columns": ["plan_code"],
        "parent_table": "plan",
        "parent_columns": ["plan_code"],
    },
    {
        "child_table": "activity_expenditure",
        "child_columns": ["gp_lgd_code"],
        "parent_table": "gram_panchayat",
        "parent_columns": ["gp_lgd_code"],
    },

    # ---------------------------------------------------------
    # Activity voucher bridge
    # ---------------------------------------------------------
    {
        "child_table": "activity_voucher",
        "child_columns": ["expenditure_id"],
        "parent_table": "activity_expenditure",
        "parent_columns": ["expenditure_id"],
    },
    {
        "child_table": "activity_voucher",
        "child_columns": ["voucher_pk"],
        "parent_table": "voucher",
        "parent_columns": ["voucher_pk"],
    },

    # ---------------------------------------------------------
    # Approvals
    # ---------------------------------------------------------
    {
        "child_table": "admin_approval",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "admin_approval_scheme",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
    {
        "child_table": "admin_approval_scheme",
        "child_columns": ["parent_row_id"],
        "parent_table": "admin_approval",
        "parent_columns": ["row_id"],
    },

    # ---------------------------------------------------------
    # Technical approval
    # ---------------------------------------------------------
    {
        "child_table": "technical_approval",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },

    # ---------------------------------------------------------
    # Physical progress
    # ---------------------------------------------------------
    {
        "child_table": "physical_progress",
        "child_columns": ["activity_code"],
        "parent_table": "planned_activity",
        "parent_columns": ["activity_code"],
    },
]


# =====================================================================
# Helpers
# =====================================================================

def table_path(
    table_name: str,
) -> Path:
    return config.TABLE_PATHS[
        table_name
    ]


def normalize_key_frame(
    df: pd.DataFrame,
    columns: list[str],
) -> pd.DataFrame:
    """
    Normalize identifiers before key comparison.
    """

    out = df[
        columns
    ].copy()

    for column in columns:

        out[column] = (
            out[column]
            .astype("string")
            .str.strip()
            .str.replace(
                r"\.0$",
                "",
                regex=True,
            )
        )

    return out


def hash_keys(
    df: pd.DataFrame,
    columns: list[str],
) -> pd.Series:
    """
    Produce stable uint64 hashes for one or more key columns.

    This uses substantially less memory than storing millions
    of full Python strings in sets.
    """

    normalized = normalize_key_frame(
        df,
        columns,
    )

    normalized = normalized.fillna(
        "<NULL>"
    )

    return pd.util.hash_pandas_object(
        normalized,
        index=False,
    )


def count_csv_rows(
    path: Path,
) -> int:
    """
    Count data rows without loading entire CSV.
    """

    with path.open(
        "r",
        encoding="utf-8",
        errors="ignore",
    ) as f:

        line_count = sum(
            1
            for _ in f
        )

    return max(
        line_count - 1,
        0,
    )


def get_columns(
    path: Path,
) -> list[str]:

    try:

        return pd.read_csv(
            path,
            nrows=0,
        ).columns.tolist()

    except pd.errors.EmptyDataError:

        return []


# =====================================================================
# Business-key validation
# =====================================================================

def validate_business_key(
    table_name: str,
    key_columns: list[str],
    *,
    chunk_size: int,
) -> dict:
    """
    Check null and duplicate business keys.

    Keeps only 64-bit hashes in memory.
    """

    path = table_path(
        table_name
    )

    seen: set[int] = set()

    duplicate_count = 0
    null_key_rows = 0
    total_rows = 0

    duplicate_samples: list[str] = []

    for chunk in pd.read_csv(
        path,
        usecols=key_columns,
        dtype="string",
        chunksize=chunk_size,
        low_memory=True,
    ):

        total_rows += len(
            chunk
        )

        normalized = normalize_key_frame(
            chunk,
            key_columns,
        )

        null_mask = (
            normalized[
                key_columns
            ]
            .isna()
            .any(
                axis=1
            )
        )

        null_key_rows += int(
            null_mask.sum()
        )

        valid = normalized[
            ~null_mask
        ].copy()

        hashes = pd.util.hash_pandas_object(
            valid,
            index=False,
        )

        for idx, hashed_value in zip(
            valid.index,
            hashes,
        ):

            h = int(
                hashed_value
            )

            if h in seen:

                duplicate_count += 1

                if len(
                    duplicate_samples
                ) < 10:

                    values = [
                        str(
                            valid.at[
                                idx,
                                column,
                            ]
                        )
                        for column
                        in key_columns
                    ]

                    duplicate_samples.append(
                        " | ".join(
                            values
                        )
                    )

            else:

                seen.add(
                    h
                )

    return {
        "check_type":
            "business_key",

        "table":
            table_name,

        "relationship":
            "+".join(
                key_columns
            ),

        "total_rows":
            total_rows,

        "matched_rows":
            pd.NA,

        "unmatched_rows":
            pd.NA,

        "match_pct":
            pd.NA,

        "null_key_rows":
            null_key_rows,

        "duplicate_key_rows":
            duplicate_count,

        "sample_unmatched":
            "",

        "sample_duplicates":
            "; ".join(
                duplicate_samples
            ),

        "status":
            (
                "PASS"
                if (
                    null_key_rows == 0
                    and duplicate_count == 0
                )
                else "FAIL"
            ),
    }


# =====================================================================
# Parent-key loader
# =====================================================================

def load_parent_hashes(
    table_name: str,
    columns: list[str],
    *,
    chunk_size: int,
) -> set[int]:
    """
    Load hashed parent keys.

    Only 64-bit integers are kept in memory.
    """

    path = table_path(
        table_name
    )

    hashes: set[int] = set()

    for chunk in pd.read_csv(
        path,
        usecols=columns,
        dtype="string",
        chunksize=chunk_size,
        low_memory=True,
    ):

        normalized = normalize_key_frame(
            chunk,
            columns,
        )

        valid = normalized.dropna(
            subset=columns
        )

        current = pd.util.hash_pandas_object(
            valid,
            index=False,
        )

        hashes.update(
            int(value)
            for value in current
        )

    return hashes


# =====================================================================
# Foreign-key validation
# =====================================================================

def validate_foreign_key(
    relation: dict,
    *,
    chunk_size: int,
) -> dict:

    child_table = relation[
        "child_table"
    ]

    child_columns = relation[
        "child_columns"
    ]

    parent_table = relation[
        "parent_table"
    ]

    parent_columns = relation[
        "parent_columns"
    ]

    logger.info(
        "FK: %s.%s -> %s.%s",
        child_table,
        "+".join(
            child_columns
        ),
        parent_table,
        "+".join(
            parent_columns
        ),
    )

    parent_hashes = load_parent_hashes(
        parent_table,
        parent_columns,
        chunk_size=chunk_size,
    )

    child_path = table_path(
        child_table
    )

    total_rows = 0
    non_null_rows = 0
    matched_rows = 0
    unmatched_rows = 0
    null_key_rows = 0

    unmatched_samples: list[str] = []

    for chunk in pd.read_csv(
        child_path,
        usecols=child_columns,
        dtype="string",
        chunksize=chunk_size,
        low_memory=True,
    ):

        total_rows += len(
            chunk
        )

        normalized = normalize_key_frame(
            chunk,
            child_columns,
        )

        null_mask = (
            normalized[
                child_columns
            ]
            .isna()
            .any(
                axis=1
            )
        )

        null_key_rows += int(
            null_mask.sum()
        )

        valid = normalized[
            ~null_mask
        ].copy()

        non_null_rows += len(
            valid
        )

        child_hashes = pd.util.hash_pandas_object(
            valid,
            index=False,
        )

        for idx, hashed_value in zip(
            valid.index,
            child_hashes,
        ):

            if int(
                hashed_value
            ) in parent_hashes:

                matched_rows += 1

            else:

                unmatched_rows += 1

                if len(
                    unmatched_samples
                ) < 10:

                    values = [
                        str(
                            valid.at[
                                idx,
                                column,
                            ]
                        )
                        for column
                        in child_columns
                    ]

                    unmatched_samples.append(
                        " | ".join(
                            values
                        )
                    )

    if non_null_rows:

        match_pct = (
            matched_rows
            / non_null_rows
            * 100
        )

    else:

        match_pct = 100.0

    del parent_hashes

    return {
        "check_type":
            "foreign_key",

        "table":
            child_table,

        "relationship":
            (
                f"{child_table}."
                f"{'+'.join(child_columns)}"
                f" -> "
                f"{parent_table}."
                f"{'+'.join(parent_columns)}"
            ),

        "total_rows":
            total_rows,

        "matched_rows":
            matched_rows,

        "unmatched_rows":
            unmatched_rows,

        "match_pct":
            round(
                match_pct,
                4,
            ),

        "null_key_rows":
            null_key_rows,

        "duplicate_key_rows":
            pd.NA,

        "sample_unmatched":
            "; ".join(
                unmatched_samples
            ),

        "sample_duplicates":
            "",

        "status":
            (
                "PASS"
                if unmatched_rows == 0
                else "WARN"
            ),
    }


# =====================================================================
# Table inventory
# =====================================================================

def build_inventory() -> pd.DataFrame:

    rows = []

    for table_name in TABLES:

        path = table_path(
            table_name
        )

        if not path.exists():

            rows.append(
                {
                    "table":
                        table_name,

                    "exists":
                        False,

                    "rows":
                        pd.NA,

                    "columns":
                        pd.NA,

                    "column_names":
                        "",
                }
            )

            continue

        columns = get_columns(
            path
        )

        rows.append(
            {
                "table":
                    table_name,

                "exists":
                    True,

                "rows":
                    count_csv_rows(
                        path
                    ),

                "columns":
                    len(
                        columns
                    ),

                "column_names":
                    ", ".join(
                        columns
                    ),
            }
        )

    return pd.DataFrame(
        rows
    )


# =====================================================================
# Main validation
# =====================================================================

def validate(
    *,
    chunk_size: int,
) -> int:

    config.QUALITY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------------------------------------------------------
    # Inventory
    # ---------------------------------------------------------

    logger.info(
        "Building table inventory"
    )

    inventory = build_inventory()

    print(
        "\nTABLE INVENTORY\n"
    )

    print(
        inventory[
            [
                "table",
                "exists",
                "rows",
                "columns",
            ]
        ].to_string(
            index=False
        )
    )

    missing_tables = inventory[
        ~inventory[
            "exists"
        ]
    ]

    inventory_path = (
        config.QUALITY_DIR
        / "schema_table_inventory.csv"
    )

    inventory.to_csv(
        inventory_path,
        index=False,
    )

    if not missing_tables.empty:

        print(
            "\nMissing schema tables detected."
        )

        print(
            missing_tables[
                "table"
            ].tolist()
        )

        return 1

    # ---------------------------------------------------------
    # Validation results
    # ---------------------------------------------------------

    results: list[
        dict
    ] = []

    # ---------------------------------------------------------
    # Business keys
    # ---------------------------------------------------------

    print(
        "\nCHECKING BUSINESS KEYS\n"
    )

    for table_name in TABLES:

        key_columns = TABLE_KEYS.get(
            table_name
        )

        if not key_columns:
            continue

        logger.info(
            "Business key: %s -> %s",
            table_name,
            key_columns,
        )

        result = validate_business_key(
            table_name,
            key_columns,
            chunk_size=chunk_size,
        )

        results.append(
            result
        )

        print(
            f"{table_name:<32} "
            f"duplicates={result['duplicate_key_rows']:,} "
            f"null_keys={result['null_key_rows']:,} "
            f"{result['status']}"
        )

    # ---------------------------------------------------------
    # Foreign keys
    # ---------------------------------------------------------

    print(
        "\nCHECKING FOREIGN KEYS\n"
    )

    for relation in FOREIGN_KEYS:

        result = validate_foreign_key(
            relation,
            chunk_size=chunk_size,
        )

        results.append(
            result
        )

        print(
            f"{result['relationship']:<85} "
            f"matched={result['matched_rows']:,} "
            f"unmatched={result['unmatched_rows']:,} "
            f"match={result['match_pct']:.2f}% "
            f"{result['status']}"
        )

    # ---------------------------------------------------------
    # Save report
    # ---------------------------------------------------------

    report = pd.DataFrame(
        results
    )

    report_path = (
        config.QUALITY_DIR
        / "schema_integrity_report.csv"
    )

    report.to_csv(
        report_path,
        index=False,
    )

    # ---------------------------------------------------------
    # Overall summary
    # ---------------------------------------------------------

    failed_business_keys = report[
        (
            report[
                "check_type"
            ]
            == "business_key"
        )
        &
        (
            report[
                "status"
            ]
            == "FAIL"
        )
    ]

    fk_warnings = report[
        (
            report[
                "check_type"
            ]
            == "foreign_key"
        )
        &
        (
            report[
                "unmatched_rows"
            ]
            .fillna(0)
            > 0
        )
    ]

    print(
        "\n"
        + "=" * 100
    )

    print(
        "VALIDATION SUMMARY"
    )

    print(
        "=" * 100
    )

    print(
        f"Tables checked: "
        f"{len(TABLES)}"
    )

    print(
        f"Business-key failures: "
        f"{len(failed_business_keys)}"
    )

    print(
        f"Foreign-key relationships with unmatched rows: "
        f"{len(fk_warnings)}"
    )

    print(
        f"\nInventory report:\n"
        f"{inventory_path}"
    )

    print(
        f"\nIntegrity report:\n"
        f"{report_path}"
    )

    if len(
        failed_business_keys
    ) == 0:

        print(
            "\nBusiness keys: PASS"
        )

    if len(
        fk_warnings
    ) == 0:

        print(
            "Foreign keys: PASS"
        )

    return 0


# =====================================================================
# CLI
# =====================================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Validate all 19 normalized "
            "eGramSwaraj schema tables."
        )
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=100_000,
        help=(
            "Rows read per validation chunk. "
            "Default: 100000."
        ),
    )

    args = parser.parse_args()

    return validate(
        chunk_size=args.chunk_size
    )


if __name__ == "__main__":

    raise SystemExit(
        main()
    )