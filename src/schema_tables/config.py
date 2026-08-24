from __future__ import annotations

from pathlib import Path

from config import directories


# ---------------------------------------------------------------------
# Source directory
# ---------------------------------------------------------------------

ALL_GPS_RAW_DIR = directories.RAW_DATA / "all_gps_data"


# ---------------------------------------------------------------------
# Source files
# ---------------------------------------------------------------------

PLANNING_CSV = (
    ALL_GPS_RAW_DIR
    / "egramswaraj_pl_new.csv"
)

EXPENDITURE_CSV = (
    ALL_GPS_RAW_DIR
    / "expenditure_all.csv"
)

VOUCHERS_CSV = (
    ALL_GPS_RAW_DIR
    / "all_vouchers.csv"
)

ADMIN_APPROVAL_CSV = (
    ALL_GPS_RAW_DIR
    / "egramswaraj_aa.csv"
)

ADMIN_APPROVAL_SCHEME_CSV = (
    ALL_GPS_RAW_DIR
    / "egramswaraj_aa__admapprovalschemewebservice.csv"
)

TECHNICAL_APPROVAL_CSV = (
    ALL_GPS_RAW_DIR
    / "egramswaraj_ta.csv"
)

PHYSICAL_PROGRESS_CSV = (
    ALL_GPS_RAW_DIR
    / "egramswaraj_pp__physicalprogressassetstageuploadwebservice.csv"
)

CODE_LOOKUP_XLSX = (
    ALL_GPS_RAW_DIR
    / "code_descriptions_updated.xlsx"
)


# ---------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------

OUTPUT_DIR = (
    directories.INTERIM_DATA
    / "schema_tables"
)

QUALITY_DIR = (
    directories.INTERIM_DATA
    / "quality"
)


# ---------------------------------------------------------------------
# 19 schema tables
# ---------------------------------------------------------------------

SCHEMA_TABLES = [
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


# ---------------------------------------------------------------------
# Output table paths
# ---------------------------------------------------------------------

TABLE_PATHS = {
    table_name: OUTPUT_DIR / f"{table_name}.csv"
    for table_name in SCHEMA_TABLES
}


# ---------------------------------------------------------------------
# Quality output paths
# ---------------------------------------------------------------------

QUARANTINE_PATH = (
    QUALITY_DIR
    / "quarantine.csv"
)

VALIDATION_REPORT_PATH = (
    QUALITY_DIR
    / "validation_report.csv"
)

EXPORT_REPORT_PATH = (
    QUALITY_DIR
    / "export_report.csv"
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def create_output_directories() -> None:
    """
    Create schema-table and quality directories.
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    QUALITY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def require(
    path: Path,
    name: str,
) -> Path:
    """
    Require an input source file to exist.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Required input '{name}' was not found:\n"
            f"{path}"
        )

    return path


def optional(
    path: Path,
) -> Path | None:
    """
    Return the input path when it exists,
    otherwise return None.
    """

    return path if path.exists() else None