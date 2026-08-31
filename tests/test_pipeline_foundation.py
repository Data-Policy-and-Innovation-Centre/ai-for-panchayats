from __future__ import annotations

import io
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
    assert load_snapshot_registry(Path(__file__).parents[1] / "config" / "snapshots.yaml").snapshots == ()


def test_file_like_payloads_are_streamed_in_bounded_chunks(tmp_path: Path):
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert size != -1
            return super().read(size)

    with RunPublisher(tmp_path / "raw", "synthetic", "stream") as publisher:
        publisher.write_payload("stream.bin", BoundedStream(b"streamed"))
        path = publisher.publish()
    assert (path / "payloads" / "stream.bin").read_bytes() == b"streamed"


def test_write_payload_removes_partial_temp_file_after_stream_failure(tmp_path: Path):
    """A stream that raises mid-read must not leave a partial temp file.

    Codex review (PR #64, manifest.py:281): the temp file is created with
    delete=False; if the file-like payload's read() raises after yielding
    some chunks and the caller catches the exception, the old code left that
    partial temp file behind in payloads/. publish() would then inventory
    and publish it under its random temp name, corrupting the run.
    """

    class FailingStream(io.BytesIO):
        def read(self, size=-1):
            chunk = super().read(size)
            if chunk == b"":
                return chunk
            if self.tell() > 4:
                raise OSError("synthetic mid-stream failure")
            return chunk

    with RunPublisher(tmp_path / "raw", "synthetic", "fail-stream") as publisher:
        staging_payloads = list((tmp_path / "raw" / "synthetic").glob(".fail-stream.staging-*"))[0] / "payloads"
        with pytest.raises(OSError, match="synthetic mid-stream failure"):
            publisher.write_payload("stream.bin", FailingStream(b"streamed-data"))
        # No stray temp file left behind in payloads/.
        assert list(staging_payloads.iterdir()) == []
        run_path = publisher.publish()

    # The partial payload never made it into the published, hash-verified run.
    assert list((run_path / "payloads").iterdir()) == []
    assert validate_run(run_path)


def test_snapshot_registry_resolves_an_exact_run_id(tmp_path: Path):
    registry_file = tmp_path / "snapshots.yaml"
    registry_file.write_text(
        "version: 1\nsnapshots:\n  - id: fixture\n    source: source\n"
        "    run_id: run-2026-01\n    schema_version: '1'\n    status: approved\n",
        encoding="utf-8",
    )
    assert load_snapshot_registry(registry_file).get("fixture").run_id == "run-2026-01"


def test_later_stages_fail_clearly_and_ingest_uses_tmp_path(tmp_path: Path, capsys):
    assert main(["build"]) == 2
    assert "not implemented" in capsys.readouterr().err

    payload = tmp_path / "payload.txt"
    payload.write_text("fixture", encoding="utf-8")
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "synthetic",
        "--run-id", "cli-run", "--payload", f"fixture.txt={payload}",
    ]) == 0
    run_path = tmp_path / "raw" / "synthetic" / "cli-run"
    assert validate_run(run_path)
