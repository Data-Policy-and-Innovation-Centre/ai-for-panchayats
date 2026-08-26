from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from pipeline.manifest import RunPublisher
from pipeline.normalize import normalize_egramswaraj


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
    assert all(row["gp_code"] == "123" and row["fiscal_year"] == "2021" for row in fund)
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
    assert {row["fiscal_year"] for row in child} == {"2021"}


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
    assert "pl" not in result.tables
    assert {kind for kind in ("aa", "ta", "pp", "re") if kind in result.tables} == {
        "aa", "ta", "pp", "re"
    }
    assert rows(files(result, "re")[0])[0]["mapping_status"] == "unmapped"
    quarantine = rows(files(result, "quarantine")[0])
    assert {row["reason_code"] for row in quarantine} == {
        "malformed_known_envelope", "malformed_json"
    }


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
    assert rows(files(result, "pl")[0])[0]["business_id"] == "2"


def test_failed_second_publication_preserves_previous_output(tmp_path: Path, monkeypatch):
    first = make_run(tmp_path, "run-good", {"2021_PL.json": [{"id": "good"}]})
    normalize_egramswaraj(first, tmp_path / "canonical")
    old = rows(next((tmp_path / "canonical" / "pl").rglob("*.parquet")))

    second = make_run(tmp_path, "run-failing", {"2021_PL.json": [{"id": "new"}]})
    import pipeline.normalize as module

    def fail(*args, **kwargs):
        raise OSError("synthetic parquet failure")

    monkeypatch.setattr(module.pq, "write_table", fail)
    with pytest.raises(OSError, match="synthetic parquet failure"):
        normalize_egramswaraj(second, tmp_path / "canonical")
    assert rows(next((tmp_path / "canonical" / "pl").rglob("*.parquet"))) == old
    assert not list(tmp_path.glob(".canonical.staging-*"))
