"""Preflight snapshot selection: approval, hashes, schema version, identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from warehouse.select import SelectionError, resolve_snapshots

from _warehouse_helpers import (
    approved, make_settings, normalize, publish_raw_run, registry, write_manual_snapshot,
)


def _minimal_pl_run(tmp_path: Path, run_id: str = "run-1"):
    run = publish_raw_run(tmp_path, run_id, {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100)
    return settings


def test_unapproved_snapshot_cannot_even_enter_the_registry(tmp_path: Path):
    """The registry itself is the first line of defense: it refuses to hold
    anything but an approved snapshot, so ``resolve_snapshots`` never sees an
    unapproved one through a validly constructed registry. The status check
    in ``resolve_snapshots`` is defense in depth for a ``SnapshotSpec`` built
    outside that registry; this test documents why it is normally unreachable.
    """

    from src.pipeline.snapshots import SnapshotRegistry, SnapshotRegistryError, SnapshotSpec
    with pytest.raises(SnapshotRegistryError, match="only approved"):
        SnapshotRegistry((SnapshotSpec("snap-1", "egramSwaraj", "run-1", "1", "pending"),))


def test_resolve_snapshots_still_checks_status_defensively(tmp_path: Path, monkeypatch):
    """Exercise ``resolve_snapshots``' own status check directly, in case a
    future registry implementation stops enforcing "approved-only" itself.
    """

    from src.pipeline.snapshots import SnapshotSpec
    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    monkeypatch.setattr(
        spec_registry, "get",
        lambda snapshot_id: SnapshotSpec("snap-1", "egramSwaraj", "run-1", "1", "pending"),
    )
    with pytest.raises(SelectionError, match="not approved"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_unknown_snapshot_id_is_rejected(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    with pytest.raises(Exception):
        resolve_snapshots(settings, ("does-not-exist",), registry=spec_registry)


def test_missing_snapshot_directory_is_rejected(tmp_path: Path):
    settings = make_settings(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-missing"))
    with pytest.raises(SelectionError, match="missing on disk"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_hash_mismatch_is_rejected(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    snapshot_root = settings.snapshot_root("egramSwaraj", "run-1")
    pl_dir = snapshot_root / "pl"
    part_files = list(pl_dir.rglob("*.parquet"))
    assert part_files
    # Tamper the published Parquet bytes after the fact.
    with part_files[0].open("r+b") as handle:
        handle.seek(0)
        handle.write(b"\x00" * 8)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    with pytest.raises(SelectionError, match="manifest validation"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_declared_row_count_mismatch_is_rejected(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    snapshot_root = settings.snapshot_root("egramSwaraj", "run-1")
    manifest_path = snapshot_root / "canonical_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"]["pl"]["row_count"] += 1
    manifest_path.write_text(json.dumps(manifest))
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    with pytest.raises(SelectionError, match="manifest validation"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_schema_version_mismatch_is_rejected(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1", schema_version="2"))
    with pytest.raises(SelectionError, match="schema_version mismatch"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_snapshot_declaring_unsupported_schema_version_is_rejected(tmp_path: Path):
    """Registry and manifest can agree on a version and still be wrong: both
    might declare a newer schema (e.g. ``"2"``) that this warehouse build --
    pinned to ``WarehouseSettings.schema_version`` (``"1"``) -- does not know
    how to consume. Without this check that agreement alone was enough to
    pass selection, letting version-1 transforms misread a version-2 layout.
    """

    settings = make_settings(tmp_path)
    write_manual_snapshot(
        settings.canonical_root, source="othersystem", run_id="run-x", schema_version="2",
        tables={},
    )
    spec_registry = registry(approved("snap-x", "othersystem", "run-x", schema_version="2"))
    with pytest.raises(SelectionError, match="unsupported schema_version"):
        resolve_snapshots(settings, ("snap-x",), registry=spec_registry)


def test_duplicate_source_run_selection_is_rejected(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(
        approved("snap-1", "egramSwaraj", "run-1"),
        approved("snap-1-again", "egramSwaraj", "run-1"),
    )
    with pytest.raises(SelectionError, match="duplicate"):
        resolve_snapshots(settings, ("snap-1", "snap-1-again"), registry=spec_registry)


def test_unrecognized_top_level_dataset_is_rejected(tmp_path: Path):
    settings = make_settings(tmp_path)
    write_manual_snapshot(
        settings.canonical_root, source="othersystem", run_id="run-x",
        tables={"xx": [{"foo": "bar"}]},
    )
    spec_registry = registry(approved("snap-x", "othersystem", "run-x"))
    with pytest.raises(SelectionError, match="unrecognized dataset"):
        resolve_snapshots(settings, ("snap-x",), registry=spec_registry)


def test_valid_selection_resolves_and_revalidates(tmp_path: Path):
    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    resolved = resolve_snapshots(settings, ("snap-1",), registry=spec_registry)
    assert len(resolved) == 1
    assert resolved[0].spec.run_id == "run-1"
    assert "pl" in resolved[0].tables


def test_empty_selection_returns_nothing(tmp_path: Path):
    settings = make_settings(tmp_path)
    spec_registry = registry()
    assert resolve_snapshots(settings, (), registry=spec_registry) == ()
