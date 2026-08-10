"""Flatten eGramSwaraj Gram Panchayat JSON files into relational CSVs.

Input : <data>/raw/Gram_Panchayat/LGD_<code>_<name>/<year>_<KIND>.json
Output: batched CSVs plus one master CSV per document kind and table.

KIND is one of AA, PL, PP, RE, TA.

Output layout
-------------
Rows from many source JSONs are buffered and flushed every ``--batch-size``
files, so nothing is written per source file (and nothing per activity)::

    PL/batches/pl__b0001.csv                       one row per activity
    PL/batches/pl__fundlist__b0001.csv             one row per fund line
    PL/batches/pl__assetdetails__b0001.csv         one row per asset
    PL/batches/pl__assetdetails__assetlocationdetails__b0001.csv

After the pass the batches of each table are concatenated into one master per
table, streamed row by row so peak memory stays at one batch::

    PL/egramswaraj_pl.csv
    PL/egramswaraj_pl__fundlist.csv
    PL/egramswaraj_pl__assetdetails.csv
    PL/egramswaraj_pl__assetdetails__assetlocationdetails.csv

A master longer than ``--master-max-rows`` is split into ``_p001``, ``_p002``
parts rather than one unreadable file. 0 disables the cap.

With --wide, each batch is additionally left-joined into a single denormalised
table per kind (``PL/egramswaraj_pl_wide.csv``). Read the warning under
merge_wide first: joining two sibling arrays multiplies rows.

Nested arrays go long, not wide
-------------------------------
A plan record holds arrays: ``fundList``, ``assetDetails``, and inside each
asset, ``assetLocationDetails``. Rather than reserving N columns per array and
JSON-dumping the remainder into an ``*_overflow_json`` cell, each array becomes
its own table.

Every table carries ``row_id`` (its own key), ``parent_row_id`` (the row it
hangs off) and ``pos`` (its index in the source array, so original ordering
survives). Descendants also carry the top-level business key (``activityCd`` by
default), so a child joins to its parent without a chain of joins. Nesting
depth is unbounded: arrays inside arrays inside arrays each get a table.

Run --tree first. It prints the tables, row counts, columns and array-length
distributions it would produce, and writes nothing.

Paths are derived from the repo's ``data/`` directory, never hardcoded to one
machine; see "Path resolution" below.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
from tqdm import tqdm

# --------------------------------------------------------------------------- #
# Path resolution
#
#   1. CLI flags:  --input-dir / --output-dir          (highest)
#   2. Env vars:   PANCHAYAT_INPUT_DIR / PANCHAYAT_OUTPUT_DIR
#   3. Env var:    PANCHAYAT_DATA_ROOT                 (path to data/)
#   4. Repo root auto-detected by walking up from this file for a marker
#   5. Current working directory                       (lowest)
# --------------------------------------------------------------------------- #

REPO_MARKERS = (".git", "pyproject.toml", "requirements.txt", "data")


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* to the first directory holding a repo marker."""
    start = (start or Path(__file__).resolve()).resolve()
    for candidate in (start, *start.parents):
        if candidate.is_dir() and any(
            (candidate / marker).exists() for marker in REPO_MARKERS
        ):
            return candidate
    return Path.cwd()


def _env_path(name: str) -> Path | None:
    """Read an env var as a path, expanding ``~``; ``None`` if unset/empty."""
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else None


REPO_ROOT = find_repo_root()
DATA_ROOT = _env_path("PANCHAYAT_DATA_ROOT") or REPO_ROOT / "data"

INPUT_DIR = (_env_path("PANCHAYAT_INPUT_DIR")
             or DATA_ROOT / "raw" / "Gram_Panchayat")
OUTPUT_DIR = _env_path("PANCHAYAT_OUTPUT_DIR") or DATA_ROOT / "raw" / "eGramSwaraj"
LOG_FILE = REPO_ROOT / "logs" / "flatten_egramswaraj.log"

KINDS = ("AA", "PL", "PP", "RE", "TA")

# LGD_115550_Angarbandha -> ("115550", "Angarbandha")
FOLDER_RE = re.compile(r"^LGD[_-]?(?P<code>\d+)[_-](?P<name>.+)$")
# 2021_PL.json -> ("2021", "PL")
FILE_RE = re.compile(rf"^(?P<year>\d{{4}})[_-](?P<kind>{'|'.join(KINDS)})$")

# --------------------------------------------------------------------------- #
# Table layout
# --------------------------------------------------------------------------- #

ROW_ID = "row_id"
PARENT_ID = "parent_row_id"
POS = "pos"

# Separator between a table's name and its child's, e.g. pl__fundlist.
TABLE_SEP = "__"

# Separator between nesting levels inside a row_id. Must never occur in a
# top-level row id, so the top-level ancestor stays recoverable from any
# descendant by splitting on it once.
LEVEL_SEP = "/"

# Copied onto every descendant table so a child joins straight to its
# top-level parent. Only keys present in the parent frame are used; add the
# equivalent key for other document kinds as needed.
BUSINESS_KEYS = ("activityCd",)

# Keys always treated as arrays even when a record carries a bare object. The
# automatic rule in coerce_nested handles nested entities; add a name here when
# a scalar-only object should still get its own table.
ALWAYS_LIST: set[str] = set()

# Columns placed first in every output CSV, in this order.
LEADING_COLUMNS = (ROW_ID, PARENT_ID, POS, "lgd_code", "gram_panchayat_name",
                   "plan_year", "doc_type", "source_file", "activityCd")

# Source JSONs buffered before a batch of CSVs is flushed to disk.
BATCH_SIZE = 500

# Rows per master CSV before it is split into parts. 0 = no cap.
MASTER_MAX_ROWS = 5_000_000

BATCH_DIRNAME = "batches"

# Tallied across the run and reported once at the end, rather than warning per
# file (200k files means 200k lines of noise otherwise).
TABLE_STATS: dict[tuple[str, str], dict[str, Any]] = defaultdict(
    lambda: {"rows": 0, "max_len": 0, "lengths": Counter()}
)

# Any shortfall between source array elements and rows written. Should stay
# empty; a non-empty tally at the end of a run means data was dropped.
LOSSES: Counter = Counter()

# Non-dict members of a mixed list, parked in a ``*_scalars`` column on the
# parent instead of a child row. Reported separately: nothing is lost.
STRAYS: Counter = Counter()

logger = logging.getLogger(__name__)


def configure_logging(log_file: Path = LOG_FILE, verbose: bool = False) -> None:
    """Log to both stderr and a plain file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                  logging.StreamHandler()],
    )


# --------------------------------------------------------------------------- #
# Discovery and metadata
# --------------------------------------------------------------------------- #


def iter_json_files(
    input_dir: Path,
    kinds: tuple[str, ...] = KINDS,
    limit_gps: int | None = None,
) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, kind)`` for every matching JSON, sorted for determinism.

    Lazy per GP folder, so the full path list is never materialised unless the
    caller collects it. Set *limit_gps* to sample N panchayats.
    """
    wanted = set(kinds)
    gps = 0
    for gp_dir in sorted(input_dir.iterdir()):
        if not gp_dir.is_dir():
            continue
        for path in sorted(gp_dir.glob("*.json")):
            match = FILE_RE.match(path.stem)
            if match and match.group("kind") in wanted:
                yield path, match.group("kind")
        gps += 1
        if limit_gps is not None and gps >= limit_gps:
            return


def parse_gp_folder(folder_name: str) -> tuple[str | None, str | None]:
    """Split ``LGD_115550_Angarbandha`` into ``("115550", "Angarbandha")``."""
    match = FOLDER_RE.match(folder_name)
    if not match:
        logger.warning("Unparseable GP folder name: %s", folder_name)
        return None, None
    return match.group("code"), match.group("name").replace("_", " ").strip()


def load_json(path: Path) -> Any | None:
    """Read one JSON file; return ``None`` on any read/parse failure."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Corrupt JSON, skipped: %s (%s)", path, exc)
    except OSError as exc:
        logger.error("Unreadable file, skipped: %s (%s)", path, exc)
    return None


def to_records(payload: Any) -> list[dict]:
    """Normalise a payload into a list of record-level dicts.

    Handles the shapes the scraper emits:
      * ``[{...}, {...}]``                     -> used as-is
      * ``{"activities": [{...}], "gp": ...}`` -> list expanded, scalar
                                                  siblings broadcast onto rows
      * ``{...}``                              -> single record
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    # Find the record list, if any: the longest list-of-dicts value.
    list_keys = [
        k for k, v in payload.items()
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
    ]
    if not list_keys:
        return [payload]

    key = max(list_keys, key=lambda k: len(payload[k]))
    header = {k: v for k, v in payload.items()
              if k != key and not isinstance(v, (list, dict))}
    return [{**header, **row} for row in payload[key]]


# --------------------------------------------------------------------------- #
# Arity normalisation
# --------------------------------------------------------------------------- #


def _holds_list_of_dicts(mapping: dict) -> bool:
    """True if any value is a non-empty list of dicts."""
    return any(
        isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
        for v in mapping.values()
    )


def coerce_nested(obj: Any) -> Any:
    """Rewrite a bare nested object as a one-element list, recursively.

    The feed is not consistent about arity: one GP sends ``assetDetails`` as a
    list of objects, another sends a single object. Left alone, the first shape
    becomes the table ``pl__assetdetails`` while the second is flattened inline
    as ``assetDetails_*`` columns and its own nested array lands in a second,
    near-identically named table. Coercing here means both shapes take the same
    path and produce one schema.

    Only objects that themselves contain a nested array are coerced — an object
    of plain scalars stays inline and prefixed. Add a key to ``ALWAYS_LIST`` to
    force coercion regardless.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            value = coerce_nested(value)
            if isinstance(value, dict) and (
                key in ALWAYS_LIST or _holds_list_of_dicts(value)
            ):
                value = [value]
            out[key] = value
        return out
    if isinstance(obj, list):
        return [coerce_nested(item) for item in obj]
    return obj


# --------------------------------------------------------------------------- #
# Flattening
# --------------------------------------------------------------------------- #


def sanitise(name: str) -> str:
    """``assetLocationDetails`` -> ``assetlocationdetails``."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def list_columns(frame: pd.DataFrame) -> list[str]:
    """Return columns holding at least one list of dicts."""
    return [
        col for col in frame.columns
        if frame[col].map(
            lambda v: isinstance(v, list) and any(isinstance(i, dict) for i in v)
        ).any()
    ]


def split_list_columns(
    frame: pd.DataFrame,
    table: str,
    tables: dict[str, pd.DataFrame],
    kind: str,
    sep: str = "_",
) -> pd.DataFrame:
    """Move every list-of-dict column out of *frame* into ``tables``.

    Returns *frame* with those columns removed. Children are added to *tables*
    under ``<table>__<column>`` and are themselves split recursively, so a list
    nested inside a list element gets its own table rather than being
    stringified into a cell.

    A list mixing dicts and scalars keeps its dict elements and JSON-encodes
    the scalars into a ``_scalars`` column on the parent, so nothing is dropped
    silently (``count_scalar_leftovers`` reports how often this happens).
    """
    while True:
        cols = list_columns(frame)
        if not cols:
            break

        for col in cols:
            values = frame.pop(col)
            parents, positions, elements = [], [], []
            strays: dict[int, list] = {}

            for idx, (row_id, value) in enumerate(zip(frame[ROW_ID], values)):
                if not isinstance(value, list):
                    continue
                kept = 0
                for element in value:
                    if not isinstance(element, dict):
                        strays.setdefault(idx, []).append(element)
                        continue
                    parents.append(row_id)
                    positions.append(kept)
                    elements.append(element)
                    kept += 1
                if kept:
                    stat = TABLE_STATS[(kind, f"{table}{TABLE_SEP}{sanitise(col)}")]
                    stat["lengths"][kept] += 1
                    stat["max_len"] = max(stat["max_len"], kept)

            if strays:
                # Non-dict members of a mixed list have no table to go to;
                # park them beside the parent row instead of losing them.
                stray_col = f"{col}{sep}scalars"
                frame[stray_col] = [
                    json.dumps(strays[i]) if i in strays else None
                    for i in range(len(frame))
                ]
                STRAYS[(kind, f"{table}{TABLE_SEP}{sanitise(col)}")] += sum(
                    len(v) for v in strays.values()
                )

            if not elements:
                continue

            child = pd.json_normalize(elements, sep=sep)
            child.insert(0, POS, positions)
            child.insert(0, PARENT_ID, parents)
            # The array name is part of the key, so two sibling arrays of the
            # same length cannot mint identical ids: fundList element 0 is
            # ``...|0/fundlist:0``, not ``...|0.0``.
            child.insert(0, ROW_ID, [f"{p}{LEVEL_SEP}{sanitise(col)}:{i}"
                                     for p, i in zip(parents, positions)])

            name = f"{table}{TABLE_SEP}{sanitise(col)}"
            child = split_list_columns(child, name, tables, kind, sep=sep)

            if name in tables:                     # same array seen twice
                child = pd.concat([tables[name], child], ignore_index=True)
            tables[name] = child

    return frame


def attach_business_keys(parent: pd.DataFrame,
                         tables: dict[str, pd.DataFrame]) -> None:
    """Copy the parent's business keys onto every descendant table.

    A descendant's ``parent_row_id`` is ``<top>/<array>:<i>[/...]`` and a
    top-level row id never contains ``LEVEL_SEP``, so the top-level ancestor is
    recoverable by truncating at the first separator however deep the nesting.
    """
    for key in BUSINESS_KEYS:
        if key not in parent.columns:
            continue
        lookup = dict(zip(parent[ROW_ID], parent[key]))
        for child in tables.values():
            if key in child.columns or PARENT_ID not in child.columns:
                continue
            root = child[PARENT_ID].astype(str).str.split(LEVEL_SEP, n=1).str[0]
            child.insert(3, key, root.map(lookup))


def encode_leftovers(frame: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode any remaining list/dict cells so CSVs hold no Python reprs.

    After splitting, the only survivors are lists of scalars and objects whose
    values are all scalars, both of which belong in a cell.
    """
    for col in frame.columns:
        mask = frame[col].map(lambda v: isinstance(v, (list, dict)))
        if mask.any():
            frame.loc[mask, col] = frame.loc[mask, col].map(json.dumps)
    return frame


def restore_integers(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast float columns whose values are all whole numbers back to integers.

    ``json_normalize`` inserts NaN wherever a field is missing from a record,
    which upcasts an int column to float — so ``astLocCd`` 404281 is written as
    ``404281.0``. Nullable ``Int64`` keeps the blanks and drops the ``.0``.
    Genuinely fractional columns are left alone.
    """
    for col in frame.select_dtypes("number").columns:
        values = frame[col].dropna()
        if len(values) and (values % 1 == 0).all() and values.abs().max() < 2 ** 63:
            frame[col] = frame[col].astype("Int64")
    return frame


def order_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Move key and identifier columns to the front, keeping the rest in place."""
    lead = [c for c in LEADING_COLUMNS if c in frame.columns]
    return frame[lead + [c for c in frame.columns if c not in lead]]


def tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Post-process one finished table."""
    return order_columns(restore_integers(encode_leftovers(frame)))


def count_raw_elements(records: list[dict], table: str,
                       acc: dict[str, int] | None = None,
                       path: str | None = None) -> dict[str, int]:
    """Tally nested array elements per table name, straight from the records.

    Counted independently of the flattening so the two can be compared: if a
    child table has fewer rows than the source had elements, something was
    dropped. Run *after* ``coerce_nested`` so both sides see the same arity.

    A list is counted when *any* member is a dict, matching what
    ``split_list_columns`` acts on; only its dict members are expected as rows,
    so the scalar members are excluded from the expected count.
    """
    acc = {} if acc is None else acc
    prefix = path if path is not None else table
    for record in records:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            dicts = ([i for i in value if isinstance(i, dict)]
                     if isinstance(value, list) else [])
            if not dicts:
                if isinstance(value, dict):
                    count_raw_elements([value], table, acc,
                                       f"{prefix}{sep_for_inline(key)}")
                continue
            name = f"{prefix}{TABLE_SEP}{sanitise(key)}"
            acc[name] = acc.get(name, 0) + len(dicts)
            count_raw_elements(dicts, table, acc, name)
    return acc


def sep_for_inline(key: str) -> str:
    """Name an inline (uncoerced) object the way ``json_normalize`` will.

    An all-scalar object is flattened into ``parent_child`` columns, so any
    array beneath it becomes ``<table>__parent_child``, not
    ``<table>__parent__child``. Getting this wrong reports phantom losses.
    """
    return f"_{sanitise(key)}"


def flatten_file(
    path: Path, kind: str
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]] | None:
    """Flatten one JSON into ``(parent_frame, {table_name: child_frame})``."""
    payload = load_json(path)
    if payload is None:
        return None

    records = to_records(payload)
    if not records:
        logger.debug("No records in %s", path)
        return None

    records = [coerce_nested(r) for r in records]
    frame = pd.json_normalize(records, sep="_")

    lgd_code, gp_name = parse_gp_folder(path.parent.name)
    plan_year = path.stem.split("_", 1)[0]
    frame.insert(0, ROW_ID,
                 [f"{lgd_code}|{plan_year}|{kind}|{i}" for i in range(len(frame))])

    table = kind.lower()
    tables: dict[str, pd.DataFrame] = {}
    frame = split_list_columns(frame, table, tables, kind)

    frame["lgd_code"] = lgd_code
    frame["gram_panchayat_name"] = gp_name
    frame["plan_year"] = plan_year
    frame["doc_type"] = kind
    frame["source_file"] = path.name

    attach_business_keys(frame, tables)

    for name, want in count_raw_elements(records, table).items():
        got = len(tables.get(name, ()))
        if got != want:
            LOSSES[(kind, name)] += want - got
            logger.error("%s: %s expected %d element(s), wrote %d — %d lost",
                         path.name, name, want, got, want - got)

    TABLE_STATS[(kind, table)]["rows"] += len(frame)
    for name, child in tables.items():
        TABLE_STATS[(kind, name)]["rows"] += len(child)

    return tidy(frame), {n: tidy(c) for n, c in tables.items()}


def report_tables() -> None:
    """Summarise every table produced, once, at the end of a run."""
    if not TABLE_STATS:
        return
    for (kind, name), n in sorted(STRAYS.items()):
        logger.warning("MIXED LIST: [%s] %s had %d non-object member(s); "
                       "kept in the parent's *_scalars column", kind, name, n)
    if LOSSES:
        for (kind, name), lost in sorted(LOSSES.items()):
            logger.error("LOSS: [%s] %s dropped %d element(s) overall",
                         kind, name, lost)
    else:
        logger.info("No element loss: every nested array element became a row.")
    logger.info("Tables produced:")
    for (kind, name), stat in sorted(TABLE_STATS.items()):
        line = f"  [{kind}] {name}: {stat['rows']} row(s)"
        if stat["max_len"]:
            dist = ", ".join(f"{n}:{c}" for n, c in sorted(stat["lengths"].items()))
            line += (f"; array length max={stat['max_len']}, "
                     f"{{length:parents}} = {{{dist}}}")
        logger.info(line)


# --------------------------------------------------------------------------- #
# Denormalised join
# --------------------------------------------------------------------------- #


def merge_wide(tables: dict[str, pd.DataFrame], root: str) -> pd.DataFrame:
    """Left-join every child table onto its parent, producing one flat table.

    WARNING — row multiplication. Two sibling arrays under the same parent form
    a cartesian product: an activity with 4 fund lines and 3 asset locations
    yields 12 rows, and every parent-level value repeats across all 12. Fund and
    asset totals summed off this table will be wrong. The per-table masters are
    the ones to aggregate; this exists for one-row-per-everything consumers
    (Excel, a BI tool that cannot join).

    Tables are joined shallowest first, so a grandchild finds the renamed
    ``row_id`` of its parent already in the frame.
    """
    wide = tables[root].rename(columns={ROW_ID: f"{root}_{ROW_ID}"})

    for name in sorted((n for n in tables if n != root),
                       key=lambda n: n.count(TABLE_SEP)):
        parent_table, _, leaf = name.rpartition(TABLE_SEP)
        left_key = f"{parent_table}_{ROW_ID}"
        if left_key not in wide.columns:
            logger.warning("Cannot join %s: %s missing from the wide frame",
                           name, left_key)
            continue

        child = tables[name]
        child = child.drop(columns=[k for k in BUSINESS_KEYS
                                    if k in child.columns])
        renames = {ROW_ID: f"{name}_{ROW_ID}", PARENT_ID: left_key,
                   POS: f"{leaf}_{POS}"}
        # Prefix the payload columns too: fundList and assetDetails can carry
        # the same field name, and an unprefixed collision would become
        # amount_x / amount_y.
        renames.update({c: f"{leaf}_{c}" for c in child.columns
                        if c not in renames})
        wide = wide.merge(child.rename(columns=renames), on=left_key, how="left")

    return order_columns(wide)


# --------------------------------------------------------------------------- #
# Batched writing
# --------------------------------------------------------------------------- #


def batch_path(output_dir: Path, kind: str, table: str, number: int) -> Path:
    """``PL/batches/pl__fundlist__b0003.csv``."""
    return (output_dir / kind / BATCH_DIRNAME
            / f"{table}{TABLE_SEP}b{number:04d}.csv")


def master_path(output_dir: Path, kind: str, table: str,
                part: int | None = None) -> Path:
    """``PL/egramswaraj_pl__fundlist.csv``, or ``..._p002.csv`` when split."""
    suffix = "" if part is None else f"_p{part:03d}"
    return output_dir / kind / f"egramswaraj_{table}{suffix}.csv"


def concat_batch(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Union a batch's frames, keeping columns that only some files carry."""
    frame = frames[0] if len(frames) == 1 else pd.concat(
        frames, ignore_index=True, sort=False)
    # pd.concat reintroduces NaN (and float dtypes) for absent columns.
    return order_columns(restore_integers(frame))


class BatchWriter:
    """Buffer flattened frames per table and flush a CSV batch every N files."""

    def __init__(self, output_dir: Path, kind: str, batch_size: int,
                 wide: bool = False) -> None:
        self.output_dir = output_dir
        self.kind = kind
        self.batch_size = batch_size
        self.wide = wide
        self.number = 0
        self.files = 0
        self.rows = 0
        self.csvs = 0
        self.buffer: dict[str, list[pd.DataFrame]] = defaultdict(list)
        # Table -> the batch CSVs written for it, in order, for the master pass.
        self.batches: dict[str, list[Path]] = defaultdict(list)

    def add(self, parent: pd.DataFrame,
            children: dict[str, pd.DataFrame]) -> None:
        self.buffer[self.kind.lower()].append(parent)
        for name, child in children.items():
            self.buffer[name].append(child)
        self.files += 1
        if self.files >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        self.number += 1
        tables = {name: concat_batch(frames)
                  for name, frames in self.buffer.items()}

        # A kind with no nested arrays has nothing to join; a wide copy of the
        # parent would just duplicate the master.
        if self.wide and len(tables) > 1:
            tables[f"{self.kind.lower()}_wide"] = merge_wide(
                tables, self.kind.lower())

        for name, frame in tables.items():
            out = batch_path(self.output_dir, self.kind, name, self.number)
            out.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(out, index=False, encoding="utf-8")
            self.batches[name].append(out)
            self.rows += len(frame)
            self.csvs += 1

        logger.info("[%s] batch %04d: %d source file(s), %d table(s)",
                    self.kind, self.number, self.files, len(tables))
        self.buffer.clear()
        self.files = 0


# --------------------------------------------------------------------------- #
# Master build: concatenate a table's batches into one CSV
# --------------------------------------------------------------------------- #


def read_header(path: Path) -> list[str]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def union_columns(paths: list[Path]) -> list[str]:
    """Union the batch headers, first-seen order, leading columns hoisted."""
    seen: dict[str, None] = {}
    for path in paths:
        for col in read_header(path):
            seen.setdefault(col, None)
    lead = [c for c in LEADING_COLUMNS if c in seen]
    return lead + [c for c in seen if c not in lead]


def build_master(paths: list[Path], output_dir: Path, kind: str, table: str,
                 max_rows: int = MASTER_MAX_ROWS) -> tuple[int, list[Path]]:
    """Stream the batch CSVs of one table into a single master CSV.

    Rows are copied as text: a batch missing a column gets an empty cell rather
    than a shifted value, and ``lgd_code`` keeps any leading zeros. Only one
    batch is open at a time, so memory does not grow with the corpus.
    """
    columns = union_columns(paths)
    part = 1 if max_rows else None
    written: list[Path] = []
    rows = rows_in_part = 0
    sink = writer = None

    def open_part() -> None:
        nonlocal sink, writer, rows_in_part
        out = master_path(output_dir, kind, table, part)
        out.parent.mkdir(parents=True, exist_ok=True)
        sink = out.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(sink, fieldnames=columns, restval="",
                                extrasaction="ignore")
        writer.writeheader()
        written.append(out)
        rows_in_part = 0

    open_part()
    try:
        for path in tqdm(paths, unit="batch", desc=f"{kind}/{table}"):
            with path.open("r", newline="", encoding="utf-8") as source:
                for record in csv.DictReader(source):
                    if max_rows and rows_in_part >= max_rows:
                        sink.close()
                        part += 1
                        open_part()
                    writer.writerow(record)
                    rows += 1
                    rows_in_part += 1
    finally:
        if sink is not None:
            sink.close()

    if len(written) == 1 and part is not None:
        # No split was needed; drop the _p001 suffix.
        final = master_path(output_dir, kind, table)
        written[0].replace(final)
        written = [final]

    logger.info("Master %s: %d row(s), %d col(s), %d file(s)",
                table, rows, len(columns), len(written))
    return rows, written


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def tree(input_dir: Path, kinds: tuple[str, ...],
         limit_gps: int | None = None) -> None:
    """Flatten in memory and print the resulting table shape; write nothing."""
    input_dir = input_dir.expanduser().resolve()
    files = list(iter_json_files(input_dir, kinds, limit_gps))
    logger.info("Dry run over %d file(s) in %s", len(files), input_dir)

    columns: dict[tuple[str, str], dict[str, None]] = defaultdict(dict)
    for path, kind in tqdm(files, unit="file", desc="Inspecting"):
        result = flatten_file(path, kind)
        if result is None:
            continue
        parent, children = result
        for col in parent.columns:
            columns[(kind, kind.lower())].setdefault(col, None)
        for name, child in children.items():
            for col in child.columns:
                columns[(kind, name)].setdefault(col, None)

    print()
    for (kind, name), stat in sorted(TABLE_STATS.items()):
        cols = columns.get((kind, name), {})
        print(f"  [{kind}] {name}  rows={stat['rows']}  cols={len(cols)}")
        if stat["max_len"]:
            dist = ", ".join(f"{n}:{c}" for n, c in sorted(stat["lengths"].items()))
            print(f"        array length max={stat['max_len']}  "
                  f"{{length:parents}} = {{{dist}}}")
        print(f"        {', '.join(list(cols)[:12])}"
              f"{' ...' if len(cols) > 12 else ''}")
    print()
    if LOSSES:
        print("  WARNING: element loss detected, see log above.\n")


# --------------------------------------------------------------------------- #
# Main pass
# --------------------------------------------------------------------------- #


def _require_input(input_dir: Path) -> None:
    if not input_dir.is_dir():
        raise SystemExit(
            f"Input directory not found: {input_dir}\n"
            "Point the script at your data with either:\n"
            "  --input-dir /path/to/data/raw/Gram_Panchayat\n"
            "  export PANCHAYAT_DATA_ROOT=/path/to/data"
        )


def _log_header(input_dir: Path, output_dir: Path, kinds: tuple[str, ...],
                batch_size: int, wide: bool) -> None:
    # The first thing anyone debugging a fresh clone needs is where we looked.
    logger.info("Repo root : %s", REPO_ROOT)
    logger.info("Input dir : %s", input_dir)
    logger.info("Output dir: %s", output_dir)
    logger.info("Doc types : %s", ", ".join(kinds))
    logger.info("Batch size: %d source file(s) per CSV", batch_size)
    logger.info("Wide join : %s", "yes" if wide else "no")


def process(
    input_dir: Path,
    output_dir: Path,
    kinds: tuple[str, ...] = KINDS,
    limit_gps: int | None = None,
    batch_size: int = BATCH_SIZE,
    master: bool = True,
    master_max_rows: int = MASTER_MAX_ROWS,
    keep_batches: bool = True,
    wide: bool = False,
) -> None:
    """Flatten every matching JSON into batched CSVs, then per-table masters."""
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    _log_header(input_dir, output_dir, kinds, batch_size, wide)
    _require_input(input_dir)

    if limit_gps:
        logger.info("TEST RUN: limited to the first %d GP folder(s)", limit_gps)

    files = list(iter_json_files(input_dir, kinds, limit_gps))
    if not files:
        logger.warning("No %s JSON files found under %s",
                       "/".join(kinds), input_dir)
        return
    logger.info("Found %d file(s) under %s", len(files), input_dir)

    writers = {kind: BatchWriter(output_dir, kind, batch_size, wide)
               for kind in kinds}
    empty = 0
    per_kind: Counter = Counter()

    for path, kind in tqdm(files, unit="file", desc="Flattening"):
        result = flatten_file(path, kind)
        if result is None:
            empty += 1
            continue
        parent, children = result
        writers[kind].add(parent, children)
        per_kind[kind] += 1

    for writer in writers.values():
        writer.flush()

    batch_csvs = sum(w.csvs for w in writers.values())
    logger.info("Batches written: %d CSV(s), %d row(s); empty_or_corrupt=%d",
                batch_csvs, sum(w.rows for w in writers.values()), empty)
    logger.info("Per doc type (source files): %s",
                ", ".join(f"{k}={per_kind[k]}" for k in kinds))

    if master:
        for kind, writer in writers.items():
            for table, paths in sorted(writer.batches.items()):
                build_master(paths, output_dir, kind, table, master_max_rows)
            if not keep_batches and writer.batches:
                shutil.rmtree(output_dir / kind / BATCH_DIRNAME,
                              ignore_errors=True)
                logger.info("[%s] removed batch directory", kind)

    report_tables()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR,
                        help=f"GP folders to read (default: {INPUT_DIR})")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help=f"where CSVs go (default: {OUTPUT_DIR})")
    parser.add_argument("--kinds", nargs="+", choices=KINDS, default=list(KINDS),
                        metavar="KIND",
                        help=f"document types to flatten (default: all of "
                             f"{' '.join(KINDS)})")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"source JSONs per batch CSV "
                             f"(default: {BATCH_SIZE})")
    parser.add_argument("--no-master", action="store_true",
                        help="stop after the batches; skip the master CSVs")
    parser.add_argument("--master-max-rows", type=int, default=MASTER_MAX_ROWS,
                        help=f"split a master beyond this many rows into "
                             f"_pNNN parts; 0 disables "
                             f"(default: {MASTER_MAX_ROWS})")
    parser.add_argument("--drop-batches", action="store_true",
                        help="delete the batch CSVs once the masters are built")
    parser.add_argument("--wide", action="store_true",
                        help="also build one denormalised CSV per doc type by "
                             "left-joining the child tables (multiplies rows "
                             "across sibling arrays — see merge_wide)")
    parser.add_argument("--tree", action="store_true",
                        help="report the tables, row counts, columns and "
                             "array-length distributions that would be "
                             "produced, then exit; writes nothing")
    parser.add_argument("--limit-gps", type=int, default=None,
                        help="process only the first N GP folders (test runs)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)
    kinds = tuple(args.kinds)

    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    if args.tree:
        tree(args.input_dir, kinds, args.limit_gps)
        return

    process(
        args.input_dir,
        args.output_dir,
        kinds,
        args.limit_gps,
        batch_size=args.batch_size,
        master=not args.no_master,
        master_max_rows=args.master_max_rows,
        keep_batches=not args.drop_batches,
        wide=args.wide,
    )


if __name__ == "__main__":
    main(sys.argv[1:])