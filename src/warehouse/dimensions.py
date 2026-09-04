"""Static code dictionaries: dim_code, dim_lsdg_theme, dim_welfare_scheme.

These three tables are what turn the warehouse's coded columns into readable
text. ``dim_code`` alone decodes 23 of them -- without it ``focus_area``,
``activity_status``, ``activity_type``, ``work_type`` and ``asset_category``
are bare integers, and the consumer's ``v_activity`` view joins it six times
(#48).

Loaded the way :mod:`warehouse.geography` loads the LGD tree, and for the
same reason: **this is conformed reference data, not run-scoped evidence.**
Nothing here was observed by a scrape. It is the dictionary the portal's
codes are written in, hand-assembled by the team, and it is identical for
every snapshot. Giving it a source kind would put it in
``schema.KIND_TABLES``, which ``select.resolve_snapshots`` then demands of
every snapshot -- retroactively invalidating the ones already built.

The CSVs are committed next to this module rather than pulled from DVC for
the same reason ``ingest/egramSwaraj_API/lgd_codes.json`` is: 44 KB of
``code -> meaning`` is closer to schema than to data, it names no panchayat
and records no observation, and a clean clone should be able to build.

``source`` and ``confidence`` are carried through deliberately. In the real
dictionary only 51 of 717 mappings are Confirmed and only 373 are
high-confidence, so **94% are not confirmed-high**. A chatbot turning a
number into a human label must be able to tell a confirmed mapping from a
derived guess, so the provenance travels with the label rather than being
flattened away at load time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping

import pandas as pd

from .load_common import CsvSchemaError, read_csv

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"

# What each table takes from its CSV, in DDL order. The file may carry more:
# dim_lsdg_theme.csv also has `distinct_themes` and `n_rows`, which are counts
# from whoever assembled it rather than part of the dimension, and are dropped.
DIMENSION_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "dim_code": ("variable", "code", "description", "source", "confidence"),
    "dim_lsdg_theme": ("focus_area_name", "lsdg_theme"),
    "dim_welfare_scheme": ("scheme_code", "scheme_name"),
}

# Columns whose duplication is a contradiction rather than a repetition,
# matching each table's PRIMARY KEY in schema.py. dim_lsdg_theme has none:
# its DDL declares no key, because one focus area legitimately maps to one
# theme but the file is a mapping table, not an identity.
DIMENSION_KEYS: Mapping[str, tuple[str, ...]] = {
    "dim_code": ("variable", "code"),
    "dim_welfare_scheme": ("scheme_code",),
}


class DimensionError(RuntimeError):
    """Raised when a code dictionary is missing, malformed, or contradictory."""


def _load(name: str, directory: Path | None = None) -> pd.DataFrame:
    kept = DIMENSION_COLUMNS[name]
    path = (directory or REFERENCE_DIR) / f"{name}.csv"
    if not path.is_file():
        raise DimensionError(f"code dictionary not found: {path}")
    try:
        # `read_csv` defaults every untyped column to `string`
        # (`load_common._csv_dtype`), which is what these tables need: all
        # three are VARCHAR throughout, and letting pandas infer `code` as an
        # integer would eat a leading zero and stop it matching the
        # CAST-to-VARCHAR join the consumer's views use. No dtype is passed
        # because passing one would only restate that default.
        frame = read_csv(path, required_columns=kept)
    except (CsvSchemaError, OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise DimensionError(f"code dictionary unreadable: {path}: {exc}") from exc

    frame = frame.loc[:, list(kept)]
    for column in kept:
        # Values arrive with stray whitespace ("Theme 5 - Clean and Green
        # Village "), and a trailing space in a label is visible to a user.
        frame[column] = frame[column].astype("string").str.strip()

    keys = DIMENSION_KEYS.get(name, ())
    if keys:
        blank = frame[list(keys)].isna().any(axis=1) | (frame[list(keys)] == "").any(axis=1)
        if blank.any():
            raise DimensionError(
                f"{path}: {int(blank.sum())} row(s) have a blank {'/'.join(keys)}"
            )
        duplicated = frame.duplicated(subset=list(keys), keep=False)
        if duplicated.any():
            # Keeping the last would silently pick one of two meanings for
            # the same code, which is exactly the failure this table exists
            # to prevent.
            examples = frame.loc[duplicated, list(keys)].drop_duplicates().head(3)
            raise DimensionError(
                f"{path}: {'/'.join(keys)} is not unique; e.g. "
                f"{examples.to_dict('records')}"
            )
    if frame.empty:
        raise DimensionError(f"{path}: contains no rows")
    return frame.reset_index(drop=True)


@lru_cache(maxsize=2)
def dimension_frames(directory: str | Path | None = None) -> Mapping[str, pd.DataFrame]:
    """The three code dictionaries, validated, keyed by table name.

    Cached: they are static, and a build would otherwise re-read and
    re-validate them once per snapshot. ``directory`` exists for tests and
    mirrors ``geography.gp_geography``'s optional path -- keyed separately by
    the cache, so a fixture cannot poison the real load or vice versa.
    """

    root = Path(directory) if directory is not None else None
    return {name: _load(name, root) for name in DIMENSION_COLUMNS}
