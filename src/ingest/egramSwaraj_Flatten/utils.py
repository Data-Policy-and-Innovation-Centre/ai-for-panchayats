"""Utility helpers for the eGramSwaraj flattener.

These are pure functions with no external dependencies beyond the standard
library and pandas so they can be imported before any heavy processing starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from collections.abc import Generator

from .config import FOLDER_RE, FILE_RE, KINDS, LEADING_COLUMNS


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any | None:
    """Load a JSON file, returning None on any parse/IO error."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        import logging
        logging.getLogger(__name__).warning("Skipping %s: %s", path, exc)
        return None


def iter_json_files(
    input_dir: Path,
    kinds: tuple[str, ...] = KINDS,
    limit_gps: int | None = None,
) -> Generator[tuple[Path, str], None, None]:
    """Yield every (json_file, kind) beneath input_dir/LGD_*/ folders.

    Folders are sorted for reproducibility; limit_gps caps the number of GP
    folders visited (useful for smoke-testing).
    """
    import logging
    logger = logging.getLogger(__name__)

    gp_dirs = sorted(d for d in input_dir.iterdir() if d.is_dir())
    if limit_gps is not None:
        gp_dirs = gp_dirs[:limit_gps]

    kind_set = {k.upper() for k in kinds}
    for gp_dir in gp_dirs:
        for json_file in sorted(gp_dir.glob("*.json")):
            stem = json_file.stem  # e.g. "2021_PL"
            m = FILE_RE.match(stem)
            if m and m.group("kind") in kind_set:
                yield json_file, m.group("kind")
            else:
                logger.debug("Skipping unexpected file: %s", json_file)


# ---------------------------------------------------------------------------
# Folder / file name parsing
# ---------------------------------------------------------------------------

def parse_gp_folder(folder_name: str) -> tuple[str, str]:
    """Extract (lgd_code, gp_name) from a GP folder name.

    Supports patterns like ``LGD_115550_Angarbandha`` or
    ``LGD-115550-Angarbandha``. Falls back to the raw folder name if the
    pattern does not match.
    """
    m = FOLDER_RE.match(folder_name)
    if m:
        return m.group("code"), m.group("name")
    # Graceful fallback: treat the whole name as the lgd_code
    return folder_name, folder_name


# ---------------------------------------------------------------------------
# Record extraction
# ---------------------------------------------------------------------------

def to_records(payload: Any) -> list[dict]:
    """Extract a flat list of dicts from the raw JSON payload.

    eGramSwaraj JSONs wrap the real records in an outer dict; this peels that
    layer and returns the first list-of-dicts found at the top level, or wraps
    the payload itself in a list when it is already a single mapping.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        # Try common envelope keys first
        for key in ("data", "records", "items", "result"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        return [payload]
    return []


def coerce_nested(record: dict) -> dict:
    """JSON-encode scalar nested dicts that contain no lists.

    Preserves nested dicts containing lists/arrays so split_list_columns
    can expand them into relational child tables.
    """
    def _contains_list(val: Any) -> bool:
        if isinstance(val, list):
            return True
        if isinstance(val, dict):
            return any(_contains_list(v) for v in val.values())
        return False

    return {
        k: (v if _contains_list(v) else json.dumps(v, ensure_ascii=False))
        if isinstance(v, dict) else v
        for k, v in record.items()
    }


# ---------------------------------------------------------------------------
# Column helpers
# ---------------------------------------------------------------------------

def list_columns(frame: pd.DataFrame) -> list[str]:
    """Return columns whose dtype is object and that contain at least one list."""
    result = []
    for col in frame.select_dtypes(include="object").columns:
        if frame[col].apply(lambda x: isinstance(x, list)).any():
            result.append(col)
    return result


def sanitise(name: str) -> str:
    """Make a column/table name safe for use in file names and CSV headers.

    Replaces runs of non-alphanumeric characters with underscores and strips
    leading/trailing underscores.
    """
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------

def tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Reorder columns: leading columns first, then the rest alphabetically."""
    leading = [c for c in LEADING_COLUMNS if c in frame.columns]
    rest = sorted(c for c in frame.columns if c not in set(leading))
    return frame[leading + rest].reset_index(drop=True)


def concat_batch(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate a list of DataFrames, aligning columns and filling gaps."""
    if not frames:
        return pd.DataFrame()
    all_cols: list[str] = []
    seen: set[str] = set()
    for df in frames:
        for col in df.columns:
            if col not in seen:
                all_cols.append(col)
                seen.add(col)
    return pd.concat(
        [df.reindex(columns=all_cols) for df in frames],
        ignore_index=True,
    )
