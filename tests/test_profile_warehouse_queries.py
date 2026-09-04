"""The profiler must report what it measured, and admit what it did not.

Both behaviours here were Codex findings on #132, and both matter for the
same reason: a profile is read as evidence. A number that is always 1, or a
subtotal that quietly omits what failed, is evidence of nothing.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from scripts.profile_warehouse_queries import Probe, main, time_probe


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    """A minimal warehouse: the tables the base-table probes read."""

    path = tmp_path / "snap.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE gram_panchayat (gp_lgd_code VARCHAR, gp_name VARCHAR, "
                    "block_name VARCHAR, zp_name VARCHAR)")
        con.executemany("INSERT INTO gram_panchayat VALUES (?, ?, ?, ?)",
                        [(str(i), f"GP {i}", "B", "D") for i in range(7)])
        # Column lists match what the base-table probes actually select, so a
        # "clean run" here really is clean rather than quietly one probe short.
        con.execute("CREATE TABLE planned_activity (activity_code VARCHAR, "
                    "gp_lgd_code VARCHAR, fiscal_year VARCHAR)")
        con.executemany("INSERT INTO planned_activity VALUES (?, ?, ?)",
                        [(str(i), str(i % 7), "2021-2022") for i in range(11)])
        con.execute("CREATE TABLE activity_asset (activity_code VARCHAR)")
        con.executemany("INSERT INTO activity_asset VALUES (?)", [(str(i),) for i in range(5)])
        con.execute("CREATE TABLE activity_expenditure (activity_code VARCHAR, "
                    "scheme_name VARCHAR, total_expenditure DECIMAL(18,2))")
        con.execute("INSERT INTO activity_expenditure VALUES ('0', 'S', 1.00)")
    finally:
        con.close()
    return path


def _conn(path: Path):
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{path.as_posix()}' AS analytics (READ_ONLY)")
    con.execute("SET search_path = 'memory.main,analytics.main'")
    return con


def test_a_count_probe_reports_the_count_not_the_result_size(snapshot: Path):
    """`SELECT count(*)` always returns exactly one row, so reporting the
    result-set length prints 1 for every count probe -- which is how a 44-row
    fan-out in v_activity went unnoticed until it was chased separately."""

    con = _conn(snapshot)
    try:
        _, answer = time_probe(
            con, Probe("count", "SELECT count(*) FROM planned_activity", scalar=True), 1,
        )
        assert answer == 11
    finally:
        con.close()


def test_a_non_scalar_probe_still_reports_rows(snapshot: Path):
    """The control: marking counts must not change what the others report."""

    con = _conn(snapshot)
    try:
        _, answer = time_probe(
            con, Probe("rows", "SELECT gp_lgd_code FROM gram_panchayat"), 1,
        )
        assert answer == 7
    finally:
        con.close()


def test_a_failing_probe_reports_the_exception_not_a_timing(snapshot: Path):
    con = _conn(snapshot)
    try:
        elapsed, answer = time_probe(con, Probe("bad", "SELECT * FROM no_such_table"), 1)
        assert elapsed is None
        assert "Exception" in answer or "Error" in answer
    finally:
        con.close()


def test_a_clean_run_exits_zero(snapshot: Path, capsys):
    assert main([str(snapshot), "--repeat", "1"]) == 0
    out = capsys.readouterr().out
    assert "FAILED" not in out
    assert "4,073" not in out  # sanity: this is the fixture, not the real snapshot


def test_a_partial_profile_exits_nonzero_and_says_so(snapshot: Path, capsys):
    """A partial profile that exits 0 is worse than no profile: a CI caller,
    or a reader of the subtotal, takes an incomplete comparison for the whole
    one. Here the view probes run against a snapshot with no views."""

    # --materialized points at the same file, which has no v_* relations, so
    # every view probe fails while the base-table probes still succeed.
    assert main([str(snapshot), "--materialized", str(snapshot), "--repeat", "1"]) == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.out
    assert "probe(s) FAILED, excluded" in captured.out
    assert "this profile is incomplete" in captured.err


def test_explain_does_not_rerun_a_failed_probe(snapshot: Path, capsys):
    """A gap introduced by the previous fix: collecting failures did not guard
    the later EXPLAIN loop, so `--explain` on a failed probe re-ran the same
    invalid SQL outside the handler -- aborting before `con.close()` and
    before the failure report, replacing it with a traceback."""

    assert main([
        str(snapshot), "--materialized", str(snapshot), "--repeat", "1", "--explain", "v_activity",
    ]) == 1
    captured = capsys.readouterr()
    assert "skipped, probe failed" in captured.out
    # The structured report still reaches stderr, which is the thing the
    # traceback was displacing.
    assert "this profile is incomplete" in captured.err
