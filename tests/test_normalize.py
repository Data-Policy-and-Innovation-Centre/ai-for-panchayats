from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.pipeline.manifest import RunPublisher
from src.pipeline.normalize import (
    NormalizationError,
    _normalise_year,
    normalize_egramswaraj,
    to_records,
    validate_canonical_manifest,
)


def make_run(tmp_path: Path, run_id: str, payloads: dict[str, object | bytes]) -> Path:
    with RunPublisher(tmp_path / "raw", "egramSwaraj", run_id) as publisher:
        for name, payload in payloads.items():
            value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            publisher.write_payload(name, value)
        return publisher.publish()


def rows(path: Path) -> list[dict]:
    return pq.read_table(path).to_pylist()


def files(result, table: str) -> list[Path]:
    return list(result.tables[table])


def test_recursive_children_sibling_arrays_and_provenance(tmp_path: Path):
    run = make_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {
            "status": "OK",
            "data": [
                {
                    "activityCd": 7,
                    "fundList": [
                        {"amount": 1, "lineItems": [{"code": "a"}, {"code": "b"}]},
                        {"amount": 2, "lineItems": []},
                    ],
                    "assetDetails": [{"asset": "well"}],
                },
                {"activityCd": 8, "fundList": [{"amount": 3}]},
            ],
        }
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=100)

    parent = rows(files(result, "pl")[0])
    fund = rows(files(result, "pl__fundlist")[0])
    nested = rows(files(result, "pl__fundlist__lineitems")[0])
    sibling = rows(files(result, "pl__assetdetails")[0])
    assert [row["business_id"] for row in parent] == ["7", "8"]
    assert len(fund) == 3 and len(nested) == 2 and len(sibling) == 1
    assert [row["pos"] for row in nested] == [0, 1]
    assert nested[0]["parent_row_id"] == fund[0]["row_id"]
    assert all(row["source_run_id"] == "run-1" for row in nested)
    assert all(row["gp_code"] == "123" and row["fiscal_year"] == "2021-2022" for row in fund)
    assert all(row["business_id"] == "7" for row in fund[:2])
    assert all("gp_code=" not in str(path) for path in result.output_root.rglob("*.parquet"))


@pytest.mark.parametrize("filename", ["2021_PL.json", "2021-PL.json"])
def test_year_filename_separators_and_mixed_array_positions(tmp_path: Path, filename: str):
    run = make_run(tmp_path, "run-year", {
        f"LGD-9-GP/{filename}": [{"activityCd": "A", "notes": ["scalar", {"x": 1}]}],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    child = rows(files(result, "pl__notes")[0])
    assert [row["pos"] for row in child] == [0, 1]
    assert child[0]["value_kind"] == "scalar"
    assert child[1]["x"] == 1
    assert {row["fiscal_year"] for row in child} == {"2021-2022"}


def test_empty_is_valid_and_malformed_is_reason_coded_quarantine(tmp_path: Path):
    run = make_run(tmp_path, "run-invalid", {
        "2021_PL.json": {"data": []},
        "2021-AA.json": {"data": [{"id": "ok"}]},
        "2021_TA.json": {"data": [{"id": "ok"}]},
        "2021_PP.json": {"data": [{"id": "ok"}]},
        "2021_RE.json": {"data": [{"id": "legacy"}]},
        "2021_PL_bad.json": {"data": [1]},
        "2021_AA_bad.json": b"{not-json",
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    assert "pl" in result.tables
    assert {kind for kind in ("aa", "ta", "pp", "re") if kind in result.tables} == {
        "aa", "ta", "pp", "re"
    }
    assert rows(files(result, "re")[0])[0]["mapping_status"] == "unmapped"
    quarantine = rows(files(result, "quarantine")[0])
    assert {row["reason_code"] for row in quarantine} == {
        "malformed_known_envelope", "malformed_json"
    }
    canonical = json.loads((result.output_root / "canonical_manifest.json").read_text())
    assert canonical["tables"]["pl"]["row_count"] == 0
    assert canonical["quarantine_count"] == 2


def test_kinds_filter_skips_supported_kinds_without_quarantining_them(tmp_path: Path):
    """A supported-but-unrequested kind must be skipped, not quarantined.

    Codex review (PR #64, normalize.py:485): with --kinds PL, valid AA/TA/
    PP/RE files used to hit `source_kind not in wanted` and get quarantined
    as unknown_source_kind, falsely reporting an intentional exclusion as
    malformed input. Only filenames with no recognized kind at all should be
    quarantined; kinds excluded by the filter should simply be absent.
    """
    run = make_run(tmp_path, "run-filtered", {
        "2021_PL.json": {"data": [{"id": "ok"}]},
        "2021_AA.json": {"data": [{"id": "ok"}]},
        "2021_unknown.json": {"data": [{"id": "ok"}]},
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical", kinds=["PL"])
    assert "pl" in result.tables
    assert "aa" not in result.tables
    quarantine = rows(files(result, "quarantine")[0])
    assert {row["source_file"] for row in quarantine} == {"2021_unknown.json"}
    assert {row["reason_code"] for row in quarantine} == {"unknown_source_kind"}
    assert result.quarantine_count == 1


def test_forged_row_count_is_rejected(tmp_path: Path):
    """A hand-edited manifest with a forged row_count must fail validation.

    validate_canonical_manifest previously only type-checked row_count as an
    int; it never cross-checked the declared count against the actual
    Parquet footers, so a corrupted manifest with every file hash intact
    would pass silently.
    """
    run = make_run(tmp_path, "run-forged", {"2021_PL.json": [{"id": "1"}, {"id": "2"}]})
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    manifest_path = result.output_root / "canonical_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["tables"]["pl"]["row_count"] == 2
    manifest["tables"]["pl"]["row_count"] = 999999
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(NormalizationError, match="row_count|row count"):
        validate_canonical_manifest(result.output_root)


def test_max_buffered_rows_reports_observed_peak_not_configured_chunk_size(tmp_path: Path):
    """max_buffered_rows must reflect what was actually buffered in memory.

    With 5 rows and a chunk_size far larger than the data, the previous
    implementation echoed the configured chunk_size (999999) back as if it
    were an observed peak, which is not evidence that buffering is bounded.
    """
    run = make_run(tmp_path, "run-small", {
        "2021_PL.json": [{"id": str(i)} for i in range(5)],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=999999)
    assert result.max_buffered_rows == 5


@pytest.mark.parametrize(
    "fiscal_year, expected",
    [
        ("1998-99", "1998-1999"),
        ("2021-22", "2021-2022"),
        ("1999-00", "1999-2000"),
    ],
)
def test_two_digit_fiscal_year_century_rollover(fiscal_year: str, expected: str):
    """The end year's century must be derived from the start year.

    Previously any two-digit end was assumed to be in the 2000s, so
    "1998-99" normalised to "1998-2099" instead of "1998-1999".
    """
    assert _normalise_year(fiscal_year) == expected


def test_four_digit_and_bare_year_behaviour_is_unchanged():
    assert _normalise_year("2021") == "2021-2022"
    assert _normalise_year("1998-1999") == "1998-1999"
    assert _normalise_year("2021-2045") == "2021-2045"


def test_stale_outputs_are_removed_and_chunks_obey_boundary(tmp_path: Path):
    first = make_run(tmp_path, "run-a", {
        "2021_PL.json": [{"id": "1", "children": [{"x": 1}]}],
    })
    result = normalize_egramswaraj(first, tmp_path / "canonical", chunk_size=1)
    assert len(files(result, "pl")) == 1
    assert "pl__children" in result.tables

    second = make_run(tmp_path, "run-b", {"2021_PL.json": [{"id": "2"}]})
    result = normalize_egramswaraj(second, tmp_path / "canonical", chunk_size=1)
    assert "pl__children" not in result.tables
    assert (tmp_path / "canonical" / "egramSwaraj" / "run-a").is_dir()
    assert (tmp_path / "canonical" / "egramSwaraj" / "run-b").is_dir()
    assert rows(files(result, "pl")[0])[0]["business_id"] == "2"


def test_failed_second_publication_preserves_previous_output(tmp_path: Path, monkeypatch):
    first = make_run(tmp_path, "run-good", {"2021_PL.json": [{"id": "good"}]})
    first_result = normalize_egramswaraj(first, tmp_path / "canonical")
    old = rows(next((first_result.output_root / "pl").rglob("*.parquet")))

    second = make_run(tmp_path, "run-failing", {"2021_PL.json": [{"id": "new"}]})
    import src.pipeline.normalize as module

    def fail(*args, **kwargs):
        raise OSError("synthetic parquet failure")

    monkeypatch.setattr(module.pq, "write_table", fail)
    with pytest.raises(OSError, match="synthetic parquet failure"):
        normalize_egramswaraj(second, tmp_path / "canonical")
    assert rows(next((first_result.output_root / "pl").rglob("*.parquet"))) == old
    assert not list((tmp_path / "canonical" / "egramSwaraj").glob(".run-failing.staging-*"))


def test_source_fields_cannot_overwrite_canonical_provenance(tmp_path: Path):
    """A source field named like a provenance column must not win.

    Codex review (PR #64, normalize.py:414): if an input record contains a
    reserved canonical name (row_id, source_run_id, source_file,
    fiscal_year, ...), the old code applied provenance first and let the
    source fields overwrite it, so a child row's parent_row_id would
    reference a hash the parent itself no longer exposed as its own row_id.
    """
    run = make_run(tmp_path, "run-collide", {
        "2021_PL.json": [{
            "activityCd": "A1",
            "row_id": "forged-row-id",
            "source_run_id": "forged-run-id",
            "source_file": "forged-file",
            "fiscal_year": "1900-1901",
            "children": [{"x": 1}],
        }],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    parent = rows(files(result, "pl")[0])[0]
    child = rows(files(result, "pl__children")[0])[0]

    assert parent["row_id"] != "forged-row-id"
    assert parent["source_run_id"] == "run-collide"
    assert parent["source_file"] == "2021_PL.json"
    assert parent["fiscal_year"] == "2021-2022"
    # Referential integrity: the child's parent_row_id must match the
    # parent's real (generated) row_id, not the forged source value.
    assert child["parent_row_id"] == parent["row_id"]


def test_existing_snapshot_is_immutable_and_row_ids_are_cross_run_stable(tmp_path: Path):
    first = make_run(tmp_path, "run-one", {"2021_PL.json": [{"id": "same"}]})
    first_result = normalize_egramswaraj(first, tmp_path / "canonical")
    first_row = rows(files(first_result, "pl")[0])[0]

    second = make_run(tmp_path, "run-two", {"2021_PL.json": [{"id": "same"}]})
    second_result = normalize_egramswaraj(second, tmp_path / "canonical")
    second_row = rows(files(second_result, "pl")[0])[0]
    assert first_row["row_id"] == second_row["row_id"]
    assert first_row["source_record_id"] == second_row["source_record_id"]
    with pytest.raises(NormalizationError, match="already exists"):
        normalize_egramswaraj(second, tmp_path / "canonical")


def test_row_id_survives_record_reordering_across_runs(tmp_path: Path):
    """row_id must key off record identity, not array position.

    Codex review (PR #64, normalize.py:407): the row_id hash's only
    record-specific component used to be root_pos. If the source ever
    returns the same two records in a different order on a later run, each
    record would inherit the *other's* row_id, silently swapping canonical
    identities and every child link. Records carry a business id
    (activityCd), so identity must follow that id regardless of position.
    """
    first = make_run(tmp_path, "run-order-a", {
        "2021_PL.json": [
            {"activityCd": "A1", "note": "first"},
            {"activityCd": "A2", "note": "second"},
        ],
    })
    first_result = normalize_egramswaraj(first, tmp_path / "canonical")
    first_by_id = {row["business_id"]: row for row in rows(files(first_result, "pl")[0])}

    second = make_run(tmp_path, "run-order-b", {
        "2021_PL.json": [
            {"activityCd": "A2", "note": "second"},
            {"activityCd": "A1", "note": "first"},
        ],
    })
    second_result = normalize_egramswaraj(second, tmp_path / "canonical")
    second_by_id = {row["business_id"]: row for row in rows(files(second_result, "pl")[0])}

    assert first_by_id["A1"]["row_id"] == second_by_id["A1"]["row_id"]
    assert first_by_id["A2"]["row_id"] == second_by_id["A2"]["row_id"]
    assert first_by_id["A1"]["row_id"] != first_by_id["A2"]["row_id"]


def test_row_id_survives_reordering_records_that_differ_only_in_children(tmp_path: Path):
    """The fallback identity must see nested content, not just scalars (#110).

    The sibling test above covers records carrying a business id. These do
    not, so they take the content-hash fallback -- and until #110 that hash
    was computed over `_flatten_scalars`, which drops every list. Two records
    with identical scalars and *different child arrays* therefore hashed
    identically, the occurrence suffix decided which was which, and swapping
    their order in the file swapped every row_id and child link between them.

    Child arrays are the only thing distinguishing these two records, which
    is exactly the case the old hash could not see.
    """

    def payload(order):
        return {"2021_PL.json": order}

    a = {"note": "same", "assets": [{"assetNm": "well"}]}
    b = {"note": "same", "assets": [{"assetNm": "road"}, {"assetNm": "drain"}]}

    first = normalize_egramswaraj(
        make_run(tmp_path, "run-nested-a", payload([a, b])), tmp_path / "canonical")
    second = normalize_egramswaraj(
        make_run(tmp_path, "run-nested-b", payload([b, a])), tmp_path / "canonical")

    # Key by child count, the one thing that tells the two records apart.
    def by_shape(result):
        parents = {row["row_id"]: row for row in rows(files(result, "pl")[0])}
        counts = {}
        for child in rows(files(result, "pl__assets")[0]):
            counts[child["parent_row_id"]] = counts.get(child["parent_row_id"], 0) + 1
        return {counts[rid]: rid for rid in parents if rid in counts}

    assert by_shape(first) == by_shape(second)
    assert len(by_shape(first)) == 2, "both records must be present and distinguishable"


def test_child_row_ids_survive_reordering_a_child_array(tmp_path: Path):
    """Child row_id derives from child identity; `pos` is ordering only (#110).

    Child ids used to be `{prefix}/{key}:{position}` even though the child's
    own business id was extracted on the very next line. Reorder an array and
    every element took the previous occupant's row_id, with nested descendants
    inheriting the swapped prefix -- the same defect the top-level fix closed,
    one level down.

    `pos` must still report the array index: ordering is retained as metadata,
    it just no longer decides identity.
    """

    def run(order):
        return normalize_egramswaraj(
            make_run(tmp_path, f"run-child-{'-'.join(o['activityCd'] for o in order)}", {
                "2021_PL.json": [{"activityCd": "PARENT", "assets": order}],
            }),
            tmp_path / "canonical",
        )

    x = {"activityCd": "X", "assetNm": "well"}
    y = {"activityCd": "Y", "assetNm": "road"}

    forward = {r["business_id"]: r for r in rows(files(run([x, y]), "pl__assets")[0])}
    reverse = {r["business_id"]: r for r in rows(files(run([y, x]), "pl__assets")[0])}

    assert forward["X"]["row_id"] == reverse["X"]["row_id"]
    assert forward["Y"]["row_id"] == reverse["Y"]["row_id"]
    assert forward["X"]["row_id"] != forward["Y"]["row_id"]
    # Ordering survives as metadata rather than as identity.
    assert (forward["X"]["pos"], forward["Y"]["pos"]) == (0, 1)
    assert (reverse["Y"]["pos"], reverse["X"]["pos"]) == (0, 1)


def test_a_business_id_that_looks_like_an_occurrence_suffix_does_not_collide(tmp_path: Path):
    """The occurrence counter must not live in the identity's namespace.

    Appending "#<n>" to the identity puts the counter where a business id can
    reach: a SECOND element with id `A` and a FIRST element whose id genuinely
    is `A#1` both produce the same key, so they share a row_id and their
    descendants collide under the shared prefix.

    This is a regression the identity fix could have introduced and the
    positional child ids could not -- `assets:0`/`assets:1` were unique by
    construction. Both spellings of the trap are covered: children (where the
    positional scheme was replaced) and top level (where the "#" suffix was
    already in use before #110).
    """

    run = make_run(tmp_path, "run-hash", {
        "2021_PL.json": [{
            "activityCd": "PARENT",
            "assets": [
                {"activityCd": "A", "n": 1},
                {"activityCd": "A", "n": 2},      # -> occurrence 1 of "A"
                {"activityCd": "A#1", "n": 3},    # -> occurrence 0 of "A#1"
            ],
        }],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    children = list(rows(files(result, "pl__assets")[0]))
    assert len(children) == 3
    row_ids = [c["row_id"] for c in children]
    assert len(set(row_ids)) == 3, f"row_id collision: {row_ids}"


def test_top_level_records_whose_ids_look_like_occurrence_suffixes_do_not_collide(
    tmp_path: Path,
):
    """The same trap one level up, where the "#" suffix predates #110."""

    run = make_run(tmp_path, "run-hash-top", {
        "2021_PL.json": [
            {"activityCd": "A", "note": "first"},
            {"activityCd": "A", "note": "second"},   # -> occurrence 1 of "A"
            {"activityCd": "A#1", "note": "third"},  # -> occurrence 0 of "A#1"
        ],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    row_ids = [r["row_id"] for r in rows(files(result, "pl")[0])]
    assert len(row_ids) == 3
    assert len(set(row_ids)) == 3, f"row_id collision: {row_ids}"


def test_row_id_deduplicates_records_without_a_business_id_deterministically(tmp_path: Path):
    """Records with no business id fall back to content, not position.

    Two records with identical content (no activityCd) must not collide on
    the same row_id, and the disambiguation must be stable across runs that
    present them in the same order.
    """
    run = make_run(tmp_path, "run-dup", {
        "2021_PL.json": [{"note": "dup"}, {"note": "dup"}, {"note": "unique"}],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical")
    row_ids = [row["row_id"] for row in rows(files(result, "pl")[0])]
    assert len(row_ids) == len(set(row_ids)) == 3


def test_to_records_streams_instead_of_materializing_a_second_full_list():
    """to_records() must not build a second full-size list up front.

    Codex review (PR #64, normalize.py:488): read_text()+json.loads()
    already holds the whole parsed payload in memory; the old
    `[{**header, **dict(item)} for item in values]` list comprehension then
    built a *second* full-size list of merged records before any row
    reached the bounded write buffer, so chunk_size/max_buffered_rows did
    not actually bound how much of a large payload was resident at once.
    to_records() now returns a generator so at most one merged record is
    alive on top of the parsed payload at a time -- this is a partial fix
    (the input file is still fully parsed by json.loads up front; see the
    PR reply for the documented residual and why it is bounded in practice
    for this source).
    """
    import types

    payload = {"status": "OK", "data": [{"id": str(i)} for i in range(50)]}
    records, reason = to_records(payload)
    assert reason is None
    assert isinstance(records, types.GeneratorType)
    materialized = list(records)
    assert len(materialized) == 50
    assert materialized[0] == {"status": "OK", "id": "0"}

    # Root-array and single-record shapes are also lazy iterables (not
    # necessarily generators, but never a pre-built list of merged dicts).
    array_records, array_reason = to_records([{"id": "1"}, {"id": "2"}])
    assert array_reason is None
    assert not isinstance(array_records, list)
    assert list(array_records) == [{"id": "1"}, {"id": "2"}]


def test_union_schema_is_stable_across_chunk_boundaries(tmp_path: Path):
    run = make_run(tmp_path, "schema", {
        "2021_PL.json": [{"id": "1", "amount": 1}, {"id": "2", "amount": "two"}],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=1)
    table = pq.read_table(files(result, "pl")[0].parent)
    assert str(table.schema.field("amount").type) == "string"
    assert [row["amount"] for row in table.to_pylist()] == ["1", "two"]
    assert result.max_buffered_rows <= 1


def _parses_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gps: int) -> tuple[int, int]:
    """(payload files, json.loads calls) for a run over `gps` GP folders."""

    import src.pipeline.normalize as normalize_module

    payloads: dict[str, object] = {}
    for index in range(gps):
        gp = f"LGD_{index}_GP"
        payloads[f"{gp}/2021_PL.json"] = {"data": [{
            "activityCd": 7, "totalCost": 100,
            "fundList": [{"schemeCode": "S1", "amountTotal": 5}],
        }]}
        payloads[f"{gp}/2021_AA.json"] = {"data": [{
            "activityCd": 7, "wrkAdmApprNo": "007",
            "admApprovalSchemeWebService": [{"wrkSchmCd": "SC1"}],
        }]}
    run = make_run(tmp_path / f"g{gps}", "run-1", payloads)

    parses = 0
    real_loads = normalize_module.json.loads

    def counting_loads(*args, **kwargs):
        nonlocal parses
        parses += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(normalize_module.json, "loads", counting_loads)
    try:
        normalize_egramswaraj(run, tmp_path / f"canonical-{gps}", chunk_size=100)
    finally:
        monkeypatch.setattr(normalize_module.json, "loads", real_loads)
    return len(payloads), parses


def test_parse_cost_scales_with_input_not_with_table_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Cost must be O(input), not O(tables x input) -- #145.

    The old shape walked the whole payload tree once to infer schemas, once
    per output table, and once more for quarantine. On the full state that
    was 12 walks of ~204,000 files: ~118 GiB of JSON parsed to emit ~1.3 GiB
    of Parquet, with 88% of wall-clock in json.loads.

    Asserted as a *slope* rather than an absolute count, so the fixed cost of
    reading the run manifests cannot mask a regression, and so the number
    does not have to be updated whenever an unrelated json.loads is added.

    Two walks remain by design: schema inference has to see every record
    before a Parquet column type can be fixed. Both fixtures produce the same
    six tables, so under the old shape this slope would be 8, not 2.
    """

    small_files, small_parses = _parses_for(tmp_path, monkeypatch, 3)
    large_files, large_parses = _parses_for(tmp_path, monkeypatch, 9)

    slope = (large_parses - small_parses) / (large_files - small_files)
    assert slope == 2, (
        f"{slope} parses per input file: expected 2 (schema inference + one "
        f"write pass), not one walk per output table"
    )


def test_peak_buffered_rows_does_not_grow_with_table_count(tmp_path: Path):
    """The single-pass rewrite must not buy speed with memory.

    Holding one buffer per table is the obvious way to walk the input once,
    and it multiplies peak buffered rows by the number of tables. Buffers are
    flushed on a shared budget instead, so this bound holds however many
    tables a run produces.

    What this does and does not pin: ``max_buffered_rows`` counts rows held
    across *all* buffers, so switching to per-table thresholds makes this
    fail. It would not catch a change that also redefined the metric to be
    per-table -- that is what the peak-RSS column in
    ``scripts/benchmark_normalize.py`` is for.
    """

    payloads = {}
    for index in range(6):
        payloads[f"LGD_{index}_GP/2021_PL.json"] = {"data": [
            {"activityCd": n, "totalCost": n,
             "fundList": [{"schemeCode": "S", "amountTotal": n}]}
            for n in range(20)
        ]}
    run = make_run(tmp_path, "run-1", payloads)

    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=8)

    assert result.max_buffered_rows <= 8, (
        f"buffered {result.max_buffered_rows} rows against a chunk_size of 8"
    )


def test_a_small_table_is_not_split_into_one_part_per_flush(tmp_path: Path):
    """Sharing a flush budget must not shred the small tables.

    Buffers share one row budget, so a big table trips it. Flushing *every*
    buffer at that point writes whatever the small tables happen to be
    holding -- a row or two -- as a Parquet file of its own, once per flush.
    On this fixture that is 12 files for 20 rows; at full-state scale it is
    hundreds of near-empty files per small table.

    Flushing fullest-first and stopping once the budget is met leaves the
    small buffers alone until the end, so they land in one file.

    The small table here is ``aa``, which sorts *before* the big ``pl``, and
    that is the point: an earlier version of this test used a small table
    that sorted last, so plain alphabetical order emptied the big buffer
    first and the test passed without fullest-first doing anything. Both
    mutations have to fail -- flushing everything, and flushing in name order
    -- and with the small table sorting first, both do.

    Peak buffered rows is unchanged either way; that is
    ``test_peak_buffered_rows_does_not_grow_with_table_count``.
    """

    payloads = {}
    for index in range(20):
        gp = f"LGD_{index}_GP"
        # 30 activities per GP dominate the shared budget.
        payloads[f"{gp}/2021_PL.json"] = {"data": [
            {"activityCd": n, "totalCost": n} for n in range(30)
        ]}
        # One AA record beside them, spread across the whole input rather
        # than sitting at the start.
        payloads[f"{gp}/2021_AA.json"] = {"data": [
            {"activityCd": 1, "wrkAdmApprNo": f"A{index}"}
        ]}
    run = make_run(tmp_path, "run-1", payloads)

    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=50)

    parts = list((result.output_root / "aa").rglob("*.parquet"))
    assert len(parts) == 1, (
        f"20 rows written as {len(parts)} part files; the small table is "
        f"being flushed alongside the big one"
    )
