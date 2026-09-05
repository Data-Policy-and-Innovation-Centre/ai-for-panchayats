"""The consumer relations build, are stored, and decode (#51)."""

from __future__ import annotations

import duckdb

from src.warehouse.conformance import DERIVED_RELATIONS, DYNAMIC_RELATIONS, check_derived_relations
from src.warehouse.dimensions import dimension_frames
from src.warehouse.schema import DDL
from src.warehouse.views import materialize, relation_names


def _empty_warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for ddl in DDL.values():
        con.execute(ddl)
    return con


def _seed_activity(con: duckdb.DuckDBPyConnection, activity_code: str, **columns) -> None:
    """One GP and one activity on it.

    v_activity inner-joins gram_panchayat, so an activity whose GP is absent
    produces no row at all -- which would make these tests pass vacuously by
    asserting on an empty result.
    """

    con.execute(
        "INSERT INTO gram_panchayat (gp_lgd_code, gp_name) VALUES ('111', 'Test GP')"
    )
    names = ["source_system", "source_run_id", "activity_code", "gp_lgd_code", *columns]
    values = ["s", "r", activity_code, "111", *columns.values()]
    placeholders = ", ".join("?" for _ in names)
    con.execute(
        f"INSERT INTO planned_activity ({', '.join(names)}) VALUES ({placeholders})",  # noqa: S608
        values,
    )


def test_every_relation_builds_and_is_stored_except_the_one_that_must_not_be():
    """The whole point of #51: stored, so the joins are paid once.

    A view here would still be correct and still be slow -- the consumer
    measured 28,564 ms through views against 791 ms materialised -- so
    "it built" is not the property worth asserting on its own.

    `v_activity` is the deliberate exception and is asserted as a view, not
    merely left out: it adds `days_since_sanction`, which counts up to
    CURRENT_DATE, so storing it would freeze that count at build time. Its
    join graph is stored as `v_activity_base`, so the cost is still paid
    once. Asserting the kind of every relation, rather than just that they
    exist, is what makes storing `v_activity` later fail here.
    """

    con = _empty_warehouse()
    try:
        counts = materialize(con)
        assert set(counts) == DERIVED_RELATIONS - DYNAMIC_RELATIONS
        kinds = dict(con.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name LIKE 'v\\_%' ESCAPE '\\'"
        ).fetchall())
        assert set(kinds) == DERIVED_RELATIONS
        assert {n for n, k in kinds.items() if k == "BASE TABLE"} == (
            DERIVED_RELATIONS - DYNAMIC_RELATIONS
        ), kinds
        assert {n for n, k in kinds.items() if k != "BASE TABLE"} == DYNAMIC_RELATIONS, kinds
    finally:
        con.close()


def test_dependency_order_is_the_file_order():
    """v_exp and v_approval feed v_activity; v_asset and v_progress read it.

    Executed as written, so if someone reorders views.sql to put a dependant
    first, the build fails rather than silently producing an empty relation.
    """

    names = relation_names()
    assert names.index("v_exp") < names.index("v_activity_base")
    assert names.index("v_approval") < names.index("v_activity_base")
    assert names.index("v_activity_base") < names.index("v_activity")
    assert names.index("v_activity") < names.index("v_asset")
    assert names.index("v_activity") < names.index("v_progress")


def test_conformance_rejects_a_warehouse_without_them():
    con = _empty_warehouse()
    try:
        findings = check_derived_relations(con)
        assert [f.check for f in findings] == ["relations.derived"]
        assert all(f.severity == "violation" for f in findings)
    finally:
        con.close()


def test_conformance_rejects_them_as_views():
    """Shipping views instead of tables passes a naive existence check and
    changes nothing about the cost, so it is called out separately."""

    con = _empty_warehouse()
    try:
        for name in sorted(DERIVED_RELATIONS):
            con.execute(f"CREATE VIEW {name} AS SELECT 1 AS x")  # noqa: S608
        findings = check_derived_relations(con)
        assert [f.check for f in findings] == ["relations.materialized"]
        assert "still views" in findings[0].actual
    finally:
        con.close()


def test_conformance_rejects_a_stored_v_activity():
    """The opposite mistake to the one above, and the quieter one.

    A view where a table belongs is slow. A *table* where the view belongs
    is fast and wrong: `days_since_sanction` is frozen at the build date and
    drifts one day further for every day the snapshot stays deployed, with
    nothing failing anywhere. Pinned here as well as in the build test,
    because conformance is what runs against a database someone is about to
    deploy.
    """

    con = _empty_warehouse()
    try:
        materialize(con)
        assert check_derived_relations(con) == [], "a normal build is clean"

        con.execute("DROP VIEW v_activity")
        con.execute("CREATE TABLE v_activity AS SELECT * FROM v_activity_base")
        findings = check_derived_relations(con)
        assert [f.check for f in findings] == ["relations.dynamic"]
        assert findings[0].severity == "violation"
        assert "v_activity" in findings[0].actual
    finally:
        con.close()


def test_a_non_numeric_code_does_not_abort_the_build():
    """activity_asset.asset_type carries free text such as 'well', and
    dim_code has one non-numeric code ('A'). A hard CAST raises
    ConversionException and takes the whole build with it; TRY_CAST leaves
    the label to COALESCE.
    """

    con = _empty_warehouse()
    try:
        con.register("dim_code_frame", dimension_frames()["dim_code"])
        con.execute("INSERT INTO dim_code SELECT * FROM dim_code_frame")
        _seed_activity(con, "act-1")
        con.execute(
            "INSERT INTO activity_asset (source_system, source_run_id, activity_code, "
            "asset_type) VALUES ('s', 'r', 'act-1', 'well')"
        )
        materialize(con)
        label = con.execute(
            "SELECT asset_type_label FROM v_asset WHERE activity_code = 'act-1'"
        ).fetchall()
        assert label == [("Unknown",)], label
    finally:
        con.close()


def test_labels_actually_decode_through_dim_code():
    """The reason dim_code had to land first (#48): without it every label
    is NULL and the relations are fast but unreadable."""

    con = _empty_warehouse()
    try:
        con.register("dim_code_frame", dimension_frames()["dim_code"])
        con.execute("INSERT INTO dim_code SELECT * FROM dim_code_frame")
        code = con.execute(
            "SELECT code FROM dim_code WHERE variable = 'activity_status' LIMIT 1"
        ).fetchone()[0]
        _seed_activity(con, "act-1", activity_status=int(code))
        materialize(con)
        label = con.execute(
            "SELECT status_label FROM v_activity WHERE activity_code = 'act-1'"
        ).fetchone()[0]
        assert label not in (None, ""), "status_label did not decode"
    finally:
        con.close()


def test_days_since_sanction_is_computed_per_query_not_frozen_at_build():
    """The column that cannot be stored, and the reason `v_activity` is a view.

    `days_since_sanction` counts days up to CURRENT_DATE. Every other column
    in these relations is a function of the data alone, so storing it is free;
    this one is a function of *when you ask*. A stored copy answers as of the
    build date and drifts one day further every day the snapshot stays
    deployed -- and nothing fails, the number is just increasingly wrong. That
    is the failure this pins.

    Asserted three ways, because any one alone is weak: the column is absent
    from the stored base (so there is no frozen copy to drift), `v_activity`
    is a view (so it is recomputed), and its value matches the arithmetic done
    fresh against CURRENT_DATE right now.
    """

    con = _empty_warehouse()
    try:
        # An activity with a real sanction date, 100 days ago. Without a row
        # the last assertion below is vacuously true -- the hazard this file's
        # own `_seed_activity` docstring warns about.
        _seed_activity(con, "A1", fiscal_year="2021-2022")
        con.execute(
            "INSERT INTO admin_approval (source_system, source_run_id, row_id, "
            "gp_lgd_code, activity_code, adm_approval_sanction_date) "
            "VALUES ('s', 'r', 'r1', '111', 'A1', CURRENT_DATE - INTERVAL 100 DAY)"
        )
        materialize(con)
        assert con.execute(
            "SELECT days_since_sanction FROM v_activity WHERE activity_code = 'A1'"
        ).fetchone()[0] == 100

        base_columns = {
            row[0] for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'v_activity_base'"
            ).fetchall()
        }
        assert "days_since_sanction" not in base_columns
        assert "sanction_day" in base_columns, "the view needs this to recompute"

        kind = con.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_name = 'v_activity'"
        ).fetchone()[0]
        assert kind != "BASE TABLE", kind

        checked, drifted = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE days_since_sanction "
            "IS DISTINCT FROM DATE_DIFF('day', sanction_day, CURRENT_DATE)) "
            "FROM v_activity WHERE sanction_day IS NOT NULL"
        ).fetchone()
        assert checked > 0, "no sanctioned row: the next assertion would be vacuous"
        assert drifted == 0
    finally:
        con.close()


def test_duplicate_approvals_do_not_fan_out_the_activity_views():
    """v_activity is documented as strictly one row per activity (#133).

    `admin_approval` and `technical_approval` are keyed on row_id, not on
    activity_code, so neither is 1:1 with an activity by construction and the
    join was free to multiply. At full state that produced exactly 44 rows of
    fan-out -- 44 activity_codes with two admin_approval rows -- which
    propagates through v_asset and every analytical SUM built on these views,
    overstating counts and money.

    Two approvals with different costs, so the collapse also has to pick the
    documented winner rather than merely picking one.
    """

    con = _empty_warehouse()
    _seed_activity(con, "A1", total_cost=100.0)
    for row_id, cost in (("r1", 10.0), ("r2", 99.0)):
        con.execute(
            "INSERT INTO admin_approval "
            "(source_system, source_run_id, row_id, gp_lgd_code, activity_code, "
            " plan_year, work_proposed_cost, adm_approval_authority) "
            "VALUES ('s', 'r', ?, '111', 'A1', '2021-2022', ?, 'SARPANCH')",
            [row_id, cost],
        )
    con.execute(
        "INSERT INTO technical_approval "
        "(source_system, source_run_id, row_id, gp_lgd_code, activity_code, plan_year) "
        "VALUES ('s', 'r', 't1', '111', 'A1', '2021-2022')"
    )
    materialize(con)

    assert con.execute("SELECT COUNT(*) FROM v_approval").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM v_activity").fetchone()[0] == 1, (
        "one planned activity must yield exactly one v_activity row"
    )
    # Largest value wins, mirroring what the scheme columns already do.
    assert con.execute(
        "SELECT work_proposed_cost FROM v_approval WHERE activity_code = 'A1'"
    ).fetchone()[0] == 99.0


def test_the_approval_collapse_is_deterministic_when_costs_tie():
    """A tie must not be settled by scan order.

    Picking arbitrarily would make the view's contents depend on how the
    table happened to be read, so two builds of the same data could disagree.
    row_id breaks the tie because it is the approval's own identity.
    """

    con = _empty_warehouse()
    _seed_activity(con, "A1", total_cost=100.0)
    for row_id, authority in (("r2", "BDO"), ("r1", "SARPANCH")):
        con.execute(
            "INSERT INTO admin_approval "
            "(source_system, source_run_id, row_id, gp_lgd_code, activity_code, "
            " plan_year, work_proposed_cost, adm_approval_authority) "
            "VALUES ('s', 'r', ?, '111', 'A1', '2021-2022', 50.0, ?)",
            [row_id, authority],
        )
    materialize(con)

    assert con.execute("SELECT COUNT(*) FROM v_approval").fetchone()[0] == 1
    assert con.execute(
        "SELECT authority_raw FROM v_approval WHERE activity_code = 'A1'"
    ).fetchone()[0] == "SARPANCH", "the lowest row_id wins a tie, not the insertion order"
