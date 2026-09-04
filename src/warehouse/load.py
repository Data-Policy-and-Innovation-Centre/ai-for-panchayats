"""Reading canonical Parquet tables and loading frames into DuckDB.

Two separate concerns live here:

* ``read_table`` fully materializes one canonical table (all of its part
  files) as a pandas DataFrame. The transform layer needs whole-table
  visibility to detect duplicate/conflicting business keys, so this is not
  chunked.
* ``insert`` writes an already-transformed frame into a DuckDB table in
  batches rather than one ``INSERT ... SELECT`` over the whole frame. This is
  where "scaling and chunk-boundary" behaviour (mined from PR #30's
  batch-oriented design, adapted to explicit constrained loading rather than
  DuckDB ``autodetect``/unconstrained CTAS) actually matters: the same code
  path runs whether a table has five rows or five million, and a table whose
  row count is not an exact multiple of the batch size is exercised directly
  by ``build.build`` in tests, not only as an isolated helper.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.dataset as ds

DEFAULT_BATCH_SIZE = 50_000

# DuckDB types an object column of ``decimal.Decimal`` values by sampling its
# first ``pandas_analyze_sample`` rows (default 1000) and then fails the whole
# load when a later row does not fit the width it picked. Every money column
# in ``transform`` is exactly that shape -- ``clean.to_decimal_money`` returns
# Decimals -- so a real ``fund_tied_general`` of 1000000.00 arriving after a
# thousand five-digit ones is rejected against an inferred DECIMAL(8,2). Test
# fixtures never reach the sample size, so this only ever appears at scale.
# Analyzing every row costs one pass over data already in memory.
PANDAS_ANALYZE_ROWS = 2**62


def read_table(snapshot_root: Path, relative_paths: tuple[str, ...]) -> pd.DataFrame:
    """Read every part file of one canonical table into a single DataFrame."""

    if not relative_paths:
        return pd.DataFrame()
    full_paths = [str(snapshot_root / path) for path in relative_paths]
    table = ds.dataset(full_paths, format="parquet").to_table()
    return table.to_pandas()


def discover_tables(manifest: dict) -> dict[str, tuple[str, ...]]:
    """Table name -> its declared Parquet part paths, from a validated manifest."""

    return {
        name: tuple(record["path"] for record in table["files"])
        for name, table in manifest["tables"].items()
    }


def insert(
    con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame,
    *, batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Insert a transformed frame into ``table`` in fixed-size batches.

    Column order and set are taken from the table's own DDL (``PRAGMA
    table_info``) rather than the frame's, so a frame with extra or
    reordered columns cannot silently shift into the wrong column.
    """

    if frame.empty:
        return 0
    con.execute(f"SET pandas_analyze_sample = {PANDAS_ANALYZE_ROWS}")
    columns = [row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
    payload = frame.reindex(columns=columns)
    total = 0
    for offset in range(0, len(payload), batch_size):
        chunk = payload.iloc[offset : offset + batch_size]
        con.register("_warehouse_load_chunk", chunk)
        try:
            con.execute(f"INSERT INTO {table} SELECT * FROM _warehouse_load_chunk")
        finally:
            con.unregister("_warehouse_load_chunk")
        total += len(chunk)
    return total
