from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from pipeline.manifest import RunPublisher
from pipeline.normalize import (
    NormalizationError,
    normalize_egramswaraj,
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


def test_union_schema_is_stable_across_chunk_boundaries(tmp_path: Path):
    run = make_run(tmp_path, "schema", {
        "2021_PL.json": [{"id": "1", "amount": 1}, {"id": "2", "amount": "two"}],
    })
    result = normalize_egramswaraj(run, tmp_path / "canonical", chunk_size=1)
    table = pq.read_table(files(result, "pl")[0].parent)
    assert str(table.schema.field("amount").type) == "string"
    assert [row["amount"] for row in table.to_pylist()] == ["1", "two"]
    assert result.max_buffered_rows <= 1
