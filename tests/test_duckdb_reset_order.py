"""The DuckDB rebuild must stay rerunnable once the extension tables exist.

duckdb_database_2.ipynb resets the core schema; add_PP_ta_AA_tables.ipynb then
adds admin_approval, admin_approval_scheme, technical_approval and
physical_progress, all of which foreign-key planned_activity. Any reset that
omits them cannot drop planned_activity, so the notebook is not idempotent.

These tests use a synthetic in-memory schema mirroring those relationships.
"""

from __future__ import annotations

import duckdb
import pytest

# Order taken from duckdb_database_2.ipynb, BLOCK 3 / BLOCK 4.
CORE_ORDER = [
    "activity_voucher", "voucher", "activity_expenditure",
    "activity_delegation", "planned_activity", "plan", "gram_panchayat",
]
EXTENSION_ORDER = [
    "physical_progress", "technical_approval",
    "admin_approval_scheme", "admin_approval",
]
RESET_ORDER = EXTENSION_ORDER + CORE_ORDER


@pytest.fixture
def con():
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE gram_panchayat (gp_lgd_code VARCHAR PRIMARY KEY);
        CREATE TABLE plan (
            plan_code VARCHAR PRIMARY KEY, gp_lgd_code VARCHAR,
            FOREIGN KEY (gp_lgd_code) REFERENCES gram_panchayat (gp_lgd_code));
        CREATE TABLE planned_activity (
            activity_code VARCHAR PRIMARY KEY, plan_code VARCHAR,
            total_cost DECIMAL(16,2),
            FOREIGN KEY (plan_code) REFERENCES plan (plan_code));
        CREATE TABLE activity_delegation (
            activity_code VARCHAR PRIMARY KEY,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code));
        CREATE TABLE activity_expenditure (
            expenditure_id INTEGER PRIMARY KEY, activity_code VARCHAR,
            total_expenditure DECIMAL(16,2));
        CREATE TABLE voucher (voucher_pk INTEGER PRIMARY KEY);
        CREATE TABLE activity_voucher (
            expenditure_id INTEGER, voucher_pk INTEGER,
            FOREIGN KEY (expenditure_id)
                REFERENCES activity_expenditure (expenditure_id));
    """)
    # add_PP_ta_AA_tables.ipynb
    connection.execute("""
        CREATE TABLE admin_approval (
            row_id VARCHAR PRIMARY KEY, activity_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code));
        CREATE TABLE admin_approval_scheme (
            row_id VARCHAR PRIMARY KEY, parent_row_id VARCHAR,
            FOREIGN KEY (parent_row_id) REFERENCES admin_approval (row_id));
        CREATE TABLE technical_approval (
            row_id VARCHAR PRIMARY KEY, activity_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code));
        CREATE TABLE physical_progress (
            row_id VARCHAR PRIMARY KEY, activity_code VARCHAR,
            FOREIGN KEY (activity_code) REFERENCES planned_activity (activity_code));
    """)
    yield connection
    connection.close()


def test_core_only_reset_cannot_drop_planned_activity(con):
    """Regression: this is what the notebook did, and why a rerun failed."""
    with pytest.raises(duckdb.CatalogException) as excinfo:
        for table in CORE_ORDER:
            con.execute(f"DROP TABLE IF EXISTS {table}")

    # DuckDB names whichever extension table still references planned_activity.
    assert any(table in str(excinfo.value) for table in EXTENSION_ORDER)


def test_full_reset_order_drops_everything(con):
    for table in RESET_ORDER:
        con.execute(f"DROP TABLE IF EXISTS {table}")

    remaining = con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()
    assert remaining == []


def test_delete_reset_order_clears_every_table(con):
    con.execute("INSERT INTO gram_panchayat VALUES ('119598')")
    con.execute("INSERT INTO plan VALUES ('P1', '119598')")
    con.execute("INSERT INTO planned_activity VALUES ('A1', 'P1', 100.00)")
    con.execute("INSERT INTO physical_progress VALUES ('R1', 'A1')")

    for table in RESET_ORDER:
        con.execute(f"DELETE FROM {table}")

    for table in RESET_ORDER:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_utilisation_must_aggregate_expenditure_before_joining(con):
    """One activity with two expenditure rows must not double its planned cost."""
    con.execute("INSERT INTO gram_panchayat VALUES ('119598')")
    con.execute("INSERT INTO plan VALUES ('P1', '119598')")
    con.execute("INSERT INTO planned_activity VALUES ('A1', 'P1', 100.00)")
    con.execute("INSERT INTO activity_expenditure VALUES (1, 'A1', 30.00), (2, 'A1', 20.00)")

    naive = con.execute("""
        SELECT count(*), sum(a.total_cost), sum(e.total_expenditure)
        FROM planned_activity a
        LEFT JOIN activity_expenditure e USING (activity_code)
    """).fetchone()
    assert naive == (2, 200.00, 50.00)      # the bug: 1 activity, 100 planned

    fixed = con.execute("""
        SELECT count(*), sum(a.total_cost), sum(e.total_expenditure)
        FROM planned_activity a
        LEFT JOIN (
            SELECT activity_code, sum(total_expenditure) AS total_expenditure
            FROM activity_expenditure
            GROUP BY activity_code
        ) e USING (activity_code)
    """).fetchone()
    assert fixed == (1, 100.00, 50.00)
