"""End-to-end warehouse build tests, driven through ``warehouse.build.build``.

Per the session's caution about helpers whose call site is never exercised,
these tests go through the real publication sequence (preflight -> temp
DuckDB -> DDL -> load+transform -> quarantine -> validate -> publish) rather
than calling transform/load functions directly, for every behaviour listed
as required coverage.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from src.pipeline.manifest import RunPublisher
from src.pipeline.normalize import NormalizationError, normalize_run
from warehouse.build import BuildResult, build, create_schema
from warehouse.conformance import check_conformance, check_satellite_row_parity, has_violations
from warehouse import transform as transform_module
from warehouse.transform import EmptyRequiredColumn, RequiredFieldUnresolved
from warehouse.validate import ValidationFailed

from _warehouse_helpers import approved, make_settings, normalize, publish_raw_run, registry, write_manual_snapshot


# The code dictionaries, which load on every build regardless of selection.
DIMENSION_TABLES = ("dim_code", "dim_lsdg_theme", "dim_welfare_scheme")


def _pl_aa_run(tmp_path: Path, run_id: str = "run-1", *, activity_code: int = 7):
    # One fund line, one asset line: activity_asset/activity_fund are
    # strictly 1:1 with planned_activity (see schema.py), so this fixture
    # only exercises the happy path. A second line for the same activity
    # (now a conflicting duplicate, quarantined) is exercised separately in
    # test_activity_fund_second_line_for_same_activity_is_quarantined below.
    run = publish_raw_run(tmp_path, run_id, {
        "LGD_123_Test_GP/2021_PL.json": {
            "data": [
                {
                    "activityCd": activity_code, "planCode": "P1", "activityName": "Well digging",
                    "totalCost": 15000.50, "activityStts": 1,
                    "fundList": [
                        {"schemeCode": "S1", "amountTotal": 5000.25},
                    ],
                    "assetDetails": [{"astTyp": "well", "astUnitCost": 15000.50}],
                },
            ],
        },
        "LGD_123_Test_GP/2021_AA.json": {
            "data": [{"activityCd": activity_code, "wrkAdmApprNo": "007", "wrkProposedCost": 15000.5}],
        },
    })
    return run


def _build_settings_and_registry(tmp_path: Path, run_id: str = "run-1"):
    settings = make_settings(tmp_path)
    run = _pl_aa_run(tmp_path, run_id)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", run_id))
    return settings, spec_registry


# --------------------------------------------------------------------- happy path


def test_full_build_publishes_and_reports_counts(tmp_path: Path):
    settings, spec_registry = _build_settings_and_registry(tmp_path)
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert isinstance(result, BuildResult)
    assert result.target.exists()
    assert result.counts["planned_activity"] == 1
    assert result.counts["activity_fund"] == 1
    assert result.counts["activity_asset"] == 1
    assert result.counts["admin_approval"] == 1
    assert result.quarantine_count == 0

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        # total_cost is a planning-side estimate: DOUBLE, not DECIMAL (see
        # schema.py's "MONEY TYPES" section). float64 exactly represents
        # 15000.50, so this equality is safe.
        row = con.execute("SELECT total_cost, typeof(total_cost) FROM planned_activity").fetchone()
        assert row == (15000.50, "DOUBLE")
        total = con.execute("SELECT sum(fund_amount_total) FROM activity_fund").fetchone()[0]
        assert total == pytest.approx(5000.25)
    finally:
        con.close()


def test_build_never_touches_real_default_db_path_unless_asked(tmp_path: Path):
    """Settings default to data/interim/panchayat.duckdb; every test here
    passes an explicit target instead, which this asserts stays true."""

    settings, spec_registry = _build_settings_and_registry(tmp_path)
    assert "data/interim/panchayat.duckdb" not in str(settings.db_path) or str(tmp_path) in str(settings.db_path)
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert str(tmp_path) in str(result.target)


# --------------------------------------------------------------------- PK/FK enforcement (through build)


def test_orphan_admin_approval_is_quarantined_not_loaded(tmp_path: Path):
    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_AA.json": {"data": [{"activityCd": 999, "wrkAdmApprNo": "1"}]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert result.counts["admin_approval"] == 0
    assert result.quarantine_count == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        reasons = con.execute("SELECT reason_code FROM quarantine").fetchall()
        assert ("orphan_reference",) in reasons
    finally:
        con.close()


def test_conflicting_duplicate_activity_is_quarantined_through_build(tmp_path: Path):
    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [
            {"activityCd": 7, "activityName": "Well", "totalCost": 100},
            {"activityCd": 7, "activityName": "Road", "totalCost": 999},
        ]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert result.counts["planned_activity"] == 1
    assert result.quarantine_count == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        duplicates = con.execute(
            "SELECT count(*) - count(DISTINCT activity_code) FROM planned_activity"
        ).fetchone()[0]
        assert duplicates == 0
    finally:
        con.close()


# --------------------------------------------------------------------- explicit valid-empty datasets


def test_kind_with_zero_records_is_explicit_empty_not_missing(tmp_path: Path):
    """The run only has PL/AA payload files; TA/PP/RE still get an explicit,
    schema-correct, zero-row canonical table (the normalizer's own contract),
    and the warehouse must load them as present-but-empty, not fail."""

    settings, spec_registry = _build_settings_and_registry(tmp_path)
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    for table in ("technical_approval", "physical_progress", "activity_expenditure"):
        assert result.counts[table] == 0
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        for table in ("technical_approval", "physical_progress", "activity_expenditure"):
            # The table exists and is queryable -- not simply absent.
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        con.close()


def test_empty_selection_still_publishes_a_valid_empty_warehouse(tmp_path: Path):
    """No snapshots means no *evidence* -- but the dictionaries still load.

    dim_code, dim_lsdg_theme and dim_welfare_scheme are conformed reference
    data, not something a scrape observed (see warehouse.dimensions), so they
    are the same rows whether five snapshots were selected or none. Asserting
    they are empty here would be asserting the opposite of what they are.
    """

    settings = make_settings(tmp_path)
    result = build(snapshot_ids=(), settings=settings, registry=registry())
    assert result.target.exists()
    derived = {
        table: count for table, count in result.counts.items()
        if table not in DIMENSION_TABLES
    }
    assert all(count == 0 for count in derived.values()), derived
    assert [result.counts.get(table) for table in DIMENSION_TABLES] == [717, 17, 12]


# --------------------------------------------------------------------- analytical grain


def test_activity_fund_second_line_for_same_activity_is_quarantined(tmp_path: Path):
    """activity_asset/activity_fund are strictly 1:1 with planned_activity
    (see schema.py); a second scheme line for one activity is a conflicting
    duplicate, quarantined through the real build path -- not a second row."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{
            "activityCd": 7, "totalCost": 100,
            "fundList": [
                {"schemeCode": "S1", "amountTotal": 100},
                {"schemeCode": "S2", "amountTotal": 200},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert result.counts["activity_fund"] == 1
    assert result.quarantine_count == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        reasons = con.execute(
            "SELECT reason_code FROM quarantine WHERE table_name = 'activity_fund'"
        ).fetchall()
        assert reasons == [("conflicting_duplicate_key",)]
    finally:
        con.close()


def test_childless_activity_gets_synthesized_satellite_rows_and_passes_conformance(tmp_path: Path):
    """A real gap between this PR's builder and the conformance rule it
    ships alongside: activity 8 below has no ``fundList``/``assetDetails``
    array at all (a perfectly valid activity -- e.g. a training-only line
    with no planned asset or funding split), while activity 7 has both.
    Before the fix, activity 8 would build successfully with zero
    activity_asset/activity_fund rows and then fail
    ``check_satellite_row_parity``, which requires exactly one row per
    planned_activity. Both tables must now carry a synthesized all-null
    row for activity 8, and the built warehouse must be conformant.
    """

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {
            "data": [
                {
                    "activityCd": 7, "planCode": "P1", "totalCost": 15000.50,
                    "fundList": [{"schemeCode": "S1", "amountTotal": 5000.25}],
                    "assetDetails": [{"astTyp": "well", "astUnitCost": 15000.50}],
                },
                {"activityCd": 8, "planCode": "P1", "totalCost": 200.0},
            ],
        },
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert result.counts["planned_activity"] == 2
    assert result.counts["activity_asset"] == 2
    assert result.counts["activity_fund"] == 2
    assert result.quarantine_count == 0

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        childless_asset = con.execute(
            "SELECT asset_type FROM activity_asset WHERE activity_code = '8'"
        ).fetchone()
        assert childless_asset == (None,)
        childless_fund = con.execute(
            "SELECT fund_scheme_code FROM activity_fund WHERE activity_code = '8'"
        ).fetchone()
        assert childless_fund == (None,)

        assert check_satellite_row_parity(con) == []
        # skip_geography: this fixture is one GP, not the state. It is a separate
        # flag from skip_reconciliation precisely so a real build cannot skip both
        # by accident.
        assert not has_violations(
            check_conformance(con, skip_reconciliation=True, skip_geography=True)
        )
    finally:
        con.close()


def test_activity_expenditure_identity_grain_through_build(tmp_path: Path):
    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_RE.json": {
            "data": [
                {"activityCd": 7, "planCode": "P1", "sNo": 1, "totalExpenditure": 500},
                {"activityCd": 7, "planCode": "P1", "sNo": 2, "totalExpenditure": 700},
            ],
        },
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert result.counts["activity_expenditure"] == 2
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        s_nos = sorted(r[0] for r in con.execute("SELECT s_no FROM activity_expenditure").fetchall())
        assert s_nos == ["1", "2"]
    finally:
        con.close()


def test_activity_expenditure_missing_required_alias_fails_build_and_does_not_publish(tmp_path: Path):
    """If the real RE payload uses a spelling for a required identity field
    (here, s_no) that isn't in RE_CANDIDATES, the build must fail loudly
    instead of silently publishing an activity_expenditure table with an
    all-null identity column."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_RE.json": {
            "data": [{"activityCd": 7, "planCode": "P1", "totalExpenditure": 500}],  # no sNo/s_no spelling
        },
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))

    target = tmp_path / "would-be-published" / "panchayat.duckdb"
    with pytest.raises(RequiredFieldUnresolved) as excinfo:
        build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry, target=target)
    assert "s_no" in str(excinfo.value)
    assert not target.exists()


def test_allocation_shaped_re_builds_and_is_reported_unconsumed(tmp_path: Path):
    """The scraped `re` kind is allocation data, and must not be forced
    into activity_expenditure.

    ``API_TYPES["RE"]`` is ``getLbAllocatedAmountData`` -- budgetary
    allocation, carrying planYear/planUnitCode and a scheme-allocation child
    list, with no expenditure field of any spelling. Feeding it to
    ``transform.activity_expenditure`` raised RequiredFieldUnresolved for a
    ``plan_code`` the source never had, which took down the whole build on
    real data. The table's real source is the separate expenditure extract
    (#49), so it stays empty -- and `re` must be reported as declared but
    unconsumed rather than counted as loaded.
    """

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_RE.json": {"data": [{
            "planYear": "2020-2021", "planUnitCode": 123,
            "budgetaryAllocationSchemeWebService": [
                {"schemeCode": 38, "alocationAmountGen": 2538218, "totalBudjAmount": 2538218},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))

    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert result.counts["activity_expenditure"] == 0
    unconsumed = result.unconsumed_tables["egramSwaraj/run-1"]
    assert "re" in unconsumed
    assert "re" not in result.consumed_tables["egramSwaraj/run-1"]


def test_build_result_records_re_field_resolutions(tmp_path: Path):
    """Every RE_CANDIDATES field's resolution outcome -- which candidate
    matched, or that none did -- must be surfaced on BuildResult, not just
    claimed in a docstring."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_RE.json": {
            "data": [{"activityCd": 7, "planCode": "P1", "sNo": 1, "totalExpenditure": 500}],
        },
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    re_resolutions = {r.field: r.matched_candidate for r in result.field_resolutions if r.table == "activity_expenditure"}
    assert re_resolutions["plan_code"] == "planCode"
    assert re_resolutions["s_no"] == "sNo"
    # scheme_name has no candidate in this payload -- optional, so it is
    # recorded as unresolved rather than raising.
    assert re_resolutions["scheme_name"] is None


# --------------------------------------------------------------------- scaling / chunk boundaries


def test_insert_batching_is_exact_at_chunk_boundaries(tmp_path: Path):
    """A table whose row count is not a multiple of the batch size, loaded
    through the real build path with a tiny batch_size, must still load
    every row exactly once -- no row dropped or duplicated at a boundary."""

    activities = [{"activityCd": i, "totalCost": i} for i in range(1, 8)]  # 7 rows
    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": activities},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry, batch_size=3)
    assert result.counts["planned_activity"] == 7
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM planned_activity").fetchone()[0] == 7
        assert con.execute("SELECT count(DISTINCT activity_code) FROM planned_activity").fetchone()[0] == 7
    finally:
        con.close()


# --------------------------------------------------------------------- cross-source provenance


def test_cross_source_activity_code_collision_fails_build_by_design(tmp_path: Path):
    """Primary keys are now the bare business key alone (activity_code,
    plan_code, row_id, ...), not (source_system, source_run_id, ...) --
    see schema.py's "single run per build" note. Two different source
    systems minting the same bare activity_code in one build therefore no
    longer coexist: the second INSERT trips the primary key and the whole
    build fails, by design, rather than silently keeping both rows or
    silently picking a winner."""

    settings = make_settings(tmp_path)
    run = _pl_aa_run(tmp_path, "run-1", activity_code=7)
    normalize(run, settings.canonical_root, chunk_size=100)

    write_manual_snapshot(
        settings.canonical_root, source="othersystem", run_id="run-9",
        tables={
            "pl": [{
                "row_id": "o1", "source_system": "othersystem", "source_run_id": "run-9",
                "business_id": "7",  # same bare code as the egramSwaraj activity, different system
                "gp_code": "123", "gram_panchayat_name": "Test GP",
                "fiscal_year": "2021-2022", "totalCost": 42.00, "activityName": "Other system activity",
            }],
            # Empty but present: this test is about cross-source PK collision,
            # not eGramSwaraj completeness.
            "aa": [], "ta": [], "pp": [], "re": [],
        },
    )

    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("snap-2", "othersystem", "run-9"),
    )
    with pytest.raises(duckdb.ConstraintException):
        build(snapshot_ids=("snap-1", "snap-2"), settings=settings, registry=spec_registry)


def test_gram_panchayat_dimension_conforms_across_sources_with_disjoint_codes(tmp_path: Path):
    """Two different source systems, same GP, but genuinely disjoint
    activity codes: this does not hit the single-run-per-build collision
    above, and the GP dimension still conforms to one row across both."""

    settings = make_settings(tmp_path)
    run = _pl_aa_run(tmp_path, "run-1", activity_code=7)
    normalize(run, settings.canonical_root, chunk_size=100)

    write_manual_snapshot(
        settings.canonical_root, source="othersystem", run_id="run-9",
        tables={
            "pl": [{
                "row_id": "o1", "source_system": "othersystem", "source_run_id": "run-9",
                "business_id": "8",  # disjoint from the egramSwaraj activity's "7"
                "gp_code": "123", "gram_panchayat_name": "Test GP",
                "fiscal_year": "2021-2022", "totalCost": 42.00, "activityName": "Other system activity",
            }],
            # Empty but present: this test is about cross-source GP
            # conformance, not eGramSwaraj completeness.
            "aa": [], "ta": [], "pp": [], "re": [],
        },
    )

    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("snap-2", "othersystem", "run-9"),
    )
    result = build(snapshot_ids=("snap-1", "snap-2"), settings=settings, registry=spec_registry)
    assert result.counts["planned_activity"] == 2  # disjoint codes: both kept
    assert result.counts["gram_panchayat"] == 1    # same GP, conformed once

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        rows = con.execute(
            "SELECT source_system, activity_code FROM planned_activity ORDER BY source_system"
        ).fetchall()
        assert rows == [("egramSwaraj", "7"), ("othersystem", "8")]
    finally:
        con.close()


# --------------------------------------------------------------------- failure handling


def test_failed_build_leaves_existing_database_untouched(tmp_path: Path, monkeypatch):
    settings, spec_registry = _build_settings_and_registry(tmp_path)
    first = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    original_bytes = first.target.read_bytes()

    import warehouse.build as build_module
    def boom(*args, **kwargs):
        raise RuntimeError("simulated mid-populate failure")
    monkeypatch.setattr(build_module, "insert", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert first.target.read_bytes() == original_bytes
    # No leftover staging directories beside the target.
    leftovers = [p for p in first.target.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_failed_validation_leaves_existing_database_untouched(tmp_path: Path, monkeypatch):
    settings, spec_registry = _build_settings_and_registry(tmp_path)
    first = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    original_bytes = first.target.read_bytes()

    import warehouse.build as build_module
    from warehouse.validate import Check
    def fail_checks(*args, **kwargs):
        return [Check("synthetic failing check", False, "forced failure for the rollback test")]
    monkeypatch.setattr(build_module, "run_checks", fail_checks)

    with pytest.raises(ValidationFailed):
        build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert first.target.read_bytes() == original_bytes
    leftovers = [p for p in first.target.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_manifest_hash_mismatch_stops_build_before_any_db_write(tmp_path: Path):
    settings, spec_registry = _build_settings_and_registry(tmp_path)
    snapshot_root = settings.snapshot_root("egramSwaraj", "run-1")
    part_files = list((snapshot_root / "pl").rglob("*.parquet"))
    with part_files[0].open("r+b") as handle:
        handle.write(b"\x00" * 8)

    with pytest.raises(Exception):
        build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert not settings.db_path.exists()


# --------------------------------------------------------------------- AA scheme child discovery


def test_admin_approval_scheme_is_discovered_as_aa_child_table(tmp_path: Path):
    """The loader discovers the scheme array by prefix, not by its key name.

    The key itself is no longer a guess: `admApprovalSchemeWebService` is the
    only child array key found in 27,672 AA arrays across 250 random GPs
    (#163), and this fixture uses it. Discovery stays signature-based anyway,
    which is what this test exercises end to end -- a survey says what the
    portal emits today, and a signature match degrades to finding nothing
    where a hardcoded key would degrade to loading an unrelated array."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_AA.json": {"data": [{
            "activityCd": 7, "wrkAdmApprNo": "007",
            "admApprovalSchemeWebService": [
                {"wrkSchmCd": "SC1", "wrkAdmApprFndSnctnGen": 100},
                {"wrkSchmCd": "SC2", "fndAllctnSchmTot": 250},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    assert result.counts["admin_approval_scheme"] == 2

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        rows = con.execute(
            "SELECT scheme_code, fund_sanctioned_general, fund_sanctioned_total "
            "FROM admin_approval_scheme ORDER BY scheme_code"
        ).fetchall()
        assert rows == [
            ("SC1", Decimal("100.00"), None),
            ("SC2", None, Decimal("250.00")),  # fndAllctnSchmTot alias (PR #30) resolves too
        ]
    finally:
        con.close()


def test_unrelated_aa_child_array_is_not_loaded_as_a_scheme(tmp_path: Path):
    """A direct AA child array with no scheme-shaped column (attachments,
    comments, ...) must not be swept into ``admin_approval_scheme`` just
    because it sits one level below ``aa``. Before the fix, the empty
    keyword used to discover the scheme table (its own JSON key is
    discovered by signature, see ``test_admin_approval_scheme_is_discovered_as_aa_child_table``)
    matched *any* direct AA child, so an attachments array would load as
    two all-null scheme rows and get marked consumed instead of reported
    unconsumed."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_AA.json": {"data": [{
            "activityCd": 7, "wrkAdmApprNo": "007",
            "admApprovalAttachments": [
                {"fileName": "doc1.pdf", "uploadedBy": "clerk1"},
                {"fileName": "doc2.pdf", "uploadedBy": "clerk2"},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert result.counts.get("admin_approval_scheme", 0) == 0
    assert "aa__admapprovalattachments" not in result.consumed_tables["egramSwaraj/run-1"]
    assert result.unconsumed_tables["egramSwaraj/run-1"] == ("aa__admapprovalattachments",)


def test_unrelated_pl_child_array_is_not_loaded_as_asset_or_fund(tmp_path: Path):
    """A direct PL child array whose key merely contains "asset" or "fund"
    but has none of the recognized fields must not be swept into
    activity_asset/activity_fund just by name substring."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{
            "activityCd": 7, "totalCost": 100,
            "assetAttachments": [
                {"fileName": "doc1.pdf", "uploadedBy": "clerk1"},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert result.counts.get("activity_asset", 0) == 1
    assert "pl__assetattachments" not in result.consumed_tables["egramSwaraj/run-1"]
    assert result.unconsumed_tables["egramSwaraj/run-1"] == ("pl__assetattachments",)


def test_scheme_and_unrelated_aa_children_are_both_handled_correctly(tmp_path: Path):
    """A scheme array and an unrelated array can be siblings under the same
    AA record; only the scheme one is loaded, the other is reported
    unconsumed."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_AA.json": {"data": [{
            "activityCd": 7, "wrkAdmApprNo": "007",
            "admApprovalSchemeWebService": [{"wrkSchmCd": "SC1", "wrkAdmApprFndSnctnGen": 100}],
            "admApprovalAttachments": [{"fileName": "doc1.pdf"}],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)

    assert result.counts["admin_approval_scheme"] == 1
    assert "aa__admapprovalschemewebservice" in result.consumed_tables["egramSwaraj/run-1"]
    assert result.unconsumed_tables["egramSwaraj/run-1"] == ("aa__admapprovalattachments",)


def test_unrecognized_child_table_is_tracked_not_silently_dropped(tmp_path: Path):
    """A nested array the warehouse has no handler for (e.g. a grandchild
    under fundList) must still be visible in the build result, never
    silently ignored."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{
            "activityCd": 7, "totalCost": 100,
            "fundList": [{"schemeCode": "S1", "lineItems": [{"code": "a"}, {"code": "b"}]}],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    result = build(snapshot_ids=("snap-1",), settings=settings, registry=spec_registry)
    unconsumed = result.unconsumed_tables["egramSwaraj/run-1"]
    assert "pl__fundlist__lineitems" in unconsumed


def test_geography_reaches_the_built_table(tmp_path: Path):
    """End-to-end (#61): a real LGD folder name arrives with its district and
    block, joined from the reference tree rather than from the folder name --
    which only ever carried the code and the name.

    Uses LGD_115550_Angarbandha, a real Odisha GP, because that is the whole
    point: the other fixtures here use LGD_123_Test_GP, which resolves against
    nothing and would prove nothing about the join.
    """

    settings = make_settings(tmp_path)
    run = publish_raw_run(tmp_path, "run-geo", {
        "LGD_115550_Angarbandha/2021_PL.json": {
            "data": [{"activityCd": 7, "planCode": "P1", "totalCost": 100}],
        },
    })
    normalize(run, settings.canonical_root, chunk_size=100)
    build(snapshot_ids=("snap-geo",), settings=settings,
          registry=registry(approved("snap-geo", "egramSwaraj", "run-geo")))

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        row = con.execute(
            "SELECT gp_lgd_code, gp_name, state_code, state_name, district_code, "
            "zp_name, block_code, block_name FROM gram_panchayat"
        ).fetchall()
    finally:
        con.close()
    assert row == [("115550", "Angarbandha", "21", "Odisha", "303", "Anugul", "3639", "Anugul")]


# --------------------------------------------------------------------- gp_profile (#123)

PROFILE_COLUMNS = (
    "basic_info_lgd,param__gp_name,"
    "demographic_details_total_gender_wise_population,"
    "demographic_details_male_population,demographic_details_female_population,"
    "demographic_details_transgender_population,demographic_details_children_population,"
    "demographic_details_sc_population,demographic_details_st_population,"
    "demographic_details_obc_population,demographic_details_general_population,"
    "general_no_of_households"
)


def _profile_snapshot(tmp_path: Path, settings, run_id: str, body: str, *,
                       columns: str = PROFILE_COLUMNS) -> None:
    """Publish and normalize a profile CSV run into the build's canonical root."""

    with RunPublisher(tmp_path / "raw", "egramswaraj_profile", run_id) as publisher:
        publisher.write_payload(
            "eGramSwaraj_panchayat_master.csv", f"{columns}\n{body}".encode(),
        )
        run = publisher.publish()
    normalize_run(run, settings.canonical_root)


# GP 123 is the one `_pl_aa_run` scrapes, so it resolves; 999 does not exist
# in gram_panchayat, and the third row carries no key at all -- the shape of
# the 84 profile-less rows in the real extract.
PROFILE_BODY = (
    "123,Test GP,900,450,440,10,200,100,150,300,350,210\n"
    "999,Ghost GP,10,5,5,0,1,1,1,1,7,3\n"
    ",Unfilled GP,,,,,,,,,,"
)


def test_gp_profile_loads_and_both_kinds_of_bad_row_are_quarantined(tmp_path: Path):
    """The three outcomes a profile row can have, in one build (#123).

    Loaded, orphaned, or unkeyed -- and the last two are told apart on
    purpose. An unkeyed row is a GP whose profile was never filled in (84 of
    them upstream, which collide on the empty string and break the primary
    key if let through); an orphan is a GP the scrape never saw. Both are
    countable in the quarantine table rather than filtered away where nothing
    would notice the source shrinking.
    """

    settings, spec_registry = _build_settings_and_registry(tmp_path)
    _profile_snapshot(tmp_path, settings, "profile-1", PROFILE_BODY)
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("profile-1", "egramswaraj_profile", "profile-1"),
    )

    result = build(
        snapshot_ids=("snap-1", "profile-1"), settings=settings, registry=spec_registry,
    )
    assert result.counts["gp_profile"] == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute(
            "SELECT gp_lgd_code, total_population, households FROM gp_profile"
        ).fetchall() == [("123", 900, 210)]
        # Zero orphans: every loaded code resolves against the dimension.
        assert con.execute(
            "SELECT count(*) FROM gp_profile p"
            " LEFT JOIN gram_panchayat g USING (gp_lgd_code) WHERE g.gp_lgd_code IS NULL"
        ).fetchone()[0] == 0
        reasons = dict(con.execute(
            "SELECT reason_code, sum(row_count) FROM quarantine"
            " WHERE table_name = 'gp_profile' GROUP BY reason_code"
        ).fetchall())
        assert reasons == {"missing_key": 1, "orphan_reference": 1}
    finally:
        con.close()


def test_gp_profile_resolves_its_key_whichever_order_the_snapshots_are_listed(tmp_path: Path):
    """The profile load must not depend on which snapshot is processed first.

    gp_profile has a FOREIGN KEY to gram_panchayat, which every scrape
    snapshot contributes to. Loading it inside the per-snapshot loop would
    mean that listing the profile snapshot first quarantined every row as an
    orphan -- and the build would still finish green, because quarantining is
    not a failure. That is #161's defect, and this pins that gp_profile does
    not reintroduce it.
    """

    counts = {}
    for order in (("snap-1", "profile-1"), ("profile-1", "snap-1")):
        root = tmp_path / "-".join(order)
        root.mkdir()
        settings, _ = _build_settings_and_registry(root)
        _profile_snapshot(root, settings, "profile-1", PROFILE_BODY)
        result = build(
            snapshot_ids=order, settings=settings,
            registry=registry(
                approved("snap-1", "egramSwaraj", "run-1"),
                approved("profile-1", "egramswaraj_profile", "profile-1"),
            ),
        )
        counts[order] = result.counts["gp_profile"]
    assert counts[("snap-1", "profile-1")] == counts[("profile-1", "snap-1")] == 1


def test_unfiltered_profile_rows_are_rejected_by_the_schema(tmp_path: Path):
    """The trap the filtering exists to avoid, proven rather than asserted.

    #123 recorded this as a primary-key collision: 84 blank-key rows all
    become '' and 83 of them duplicate the first. With the FOREIGN KEY to
    gram_panchayat in place the schema is stricter than that -- the *first*
    blank row is already rejected, because '' is not a GP -- so all 84 fail,
    not 83. Both refusals are asserted below, in that order, because relaxing
    either one on its own still leaves the other standing and the comment
    alone would not say so.
    """

    con = duckdb.connect(str(tmp_path / "trap.duckdb"))
    try:
        create_schema(con)
        con.execute("INSERT INTO gram_panchayat (gp_lgd_code, gp_name) VALUES ('123', 'Test GP')")
        with pytest.raises(duckdb.ConstraintException, match="foreign key"):
            con.execute("INSERT INTO gp_profile (gp_lgd_code) VALUES ('')")

        # And behind the FK, the collision #123 described.
        con.execute("INSERT INTO gram_panchayat (gp_lgd_code, gp_name) VALUES ('', NULL)")
        con.execute("INSERT INTO gp_profile (gp_lgd_code) VALUES ('')")
        with pytest.raises(duckdb.ConstraintException, match="[Pp]rimary key|unique"):
            con.execute("INSERT INTO gp_profile (gp_lgd_code) VALUES ('')")
    finally:
        con.close()


def test_gp_profile_fails_the_build_when_a_population_column_is_renamed(tmp_path: Path):
    """An all-NULL demographics table would pass every other check.

    Right row count, valid primary key, zero orphans -- and every population
    null. `_first_present(..., required=True)` is what turns an upstream
    rename into a stopped build instead of a silently empty one.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _profile_snapshot(
        tmp_path, settings, "profile-1", "123,Test GP,900,210",
        columns="basic_info_lgd,param__gp_name,"
                "demographic_details_total_population,general_no_of_households",
    )
    with pytest.raises(RequiredFieldUnresolved, match="total_population"):
        build(
            snapshot_ids=("snap-1", "profile-1"), settings=settings,
            registry=registry(
                approved("snap-1", "egramSwaraj", "run-1"),
                approved("profile-1", "egramswaraj_profile", "profile-1"),
            ),
        )


def test_gp_profile_fails_the_build_when_a_column_survives_but_its_values_do_not(tmp_path: Path):
    """`required=True` proves a column NAME survived, not that values did.

    The header can stay while the scrape starts emitting blanks, and `to_int`
    turns every one of them into NULL without complaint. The columns are
    nullable and no conformance rule reads their contents, so the build would
    publish the right number of GPs with entirely empty demographics -- the
    exact silent failure the required-field logic is there to prevent, reached
    by the route it does not cover.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _profile_snapshot(tmp_path, settings, "profile-1", "123,Test GP,,,,,,,,,,")
    with pytest.raises(EmptyRequiredColumn, match="total_population"):
        build(
            snapshot_ids=("snap-1", "profile-1"), settings=settings,
            registry=registry(
                approved("snap-1", "egramSwaraj", "run-1"),
                approved("profile-1", "egramswaraj_profile", "profile-1"),
            ),
        )


def test_gp_profile_quarantines_a_row_whose_measure_cannot_be_read(tmp_path: Path):
    """A present-but-unreadable value is a parse failure, not an absent measure.

    `to_int` coerces both to NA and cannot tell them apart, so the raw text is
    checked while it is still in reach. The row is quarantined rather than
    loaded with a hole: nine good measures do not make a tenth trustworthy,
    and a silently-NULL cell is what this whole path exists to prevent.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _profile_snapshot(
        tmp_path, settings, "profile-1",
        "123,Test GP,900,450,440,10,200,100,150,300,350,not-a-number\n"
        "456,Other GP,800,400,390,10,180,90,140,280,300,190",
    )
    # 456 is not in gram_panchayat, so it is an orphan; 123 is the readable one.
    result = build(
        snapshot_ids=("snap-1", "profile-1"), settings=settings,
        registry=registry(
            approved("snap-1", "egramSwaraj", "run-1"),
            approved("profile-1", "egramswaraj_profile", "profile-1"),
        ),
    )
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM gp_profile").fetchone()[0] == 0
        reasons = dict(con.execute(
            "SELECT reason_code, sum(row_count) FROM quarantine "
            "WHERE table_name = 'gp_profile' GROUP BY reason_code"
        ).fetchall())
        assert reasons == {"unreadable_measure": 1, "orphan_reference": 1}
    finally:
        con.close()


def test_gp_profile_quarantines_a_fractional_count_rather_than_rounding_it(tmp_path: Path):
    """`to_int` rounds, so a null-check alone accepts an invented number.

    `clean.to_int` ends in `numeric.round()`. A population of "1.5" therefore
    parses cleanly to 2 and the unreadable-value check -- which asks whether
    the cast produced NULL -- never sees it. A rounded count is not a
    recovered count; it is a number nobody observed. Same predicate as
    `activity_nsap`'s beneficiary counts (#116), which is why it now lives at
    module scope rather than nested in one transform.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _profile_snapshot(
        tmp_path, settings, "profile-1",
        "123,Test GP,900,450,440,10,200,100,150,300,350,1.5",
    )
    result = build(
        snapshot_ids=("snap-1", "profile-1"), settings=settings,
        registry=registry(
            approved("snap-1", "egramSwaraj", "run-1"),
            approved("profile-1", "egramswaraj_profile", "profile-1"),
        ),
    )
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM gp_profile").fetchone()[0] == 0
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'gp_profile' "
            "AND reason_code = 'unreadable_measure'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_gp_profile_quarantines_a_negative_count(tmp_path: Path):
    """`to_int` carries a negative through untouched.

    It is not null, not fractional and not non-finite, so every other
    predicate in the unreadable check accepts it -- and nobody ever counted
    -1 people.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _profile_snapshot(
        tmp_path, settings, "profile-1",
        "123,Test GP,900,-1,440,10,200,100,150,300,350,210",
    )
    result = build(
        snapshot_ids=("snap-1", "profile-1"), settings=settings,
        registry=registry(
            approved("snap-1", "egramSwaraj", "run-1"),
            approved("profile-1", "egramswaraj_profile", "profile-1"),
        ),
    )
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM gp_profile").fetchone()[0] == 0
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'gp_profile' "
            "AND reason_code = 'unreadable_measure'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_admin_approval_scheme_activity_code_is_cleaned_like_every_other_one(tmp_path: Path):
    """The one activity_code that was assigned raw rather than through to_code.

    Eight other `activity_code` columns in `transform` go through
    `clean.to_code`; this one took the provenance value straight through. The
    same activity could therefore be spelled one way here and another in
    `planned_activity` -- in a column whose only purpose is to join them.

    Inert on today's data: every sampled `activityCd` is a JSON int, so
    `str()` and `to_code()` agree. Asserted against `planned_activity` rather
    than against a literal, because the property that matters is that the two
    agree, not what either one says.
    """

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
        "LGD_123_Test_GP/2021_AA.json": {"data": [{
            "activityCd": 7, "wrkAdmApprNo": "007",
            "admApprovalSchemeWebService": [
                {"wrkSchmCd": "SC1", "wrkSchmCmpntCd": "C1", "wrkAdmApprFndSnctnGen": 100},
            ],
        }]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    result = build(
        snapshot_ids=("snap-1",), settings=settings,
        registry=registry(approved("snap-1", "egramSwaraj", "run-1")),
    )
    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute(
            "SELECT s.activity_code FROM admin_approval_scheme s"
            " JOIN planned_activity a ON a.activity_code = s.activity_code"
        ).fetchall() == [("7",)], "scheme activity_code must join planned_activity"
    finally:
        con.close()


# --------------------------------------------------------------------- voucher (#46, #129)

def _accounting_payload(gp_code: str, year: str, receipts: list[dict], payments: list[dict]) -> bytes:
    return json.dumps({
        "gp_name": "Test GP", "gp_lgd_code": gp_code, "state": "21",
        "district_name": "Deogarh", "district_code": "310",
        "block_name": "Barkote", "block_code": "3709",
        "year": year, "status": "ok",
        "receipt_count": len(receipts), "payment_count": len(payments),
        "total_receipts": 0.0, "total_payments": 0.0,
        "receipts": receipts, "payments": payments, "opening_balance": 0.0,
    }).encode()


def _voucher(no: str, amount, *, vid: str = "1", vtype: str = "Expenditures",
             date: str = "03/04/2022", month: str = "April") -> dict:
    return {"month": month, "date": date, "voucher_no": no,
            "type": vtype, "amount": amount, "voucher_id": vid}


def _accounting_snapshot(tmp_path: Path, settings, run_id: str,
                         payloads: dict[str, bytes]) -> None:
    """Publish and normalize a nested accounting run into the build's canonical root."""

    with RunPublisher(tmp_path / "raw", "egramswaraj_accounting", run_id) as publisher:
        for name, body in payloads.items():
            publisher.write_payload(name, body)
        run = publisher.publish()
    normalize_run(run, settings.canonical_root)


def test_voucher_loads_receipts_and_payments_with_their_direction(tmp_path: Path):
    """Both arrays reach one table, each tagged with what it means (#129).

    The shape that makes this worth asserting: a receipt and a payment can
    carry the *same* voucher number within a GP and year only if they differ
    elsewhere, and the ledger is meaningless if the two sides are not told
    apart. Also pins that money survives as an exact Decimal rather than a
    float -- #46's acceptance is a total matching to the paisa.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _accounting_snapshot(tmp_path, settings, "acct-1", {
        "Deogarh/Barkote/Test/2022-2023.json": _accounting_payload(
            "123", "2022-2023",
            receipts=[_voucher("XVFC/2022-23/R/1", 304690.55, vtype="Direct Receipts")],
            payments=[_voucher("XVFC/2022-23/P/1", 44280.45)],
        ),
    })
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("acct-1", "egramswaraj_accounting", "acct-1"),
    )
    result = build(snapshot_ids=("snap-1", "acct-1"), settings=settings, registry=spec_registry)
    assert result.counts["voucher"] == 2

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute(
            "SELECT direction, voucher_no, amount FROM voucher ORDER BY voucher_no"
        ).fetchall() == [
            ("payment", "XVFC/2022-23/P/1", Decimal("44280.45")),
            ("receipt", "XVFC/2022-23/R/1", Decimal("304690.55")),
        ]
        # Exact to the paisa: the pair sums without binary-float drift.
        assert con.execute("SELECT sum(amount) FROM voucher").fetchone()[0] == Decimal("348971.00")
        # dayfirst: 03/04/2022 is 3 April, not 4 March.
        assert con.execute("SELECT DISTINCT date FROM voucher").fetchall() == [
            (datetime(2022, 4, 3),),
        ]
    finally:
        con.close()


def test_voucher_id_repeats_across_gps_but_voucher_pk_does_not(tmp_path: Path):
    """#46's named acceptance: voucher_id is not the key and never becomes one.

    It repeats across GP, year and direction in the real extract -- a
    portal-internal sequence unique only within a GP. Keying on it would
    merge unrelated vouchers from different panchayats into one row.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _accounting_snapshot(tmp_path, settings, "acct-1", {
        # Same voucher_id "7", and the same voucher_no, in two different GPs
        # and two different years. Four rows, four distinct voucher_pk.
        "a/b/One/2022-2023.json": _accounting_payload(
            "123", "2022-2023", receipts=[],
            payments=[_voucher("V/1", 100.0, vid="7"), _voucher("V/2", 200.0, vid="7")],
        ),
        "a/b/Two/2023-2024.json": _accounting_payload(
            "456", "2023-2024", receipts=[],
            payments=[_voucher("V/1", 300.0, vid="7"), _voucher("V/2", 400.0, vid="7")],
        ),
    })
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("acct-1", "egramswaraj_accounting", "acct-1"),
    )
    result = build(snapshot_ids=("snap-1", "acct-1"), settings=settings, registry=spec_registry)

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        # GP 456 is not in gram_panchayat, so its two rows are orphans; the
        # point stands on the pair that loads plus the pair that is counted.
        assert con.execute("SELECT count(DISTINCT voucher_id) FROM voucher").fetchone()[0] == 1
        total, distinct = con.execute(
            "SELECT count(*), count(DISTINCT voucher_pk) FROM voucher"
        ).fetchone()
        assert total == distinct, "voucher_pk must be unique even when voucher_id is not"
        assert con.execute(
            "SELECT count(*) FROM quarantine WHERE table_name = 'voucher' AND reason_code = 'orphan_gp'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_voucher_quarantines_a_duplicate_natural_key_rather_than_failing_the_insert(tmp_path: Path):
    """The UNIQUE constraint is real, so a collision must not reach it.

    Two rows sharing (gp_lgd_code, fiscal_year, voucher_no) would abort the
    whole insert and take every good voucher with them. Quarantined instead,
    where the count stays visible.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _accounting_snapshot(tmp_path, settings, "acct-1", {
        "a/b/One/2022-2023.json": _accounting_payload(
            "123", "2022-2023", receipts=[],
            payments=[_voucher("V/1", 100.0, vid="1"), _voucher("V/1", 999.0, vid="2")],
        ),
    })
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("acct-1", "egramswaraj_accounting", "acct-1"),
    )
    result = build(snapshot_ids=("snap-1", "acct-1"), settings=settings, registry=spec_registry)

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM voucher").fetchone()[0] == 1
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine "
            "WHERE table_name = 'voucher' AND reason_code = 'conflicting_duplicate_key'"
        ).fetchone()[0] == 1
    finally:
        con.close()


# --------------------------------------------------------------------- expenditure (#49)

EXPENDITURE_COLUMNS = (
    "planYear,stateName,zpName,blockName,gpName,gpCode,planType,approvalDate,planCode,"
    "planCodeStatus,S.No.,Activity Code,Activity Name,Activity For,Focus Area,"
    "Approved Cost in Action Plan,Technical Approved Cost,Admin Approved Cost,Scheme Name,"
    "General,SC,ST,Total Expenditure,Voucher Date,Voucher No,Voucher Cost"
)


def _expenditure_row(*, s_no: str, activity: str, total: str,
                     v_no: str, v_cost: str, v_date: str, gp: str = "123") -> str:
    return (
        f"2022-2023,Odisha,Koraput,Dasamantapur,Test GP,{gp},131,,4810158,123,{s_no},"
        f"{activity},Some work,GEN,Education,200000.00,200000,200000.00,XV Finance Commission,"
        f"200000,,,{total},{v_date},{v_no},{v_cost}"
    )


def _expenditure_snapshot(tmp_path: Path, settings, run_id: str, body: str) -> None:
    with RunPublisher(tmp_path / "raw", "egramswaraj_expenditure", run_id) as publisher:
        publisher.write_payload(
            "expenditure_all.csv", f"﻿{EXPENDITURE_COLUMNS}\n{body}".encode("utf-8"),
        )
        run = publisher.publish()
    normalize_run(run, settings.canonical_root)


def test_expenditure_explodes_parallel_voucher_cells_and_links_both_ways(tmp_path: Path):
    """#49's core shape: three pipe cells recombined positionally.

    Also pins the two joins that make the bridge worth having -- every row
    reaches an activity_expenditure row, and reaches a voucher row where the
    accounting extract actually covers it.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _accounting_snapshot(tmp_path, settings, "acct-1", {
        "a/b/One/2022-2023.json": _accounting_payload(
            "123", "2022-2023", receipts=[],
            payments=[_voucher("XVFC/2022-23/P/7", 1028.0)],
        ),
    })
    _expenditure_snapshot(tmp_path, settings, "exp-1", "\n".join([
        # Three references in one row; the same voucher_no repeats, which is
        # legitimate -- three payments settled against one voucher.
        _expenditure_row(
            s_no="1", activity="44134242", total="119143.0",
            v_no="XVFC/2022-23/P/7 | XVFC/2022-23/P/7 | XVFC/2099-00/P/9",
            v_cost="1028.0 | 1143.0 | 116972.0",
            v_date="05/07/2023 | 05/07/2023 | 05/07/2023",
        ),
    ]))
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("acct-1", "egramswaraj_accounting", "acct-1"),
        approved("exp-1", "egramswaraj_expenditure", "exp-1"),
    )
    result = build(
        snapshot_ids=("snap-1", "acct-1", "exp-1"), settings=settings, registry=spec_registry,
    )
    assert result.counts["activity_expenditure"] == 1
    assert result.counts["activity_voucher"] == 3, "one row per voucher reference"

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        rows = con.execute(
            "SELECT voucher_no, voucher_cost, fiscal_year, voucher_date, voucher_pk IS NOT NULL "
            "FROM activity_voucher ORDER BY voucher_cost"
        ).fetchall()
        assert rows == [
            ("XVFC/2022-23/P/7", Decimal("1028.00"), "2022-2023", datetime(2023, 7, 5), True),
            ("XVFC/2022-23/P/7", Decimal("1143.00"), "2022-2023", datetime(2023, 7, 5), True),
            # Cites a voucher the accounting extract does not reach: kept, and
            # left unmatched by design (#49, and much commoner under #171).
            ("XVFC/2099-00/P/9", Decimal("116972.00"), "2099-2100", datetime(2023, 7, 5), False),
        ]
        # The bridge resolves to a real expenditure row in every case.
        assert con.execute(
            "SELECT count(*) FROM activity_voucher av "
            "WHERE NOT EXISTS (SELECT 1 FROM activity_expenditure e "
            "                  WHERE e.expenditure_id = av.expenditure_id)"
        ).fetchone()[0] == 0
        # The costs recombine to the row's own stated total.
        assert con.execute("SELECT sum(voucher_cost) FROM activity_voucher").fetchone()[0] == Decimal("119143.00")
        assert con.execute("SELECT total_expenditure FROM activity_expenditure").fetchone()[0] == Decimal("119143.00")
    finally:
        con.close()


def test_expenditure_refuses_misaligned_voucher_cells_rather_than_truncating(tmp_path: Path):
    """#49 names this: `zip` would silently drop the tail and lose real money."""

    settings, _ = _build_settings_and_registry(tmp_path)
    _expenditure_snapshot(tmp_path, settings, "exp-1", "\n".join([
        _expenditure_row(
            s_no="1", activity="44134242", total="119143.0",
            v_no="XVFC/2022-23/P/7 | XVFC/2022-23/P/8 | XVFC/2022-23/P/9",
            v_cost="1028.0 | 1143.0",          # one short
            v_date="05/07/2023 | 05/07/2023 | 05/07/2023",
        ),
    ]))
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("exp-1", "egramswaraj_expenditure", "exp-1"),
    )
    with pytest.raises(transform_module.MisalignedVoucherCells, match="differing lengths"):
        build(snapshot_ids=("snap-1", "exp-1"), settings=settings, registry=spec_registry)


def test_expenditure_lane_refuses_a_repeated_natural_key(tmp_path: Path):
    """Streaming is only sound while the key stays unique; a repeat must stop it.

    Measured unique across all 4,075,935 rows of the real extract. If that
    stops being true the lane cannot assign row_ids one row at a time, so it
    fails loudly instead of publishing two rows with one identity.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    duplicate = _expenditure_row(
        s_no="1", activity="44134242", total="1.0",
        v_no="XVFC/2022-23/P/1", v_cost="1.0", v_date="05/07/2023",
    )
    with pytest.raises(NormalizationError, match="repeats the key"):
        _expenditure_snapshot(tmp_path, settings, "exp-1", "\n".join([duplicate, duplicate]))


def test_expenditure_names_the_right_vouchers_when_their_expenditure_row_is_dropped(tmp_path: Path):
    """The orphan-bridge path, which nothing exercised until it was wrong.

    An expenditure line for a GP outside `gram_panchayat` is quarantined by
    `activity_expenditure`, so its voucher references have no `expenditure_id`
    to point at and cannot be loaded either. Quarantine has to name *those*
    vouchers -- selecting them by index membership after a merge picked
    unrelated rows, because `merge` returns a fresh RangeIndex while `explode`
    leaves repeated labels.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    # Order and arity are both load-bearing. The dropped row comes FIRST and
    # carries SEVERAL vouchers, so `explode` emits repeated index labels
    # (0,0) before the kept row's (1) while `merge` renumbers to 0,1,2. Put
    # the kept row first, or give each row one voucher, and the broken
    # index-membership selection happens to pick the right rows anyway --
    # a test that passes either way pins nothing.
    _expenditure_snapshot(tmp_path, settings, "exp-1", "\n".join([
        _expenditure_row(
            s_no="2", activity="44134243", total="20.0",
            v_no="ORPHAN/2022-23/P/2 | ORPHAN/2022-23/P/3",
            v_cost="20.0 | 30.0", v_date="05/07/2023 | 05/07/2023", gp="999",
        ),
        _expenditure_row(
            s_no="1", activity="44134242", total="10.0",
            v_no="KEPT/2022-23/P/1", v_cost="10.0", v_date="05/07/2023", gp="123",
        ),
    ]))
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("exp-1", "egramswaraj_expenditure", "exp-1"),
    )
    result = build(snapshot_ids=("snap-1", "exp-1"), settings=settings, registry=spec_registry)
    assert result.counts["activity_expenditure"] == 1
    assert result.counts["activity_voucher"] == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT voucher_no FROM activity_voucher").fetchall() == [
            ("KEPT/2022-23/P/1",),
        ]
        # The quarantined key must be the orphan's voucher, not the kept one.
        assert sorted(con.execute(
            "SELECT key_value FROM quarantine "
            "WHERE table_name = 'activity_voucher' AND reason_code = 'orphan_expenditure'"
        ).fetchall()) == [("ORPHAN/2022-23/P/2",), ("ORPHAN/2022-23/P/3",)]
    finally:
        con.close()


def test_a_second_accounting_snapshot_does_not_collide_on_the_unique_key(tmp_path: Path):
    """Two snapshots may legitimately overlap; the build must survive it (Codex, #173).

    `_dedupe` only sees one frame. Two accounting snapshots can each carry the
    same (gp, year, voucher_no) -- exactly what #171's remedy produces, since
    re-scraping the 358 missing GPs yields a second run alongside the first --
    and each survives its own dedupe, so the second insert hits voucher's
    UNIQUE constraint and aborts the whole build.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    shared = _voucher("XVFC/2022-23/P/1", 100.0)
    for run in ("acct-1", "acct-2"):
        _accounting_snapshot(tmp_path, settings, run, {
            "a/b/One/2022-2023.json": _accounting_payload(
                "123", "2022-2023", receipts=[],
                # The overlap, plus one row unique to each run so neither
                # snapshot is wholly redundant.
                payments=[shared, _voucher(f"XVFC/2022-23/P/{run[-1]}0", 5.0)],
            ),
        })
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("acct-1", "egramswaraj_accounting", "acct-1"),
        approved("acct-2", "egramswaraj_accounting", "acct-2"),
    )
    result = build(
        snapshot_ids=("snap-1", "acct-1", "acct-2"), settings=settings, registry=spec_registry,
    )
    # Three distinct vouchers across two snapshots: the shared one once, plus
    # one unique to each run.
    assert result.counts["voucher"] == 3

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        total, distinct = con.execute(
            "SELECT count(*), count(DISTINCT (gp_lgd_code, fiscal_year, voucher_no)) FROM voucher"
        ).fetchone()
        assert total == distinct == 3
        # voucher_pk stays dense: the collision is dropped before ids are
        # assigned, so activity_voucher's foreign key has no gaps.
        assert con.execute(
            "SELECT min(voucher_pk), max(voucher_pk), count(DISTINCT voucher_pk) FROM voucher"
        ).fetchone() == (1, 3, 3)
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'voucher' "
            "AND reason_code = 'cross_snapshot_duplicate_key'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_a_voucher_position_with_a_cost_but_no_number_is_quarantined_not_dropped(tmp_path: Path):
    """Equal cell counts can still hide a missing voucher number (Codex, #174).

    `V1||V3` against three costs passes the length check, and filtering on a
    non-empty voucher number alone then discards the middle payment silently
    -- making sum(voucher_cost) fall below the row's own total_expenditure,
    which is the arithmetic anyone would use to check this table.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    _expenditure_snapshot(tmp_path, settings, "exp-1", "\n".join([
        _expenditure_row(
            s_no="1", activity="44134242", total="600.0",
            v_no="XVFC/2022-23/P/1 |  | XVFC/2022-23/P/3",
            v_cost="100.0 | 200.0 | 300.0",
            v_date="05/07/2023 | 05/07/2023 | 05/07/2023",
        ),
    ]))
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("exp-1", "egramswaraj_expenditure", "exp-1"),
    )
    result = build(snapshot_ids=("snap-1", "exp-1"), settings=settings, registry=spec_registry)
    assert result.counts["activity_voucher"] == 2

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        # The 200.00 payment is absent from the bridge -- and *counted*, so the
        # shortfall against total_expenditure is explained rather than silent.
        assert con.execute("SELECT sum(voucher_cost) FROM activity_voucher").fetchone()[0] == Decimal("400.00")
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'activity_voucher' "
            "AND reason_code = 'partial_voucher_slot'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_a_second_profile_snapshot_does_not_collide_on_the_primary_key(tmp_path: Path):
    """gp_profile had voucher's cross-snapshot defect too, since #123.

    Found by reviewing the sibling rather than by report: `_dedupe` sees one
    frame, so two profile snapshots both carrying a GP each survive their own
    dedupe and the second insert hits gp_lgd_code's PRIMARY KEY.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    for run in ("profile-1", "profile-2"):
        _profile_snapshot(tmp_path, settings, run, PROFILE_BODY)
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("profile-1", "egramswaraj_profile", "profile-1"),
        approved("profile-2", "egramswaraj_profile", "profile-2"),
    )
    result = build(
        snapshot_ids=("snap-1", "profile-1", "profile-2"),
        settings=settings, registry=spec_registry,
    )
    assert result.counts["gp_profile"] == 1

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM gp_profile").fetchone()[0] == 1
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'gp_profile' "
            "AND reason_code = 'cross_snapshot_duplicate_key'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_a_second_expenditure_snapshot_does_not_double_the_money(tmp_path: Path):
    """The silent one of the three cross-snapshot defects (Codex, #174).

    voucher and gp_profile collide on a UNIQUE/PRIMARY KEY, so an overlapping
    second snapshot aborts the build. activity_expenditure's only key is the
    `expenditure_id` surrogate, so nothing stops the duplicates:
    total_expenditure doubles and the bridge duplicates with it, on a green
    build and a green conformance run.

    `_dedupe` cannot catch it even in principle -- its key includes
    source_run_id, so one business row seen by two runs is two rows to it.
    """

    settings, _ = _build_settings_and_registry(tmp_path)
    row = _expenditure_row(
        s_no="1", activity="44134242", total="500.0",
        v_no="XVFC/2022-23/P/1", v_cost="500.0", v_date="05/07/2023",
    )
    for run in ("exp-1", "exp-2"):
        _expenditure_snapshot(tmp_path, settings, run, row)
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("exp-1", "egramswaraj_expenditure", "exp-1"),
        approved("exp-2", "egramswaraj_expenditure", "exp-2"),
    )
    result = build(
        snapshot_ids=("snap-1", "exp-1", "exp-2"), settings=settings, registry=spec_registry,
    )
    assert result.counts["activity_expenditure"] == 1, "the overlap must not load twice"

    con = duckdb.connect(str(result.target), read_only=True)
    try:
        # The number the whole reconciliation rests on: it must not double.
        assert con.execute(
            "SELECT sum(total_expenditure) FROM activity_expenditure"
        ).fetchone()[0] == Decimal("500.00")
        # And the bridge must not duplicate along with it.
        assert con.execute("SELECT count(*) FROM activity_voucher").fetchone()[0] == 1
        assert con.execute("SELECT sum(voucher_cost) FROM activity_voucher").fetchone()[0] == Decimal("500.00")
        assert con.execute(
            "SELECT sum(row_count) FROM quarantine WHERE table_name = 'activity_expenditure' "
            "AND reason_code = 'cross_snapshot_duplicate_key'"
        ).fetchone()[0] == 1
    finally:
        con.close()
