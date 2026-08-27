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


def test_ledger_tables_use_decimal_16_2():
    """activity_expenditure/voucher/activity_voucher are the ledger-facing
    tables and must store money as exact DECIMAL(16,2), never DOUBLE."""

    for table in ("activity_expenditure", "voucher", "activity_voucher"):
        ddl = DDL[table]
        assert "DECIMAL(16,2)" in ddl, f"{table} must use DECIMAL(16,2) for its money columns"
        assert "DOUBLE" not in ddl, f"{table} must not use DOUBLE for money"


def test_planning_side_cost_columns_are_double():
    """Planning-side cost estimates (as opposed to ledger postings) are
    advisory figures and are stored as DOUBLE, not DECIMAL, per spec."""

    planning_money_tables = (
        "planned_activity", "activity_asset", "activity_fund",
        "admin_approval", "admin_approval_scheme", "technical_approval",
    )
    for table in planning_money_tables:
        assert "DOUBLE" in DDL[table], f"{table} should use DOUBLE for its planning-side cost columns"


def test_activity_nsap_beneficiary_count_is_integer_not_money(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        typ = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'activity_nsap' AND column_name = 'beneficiary_count'"
        ).fetchone()[0]
        assert typ == "INTEGER"
    finally:
        con.close()


def test_activity_asset_and_fund_have_no_row_id_and_key_on_activity_code(tmp_path: Path):
    """activity_asset/activity_fund are strictly 1:1 with planned_activity:
    no invented row_id, keyed on activity_code alone. A second row for the
    same activity_code must be rejected by the primary key."""

    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        for table in ("activity_asset", "activity_fund"):
            columns = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            assert "row_id" not in columns, f"{table} should not have a row_id column"
        con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Test GP')")
        con.execute(
            "INSERT INTO plan VALUES (NULL, NULL, 'P1', '123', '2021-2022', NULL, NULL, NULL)"
        )
        con.execute(
            "INSERT INTO planned_activity VALUES "
            "(NULL, NULL, '7', 'P1', '123', '2021-2022', NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
        )
        con.execute(
            "INSERT INTO activity_fund VALUES "
            "(NULL, NULL, '7', 'S1', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL, NULL, NULL)"
        )
        with pytest.raises(duckdb.ConstraintException):
            con.execute(
                "INSERT INTO activity_fund VALUES "
                "(NULL, NULL, '7', 'S2', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL, NULL)"
            )
    finally:
        con.close()


def test_voucher_unique_constraint_rejects_duplicate_business_key(tmp_path: Path):
    """voucher has no natural business-key primary key (voucher_pk is a
    surrogate); the UNIQUE constraint on (gp_lgd_code, fiscal_year,
    voucher_no) is the real, spec-required, verified-against-real-data
    identity guard and must actually reject a duplicate."""

    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Test GP')")
        con.execute(
            "INSERT INTO voucher VALUES (1, '123', '2021-2022', 'V1', 'X1', "
            "'payment', NULL, NULL, NULL, 100.00)"
        )
        with pytest.raises(duckdb.ConstraintException):
            con.execute(
                "INSERT INTO voucher VALUES (2, '123', '2021-2022', 'V1', 'X2', "
                "'receipt', NULL, NULL, NULL, 200.00)"
            )
    finally:
        con.close()


def test_voucher_direction_check_constraint_rejects_other_values(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "schema.duckdb"))
    try:
        _create_all(con)
        con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Test GP')")
        with pytest.raises(duckdb.ConstraintException):
            con.execute(
                "INSERT INTO voucher VALUES (1, '123', '2021-2022', 'V1', 'X1', "
                "'refund', NULL, NULL, NULL, 100.00)"
            )
    finally:
        con.close()


def test_activity_voucher_allows_null_voucher_pk():
    """488 real bridge rows cite FY 2026-27 vouchers the accounting extract
    does not reach and are legitimately unmatched; voucher_pk must stay
    nullable, and the row must still be insertable."""

    con = duckdb.connect(":memory:")
    try:
        _create_all(con)
        con.execute("INSERT INTO gram_panchayat VALUES ('123', 'Test GP')")
        con.execute(
            "INSERT INTO plan VALUES (NULL, NULL, 'P1', '123', '2021-2022', NULL, NULL, NULL)"
        )
        con.execute(
            "INSERT INTO activity_expenditure VALUES "
            "(1, NULL, NULL, '123', 'P1', '7', '2021-2022', '1', NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, NULL)"
        )
        con.execute(
            "INSERT INTO activity_voucher VALUES (1, NULL, '123', '2021-2022', 'V1', NULL, NULL)"
        )
        row = con.execute("SELECT voucher_pk FROM activity_voucher").fetchone()
        assert row == (None,)
    finally:
        con.close()


def test_dim_code_keeps_source_and_confidence_columns():
    """dim_code's source/confidence columns are a deliberate, documented
    addition beyond the ER diagram (see schema.py's module docstring): they
    must not be silently dropped."""

    columns = DDL["dim_code"]
    assert "source" in columns
    assert "confidence" in columns
