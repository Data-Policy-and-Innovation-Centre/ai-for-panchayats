"""Loading transformed frames into DuckDB.

The one behaviour here that ordinary fixtures do not reach is DuckDB's
sampled type inference over pandas object columns: it reads only
``pandas_analyze_sample`` (1000) rows, spread across the column, so it
misfires only on a column long enough for the stride to step over the widest
value. Every unit fixture in this repo is far shorter than that. The miss
cost a full-state build -- a real ``pl__fundlist`` row of 1000000.00
rejected against a DECIMAL(8,2) inferred from smaller neighbours -- so it
gets a test long enough to trip it.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from src.warehouse.load import insert

# Long enough for DuckDB to sample rather than read every row, with the wide
# value parked at an index no uniform stride over 1000 samples lands on.
ROWS = 10_001
WIDE_ROW = 5_001


@pytest.mark.parametrize("column_type", ["DOUBLE", "DECIMAL(16,2)"])
def test_insert_accepts_a_wide_decimal_the_sample_would_miss(column_type: str) -> None:
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t (amount {column_type})")
    amounts = [Decimal("0.00")] * ROWS
    amounts[WIDE_ROW] = Decimal("1000000.00")

    assert insert(con, "t", pd.DataFrame({"amount": amounts})) == ROWS
    count, largest = con.execute("SELECT count(*), max(amount) FROM t").fetchone()
    assert count == ROWS
    assert Decimal(str(largest)) == Decimal("1000000.00")
