"""Input locations and build settings for the panchayat DuckDB read model.

Every path resolves through the project's configured data directories and can
be overridden per machine with an environment variable. No path here may name
a personal home directory or a mount that exists on one contributor's laptop.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_ROOT = Path(os.getenv("PANCHAYAT_DATA_ROOT", REPO_ROOT / "data")).expanduser()
RAW_DATA = DATA_ROOT / "raw"
INTERIM_DATA = DATA_ROOT / "interim"
PROCESSED_DATA = DATA_ROOT / "processed"
EXTERNAL_DATA = DATA_ROOT / "external"

# The read model is derived and disposable: it is rebuilt from source, never
# edited in place, so it belongs in interim rather than processed.
DB_PATH = Path(os.getenv("PANCHAYAT_DB_PATH", INTERIM_DATA / "panchayat.duckdb"))


def _input(env_var: str, default: Path) -> Path:
    return Path(os.getenv(env_var, default)).expanduser()


# ---------------------------------------------------------------- core inputs

PLANNING_CSV = _input("PANCHAYAT_PLANNING_CSV",
                      RAW_DATA / "planning" / "gram_panchayat_filtered.csv")
EXPENDITURE_CSV = _input("PANCHAYAT_EXPENDITURE_CSV",
                         RAW_DATA / "Activity_expenditure" / "expenditure_all.csv")
VOUCHERS_CSV = _input("PANCHAYAT_VOUCHERS_CSV",
                      PROCESSED_DATA / "all_vouchers_flat.csv")
CODE_LOOKUP_XLSX = _input("PANCHAYAT_CODE_LOOKUP_XLSX",
                          EXTERNAL_DATA / "code_descriptions_updated.xlsx")

# ------------------------------------------------- eGramSwaraj extension inputs

ADMIN_APPROVAL_CSV = _input(
    "PANCHAYAT_AA_CSV", RAW_DATA / "egramswaraj_aa_filtered.csv")
ADMIN_APPROVAL_SCHEME_CSV = _input(
    "PANCHAYAT_AAS_CSV",
    RAW_DATA / "egramswaraj_aa__admapprovalschemewebservice_filtered.csv")
TECHNICAL_APPROVAL_CSV = _input(
    "PANCHAYAT_TA_CSV", RAW_DATA / "egramswaraj_ta_filtered.csv")
PHYSICAL_PROGRESS_CSV = _input(
    "PANCHAYAT_PP_CSV",
    RAW_DATA / "egramswaraj_pp__physicalprogressassetstageuploadwebservice_filtered.csv")

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"


class MissingInput(FileNotFoundError):
    """A declared build input is not present."""


def require(path: Path, env_var: str) -> Path:
    """Return path, or explain how to point the build somewhere else."""
    if not path.exists():
        raise MissingInput(
            f"{path} not found. Place the file there, or set {env_var} to its "
            f"location. Inputs are not committed to Git; fetch them with DVC."
        )
    return path
