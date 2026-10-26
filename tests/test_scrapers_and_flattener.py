"""Synthetic regression tests for PAI scraper, Panchayat Profile, and eGramSwaraj flattener."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from src.eGramSwaraj_panchayat_profile.scrapper import scrape_panchayat_info
from src.ingest.PAI.extractor import scrape_all
from src.ingest.PAI.utils import has_next_page
from src.ingest.egramSwaraj_Flatten.extractor import flatten_file, process
from src.pipeline.manifest import validate_run

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. PAI pagination disabled attribute checks
# ---------------------------------------------------------------------------

def test_has_next_page_handles_disabled_boolean_attribute():
    # Boolean disabled attribute without value
    soup1 = BeautifulSoup('<input id="btnNext" disabled>', "html.parser")
    assert has_next_page(soup1) is False

    # Disabled attribute with explicit "disabled" value
    soup2 = BeautifulSoup('<input id="btnNext" disabled="disabled">', "html.parser")
    assert has_next_page(soup2) is False

    # Enabled button
    soup3 = BeautifulSoup('<input id="btnNext">', "html.parser")
    assert has_next_page(soup3) is True

    # Missing button
    soup4 = BeautifulSoup("<div><p>No button here</p></div>", "html.parser")
    assert has_next_page(soup4) is False


# ---------------------------------------------------------------------------
# 2. PAI RunPublisher and failure retention
# ---------------------------------------------------------------------------

def test_pai_scrape_all_failed_block_does_not_overwrite_good_run(tmp_path: Path):
    raw_root = tmp_path / "raw"
    canonical_output_dir = tmp_path / "canonical"
    canonical_file = canonical_output_dir / "odisha_pai_scores.json"
    canonical_output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-existing good output
    existing_content = {"metadata": {"status": "complete"}, "data": [{"gp_name": "Good GP"}]}
    canonical_file.write_text(json.dumps(existing_content), encoding="utf-8")

    with (
        patch("src.ingest.PAI.extractor.OUTPUT_DIR", canonical_output_dir),
        patch("src.ingest.PAI.extractor.create_session"),
        patch("src.ingest.PAI.extractor.get_districts", return_value=[{"id": "1", "name": "Dist 1"}]),
        patch("src.ingest.PAI.extractor.get_blocks", return_value=[{"id": "101", "name": "Block 101"}]),
        patch("src.ingest.PAI.extractor.scrape_block", side_effect=TimeoutError("Connection timed out")),
    ):
        run_dest = scrape_all(raw_root=raw_root, run_id="test_pai_failure_run")

    report = validate_run(run_dest)
    manifest = report.manifest
    assert manifest.terminal_state == "failed"
    assert len(manifest.failures) == 1
    assert manifest.failures[0]["block_name"] == "Block 101"

    # Verify existing canonical file was NOT overwritten or emptied
    current_content = json.loads(canonical_file.read_text(encoding="utf-8"))
    assert current_content == existing_content


# ---------------------------------------------------------------------------
# 3. Panchayat profile scraper empty/invalid section handling
# ---------------------------------------------------------------------------

def test_panchayat_profile_rejects_empty_sections():
    # Only <h6> tag without tables
    html_h6_only = "<html><body><h6>Panchayat At a Glance</h6></body></html>"
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = html_h6_only
        result = scrape_panchayat_info("21", "GP", "1", "11", "111")
        assert result is None

    # Single-cell login/error message
    html_login_error = "<html><body><table><tr><td>Session Expired, please login again</td></tr></table></body></html>"
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = html_login_error
        result = scrape_panchayat_info("21", "GP", "1", "11", "111")
        assert result is None


# ---------------------------------------------------------------------------
# 4. Flattener nested object with array preservation
# ---------------------------------------------------------------------------

def test_flattener_preserves_nested_objects_with_arrays(tmp_path: Path):
    gp_dir = tmp_path / "LGD_123_TestGP"
    gp_dir.mkdir(parents=True)
    json_file = gp_dir / "2021_PL.json"

    payload = [{"activityCd": 7, "details": {"funds": [{"amount": 10}]}}]
    json_file.write_text(json.dumps(payload), encoding="utf-8")

    result = flatten_file(json_file, "PL")
    assert result is not None
    parent_df, children = result

    # Check that the nested array was unpacked into a child table
    child_table_names = list(children.keys())
    assert any("funds" in name for name in child_table_names)
    funds_table = next(children[n] for n in child_table_names if "funds" in n)
    assert not funds_table.empty
    assert "amount" in funds_table.columns
    assert funds_table["amount"].iloc[0] == 10


# ---------------------------------------------------------------------------
# 5. Flattener two-run child table retirement
# ---------------------------------------------------------------------------

def test_flattener_retires_disappeared_child_tables(tmp_path: Path):
    in_dir = tmp_path / "input"
    out_dir = tmp_path / "output"
    gp_dir = in_dir / "LGD_123_TestGP"
    gp_dir.mkdir(parents=True)
    json_file = gp_dir / "2021_PL.json"

    # Run 1: has funds array -> creates pl__funds table
    payload_run1 = [{"activityCd": 7, "funds": [{"amount": 10}]}]
    json_file.write_text(json.dumps(payload_run1), encoding="utf-8")

    process(input_dir=in_dir, output_dir=out_dir, kinds=("PL",), master=True)
    pl_dir = out_dir / "PL"
    funds_csv = pl_dir / "egramswaraj_pl__funds.csv"
    assert funds_csv.exists()

    # Run 2: funds array is now empty -> should retire pl__funds table
    payload_run2 = [{"activityCd": 7, "funds": []}]
    json_file.write_text(json.dumps(payload_run2), encoding="utf-8")

    process(input_dir=in_dir, output_dir=out_dir, kinds=("PL",), master=True)
    assert not funds_csv.exists(), "Obsolete child table pl__funds.csv was not retired on rerun"


# ---------------------------------------------------------------------------
# 6. CLI entrypoint isolated execution without PYTHONPATH
# ---------------------------------------------------------------------------

def test_run_egram_flatten_cli_runs_without_pythonpath():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    script = REPO_ROOT / "scripts/run_egram_flatten.py"
    res = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=30,
    )
    assert "ModuleNotFoundError" not in res.stderr
    assert res.returncode == 0
