"""Regression tests for the eGramSwaraj flattener.

Every test builds its own synthetic payload; none touch data/ or the network.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from ingest.egramSwaraj_Flatten.extractor import (build_master, flatten_file,
                                                  master_path, process)
from ingest.egramSwaraj_Flatten.utils import parse_plan_year, to_records


def write_json(directory: Path, name: str, payload) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------- to_records


def test_envelope_key_is_unwrapped():
    payload = {"status": "OK", "data": [{"activityCd": "1"}, {"activityCd": "2"}]}
    assert to_records(payload) == [
        {"status": "OK", "activityCd": "1"},
        {"status": "OK", "activityCd": "2"},
    ]


def test_an_unrecognised_key_is_not_treated_as_an_envelope():
    """Only ENVELOPE_KEYS unwrap. An unknown key keeps its array as a child.

    The cost of guessing wrong in this direction is one extra table, which is
    visible and reversible. Guessing wrong the other way destroys the parent
    row and the relationship, silently. If the feed turns out to wrap records
    under this key, add it to ENVELOPE_KEYS after inspecting a payload.
    """
    payload = {"gp": "115550", "activities": [{"activityCd": "1"}]}

    assert to_records(payload) == [payload]


def test_single_record_with_sibling_arrays_is_kept_whole():
    """The bug: the longest array was treated as the record list and the rest dropped."""
    payload = {
        "activityCd": "A1",
        "fundList": [{"amount": 10}, {"amount": 20}],
        "assetDetails": [{"assetNm": "well"}],
    }
    records = to_records(payload)

    assert records == [payload]
    assert records[0]["assetDetails"] == [{"assetNm": "well"}]


def test_sibling_arrays_become_child_tables(tmp_path: Path):
    gp_dir = tmp_path / "LGD_115550_Angarbandha"
    path = write_json(gp_dir, "2021_PL.json", {
        "activityCd": "A1",
        "fundList": [{"amount": 10}, {"amount": 20}],
        "assetDetails": [{"assetNm": "well"}],
    })

    parent, children = flatten_file(path, "PL")

    assert len(parent) == 1
    assert set(children) == {"pl__fundlist", "pl__assetdetails"}
    assert len(children["pl__fundlist"]) == 2
    assert children["pl__assetdetails"]["assetNm"].tolist() == ["well"]


# --------------------------------------------------------------- plan_year


@pytest.mark.parametrize("name", ["2021_PL.json", "2021-PL.json"])
def test_both_filename_separators_give_the_same_plan_year(tmp_path: Path, name: str):
    assert parse_plan_year(tmp_path / name) == "2021"


@pytest.mark.parametrize("name", ["2021_PL.json", "2021-PL.json"])
def test_plan_year_and_row_id_match_across_separators(tmp_path: Path, name: str):
    gp_dir = tmp_path / name[:4] / "LGD_115550_Angarbandha"
    path = write_json(gp_dir, name, [{"activityCd": "A1"}])

    parent, _ = flatten_file(path, "PL")

    assert parent["plan_year"].tolist() == ["2021"]
    assert parent["row_id"].tolist() == ["115550|2021|PL|0"]


# --------------------------------------------------------------- mixed arrays


def test_mixed_array_records_source_positions(tmp_path: Path):
    """A scalar before an object must not shift the object's pos to 0."""
    gp_dir = tmp_path / "LGD_115550_Angarbandha"
    path = write_json(gp_dir, "2021_PL.json", [
        {"activityCd": "A1", "notes": ["a note", {"text": "structured"}]},
    ])

    parent, children = flatten_file(path, "PL")
    child = children["pl__notes"]

    assert child["pos"].tolist() == [1]
    assert child["row_id"].tolist() == ["115550|2021|PL|0/notes:1"]

    strays = json.loads(parent["notes_scalars"].iloc[0])
    assert strays == [{"pos": 0, "value": "a note"}]


# --------------------------------------------------------------- publication


def make_batch(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def master_csvs(output_dir: Path, kind: str) -> list[str]:
    return sorted(p.name for p in (output_dir / kind).glob("*.csv"))


def test_split_master_replaces_a_previous_unsplit_master(tmp_path: Path):
    batch = make_batch(tmp_path / "b1.csv", [{"row_id": str(i)} for i in range(4)])

    build_master([batch], tmp_path / "out", "PL", "pl", max_rows=10)
    assert master_csvs(tmp_path / "out", "PL") == ["egramswaraj_pl.csv"]

    build_master([batch], tmp_path / "out", "PL", "pl", max_rows=2)
    assert master_csvs(tmp_path / "out", "PL") == [
        "egramswaraj_pl_p001.csv", "egramswaraj_pl_p002.csv"]


def test_unsplit_master_removes_previous_parts(tmp_path: Path):
    batch = make_batch(tmp_path / "b1.csv", [{"row_id": str(i)} for i in range(4)])

    build_master([batch], tmp_path / "out", "PL", "pl", max_rows=2)
    build_master([batch], tmp_path / "out", "PL", "pl", max_rows=10)

    assert master_csvs(tmp_path / "out", "PL") == ["egramswaraj_pl.csv"]


def test_shrinking_run_removes_orphan_tail_parts(tmp_path: Path):
    six = make_batch(tmp_path / "b6.csv", [{"row_id": str(i)} for i in range(6)])
    two = make_batch(tmp_path / "b2.csv", [{"row_id": str(i)} for i in range(2)])

    build_master([six], tmp_path / "out", "PL", "pl", max_rows=2)
    assert len(master_csvs(tmp_path / "out", "PL")) == 3

    build_master([two], tmp_path / "out", "PL", "pl", max_rows=1)
    assert master_csvs(tmp_path / "out", "PL") == [
        "egramswaraj_pl_p001.csv", "egramswaraj_pl_p002.csv"]


def test_child_table_masters_are_not_clobbered_by_the_parent(tmp_path: Path):
    parent = make_batch(tmp_path / "p.csv", [{"row_id": "1"}])
    child = make_batch(tmp_path / "c.csv", [{"row_id": "1/fundlist:0"}])

    build_master([parent], tmp_path / "out", "PL", "pl")
    build_master([child], tmp_path / "out", "PL", "pl__fundlist")
    build_master([parent], tmp_path / "out", "PL", "pl")

    assert master_csvs(tmp_path / "out", "PL") == [
        "egramswaraj_pl.csv", "egramswaraj_pl__fundlist.csv"]


# --------------------------------------------------------------- rerun


def test_rerun_is_idempotent(tmp_path: Path):
    raw = tmp_path / "raw"
    write_json(raw / "LGD_115550_Angarbandha", "2021_PL.json",
               [{"activityCd": "A1", "fundList": [{"amount": 10}]}])
    out = tmp_path / "out"

    def run():
        process(raw, out, kinds=("PL",), keep_batches=False)
        return master_path(out, "PL", "pl").read_text(encoding="utf-8")

    first = run()
    assert run() == first
    assert master_csvs(out, "PL") == [
        "egramswaraj_pl.csv", "egramswaraj_pl__fundlist.csv"]


# --------------------------------------------------- envelope discipline
# Structure alone cannot distinguish an envelope from a domain record, so only
# a recognised key unwraps. These cover the second Codex review on PR #8.


def test_a_lone_child_array_stays_attached_to_its_record():
    """{"activityCd": ..., "fundList": [...]} is a record, not an envelope."""
    payload = {"activityCd": "A1", "fundList": [{"amount": 10}, {"amount": 20}]}

    assert to_records(payload) == [payload]


def test_a_lone_child_array_becomes_its_own_table(tmp_path: Path):
    gp_dir = tmp_path / "LGD_115550_Angarbandha"
    path = write_json(gp_dir, "2021_PL.json",
                      {"activityCd": "A1", "fundList": [{"amount": 10}]})

    parent, children = flatten_file(path, "PL")

    assert len(parent) == 1
    assert "pl__fundlist" in children
    assert children["pl__fundlist"]["amount"].tolist() == [10]


def test_an_empty_named_envelope_is_zero_records():
    """Otherwise an empty response becomes one fabricated row holding "[]"."""
    assert to_records({"status": "OK", "data": []}) == []


def test_an_empty_envelope_file_produces_no_rows(tmp_path: Path):
    gp_dir = tmp_path / "LGD_115550_Angarbandha"
    path = write_json(gp_dir, "2021_PL.json", {"status": "OK", "data": []})

    assert flatten_file(path, "PL") is None


def test_a_populated_named_envelope_still_unwraps():
    payload = {"status": "OK", "data": [{"activityCd": "1"}]}

    assert to_records(payload) == [{"status": "OK", "activityCd": "1"}]


# --------------------------------------------------- publication, continued


def test_splitting_disabled_publishes_an_unsuffixed_master(tmp_path: Path):
    batch = make_batch(tmp_path / "b.csv", [{"row_id": "1"}])

    build_master([batch], tmp_path / "out", "PL", "pl", max_rows=0)

    assert master_csvs(tmp_path / "out", "PL") == ["egramswaraj_pl.csv"]


def test_a_table_that_vanishes_leaves_no_master_behind(tmp_path: Path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    gp_dir = raw / "LGD_115550_Angarbandha"

    write_json(gp_dir, "2021_PL.json",
               [{"activityCd": "A1", "fundList": [{"amount": 10}]}])
    process(raw, out, kinds=("PL",))
    assert "egramswaraj_pl__fundlist.csv" in master_csvs(out, "PL")

    write_json(gp_dir, "2021_PL.json", [{"activityCd": "A1"}])
    process(raw, out, kinds=("PL",))

    assert master_csvs(out, "PL") == ["egramswaraj_pl.csv"]


def test_a_table_that_vanishes_leaves_no_batch_behind(tmp_path: Path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    gp_dir = raw / "LGD_115550_Angarbandha"

    write_json(gp_dir, "2021_PL.json",
               [{"activityCd": "A1", "fundList": [{"amount": 10}]}])
    process(raw, out, kinds=("PL",))

    write_json(gp_dir, "2021_PL.json", [{"activityCd": "A1"}])
    process(raw, out, kinds=("PL",))

    batches = sorted(p.name for p in (out / "PL" / "batches").glob("*.csv"))
    assert batches == ["pl__b0001.csv"]


def test_a_shorter_run_leaves_no_tail_batches(tmp_path: Path):
    """Numbering restarts at 1, so old high-numbered batches must not survive."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    for i in range(3):
        write_json(raw / f"LGD_11555{i}_Gp{i}", "2021_PL.json",
                   [{"activityCd": f"A{i}"}])
    process(raw, out, kinds=("PL",), batch_size=1)
    assert len(list((out / "PL" / "batches").glob("pl__b*.csv"))) == 3

    for i in range(1, 3):
        (raw / f"LGD_11555{i}_Gp{i}" / "2021_PL.json").unlink()
    process(raw, out, kinds=("PL",), batch_size=1)

    assert sorted(p.name for p in (out / "PL" / "batches").glob("pl__b*.csv")) \
        == ["pl__b0001.csv"]
