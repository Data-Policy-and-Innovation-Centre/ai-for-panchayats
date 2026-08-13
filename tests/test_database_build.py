"""End-to-end build against a real DuckDB, on synthetic sources."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from database.build import Sources, build, build_into, create_schema, table_counts
from database.schema import CREATE_ORDER, RESET_ORDER
from database.validate import ValidationFailed, run_checks, validate_database


@pytest.fixture
def database(tmp_path, sources):
    path = tmp_path / "panchayat.duckdb"
    counts, quarantine = build_into(path, sources)
    return path, counts, quarantine


# ---------------------------------------------------------------- schema


def test_schema_creates_every_declared_table(tmp_path):
    con = duckdb.connect(str(tmp_path / "empty.duckdb"))
    try:
        create_schema(con)
        present = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()

    assert present == set(CREATE_ORDER)


def test_schema_is_rerunnable_once_the_extension_tables_exist(tmp_path):
    """The notebook's reset order could not drop planned_activity a second time."""
    path = tmp_path / "rerun.duckdb"
    con = duckdb.connect(str(path))
    try:
        create_schema(con)
        create_schema(con)      # the rerun that used to raise CatalogException
        present = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()

    assert present == set(CREATE_ORDER)


def test_reset_order_drops_children_before_parents():
    for child, parent in [("activity_voucher", "voucher"),
                          ("activity_voucher", "activity_expenditure"),
                          ("admin_approval_scheme", "admin_approval"),
                          ("physical_progress", "planned_activity"),
                          ("planned_activity", "gram_panchayat")]:
        assert RESET_ORDER.index(child) < RESET_ORDER.index(parent)


# ---------------------------------------------------------------- load


def test_build_loads_the_expected_shape(database):
    _, counts, _ = database

    assert counts["gram_panchayat"] == 1
    assert counts["planned_activity"] == 2
    assert counts["activity_expenditure"] == 2
    assert counts["voucher"] == 2
    assert counts["activity_voucher"] == 2
    assert counts["admin_approval"] == 1
    assert counts["physical_progress"] == 2


def test_codes_survive_the_round_trip_as_strings(database):
    path, _, _ = database
    con = duckdb.connect(str(path), read_only=True)
    try:
        code = con.execute(
            "SELECT activity_code FROM planned_activity ORDER BY 1").fetchone()[0]
        types = dict(con.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = 'planned_activity'
        """).fetchall())
    finally:
        con.close()

    assert code == "128856295"          # not 128856295.0
    assert types["activity_code"] == "VARCHAR"
    assert types["gp_lgd_code"] == "VARCHAR"


def test_money_is_decimal_not_double(database):
    path, _, _ = database
    con = duckdb.connect(str(path), read_only=True)
    try:
        types = dict(con.execute("""
            SELECT table_name || '.' || column_name, data_type
            FROM information_schema.columns
            WHERE column_name IN ('total_cost', 'total_expenditure', 'amount')
        """).fetchall())
    finally:
        con.close()

    for column, kind in types.items():
        assert kind.startswith("DECIMAL"), f"{column} is {kind}"


def test_rebuild_is_idempotent(tmp_path, sources):
    path = tmp_path / "twice.duckdb"

    first, _ = build_into(path, sources)
    second, _ = build_into(path, sources)

    assert first == second


# ---------------------------------------------------------------- constraints


def test_both_bridge_parents_are_enforced(database):
    """The notebook constrained expenditure_id but not voucher_pk."""
    path, _, _ = database
    con = duckdb.connect(str(path))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO activity_voucher VALUES "
                        "(1, 999999, '119598', '2025-2026', 'X', NULL, NULL)")
        with pytest.raises(duckdb.ConstraintException):
            con.execute("INSERT INTO activity_voucher VALUES "
                        "(999999, 1, '119598', '2025-2026', 'X', NULL, NULL)")
    finally:
        con.close()


def test_expenditure_activity_code_is_enforced(database):
    path, _, _ = database
    con = duckdb.connect(str(path))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""INSERT INTO activity_expenditure
                (expenditure_id, activity_code, plan_code, gp_lgd_code)
                VALUES (9999, 'nope', '6012003', '119598')""")
    finally:
        con.close()


def test_voucher_business_key_is_unique(database):
    path, _, _ = database
    con = duckdb.connect(str(path))
    try:
        with pytest.raises(duckdb.ConstraintException):
            con.execute("""INSERT INTO voucher (voucher_pk, gp_lgd_code,
                fiscal_year, voucher_no) VALUES
                (999, '119598', '2025-2026', 'XVFC/2025-26/P/143')""")
    finally:
        con.close()


# ---------------------------------------------------------------- quarantine


def test_quarantined_rows_are_recorded_in_the_database(tmp_path, sources):
    """An approval for an unknown activity must be countable, not vanish."""
    sources.admin_approval = sources.admin_approval.copy()
    sources.admin_approval.loc[0, "activityCd"] = 999999999

    path = tmp_path / "quarantined.duckdb"
    counts, quarantine = build_into(path, sources)

    assert counts["admin_approval"] == 0
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute("""SELECT table_name, reason, key_value, row_count
                              FROM quarantine WHERE table_name = 'admin_approval'
                           """).fetchall()
    finally:
        con.close()

    assert rows == [("admin_approval", "activity_code not in parent",
                     "999999999", 1)]
    assert quarantine.total("admin_approval") == 1


# ---------------------------------------------------------------- validation


def test_a_clean_build_passes_every_check(database):
    path, counts, _ = database
    con = duckdb.connect(str(path), read_only=True)
    try:
        checks = run_checks(con, counts, manifest={})
    finally:
        con.close()

    assert [c for c in checks if not c.passed] == []


def test_manifest_count_mismatch_fails_validation(database):
    path, counts, _ = database
    con = duckdb.connect(str(path), read_only=True)
    try:
        checks = run_checks(con, counts,
                            manifest={"expected_rows": {"planned_activity": 99}})
    finally:
        con.close()

    failed = [c for c in checks if not c.passed]
    assert len(failed) == 1
    assert "expected 99, loaded 2" in failed[0].detail


def test_bridge_match_rate_floor_is_enforced(tmp_path, sources):
    """A voucher the accounting extract does not cover drops the match rate."""
    sources.vouchers = sources.vouchers.iloc[:0]
    path = tmp_path / "unmatched.duckdb"
    counts, _ = build_into(path, sources)

    con = duckdb.connect(str(path), read_only=True)
    try:
        checks = run_checks(con, counts,
                            manifest={"thresholds": {"bridge_match_rate": 0.9}})
    finally:
        con.close()

    rate_check = next(c for c in checks if c.name == "bridge match rate")
    assert not rate_check.passed
    assert "0.0% matched" in rate_check.detail


def test_validate_database_reads_an_existing_build(database):
    path, _, _ = database

    assert [c for c in validate_database(path) if not c.passed] == []


# ---------------------------------------------------------------- publication


def test_build_publishes_atomically(tmp_path, sources):
    target = tmp_path / "published.duckdb"

    build(target=target, sources=sources)

    assert target.exists()
    assert list(tmp_path.glob(".published-build-*")) == []
    assert table_counts(target)["planned_activity"] == 2


def test_a_failed_validation_leaves_the_previous_database_untouched(
        tmp_path, sources, monkeypatch):
    target = tmp_path / "published.duckdb"
    build(target=target, sources=sources)
    before = target.read_bytes()

    monkeypatch.setattr(
        "database.validate.load_manifest",
        lambda path=None: {"expected_rows": {"planned_activity": 99}})

    with pytest.raises(ValidationFailed):
        build(target=target, sources=sources)

    assert target.read_bytes() == before
    assert list(tmp_path.glob(".published-build-*")) == []


def test_a_failed_build_leaves_no_partial_database(tmp_path, sources):
    target = tmp_path / "published.duckdb"
    broken = Sources(
        planning=sources.planning,
        expenditure=sources.expenditure.drop(columns=["Voucher No"]),
        vouchers=sources.vouchers,
    )

    with pytest.raises(KeyError):
        build(target=target, sources=broken)

    assert not target.exists()
    assert list(tmp_path.glob(".published-build-*")) == []


# ---------------------------------------------------------------- queries


def test_utilisation_must_aggregate_expenditure_before_joining(tmp_path, sources):
    """Guards the multiplication bug the notebook query had."""
    extra = sources.expenditure.iloc[[0]].copy()
    extra["S.No."] = 3
    extra["Total Expenditure"] = 10000.00
    extra["Voucher No"] = None
    extra["Voucher Date"] = None
    extra["Voucher Cost"] = None
    sources.expenditure = pd.concat([sources.expenditure, extra],
                                    ignore_index=True)

    path = tmp_path / "grain.duckdb"
    build_into(path, sources)

    con = duckdb.connect(str(path), read_only=True)
    try:
        naive = con.execute("""
            SELECT count(*), sum(a.total_cost)
            FROM planned_activity a
            LEFT JOIN activity_expenditure e USING (activity_code)
        """).fetchone()
        correct = con.execute("""
            SELECT count(*), sum(a.total_cost)
            FROM planned_activity a
            LEFT JOIN (
                SELECT activity_code, sum(total_expenditure) AS total_expenditure
                FROM activity_expenditure GROUP BY activity_code
            ) e USING (activity_code)
        """).fetchone()
    finally:
        con.close()

    assert naive == (3, 297847.00)        # one activity counted twice
    assert correct == (2, 200847.00)
