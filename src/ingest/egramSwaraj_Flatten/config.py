"""Paths, constants and logging."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

REPO_MARKERS = (".git", "pyproject.toml", "requirements.txt", "data")


def find_repo_root(start: Path | None = None) -> Path:
    """First parent directory holding a repo marker."""
    start = (start or Path(__file__).resolve()).resolve()
    for candidate in (start, *start.parents):
        if candidate.is_dir() and any(
            (candidate / marker).exists() for marker in REPO_MARKERS
        ):
            return candidate
    return Path.cwd()


def env_path(name: str) -> Path | None:
    """Env var as a path; None if unset."""
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else None


# Precedence: CLI flags > PANCHAYAT_*_DIR > PANCHAYAT_DATA_ROOT > repo root.
REPO_ROOT = find_repo_root()
DATA_ROOT = env_path("PANCHAYAT_DATA_ROOT") or REPO_ROOT / "data"

# Producer contract: src/ingest/egramSwaraj_API writes GP folders beneath
# data/raw/eGramSwaraj_Data. Flattened tables are derived, so they land in
# interim, never back in raw.
INPUT_DIR = (env_path("PANCHAYAT_INPUT_DIR")
             or DATA_ROOT / "raw" / "eGramSwaraj_Data" / "Gram_Panchayat")
OUTPUT_DIR = (env_path("PANCHAYAT_OUTPUT_DIR")
              or DATA_ROOT / "interim" / "eGramSwaraj")
LOG_FILE = REPO_ROOT / "logs" / "flatten_egramswaraj.log"

KINDS = ("AA", "PL", "PP", "RE", "TA")

# LGD_115550_Angarbandha -> ("115550", "Angarbandha")
FOLDER_RE = re.compile(r"^LGD[_-]?(?P<code>\d+)[_-](?P<name>.+)$")
# 2021_PL.json -> ("2021", "PL")
FILE_RE = re.compile(rf"^(?P<year>\d{{4}})[_-](?P<kind>{'|'.join(KINDS)})$")

ROW_ID = "row_id"
PARENT_ID = "parent_row_id"
POS = "pos"

TABLE_SEP = "__"          # pl__fundlist
LEVEL_SEP = "/"           # nesting level inside a row_id; never in a top-level id

# Copied onto every descendant table for direct joins to the top-level row.
BUSINESS_KEYS = ("activityCd",)

# Keys the API uses to wrap a record list. A dict carrying one of these is an
# envelope and is unwrapped; any other dict is a domain record and is kept
# whole, so its arrays become child tables rather than being dropped.
ENVELOPE_KEYS = frozenset({"data", "response", "result", "records", "rows"})

LEADING_COLUMNS = (ROW_ID, PARENT_ID, POS, "lgd_code", "gram_panchayat_name",
                   "plan_year", "doc_type", "source_file", "activityCd")

BATCH_SIZE = 1000         # source JSONs per batch CSV
MASTER_MAX_ROWS = 8_000_000   # rows per master before _pNNN split; 0 = no cap
BATCH_DIRNAME = "batches"


def configure_logging(log_file: Path = LOG_FILE, verbose: bool = False) -> None:
    """Log to stderr and file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                  logging.StreamHandler()],
    )