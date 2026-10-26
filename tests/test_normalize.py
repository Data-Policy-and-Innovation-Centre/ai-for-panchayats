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
    normalize_run,
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


def test_row_ids_survive_reordering_siblings_that_share_a_business_id(tmp_path: Path):
    """A shared business id must not put identity back on array position (#110).

    The reordering tests above pin the two clean cases: distinct business ids,
    and no business id at all. This is the case between them -- two siblings
    carrying the *same* activityCd but differing in their other fields. Both
    hash to the identity `id:SAME`, so the occurrence counter alone decided
    which was which, and reversing the array swapped their row_ids and every
    descendant link, exactly the defect the business id was supposed to close.

    Asserted at both levels, because the two loops number occurrences
    independently and a fix to one leaves the other broken.
    """

    def child_run(order):
        result = normalize_egramswaraj(
            make_run(tmp_path, f"dupid-child-{'-'.join(o['assetNm'] for o in order)}", {
                "2021_PL.json": [{"activityCd": "PARENT", "assets": order}],
            }),
            tmp_path / "canonical",
        )
        return {r["assetNm"]: r["row_id"] for r in rows(files(result, "pl__assets")[0])}

    def record_run(order):
        result = normalize_egramswaraj(
            make_run(tmp_path, f"dupid-top-{'-'.join(o['note'] for o in order)}", {"2021_PL.json": order}),
            tmp_path / "canonical",
        )
        return {r["note"]: r["row_id"] for r in rows(files(result, "pl")[0])}

    well = {"activityCd": "SAME", "assetNm": "well"}
    road = {"activityCd": "SAME", "assetNm": "road"}
    forward, reverse = child_run([well, road]), child_run([road, well])
    assert forward == reverse
    assert len(set(forward.values())) == 2

    first = {"activityCd": "SAME", "note": "first"}
    second = {"activityCd": "SAME", "note": "second"}
    forward, reverse = record_run([first, second]), record_run([second, first])
    assert forward == reverse
    assert len(set(forward.values())) == 2


def test_reordering_a_child_array_does_not_move_its_parent(tmp_path: Path):
    """Content is a tiebreaker only, never part of an unambiguous identity.

    The obvious fix for the test above -- fold the element's content into
    every identity -- trades one order bug for a wider one. A record's content
    includes its child arrays in order, so reordering a *grandchild* would
    change the parent's row_id and orphan every child link beneath it. The
    refinement therefore applies only to identities a sibling duplicates.
    """

    def run(name, assets):
        result = normalize_egramswaraj(
            make_run(tmp_path, name, {
                "2021_PL.json": [{"activityCd": "P", "assets": assets}],
            }),
            tmp_path / "canonical",
        )
        return rows(files(result, "pl")[0])[0]["row_id"]

    x = {"activityCd": "X", "assetNm": "well"}
    y = {"activityCd": "Y", "assetNm": "road"}
    assert run("parent-fwd", [x, y]) == run("parent-rev", [y, x])


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


# --------------------------------------------------------------------- flat CSV lane (#123)

PROFILE_HEADER = "basic_info_lgd,param__gp_name,demographic_details_male_population"


def profile_run(tmp_path: Path, run_id: str, body: str) -> Path:
    """A raw run published the way the profile extract is: one CSV, its own source."""

    with RunPublisher(tmp_path / "raw", "egramswaraj_profile", run_id) as publisher:
        publisher.write_payload(
            "eGramSwaraj_panchayat_master.csv",
            f"{PROFILE_HEADER}\n{body}".encode(),
        )
        return publisher.publish()


def test_flat_csv_run_normalizes_to_one_canonical_table(tmp_path: Path):
    """The lane the JSON normalizer cannot serve (#123).

    A one-row-per-GP reference CSV has no `LGD_<code>_<name>` folder, no
    fiscal year in its filename and no nested child arrays, so it matches
    neither `_gp_context` nor `KIND_RE`. What it must still produce is the
    same *output* contract -- provenance columns, atomic publication, a
    canonical manifest -- because that is the half a snapshot depends on.
    """

    run = profile_run(tmp_path, "profile-1", "115550,Angarbandha,120\n115551,Badabandha,130")
    result = normalize_run(run, tmp_path / "canonical")

    assert set(result.tables) == {"profile"}
    canonical = rows(files(result, "profile")[0])
    assert len(canonical) == 2
    assert [row["basic_info_lgd"] for row in canonical] == ["115550", "115551"]
    # The key column stands in for the JSON lane's activityCd.
    assert [row["business_id"] for row in canonical] == ["115550", "115551"]
    assert {row["source_kind"] for row in canonical} == {"PROFILE"}
    assert {row["source_system"] for row in canonical} == {"egramswaraj_profile"}
    # No fiscal year exists in this source, so none is invented -- and the
    # rows are not filed under a `fiscal_year-unknown` partition that would
    # read as missing data rather than as inapplicable.
    assert {row["fiscal_year"] for row in canonical} == {None}
    assert not list((result.output_root / "profile").glob("fiscal_year-*"))
    assert (result.output_root / "canonical_manifest.json").is_file()


def test_flat_csv_row_ids_follow_the_key_not_the_line_number(tmp_path: Path):
    """Same guarantee as the JSON lane's identity tests, one source over (#110).

    A reference extract is re-scraped periodically and there is nothing that
    fixes its row order. Keying identity on the line number would make every
    row_id -- and every link anything later builds on one -- move the first
    time the portal returned the same GPs in a different order.
    """

    forward = normalize_run(
        profile_run(tmp_path, "profile-fwd", "115550,Angarbandha,120\n115551,Badabandha,130"),
        tmp_path / "canonical",
    )
    reverse = normalize_run(
        profile_run(tmp_path, "profile-rev", "115551,Badabandha,130\n115550,Angarbandha,120"),
        tmp_path / "canonical",
    )

    def by_key(result):
        return {row["basic_info_lgd"]: row["row_id"] for row in rows(files(result, "profile")[0])}

    assert by_key(forward) == by_key(reverse)
    assert len(set(by_key(forward).values())) == 2


def test_flat_csv_blank_keys_still_get_distinct_row_ids(tmp_path: Path):
    """The 84 profile-less rows must survive normalization to be counted later.

    Rejecting them here would make the warehouse's quarantine count wrong by
    84 and hide a shrinking source. They carry no key, so identity falls back
    to content -- which is why the two below must not collide.
    """

    result = normalize_run(
        profile_run(tmp_path, "profile-blank", ",Angarbandha,\n,Badabandha,\n115550,Kendu,120"),
        tmp_path / "canonical",
    )
    canonical = rows(files(result, "profile")[0])
    assert len(canonical) == 3
    assert len({row["row_id"] for row in canonical}) == 3
    assert sum(1 for row in canonical if row["business_id"] is None) == 2


def test_flat_csv_run_refuses_an_ambiguous_or_unkeyed_payload(tmp_path: Path):
    """Two failures that must stop the run rather than produce a partial table.

    Two CSVs in one run would share a run_id and a canonical table, so their
    rows would be indistinguishable in provenance. A missing key column would
    give every row a content-derived identity and silently load a table whose
    primary key is null throughout.
    """

    with RunPublisher(tmp_path / "raw", "egramswaraj_profile", "profile-two") as publisher:
        publisher.write_payload("a.csv", f"{PROFILE_HEADER}\n1,A,2".encode())
        publisher.write_payload("b.csv", f"{PROFILE_HEADER}\n2,B,3".encode())
        two = publisher.publish()
    with pytest.raises(NormalizationError, match="exactly one .csv payload"):
        normalize_run(two, tmp_path / "canonical")

    with RunPublisher(tmp_path / "raw", "egramswaraj_profile", "profile-unkeyed") as publisher:
        publisher.write_payload("p.csv", b"gp_name,population\nAngarbandha,120")
        unkeyed = publisher.publish()
    with pytest.raises(NormalizationError, match="basic_info_lgd"):
        normalize_run(unkeyed, tmp_path / "canonical")


def test_flat_csv_keys_that_look_like_occurrence_suffixes_do_not_collide(tmp_path: Path):
    """The lane mirrors the JSON lane's identity scheme rather than re-spelling it.

    The pre-#110 form appended `#<n>` to the identity, which put the counter
    inside the identity's own namespace: a key that literally reads `X#1`
    produced the same key as the *second* row keyed `X`, so the two shared a
    row_id. LGD codes are numeric and cannot trip this, but the lane is
    generic -- #48's spreadsheet brings its own key column.
    """

    result = normalize_run(
        profile_run(tmp_path, "profile-hash", "X,A,1\nX,B,2\nX#1,C,3"),
        tmp_path / "canonical",
    )
    canonical = rows(files(result, "profile")[0])
    assert len(canonical) == 3
    row_ids = [row["row_id"] for row in canonical]
    assert len(set(row_ids)) == 3, f"row_id collision: {row_ids}"


def test_flat_csv_rows_sharing_a_key_survive_reordering(tmp_path: Path):
    """The duplicate-key tiebreaker reaches this lane too.

    Two rows with the same key and different content would otherwise be told
    apart by line number alone, and re-exporting the file in a different order
    would swap their row_ids. The real extract has no duplicate keys -- 6,710
    distinct, verified -- so this pins the generic property the lane offers
    rather than a defect in the profile file.
    """

    def by_name(order):
        result = normalize_run(
            profile_run(tmp_path, f"dupkey-{order[0][0]}", "\n".join(
                f"115550,{name},{pop}" for name, pop in order
            )),
            tmp_path / "canonical",
        )
        return {r["param__gp_name"]: r["row_id"] for r in rows(files(result, "profile")[0])}

    forward = by_name([("A", "1"), ("B", "2")])
    reverse = by_name([("B", "2"), ("A", "1")])
    assert forward == reverse
    assert len(set(forward.values())) == 2


# --------------------------------------------------------------------- child identity keys (#163)

def _scheme_run(tmp_path: Path, run_id: str, elements: list[dict]) -> Path:
    return make_run(tmp_path, run_id, {
        "2021_AA.json": [{"activityCd": "A1", "admApprovalSchemeWebService": elements}],
    })


def _scheme_rows(tmp_path: Path, run_id: str, elements: list[dict]) -> list[dict]:
    result = normalize_egramswaraj(
        _scheme_run(tmp_path, run_id, elements), tmp_path / "canonical",
    )
    return rows(files(result, "aa__admapprovalschemewebservice")[0])


def test_a_child_with_its_own_key_keeps_its_row_id_when_an_amount_changes(tmp_path: Path):
    """`ID_KEYS` sees activity-style fields only, so this fell back to content (#163).

    An `admApprovalSchemeWebService` element carries `wrkSchmCd`/`wrkSchmCmpntCd`,
    which none of `ID_KEYS` matches. Its identity was therefore a hash of the
    whole element, and editing an amount -- an ordinary correction upstream --
    re-identified a logically unchanged row and moved every descendant prefix
    beneath it.

    Measured across 250 random GPs: the composite is present, non-blank and
    unique in all 27,672 scheme arrays observed.
    """

    before = _scheme_rows(tmp_path, "scheme-before", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": "C1", "wrkAdmApprFndSnctnGen": 100},
    ])
    after = _scheme_rows(tmp_path, "scheme-after", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": "C1", "wrkAdmApprFndSnctnGen": 250},
    ])
    assert before[0]["row_id"] == after[0]["row_id"]
    assert before[0]["wrkAdmApprFndSnctnGen"] != after[0]["wrkAdmApprFndSnctnGen"]


def test_a_child_keys_business_id_still_inherits_the_activity(tmp_path: Path):
    """The identity key must not reach the `business_id` provenance column.

    `transform._base_identity` and `transform.admin_approval_scheme` both read
    `business_id` as an *activity* code. A child with no id of its own inherits
    its parent's, which is what makes those two correct. If the collection key
    were routed through `_business_id` instead of through identity alone, this
    row's `business_id` would become a scheme code and a wrong value would be
    loaded into `admin_approval_scheme.activity_code`.
    """

    row = _scheme_rows(tmp_path, "scheme-bid", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": "C1", "wrkAdmApprFndSnctnGen": 100},
    ])[0]
    assert row["business_id"] == "A1", "business_id must stay the inherited activity code"


def test_a_partial_composite_is_not_treated_as_a_key(tmp_path: Path):
    """Half a composite is not an identity.

    Two elements agreeing on the half that happens to be filled in would share
    it, and the occurrence counter would then decide which was which -- exactly
    the order dependence the key is meant to remove. A missing part must fall
    back to content, which still tells these two apart.
    """

    canonical = _scheme_rows(tmp_path, "scheme-partial", [
        {"wrkSchmCd": "S1", "wrkAdmApprFndSnctnGen": 100},
        {"wrkSchmCd": "S1", "wrkAdmApprFndSnctnGen": 250},
    ])
    assert len(canonical) == 2
    assert len({r["row_id"] for r in canonical}) == 2


def test_a_child_without_a_known_collection_key_is_unchanged(tmp_path: Path):
    """Collections not in the table keep the previous behaviour exactly.

    The keys were chosen by measuring five collections; anything else still
    falls back to its own business id, then to content. Adding the table must
    not quietly re-identify arrays nobody surveyed.
    """

    result = normalize_egramswaraj(
        make_run(tmp_path, "unknown-collection", {
            "2021_PL.json": [{"activityCd": "P1", "someOtherArray": [
                {"schemeCode": "S1", "componentCode": "C1", "amount": 5},
            ]}],
        }),
        tmp_path / "canonical",
    )
    child = rows(files(result, "pl__someotherarray")[0])[0]
    # `fundList`'s key would have matched these field names; the collection
    # name is what gates it, so this row still inherits and hashes content.
    assert child["business_id"] == "P1"


def test_a_whitespace_only_key_part_is_not_a_key(tmp_path: Path):
    """`" "` is as absent as `""`, and had to be tested before stripping.

    Accepting it hands a lone row a stable-looking identity that survives
    arbitrary content changes -- the exact opposite of what falling back to
    content is for, and a silent one because a single-element array cannot
    collide with anything to reveal it.

    Whitespace is stripped inside the key as well, so `"S1"` and `" S1 "` are
    one scheme rather than two.
    """

    blank = _scheme_rows(tmp_path, "scheme-ws-a", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": " ", "wrkAdmApprFndSnctnGen": 100},
    ])
    changed = _scheme_rows(tmp_path, "scheme-ws-b", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": " ", "wrkAdmApprFndSnctnGen": 250},
    ])
    assert blank[0]["row_id"] != changed[0]["row_id"], (
        "a whitespace-only part must fall back to content identity"
    )

    padded = _scheme_rows(tmp_path, "scheme-ws-c", [
        {"wrkSchmCd": " S1 ", "wrkSchmCmpntCd": " C1 ", "wrkAdmApprFndSnctnGen": 100},
    ])
    tight = _scheme_rows(tmp_path, "scheme-ws-d", [
        {"wrkSchmCd": "S1", "wrkSchmCmpntCd": "C1", "wrkAdmApprFndSnctnGen": 250},
    ])
    assert padded[0]["row_id"] == tight[0]["row_id"], (
        "padding is not a different scheme"
    )


def test_a_blank_business_id_is_absent_the_same_way_a_blank_key_part_is(tmp_path: Path):
    """The two identity sources must agree on what "blank" means.

    `_collection_key` and `_business_id` are alternatives in one expression,
    so a `" "` accepted by one and rejected by the other would be an identity
    that exists only because of which field it came from. Found by
    self-review after the collection-key fix, not by a failure: no id value in
    the scrape is padded or whitespace-only (420,821 sampled).

    Stripping also matches the warehouse, where every `activity_code` goes
    through `clean.to_code`. A padded id would otherwise make a parent's
    stripped code and a child's unstripped one fail to match, and the child
    would be quarantined as an orphan of a row that is right there.
    """

    result = normalize_egramswaraj(
        make_run(tmp_path, "blank-bid", {
            "2021_PL.json": [
                {"activityCd": " ", "note": "whitespace-only"},
                {"activityCd": " A1 ", "note": "padded"},
            ],
        }),
        tmp_path / "canonical",
    )
    by_note = {r["note"]: r for r in rows(files(result, "pl")[0])}
    assert by_note["whitespace-only"]["business_id"] is None
    assert by_note["padded"]["business_id"] == "A1"


EXPENDITURE_HEADER = (
    "planYear,stateName,zpName,blockName,gpName,gpCode,planType,approvalDate,planCode,"
    "planCodeStatus,S.No.,Activity Code,Activity Name,Activity For,Focus Area,"
    "Approved Cost in Action Plan,Technical Approved Cost,Admin Approved Cost,Scheme Name,"
    "General,SC,ST,Total Expenditure,Voucher Date,Voucher No,Voucher Cost"
)


def expenditure_row(*, s_no: str = "1", activity: str = "44134242",
                    total: str = "1028.00") -> str:
    return (
        f"2022-2023,Odisha,Koraput,Dasamantapur,Test GP,123,131,,4810158,123,{s_no},"
        f"{activity},Some work,GEN,Education,200000.00,200000,200000.00,XV Finance Commission,"
        f"200000,,,{total},05/07/2023,XVFC/2022-23/P/7,1028.00"
    )


def expenditure_run(tmp_path: Path, run_id: str, body: str) -> Path:
    with RunPublisher(tmp_path / "raw", "egramswaraj_expenditure", run_id) as publisher:
        publisher.write_payload(
            "expenditure_all.csv", f"﻿{EXPENDITURE_HEADER}\n{body}".encode(),
        )
        return publisher.publish()


def test_large_csv_rejects_a_record_wider_than_its_header(tmp_path: Path):
    """One unescaped comma shifts the money columns, and nothing raises (#49).

    `csv.DictReader` parks the overflow under the None key, which the lane's
    `if k` then discards -- so the record loads with `Total Expenditure`
    holding what belonged to `ST`, every later value one column across, and
    provenance that says the row is fine. The key columns sit ahead of the
    inserted comma, so the existing key check passes and the only visible
    effect is a reconciliation total that has quietly moved.
    """

    shifted = expenditure_row().replace("Some work", "Some, work")
    run = expenditure_run(tmp_path, "exp-wide", shifted)
    with pytest.raises(NormalizationError, match="27 field.s. against a 26-column header"):
        normalize_run(run, tmp_path / "canonical")


def test_large_csv_rejects_a_truncated_record(tmp_path: Path):
    """The other half: DictReader pads a short line with None rather than raising.

    The four key columns are all in the first eleven, so a line truncated
    after them establishes identity perfectly well and then publishes null
    money under it.
    """

    truncated = ",".join(expenditure_row().split(",")[:20])
    run = expenditure_run(tmp_path, "exp-short", truncated)
    with pytest.raises(NormalizationError, match="20 field.s. against a 26-column header"):
        normalize_run(run, tmp_path / "canonical")


def test_flat_csv_rejects_a_ragged_record(tmp_path: Path):
    """The same guard on the sibling lane, which had it too (#123).

    `_flatten_scalars` drops the overflow because it is a list, so a shifted
    profile row would publish one population figure under another's column.
    """

    run = profile_run(tmp_path, "profile-ragged", "115550,Anga,rbandha,120")
    with pytest.raises(NormalizationError, match="4 field.s. against a 3-column header"):
        normalize_run(run, tmp_path / "canonical")

    short = profile_run(tmp_path, "profile-short", "115550,Angarbandha")
    with pytest.raises(NormalizationError, match="2 field.s. against a 3-column header"):
        normalize_run(short, tmp_path / "canonical")


def test_large_csv_reports_the_peak_it_buffered_not_the_row_count(tmp_path: Path):
    """The streaming claim has to be checkable, and this is the only thing checking it.

    `max_buffered_rows` is what `scripts/benchmark_normalize.py` records and
    what #109 is measured against. Returning `total_rows` made the
    4,075,935-row production run report 4,075,935 rows buffered against a
    chunk_size of 100,000 -- a diagnostic that cannot fail, and so cannot
    detect the lane silently ceasing to stream.
    """

    run = expenditure_run(tmp_path, "exp-peak", "\n".join(
        expenditure_row(s_no=str(n), activity=f"441342{n:02d}") for n in range(1, 11)
    ))
    result = normalize_run(run, tmp_path / "canonical", chunk_size=3)

    assert result.tables["expenditure"], "the run published rows"
    assert result.max_buffered_rows == 3, (
        f"buffered {result.max_buffered_rows} rows against a chunk_size of 3"
    )


def accounting_run(tmp_path: Path, run_id: str, files_by_name: dict[str, int]) -> Path:
    """One accounting payload per name, carrying `n` payment vouchers each."""

    with RunPublisher(tmp_path / "raw", "egramswaraj_accounting", run_id) as publisher:
        for name, count in files_by_name.items():
            publisher.write_payload(name, json.dumps({
                "gp_name": "Test GP", "gp_lgd_code": "123", "state": "21",
                "district_name": "Deogarh", "district_code": "310",
                "block_name": "Barkote", "block_code": "3709",
                "year": "2022-2023", "status": "ok",
                "receipt_count": 0, "payment_count": count,
                "total_receipts": 0.0, "total_payments": 0.0, "opening_balance": 0.0,
                "receipts": [],
                "payments": [
                    {"month": "April", "date": "03/04/2022", "voucher_no": f"{name}-{n}",
                     "type": "Expenditures", "amount": 10.0, "voucher_id": str(n)}
                    for n in range(count)
                ],
            }).encode())
        return publisher.publish()


def test_accounting_reports_the_peak_it_buffered_not_the_row_count(tmp_path: Path):
    """The sibling streaming lane had the same defect, uncited (#129).

    This lane batches by file, so its peak is neither `total_rows` nor
    `chunk_size` -- it overshoots the bound by whatever the file that crossed
    it carried. That is the number worth reporting, and the reason the peak
    has to be observed rather than inferred from the configured chunk_size.
    """

    run = accounting_run(tmp_path, "acct-peak", {f"a/{n}.json": 3 for n in range(4)})
    result = normalize_run(run, tmp_path / "canonical", chunk_size=5)

    assert result.tables["voucher"], "the run published rows"
    assert result.max_buffered_rows == 6, (
        "two 3-voucher files cross a chunk_size of 5, so the peak is 6 -- "
        f"got {result.max_buffered_rows}"
    )


def test_accounting_rejects_a_payload_that_contradicts_its_own_count(tmp_path: Path):
    """A short or absent array publishes as complete, and nothing downstream can tell (#129).

    The canonical manifest verifies the rows that were emitted, so a payload
    declaring 3 payments and carrying 1 -- or carrying no `payments` key at
    all -- produces a valid, `complete` snapshot with the difference simply
    gone. The payload's own declared count is the only witness, which is why
    it has to be read rather than trusted.
    """

    def run(run_id: str, body: dict) -> Path:
        with RunPublisher(tmp_path / "raw", "egramswaraj_accounting", run_id) as publisher:
            publisher.write_payload("a/1.json", json.dumps({
                "gp_lgd_code": "123", "gp_name": "Test GP", "year": "2022-2023",
                "receipt_count": 0, "receipts": [], **body,
            }).encode())
            return publisher.publish()

    voucher = {"month": "April", "date": "03/04/2022", "voucher_no": "V1",
               "type": "Expenditures", "amount": 10.0, "voucher_id": "1"}

    short = run("acct-short", {"payment_count": 3, "payments": [voucher]})
    with pytest.raises(NormalizationError, match="declares 3 payment.s. but carries 1"):
        normalize_run(short, tmp_path / "canonical")

    # The absent-array case is the one the lane used to skip outright.
    absent = run("acct-absent", {"payment_count": 3})
    with pytest.raises(NormalizationError, match="declares 3 payment.s. but carries 0"):
        normalize_run(absent, tmp_path / "canonical")

    # A payload that agrees with itself still loads, including one that
    # genuinely carries nothing and says so.
    agrees = run("acct-ok", {"payment_count": 1, "payments": [voucher]})
    assert len(rows(files(normalize_run(agrees, tmp_path / "canonical"), "voucher")[0])) == 1
    empty = run("acct-empty", {"payment_count": 0})
    assert normalize_run(empty, tmp_path / "canonical").tables["voucher"]
