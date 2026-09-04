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


def test_truncated_snapshot_missing_a_kind_is_rejected(tmp_path: Path):
    """kinds={"PL"} produces a manifest with no aa/ta/pp/re entry at all --
    a truncated build, not a real dataset that happened to be empty."""

    run = publish_raw_run(tmp_path, "run-1", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    settings = make_settings(tmp_path)
    normalize(run, settings.canonical_root, chunk_size=100, kinds={"PL"})
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    with pytest.raises(SelectionError, match="missing required source-kind"):
        resolve_snapshots(settings, ("snap-1",), registry=spec_registry)


def test_two_partial_snapshots_whose_union_is_complete_are_accepted(tmp_path: Path):
    """Completeness is a property of the BUILD, not of every contributor.

    The kind check used to sit inside the per-snapshot loop, which read as
    "each snapshot must carry all five kinds". That is stronger than the
    design intends -- `build_warehouse.py` accepts repeated --snapshot-id and
    `build._merge_gram_panchayat` merges contributions across snapshots -- and
    it made a single-source snapshot illegal. Adding any new kind (gp_profile
    for #123, voucher for #129) would then have retroactively invalidated
    every snapshot already approved, including the full-state one production
    is about to serve.

    The split here is along a legal line: `aa`/`ta`/`pp` stay with the `pl`
    they are filtered against (see the co-location guard below), and `re`,
    which is filtered against nothing, travels alone.
    """

    run_a = publish_raw_run(tmp_path, "run-a", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    run_b = publish_raw_run(tmp_path, "run-b", {
        "LGD_123_Test_GP/2021_RE.json": {"data": [{"planYear": "2021"}]},
    })
    settings = make_settings(tmp_path)
    normalize(run_a, settings.canonical_root, chunk_size=100, kinds={"PL", "AA", "TA", "PP"})
    normalize(run_b, settings.canonical_root, chunk_size=100, kinds={"RE"})
    spec_registry = registry(
        approved("snap-a", "egramSwaraj", "run-a"),
        approved("snap-b", "egramSwaraj", "run-b"),
    )

    # Neither is complete alone -- proven, not assumed, so this cannot pass by
    # both snapshots happening to carry everything.
    with pytest.raises(SelectionError, match="missing required source-kind"):
        resolve_snapshots(settings, ("snap-a",), registry=spec_registry)
    with pytest.raises(SelectionError, match="missing required source-kind"):
        resolve_snapshots(settings, ("snap-b",), registry=spec_registry)

    resolved = resolve_snapshots(settings, ("snap-a", "snap-b"), registry=spec_registry)
    assert len(resolved) == 2
    covered = {name for snapshot in resolved for name in snapshot.tables}
    assert {"pl", "aa", "ta", "pp", "re"} <= covered


def test_a_selection_whose_union_is_still_incomplete_is_rejected(tmp_path: Path):
    """The guarantee is unchanged: a build cannot be missing a source kind.

    Two snapshots that are each individually legal but between them still lack
    `re` must fail, or lifting the check out of the loop would have weakened
    it into nothing.
    """

    run_a = publish_raw_run(tmp_path, "run-a", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    run_b = publish_raw_run(tmp_path, "run-b", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 8, "totalCost": 100}]},
    })
    settings = make_settings(tmp_path)
    normalize(run_a, settings.canonical_root, chunk_size=100, kinds={"PL", "AA", "TA", "PP"})
    normalize(run_b, settings.canonical_root, chunk_size=100, kinds={"PL"})
    spec_registry = registry(
        approved("snap-a", "egramSwaraj", "run-a"),
        approved("snap-b", "egramSwaraj", "run-b"),
    )
    with pytest.raises(SelectionError, match="missing required source-kind"):
        resolve_snapshots(settings, ("snap-a", "snap-b"), registry=spec_registry)


def test_approvals_without_their_own_pl_are_refused_not_quarantined(tmp_path: Path):
    """The co-location guard, and why it is a guard rather than a feature.

    `build.populate` walks snapshots one at a time and filters approvals and
    progress against the activities THIS snapshot's `pl` produced. A snapshot
    carrying `aa`/`ta`/`pp` without a `pl` therefore has every one of those
    rows quarantined as an orphan -- and quarantine is not a build failure, so
    the warehouse would publish with those facts silently missing.

    Lifting the per-snapshot kind check (above) made that arrangement legal to
    select for the first time, so it has to be refused explicitly. Failing at
    selection, before any DuckDB file is touched, is the loud version of a
    hole that is otherwise silent. Making the loader order-independent instead
    is #161.
    """

    run_a = publish_raw_run(tmp_path, "run-a", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    run_b = publish_raw_run(tmp_path, "run-b", {
        "LGD_123_Test_GP/2021_AA.json": {"data": [{"activityCd": 7}]},
    })
    settings = make_settings(tmp_path)
    normalize(run_a, settings.canonical_root, chunk_size=100, kinds={"PL", "TA", "PP", "RE"})
    normalize(run_b, settings.canonical_root, chunk_size=100, kinds={"AA"})
    spec_registry = registry(
        approved("snap-a", "egramSwaraj", "run-a"),
        approved("snap-b", "egramSwaraj", "run-b"),
    )
    with pytest.raises(SelectionError, match="no 'pl' rows"):
        resolve_snapshots(settings, ("snap-a", "snap-b"), registry=spec_registry)


def test_approvals_with_a_declared_but_empty_pl_are_refused_too(tmp_path: Path):
    """Presence is not the test; rows are.

    The normalizer writes a schema-correct EMPTY table for every kind that
    was requested, so a snapshot scraped as `--kinds PL,AA` that found no
    plans still declares `pl`. A presence-only guard waves that through, and
    `populate` then derives an empty activity_codes set from it and
    quarantines every approval -- the exact outcome the guard exists to
    prevent, reached by a different route.

    The sibling case is asserted in the same test on purpose: a *declared but
    empty* aa has no rows to orphan, so tightening this must not start
    refusing it. A guard that fires on both pins nothing.
    """

    run_a = publish_raw_run(tmp_path, "run-a", {
        "LGD_123_Test_GP/2021_PL.json": {"data": [{"activityCd": 7, "totalCost": 100}]},
    })
    # `pl` is requested here and yields nothing: declared, empty, and useless
    # to the `aa` rows alongside it.
    run_b = publish_raw_run(tmp_path, "run-b", {
        "LGD_123_Test_GP/2021_AA.json": {"data": [{"activityCd": 7}]},
    })
    run_c = publish_raw_run(tmp_path, "run-c", {
        "LGD_123_Test_GP/2021_TA.json": {"data": []},
    })
    settings = make_settings(tmp_path)
    normalize(run_a, settings.canonical_root, chunk_size=100, kinds={"PL", "TA", "PP", "RE"})
    normalize(run_b, settings.canonical_root, chunk_size=100, kinds={"PL", "AA"})
    normalize(run_c, settings.canonical_root, chunk_size=100, kinds={"PL", "TA", "AA"})
    spec_registry = registry(
        approved("snap-a", "egramSwaraj", "run-a"),
        approved("snap-b", "egramSwaraj", "run-b"),
        approved("snap-c", "egramSwaraj", "run-c"),
    )
    with pytest.raises(SelectionError, match=r"snapshot 'snap-b'.*no 'pl' rows"):
        resolve_snapshots(settings, ("snap-a", "snap-b"), registry=spec_registry)

    # snap-c: empty pl AND empty aa/ta. Nothing to orphan, so it must resolve.
    assert len(resolve_snapshots(settings, ("snap-a", "snap-c"), registry=spec_registry)) == 2


def test_snapshot_with_all_kinds_explicitly_empty_is_still_accepted(tmp_path: Path):
    """A kind requested but producing zero rows is not the same as a kind
    never requested -- this must still resolve cleanly."""

    settings = _minimal_pl_run(tmp_path)
    spec_registry = registry(approved("snap-1", "egramSwaraj", "run-1"))
    resolved = resolve_snapshots(settings, ("snap-1",), registry=spec_registry)
    assert len(resolved) == 1


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
