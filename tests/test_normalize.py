from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from pipeline.manifest import RunPublisher
from pipeline.normalize import (
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
    import pipeline.normalize as module

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
