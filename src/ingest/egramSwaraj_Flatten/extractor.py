"""Flatten eGramSwaraj GP JSONs into relational CSVs.

Each nested array becomes its own table, keyed by row_id / parent_row_id / pos
and carrying the top-level business key. Nesting depth is unbounded.

    PL/batches/pl__b0001.csv, pl__fundlist__b0001.csv, ...
    PL/egramswaraj_pl.csv, egramswaraj_pl__fundlist.csv, ...

Batches are flushed every batch_size source files, then streamed row by row
into one master per table (split into _pNNN beyond master_max_rows).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from .config import (BATCH_DIRNAME, BATCH_SIZE, BUSINESS_KEYS, KINDS,
                     LEADING_COLUMNS, LEVEL_SEP, MASTER_MAX_ROWS, PARENT_ID,
                     POS, ROW_ID, TABLE_SEP)
from .utils import (coerce_nested, concat_batch, iter_json_files, list_columns,
                    load_json, parse_gp_folder, parse_plan_year, sanitise,
                    tidy, to_records)

logger = logging.getLogger(__name__)

TABLE_ROWS: Counter = Counter()   # (kind, table) -> rows, reported at the end


def split_list_columns(
    frame: pd.DataFrame,
    table: str,
    tables: dict[str, pd.DataFrame],
    sep: str = "_",
) -> pd.DataFrame:
    """Move every list-of-dict column into its own table, recursively.

    Returns frame without those columns. Non-dict members of a mixed list are
    JSON-encoded into a <col>_scalars column on the parent, each tagged with its
    source index so the original array can be rebuilt by merging on pos.
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
                # pos is the index in the source array, not a count of the
                # objects kept, so mixed arrays stay reconstructable.
                for source_pos, element in enumerate(value):
                    if not isinstance(element, dict):
                        strays.setdefault(idx, []).append(
                            {POS: source_pos, "value": element})
                        continue
                    parents.append(row_id)
                    positions.append(source_pos)
                    elements.append(element)

            if strays:
                frame[f"{col}{sep}scalars"] = [
                    json.dumps(strays[i]) if i in strays else None
                    for i in range(len(frame))
                ]

            if not elements:
                continue

            child = pd.json_normalize(elements, sep=sep)
            child.insert(0, POS, positions)
            child.insert(0, PARENT_ID, parents)
            # Array name is part of the id, so sibling arrays cannot collide.
            child.insert(0, ROW_ID, [f"{p}{LEVEL_SEP}{sanitise(col)}:{i}"
                                     for p, i in zip(parents, positions)])

            name = f"{table}{TABLE_SEP}{sanitise(col)}"
            child = split_list_columns(child, name, tables, sep=sep)

            if name in tables:                     # same array seen twice
                child = pd.concat([tables[name], child], ignore_index=True)
            tables[name] = child

    return frame


def attach_business_keys(parent: pd.DataFrame,
                         tables: dict[str, pd.DataFrame]) -> None:
    """Copy the parent's business keys onto every descendant table."""
    for key in BUSINESS_KEYS:
        if key not in parent.columns:
            continue
        lookup = dict(zip(parent[ROW_ID], parent[key]))
        for child in tables.values():
            if key in child.columns or PARENT_ID not in child.columns:
                continue
            # Top-level id never contains LEVEL_SEP, so truncating recovers it.
            root = child[PARENT_ID].astype(str).str.split(LEVEL_SEP, n=1).str[0]
            child.insert(3, key, root.map(lookup))


def flatten_file(
    path: Path, kind: str
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]] | None:
    """Flatten one JSON into (parent_frame, {table_name: child_frame})."""
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
    plan_year = parse_plan_year(path)
    frame.insert(0, ROW_ID,
                 [f"{lgd_code}|{plan_year}|{kind}|{i}" for i in range(len(frame))])

    table = kind.lower()
    tables: dict[str, pd.DataFrame] = {}
    frame = split_list_columns(frame, table, tables)

    frame["lgd_code"] = lgd_code
    frame["gram_panchayat_name"] = gp_name
    frame["plan_year"] = plan_year
    frame["doc_type"] = kind
    frame["source_file"] = path.name

    attach_business_keys(frame, tables)

    TABLE_ROWS[(kind, table)] += len(frame)
    for name, child in tables.items():
        TABLE_ROWS[(kind, name)] += len(child)

    return tidy(frame), {n: tidy(c) for n, c in tables.items()}


def report_tables() -> None:
    """Row counts per table, once, at the end of a run."""
    if not TABLE_ROWS:
        return
    logger.info("Tables produced:")
    for (kind, name), rows in sorted(TABLE_ROWS.items()):
        logger.info("  [%s] %s: %d row(s)", kind, name, rows)


def batch_path(output_dir: Path, kind: str, table: str, number: int) -> Path:
    """PL/batches/pl__fundlist__b0003.csv."""
    return (output_dir / kind / BATCH_DIRNAME
            / f"{table}{TABLE_SEP}b{number:04d}.csv")


def master_path(output_dir: Path, kind: str, table: str,
                part: int | None = None) -> Path:
    """PL/egramswaraj_pl__fundlist.csv, or ..._p002.csv when split."""
    suffix = "" if part is None else f"_p{part:03d}"
    return output_dir / kind / f"egramswaraj_{table}{suffix}.csv"


class BatchWriter:
    """Buffer frames per table and flush a CSV batch every N source files."""

    def __init__(self, output_dir: Path, kind: str,
                 batch_size: int = BATCH_SIZE) -> None:
        self.output_dir = output_dir
        self.kind = kind
        self.batch_size = batch_size
        self.number = 0
        self.files = 0
        self.rows = 0
        self.csvs = 0
        self.buffer: dict[str, list[pd.DataFrame]] = defaultdict(list)
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


def read_header(path: Path) -> list[str]:
    with path.open("r", newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def union_columns(paths: list[Path]) -> list[str]:
    """Union of the batch headers, first-seen order, key columns first."""
    seen: dict[str, None] = {}
    for path in paths:
        for col in read_header(path):
            seen.setdefault(col, None)
    lead = [c for c in LEADING_COLUMNS if c in seen]
    return lead + [c for c in seen if c not in lead]


def existing_masters(output_dir: Path, kind: str, table: str) -> list[Path]:
    """Every master file a previous run may have left for this table."""
    directory = output_dir / kind
    if not directory.is_dir():
        return []
    stem = master_path(output_dir, kind, table).stem
    return sorted(
        path for path in directory.glob(f"{stem}*.csv")
        if path.stem == stem or re.fullmatch(rf"{re.escape(stem)}_p\d{{3}}", path.stem)
    )


def build_master(paths: list[Path], output_dir: Path, kind: str, table: str,
                 max_rows: int = MASTER_MAX_ROWS) -> tuple[int, list[Path]]:
    """Stream one table's batches into a single master CSV.

    Rows are copied as text: a missing column gets an empty cell, and lgd_code
    keeps leading zeros. One batch open at a time.

    Parts are built under a staging directory and published only once the whole
    table is written, after every master file from a previous run is removed.
    A rerun therefore cannot leave a stale unsplit master beside new parts, or
    orphan _pNNN parts beside a new unsplit master.
    """
    columns = union_columns(paths)
    part = 1 if max_rows else None
    staging = output_dir / kind / f".{table}{TABLE_SEP}staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    rows = rows_in_part = 0
    sink = writer = None

    def open_part() -> None:
        nonlocal sink, writer, rows_in_part
        out = staging / master_path(output_dir, kind, table, part).name
        sink = out.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(sink, fieldnames=columns, restval="",
                                extrasaction="ignore")
        writer.writeheader()
        staged.append(out)
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
        if sink is not None:
            sink.close()
            sink = None
    finally:
        if sink is not None:
            sink.close()
            shutil.rmtree(staging, ignore_errors=True)

    # Publish: clear the old layout, then move the new one into place.
    for stale in existing_masters(output_dir, kind, table):
        stale.unlink()

    single = len(staged) == 1 and part is not None
    written: list[Path] = []
    for number, source in enumerate(staged, start=1):
        final = master_path(output_dir, kind, table,
                            None if single else number)
        final.parent.mkdir(parents=True, exist_ok=True)
        source.replace(final)
        written.append(final)
    shutil.rmtree(staging, ignore_errors=True)

    logger.info("Master %s: %d row(s), %d col(s), %d file(s)",
                table, rows, len(columns), len(written))
    return rows, written


def _require_input(input_dir: Path) -> None:
    if not input_dir.is_dir():
        raise SystemExit(
            f"Input directory not found: {input_dir}\n"
            "Point the script at your data with either:\n"
            "  --input-dir /path/to/data/raw/Gram_Panchayat\n"
            "  export PANCHAYAT_DATA_ROOT=/path/to/data"
        )


def process(
    input_dir: Path,
    output_dir: Path,
    kinds: tuple[str, ...] = KINDS,
    limit_gps: int | None = None,
    batch_size: int = BATCH_SIZE,
    master: bool = True,
    master_max_rows: int = MASTER_MAX_ROWS,
    keep_batches: bool = True,
) -> None:
    """Flatten every matching JSON into batched CSVs, then per-table masters."""
    input_dir = input_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    logger.info("Input dir : %s", input_dir)
    logger.info("Output dir: %s", output_dir)
    logger.info("Doc types : %s", ", ".join(kinds))
    logger.info("Batch size: %d source file(s) per CSV", batch_size)
    _require_input(input_dir)

    if limit_gps:
        logger.info("TEST RUN: limited to the first %d GP folder(s)", limit_gps)

    files = list(iter_json_files(input_dir, kinds, limit_gps))
    if not files:
        logger.warning("No %s JSON files found under %s",
                       "/".join(kinds), input_dir)
        return
    logger.info("Found %d file(s) under %s", len(files), input_dir)

    writers = {kind: BatchWriter(output_dir, kind, batch_size) for kind in kinds}
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

    logger.info("Batches written: %d CSV(s), %d row(s); empty_or_corrupt=%d",
                sum(w.csvs for w in writers.values()),
                sum(w.rows for w in writers.values()), empty)
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