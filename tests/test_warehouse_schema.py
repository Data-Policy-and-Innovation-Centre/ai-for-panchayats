"""DDL sanity: every table creates, and PK/FK constraints actually enforce."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from warehouse.schema import CREATE_ORDER, DDL, FACT_TABLES, RESET_ORDER


def test_create_order_is_reset_order_reversed():
    assert CREATE_ORDER == list(reversed(RESET_ORDER))
    assert set(CREATE_ORDER) == set(DDL)


def test_fact_tables_excludes_quarantine():
    assert "quarantine" not in FACT_TABLES
    assert set(FACT_TABLES) | {"quarantine"} == set(DDL)


def _create_all(con: duckdb.DuckDBPyConnection) -> None:
    for table in RESET_ORDER:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for table in CREATE_ORDER:
        con.execute(DDL[table])


def test_schema_creates_and_is_rerunnable(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        _create_all(con)  # rerunnable: RESET_ORDER must drop children before parents
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        assert tables == set(DDL)
    finally:
        con.close()


def test_primary_key_rejects_duplicate(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Test GP')")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Different Name')")
    finally:
        con.close()


def test_foreign_key_rejects_orphan(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        with pytest.raises(duckdb.ConstraintException):
            con.execute(
                "INSERT INTO plan VALUES "
                "('egramSwaraj', 'run-1', 'P1', 'does-not-exist', '2021-2022', NULL, NULL, NULL)"
            )
    finally:
        con.close()


def test_money_columns_are_decimal_not_double():
    for table, ddl in DDL.items():
        assert "DOUBLE" not in ddl or table in {"physical_progress"}, (
            f"{table} uses DOUBLE where money columns should be DECIMAL"
        )
