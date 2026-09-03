from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.pipeline.cli import main
from src.pipeline.manifest import ManifestError, RunPublisher, approve_run, validate_run
from src.pipeline.settings import load_settings
from src.pipeline.snapshots import load_snapshot_registry


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


# --------------------------------------------------------------------- --payload-tree


def _scraper_tree(root: Path, gps=("LGD_115550_Angarbandha", "LGD_115551_Antula")) -> Path:
    """The shape scripts/run_egram_scraper.py actually writes."""

    for gp in gps:
        (root / gp).mkdir(parents=True, exist_ok=True)
        for kind in ("PL", "AA", "TA", "PP", "RE"):
            (root / gp / f"2021_{kind}.json").write_text(json.dumps({"data": []}))
    return root


def test_payload_tree_publishes_a_whole_directory(tmp_path: Path):
    """The gap that made the pipeline unrunnable: --payload takes one flag per
    file, and a full scrape is ~204,000 files."""

    tree = _scraper_tree(tmp_path / "tree")
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tree),
    ]) == 0

    run = tmp_path / "raw" / "egramSwaraj" / "run-1"
    published = sorted(
        str(p.relative_to(run / "payloads"))
        for p in (run / "payloads").rglob("*") if p.is_file()
    )
    assert len(published) == 10
    # Keyed by path relative to the tree root, so the LGD_<code>_<name> parent
    # survives -- normalize._gp_context reads the GP code from exactly there.
    assert "LGD_115550_Angarbandha/2021_PL.json" in published
    validate_run(run)


def test_payload_tree_records_the_file_count_in_the_audit_log(tmp_path: Path):
    """A run that published 203,812 of an expected 203,820 files is only
    answerable if the number it wrote was recorded."""

    tree = _scraper_tree(tmp_path / "tree")
    main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tree),
    ])
    audit = [
        json.loads(line)
        for line in (tmp_path / "raw" / "egramSwaraj" / "run-1" / "audit.jsonl")
        .read_text().splitlines() if line.strip()
    ]
    assert audit[0]["payload_files"] == 10
    assert audit[0]["payload_trees"] == [str(tree.resolve())]


def test_payload_tree_skips_symlinks_but_never_silently(tmp_path: Path, capsys):
    """A manifest is an inventory of real bytes, so links are not followed --
    but a skipped link is data that did not arrive, and must be reported."""

    tree = _scraper_tree(tmp_path / "tree")
    (tree / "LGD_115550_Angarbandha" / "link.json").symlink_to(
        tree / "LGD_115551_Antula" / "2021_PL.json"
    )
    main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tree),
    ])
    assert "skipped 1 symlink" in capsys.readouterr().err
    audit = json.loads(
        (tmp_path / "raw" / "egramSwaraj" / "run-1" / "audit.jsonl").read_text().splitlines()[0]
    )
    assert audit["skipped_symlinks"] == ["LGD_115550_Angarbandha/link.json"]


def test_payload_tree_combines_with_individual_payloads(tmp_path: Path):
    tree = _scraper_tree(tmp_path / "tree")
    extra = tmp_path / "extra.json"
    extra.write_text("{}")
    main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tree),
        "--payload", f"notes/extra.json={extra}",
    ])
    payloads = tmp_path / "raw" / "egramSwaraj" / "run-1" / "payloads"
    assert (payloads / "notes" / "extra.json").is_file()
    assert (payloads / "LGD_115550_Angarbandha" / "2021_PL.json").is_file()


def test_colliding_payload_paths_are_refused_not_silently_overwritten(tmp_path: Path, capsys):
    """Two trees sharing a relative path must not let one quietly win.

    ``write_payload`` finishes with ``os.replace``, so the second write used
    to take the target and the first file's bytes simply left the run -- while
    the audit log still counted both. The manifest would then inventory fewer
    files than the run claimed to have published, with a green exit to show
    for it. A raw run publishes each path exactly once.
    """

    first = _scraper_tree(tmp_path / "a", gps=("LGD_115550_Angarbandha",))
    second = _scraper_tree(tmp_path / "b", gps=("LGD_115550_Angarbandha",))
    (second / "LGD_115550_Angarbandha" / "2021_PL.json").write_text('{"data": ["different"]}')

    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(first), "--payload-tree", str(second),
    ]) == 2
    assert "publishes each path once" in capsys.readouterr().err
    # The staging directory is discarded, so no half-run is left behind.
    assert not (tmp_path / "raw" / "egramSwaraj" / "run-1").exists()


def test_a_payload_colliding_with_a_tree_path_is_refused(tmp_path: Path, capsys):
    """The sibling path: --payload and --payload-tree write into the same
    namespace, so the collision is not limited to two trees."""

    tree = _scraper_tree(tmp_path / "tree", gps=("LGD_115550_Angarbandha",))
    extra = tmp_path / "extra.json"
    extra.write_text("{}")
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1",
        "--payload", f"LGD_115550_Angarbandha/2021_PL.json={extra}",
        "--payload-tree", str(tree),
    ]) == 2
    assert "publishes each path once" in capsys.readouterr().err


def test_payload_tree_rejects_a_missing_root(tmp_path: Path, capsys):
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tmp_path / "absent"),
    ]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert not (tmp_path / "raw" / "egramSwaraj" / "run-1").exists()


def test_payload_tree_rejects_an_empty_root(tmp_path: Path, capsys):
    """Publishing an empty run silently is worse than refusing it: the build
    would reject it much later as a 'partial normalization', a long way from
    the cause."""

    (tmp_path / "empty").mkdir()
    assert main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "run-1", "--payload-tree", str(tmp_path / "empty"),
    ]) == 2
    assert "contains no files" in capsys.readouterr().err
    # Nothing was published: the staging directory is renamed into place only
    # on a clean exit from the context manager.
    assert not (tmp_path / "raw" / "egramSwaraj" / "run-1").exists()


# --------------------------------------------------------------------- registry stanza


def test_normalize_prints_a_pasteable_registry_stanza(tmp_path: Path, capsys):
    """config/snapshots.yaml is `snapshots: []`, and resolve_snapshots has no
    "build everything" mode, so a snapshot nobody registered can never be
    built. Getting the fields right is mechanical; approving it is not."""

    from src.pipeline.snapshots import load_snapshot_registry

    tree = _scraper_tree(tmp_path / "tree")
    main([
        "ingest", "--raw-root", str(tmp_path / "raw"), "--source", "egramSwaraj",
        "--run-id", "2026-09-03", "--payload-tree", str(tree),
    ])
    capsys.readouterr()
    main([
        "normalize", "--run-path", str(tmp_path / "raw" / "egramSwaraj" / "2026-09-03"),
        "--output-root", str(tmp_path / "canonical"), "--chunk-size", "100",
    ])
    out = capsys.readouterr().out
    stanza = out.split("snapshots:` --\n\n", 1)[1].rstrip()

    # The point of the stanza is that pasting it produces a *valid* registry.
    registry_path = tmp_path / "snapshots.yaml"
    registry_path.write_text(f"version: 1\nsnapshots:\n{stanza}\n")
    spec, = load_snapshot_registry(registry_path).snapshots
    assert spec.source == "egramSwaraj"
    assert spec.run_id == "2026-09-03"
    assert spec.schema_version == "1"
    assert spec.status == "approved"
