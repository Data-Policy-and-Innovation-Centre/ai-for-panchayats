"""Geography for gram_panchayat (#61).

The acceptance criteria in #61 are asserted here, not eyeballed: a geography
backfill that joins on the wrong key still produces non-null columns, so
"the columns are populated" is not evidence that they are populated
*correctly*. The shape of the result is.
"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from warehouse import transform as t
from warehouse import validate
from warehouse.conformance import check_geography_completeness
from warehouse.geography import (
    STATE_NAME,
    GEOGRAPHY_COLUMNS,
    GeographyError,
    gp_geography,
)
from warehouse.schema import DDL


def _row(**overrides) -> dict:
    base = {
        "source_system": "egramSwaraj", "source_run_id": "run-1", "row_id": "r0",
        "parent_row_id": None, "pos": None, "gp_code": "123", "gram_panchayat_name": "Test GP",
        "fiscal_year": "2021-2022", "plan_year": "2021-2022", "business_id": "7",
        "mapping_status": "mapped",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- the reference tree


def test_reference_tree_covers_the_whole_state():
    """#61: 6,794 GPs in ~30 districts and ~314 blocks -- asserted, not eyeballed."""

    lookup = gp_geography()
    assert len(lookup) == 6794
    assert len({geo["zp_name"] for geo in lookup.values()}) == 30
    assert len({geo["block_code"] for geo in lookup.values()}) == 314
    assert {geo["state_name"] for geo in lookup.values()} == {STATE_NAME}


def test_every_gp_has_all_six_columns():
    """A partially-populated row is the failure #61 describes; it must be impossible."""

    for code, geo in gp_geography().items():
        missing = [col for col in GEOGRAPHY_COLUMNS if not geo.get(col)]
        assert not missing, f"{code} missing {missing}"


def test_reference_tree_rejects_a_gp_in_two_blocks(tmp_path):
    """A GP in two blocks means the tree contradicts itself. Keeping the last
    one silently would assign real GPs to the wrong block."""

    path = tmp_path / "lgd.json"
    path.write_text(json.dumps({
        "state_code": "21",
        "zillas": [{"zp_name": "D", "zp_lgd_code": 1, "blocks": [
            {"bp_name": "B1", "bp_lgd_code": 11, "gps": [{"gp_name": "G", "gp_lgd_code": 100}]},
            {"bp_name": "B2", "bp_lgd_code": 12, "gps": [{"gp_name": "G", "gp_lgd_code": 100}]},
        ]}],
    }))
    with pytest.raises(GeographyError, match="more than one block"):
        gp_geography(path)


def test_missing_reference_tree_is_loud(tmp_path):
    with pytest.raises(GeographyError, match="not found"):
        gp_geography(tmp_path / "absent.json")


# --------------------------------------------------------------------- the join


def test_repeated_gp_name_gets_different_geography():
    """#61's central hazard: 505 GP names are shared by more than one GP.

    'Agalpur' is a real collision -- 115759 is in Balangir/Agalpur, 116395 is
    in Bargarh/Barpali. A name-keyed join hands one of them the other's
    district with no error anywhere, so this is the test that would catch it.
    """

    frames = [pd.DataFrame([
        _row(gp_code="115759", gram_panchayat_name="Agalpur", row_id="a"),
        _row(gp_code="116395", gram_panchayat_name="Agalpur", row_id="b"),
    ])]
    out = t.gram_panchayat(
        frames, t.Quarantine(), source_system="egramSwaraj", source_run_id="run-1",
        geography=gp_geography(),
    ).set_index("gp_lgd_code")

    assert out.loc["115759", "gp_name"] == out.loc["116395", "gp_name"] == "Agalpur"
    assert out.loc["115759", "zp_name"] == "Balangir"
    assert out.loc["116395", "zp_name"] == "Bargarh"
    assert out.loc["115759", "block_name"] == "Agalpur"
    assert out.loc["116395", "block_name"] == "Barpali"


def test_geography_is_populated_for_a_known_gp():
    out = t.gram_panchayat(
        [pd.DataFrame([_row(gp_code="115550", gram_panchayat_name="Angarbandha")])],
        t.Quarantine(), source_system="egramSwaraj", source_run_id="run-1",
        geography=gp_geography(),
    )
    row = out.iloc[0]
    assert row["state_code"] == "21"
    assert row["state_name"] == "Odisha"
    assert row["district_code"] == "303"
    assert row["zp_name"] == "Anugul"
    assert row["block_code"] == "3639"
    assert row["block_name"] == "Anugul"


def test_unknown_gp_code_yields_null_geography_not_an_error():
    """An unresolved code is unknown geography, not a broken join: the row
    still loads, and the columns are honestly null rather than guessed."""

    out = t.gram_panchayat(
        [pd.DataFrame([_row(gp_code="999999999")])],
        t.Quarantine(), source_system="egramSwaraj", source_run_id="run-1",
        geography=gp_geography(),
    )
    assert len(out) == 1
    assert out.iloc[0][list(GEOGRAPHY_COLUMNS)].isna().all()


def test_geography_adds_columns_not_rows():
    """#61: 'Row count of gram_panchayat is unchanged by the addition --
    geography is enrichment, not a filter.'"""

    frames = [pd.DataFrame([
        _row(gp_code="115550", row_id="a"),
        _row(gp_code="999999999", row_id="b"),  # not in the tree
    ])]
    kwargs = dict(source_system="egramSwaraj", source_run_id="run-1")
    without = t.gram_panchayat(frames, t.Quarantine(), **kwargs)
    with_geo = t.gram_panchayat(frames, t.Quarantine(), geography=gp_geography(), **kwargs)
    assert len(with_geo) == len(without) == 2
    assert list(with_geo["gp_lgd_code"]) == list(without["gp_lgd_code"])


def test_empty_input_still_declares_the_geography_columns():
    out = t.gram_panchayat([], t.Quarantine(), source_system="s", source_run_id="r")
    assert list(out.columns) == ["gp_lgd_code", "gp_name", *GEOGRAPHY_COLUMNS]


# --------------------------------------------------------------------- the build guard


def _gp_table(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(DDL["gram_panchayat"])
    con.executemany(
        "INSERT INTO gram_panchayat (gp_lgd_code, gp_name, state_code, state_name, "
        "district_code, zp_name, block_code, block_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return con


def test_build_check_fails_when_a_known_gp_has_blank_geography():
    con = _gp_table([("115550", "Angarbandha", "21", "Odisha", "303", None, "3639", "Anugul")])
    try:
        check, = validate.check_geography(con)
        assert not check.passed
        assert "1 of those are missing at least one" in check.detail
    finally:
        con.close()


def test_build_check_passes_when_a_known_gp_is_populated():
    con = _gp_table([("115550", "Angarbandha", "21", "Odisha", "303", "Anugul", "3639", "Anugul")])
    try:
        check, = validate.check_geography(con)
        assert check.passed
    finally:
        con.close()


def test_build_check_reports_but_tolerates_a_gp_outside_the_tree():
    """A synthetic fixture's 'LGD_123_Test_GP' resolves against nothing. That
    is not a loader failure, so it is reported and does not fail the build --
    full-state completeness is conformance's job, where the expected
    cardinality is known."""

    con = _gp_table([("123", "Test GP", None, None, None, None, None, None)])
    try:
        check, = validate.check_geography(con)
        assert check.passed
        assert "1 row(s) not in the tree" in check.detail
    finally:
        con.close()


# --------------------------------------------------------------------- review regressions


def _full_state_rows(**blanked):
    """All 6,794 real GPs, with the named columns forced to NULL."""

    return [
        (code, f"GP {code}", *[
            None if col in blanked else geo[col] for col in GEOGRAPHY_COLUMNS
        ])
        for code, geo in gp_geography().items()
    ]


@pytest.mark.parametrize("blanked", [
    ("district_code",), ("state_code",), ("block_code",),
    ("district_code", "state_code"),
])
def test_every_geography_column_is_guarded(blanked):
    """A guard that names three of the six columns lets the other three ship.

    An earlier revision checked only zp_name/block_name/state_name, so a
    table with district_code and state_code 100% NULL passed both guards --
    and the build check's own detail line reported "0 of those have no
    district". That is #61 surviving the check written to catch #61.
    """

    con = _gp_table(_full_state_rows(**{col: None for col in blanked}))
    try:
        check, = validate.check_geography(con)
        assert not check.passed, f"blanking {blanked} was not caught"
        findings = check_geography_completeness(con)
        assert "geography.populated" in {f.check for f in findings}
    finally:
        con.close()


def test_full_state_with_all_columns_populated_passes_both_guards():
    """The control for the test above: a guard that fails everything pins
    nothing."""

    con = _gp_table(_full_state_rows())
    try:
        check, = validate.check_geography(con)
        assert check.passed, check.detail
        assert check_geography_completeness(con) == []
    finally:
        con.close()


@pytest.mark.parametrize("tree", [
    [],                                                     # top-level array
    {"state_code": "21", "zillas": "nope"},                 # zillas not a list
    {"state_code": "21", "zillas": ["nope"]},               # zilla not an object
    {"state_code": "21", "zillas": [{"zp_name": "D", "zp_lgd_code": 1, "blocks": {}}]},
])
def test_malformed_tree_raises_geography_error(tmp_path, tree):
    """GeographyError's docstring promises to cover a malformed tree, so a
    bare .get() surfacing AttributeError breaks that promise."""

    path = tmp_path / "lgd.json"
    path.write_text(json.dumps(tree))
    with pytest.raises(GeographyError):
        gp_geography(path)


def test_tree_for_another_state_is_refused(tmp_path):
    """STATE_NAME is a constant, so a tree for another state would label every
    GP 'Odisha'. Refuse rather than mislabel."""

    path = tmp_path / "lgd.json"
    path.write_text(json.dumps({"state_code": "28", "zillas": []}))
    with pytest.raises(GeographyError, match="built for"):
        gp_geography(path)
