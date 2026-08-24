from pathlib import Path

import duckdb


# =====================================================================
# Paths
# =====================================================================

SCHEMA_DIR = Path(
    "data/interim/schema_tables"
)

DB_PATH = Path(
    "data/interim/database_allgps.duckdb"
)


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
# Columns that must be VARCHAR
#
# These are IDs/codes, not numeric measures.
# Keeping them as VARCHAR:
# - avoids floating-point precision issues
# - keeps joins consistent across tables
# - preserves identifier semantics
# =====================================================================

VARCHAR_COLUMNS = {
    "gram_panchayat": [
        "gp_lgd_code",
        "state_code",
        "district_code",
        "block_code",
    ],

    "plan": [
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
        "plan_type",
        "plan_code_status",
    ],

    "planned_activity": [
        "activity_code",
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
    ],

    "activity_asset": [
        "activity_code",
        "asset_coverage_code",
        "asset_loc_code",
        "asset_loc_unit_code",
    ],

    "activity_fund": [
        "activity_code",
        "fund_scheme_code",
        "fund_component_code",
    ],

    "activity_training": [
        "activity_code",
        "training_category_code",
        "training_organiser_code",
    ],

    "activity_delegation": [
        "activity_code",
        "delegated_unit_code",
        "delegated_parent_unit_code",
    ],

    "activity_community_service": [
        "activity_code",
        "community_service_code",
    ],

    "activity_nsap": [
        "nsap_id",
        "activity_code",
    ],

    "activity_expenditure": [
        "expenditure_id",
        "activity_code",
        "plan_code",
        "gp_lgd_code",
        "fiscal_year",
    ],

    "voucher": [
        "voucher_pk",
        "gp_lgd_code",
        "district_code",
        "block_code",
        "fiscal_year",
        "voucher_no",
        "voucher_id",
    ],

    "activity_voucher": [
        "expenditure_id",
        "voucher_pk",
        "gp_lgd_code",
        "fiscal_year",
        "voucher_no",
    ],

    "admin_approval": [
        "row_id",
        "gp_lgd_code",
        "activity_code",
        "adm_approval_no",
    ],

    "admin_approval_scheme": [
        "row_id",
        "parent_row_id",
        "activity_code",
        "scheme_code",
        "scheme_component_code",
    ],

    "technical_approval": [
        "row_id",
        "gp_lgd_code",
        "activity_code",
        "tec_approval_order_no",
    ],

    "physical_progress": [
        "row_id",
        "parent_row_id",
        "activity_code",
        "file_upload_id",
        "plan_unit_type_code",
    ],

    "dim_code": [
        "variable",
        "code",
    ],

    "dim_welfare_scheme": [
        "scheme_code",
    ],

    "dim_lsdg_theme": [],
}


# =====================================================================
# Helper
# =====================================================================

def build_types_clause(table: str) -> str:
    """
    Build DuckDB read_csv() type overrides.

    Example:

    types = {
        'gp_lgd_code': 'VARCHAR',
        'activity_code': 'VARCHAR'
    }
    """

    columns = VARCHAR_COLUMNS.get(
        table,
        [],
    )

    if not columns:
        return ""

    type_definitions = ", ".join(
        f"'{column}': 'VARCHAR'"
        for column in columns
    )

    return (
        f", types = {{{type_definitions}}}"
    )


# =====================================================================
# Build database
# =====================================================================

DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True,
)

con = duckdb.connect(
    str(DB_PATH)
)

# Helpful for large bulk imports
con.execute(
    "SET preserve_insertion_order = false"
)


for table in TABLES:

    csv_path = (
        SCHEMA_DIR
        / f"{table}.csv"
    )

    if not csv_path.exists():

        raise FileNotFoundError(
            f"Missing schema table: {csv_path}"
        )

    print(
        f"Loading {table}..."
    )

    types_clause = build_types_clause(
        table
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS

        SELECT *
        FROM read_csv(
            '{csv_path}',

            header = true,
            auto_detect = true,

            delim = ',',
            quote = '"',
            escape = '"',

            sample_size = 100000

            {types_clause}
        )
        """
    )

    rows = con.execute(
        f"""
        SELECT COUNT(*)
        FROM {table}
        """
    ).fetchone()[0]

    print(
        f"  {rows:,} rows"
    )


# =====================================================================
# Persist database
# =====================================================================

con.execute(
    "CHECKPOINT"
)


# =====================================================================
# Table summary
# =====================================================================

print(
    "\n"
    + "=" * 90
)

print(
    "DATABASE TABLE SUMMARY"
)

print(
    "=" * 90
)

print(
    f"{'table':<32}"
    f"{'rows':>15}"
    f"{'columns':>12}"
)

print(
    "-" * 59
)


for table in TABLES:

    rows = con.execute(
        f"""
        SELECT COUNT(*)
        FROM {table}
        """
    ).fetchone()[0]

    columns = con.execute(
        """
        SELECT COUNT(*)

        FROM information_schema.columns

        WHERE table_schema = 'main'
          AND table_name = ?
        """,
        [
            table,
        ],
    ).fetchone()[0]

    print(
        f"{table:<32}"
        f"{rows:>15,}"
        f"{columns:>12,}"
    )

#  identifier type check

print(
    "\n"
    + "=" * 90
)

print(
    "IDENTIFIER TYPE CHECK"
)

print(
    "=" * 90
)


TYPE_CHECKS = [
    (
        "gram_panchayat",
        "gp_lgd_code",
    ),
    (
        "plan",
        "plan_code",
    ),
    (
        "planned_activity",
        "activity_code",
    ),
    (
        "activity_expenditure",
        "expenditure_id",
    ),
    (
        "activity_expenditure",
        "activity_code",
    ),
    (
        "voucher",
        "voucher_pk",
    ),
    (
        "voucher",
        "gp_lgd_code",
    ),
    (
        "voucher",
        "voucher_no",
    ),
    (
        "activity_voucher",
        "voucher_pk",
    ),
    (
        "activity_voucher",
        "expenditure_id",
    ),
    (
        "admin_approval",
        "row_id",
    ),
    (
        "admin_approval",
        "adm_approval_no",
    ),
    (
        "technical_approval",
        "row_id",
    ),
    (
        "physical_progress",
        "row_id",
    ),
]


for table, column in TYPE_CHECKS:

    result = con.execute(
        """
        SELECT data_type

        FROM information_schema.columns

        WHERE table_schema = 'main'
          AND table_name = ?
          AND column_name = ?
        """,
        [
            table,
            column,
        ],
    ).fetchone()

    dtype = (
        result[0]
        if result
        else "NOT FOUND"
    )

    print(
        f"{table:<30}"
        f"{column:<30}"
        f"{dtype}"
    )


con.close()


print(
    "\n"
    + "=" * 90
)

print(
    "DATABASE CREATED SUCCESSFULLY"
)

print(
    "=" * 90
)

print(
    DB_PATH.resolve()
)