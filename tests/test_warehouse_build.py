"""End-to-end warehouse build tests, driven through ``warehouse.build.build``.

Per the session's caution about helpers whose call site is never exercised,
these tests go through the real publication sequence (preflight -> temp
DuckDB -> DDL -> load+transform -> quarantine -> validate -> publish) rather
than calling transform/load functions directly, for every behaviour listed
as required coverage.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from warehouse.build import BuildResult, build
from warehouse.conformance import check_conformance, check_satellite_row_parity, has_violations
from warehouse.transform import RequiredFieldUnresolved
from warehouse.validate import ValidationFailed

from _warehouse_helpers import approved, make_settings, normalize, publish_raw_run, registry, write_manual_snapshot


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
    settings = make_settings(tmp_path)
    result = build(snapshot_ids=(), settings=settings, registry=registry())
    assert result.target.exists()
    assert all(count == 0 for count in result.counts.values())


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
    """The scheme/fund array's own JSON key name is unverified (see
    transform.py's module docstring); the loader discovers it by prefix
    (``aa__*``) rather than assuming a specific key, so this fixture uses an
    arbitrary plausible key to prove that discovery path end to end."""

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
    unverified, see ``test_admin_approval_scheme_is_discovered_as_aa_child_table``)
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
