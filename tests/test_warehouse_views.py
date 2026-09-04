"""The consumer relations build, are stored, and decode (#51)."""

from __future__ import annotations

import duckdb

from src.warehouse.conformance import DERIVED_RELATIONS, check_derived_relations
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


def test_every_relation_builds_and_is_stored_not_a_view():
    """The whole point of #51: stored, so the joins are paid once.

    A view here would still be correct and still be slow -- the consumer
    measured 28,564 ms through views against 791 ms materialised -- so
    "it built" is not the property worth asserting on its own.
    """

    con = _empty_warehouse()
    try:
        counts = materialize(con)
        assert set(counts) == DERIVED_RELATIONS
        kinds = dict(con.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name LIKE 'v\\_%' ESCAPE '\\'"
        ).fetchall())
        assert set(kinds) == DERIVED_RELATIONS
        assert set(kinds.values()) == {"BASE TABLE"}, kinds
    finally:
        con.close()


def test_dependency_order_is_the_file_order():
    """v_exp and v_approval feed v_activity; v_asset and v_progress read it.

    Executed as written, so if someone reorders views.sql to put a dependant
    first, the build fails rather than silently producing an empty relation.
    """

    names = relation_names()
    assert names.index("v_exp") < names.index("v_activity")
    assert names.index("v_approval") < names.index("v_activity")
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
