from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cli import main
from pipeline.manifest import ManifestError, RunPublisher, approve_run, validate_run
from pipeline.settings import load_settings
from pipeline.snapshots import load_snapshot_registry


def test_settings_are_validated_without_creating_directories(tmp_path: Path):
    raw = tmp_path / "raw"
    settings = load_settings(project_root=tmp_path, raw_root=raw)

    assert settings.raw_run_root("source", "run-1") == raw / "source" / "run-1"
    assert not raw.exists()
    with pytest.raises(ValueError):
        settings.raw_run_root("../source", "run-1")


def test_run_is_published_atomically_and_validated(tmp_path: Path):
    with RunPublisher(
        tmp_path / "raw",
        "synthetic",
        "run-1",
        code_sha="abc",
        config_hash="def",
        requested_scope={"limit": 2},
        parent_run_id="run-0",
        resume_id="resume-1",
        privacy_class="restricted",
    ) as publisher:
        publisher.write_payload("records.json", b'{"ok": true}\n')
        publisher.append_audit({"event": "fetch", "count": 1})
        run_path = publisher.publish(
            observed_scope={"pages": 1}, counts={"records": 1}
        )

    report = approve_run(run_path)
    manifest = json.loads((run_path / "manifest.json").read_text())
    assert report.checked_files == 2
    assert manifest["source"] == "synthetic"
    assert manifest["files"]["payloads/records.json"]["bytes"] == 13
    assert manifest["parent_run_id"] == "run-0"
    assert manifest["resume_id"] == "resume-1"
    assert not list(run_path.parent.glob("*.staging-*"))

    with pytest.raises(ManifestError, match="already exists"):
        with RunPublisher(tmp_path / "raw", "synthetic", "run-1") as publisher:
            publisher.publish()


def test_tampering_is_rejected_before_approval(tmp_path: Path):
    with RunPublisher(tmp_path / "raw", "synthetic", "run-2") as publisher:
        publisher.write_payload("records.json", b"original")
        run_path = publisher.publish()

    (run_path / "payloads" / "records.json").write_bytes(b"tampered")
    with pytest.raises(ManifestError, match="hash or byte count"):
        validate_run(run_path)


def test_failed_terminal_run_is_integrity_valid_but_not_approvable(tmp_path: Path):
    with RunPublisher(tmp_path / "raw", "synthetic", "run-3") as publisher:
        run_path = publisher.publish(terminal_state="failed", failures=["timeout"])

    assert validate_run(run_path)
    with pytest.raises(ManifestError, match="only a complete"):
        approve_run(run_path)


def test_snapshot_registry_has_only_approved_entries():
    registry = load_snapshot_registry(Path(__file__).parents[1] / "config" / "snapshots.yaml")
    assert registry.get("synthetic-v1").status == "approved"


def test_later_stages_fail_clearly_and_ingest_uses_tmp_path(tmp_path: Path, capsys):
    assert main(["normalize"]) == 2
    assert "not implemented" in capsys.readouterr().err

    payload = tmp_path / "payload.txt"
    payload.write_text("fixture", encoding="utf-8")
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "synthetic",
        "--run-id", "cli-run", "--payload", f"fixture.txt={payload}",
    ]) == 0
    run_path = tmp_path / "raw" / "synthetic" / "cli-run"
    assert validate_run(run_path)
