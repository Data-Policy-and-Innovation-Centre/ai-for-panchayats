"""Preflight: resolve and validate the exact snapshots a build may consume.

Nothing here is a shortcut around ``validate_canonical_manifest``
(``pipeline.normalize``): every snapshot the warehouse touches is
re-validated immediately before use (hash, size, and Parquet-footer row
counts, per the upstream gate) rather than trusted because it was validated
once at normalization time. The build must also never silently reach for a
snapshot that was not explicitly selected, and never silently ignore a
declared table it does not recognize.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.pipeline.normalize import NormalizationError, validate_canonical_manifest
from src.pipeline.snapshots import (
    SnapshotRegistry,
    SnapshotRegistryError,
    SnapshotSpec,
    load_snapshot_registry,
)

from .config import WarehouseSettings
from .load import discover_tables
from .schema import KIND_TABLES

KNOWN_KIND_PREFIXES = tuple(kind.lower() for kind in KIND_TABLES)

# Kinds whose transforms filter their rows against the set of activities the
# snapshot's own `pl` produced: an approval or progress row referencing an
# activity that is not in that set is quarantined as an orphan.
#
# They must therefore travel WITH the `pl` they reference. `build.populate`
# walks snapshots one at a time and derives that set from the snapshot in
# hand, so a snapshot carrying these without a `pl` has every row of them
# quarantined -- and quarantine is not a build failure, so the warehouse
# would publish with those facts silently missing.
#
# Refused here rather than supported, deliberately. Making the loader
# order-independent means loading every snapshot's `pl` before transforming
# any dependent snapshot, which restructures the loop that builds the whole
# warehouse; that is #161. Nothing needs it yet: #123 and #129 each add a
# NEW kind in its own snapshot, which this permits.
DEPENDENT_KINDS: tuple[str, ...] = ("aa", "ta", "pp")
PLANNING_KIND = "pl"


class SelectionError(ValueError):
    """Raised when a requested snapshot selection cannot be safely built."""


@dataclass(frozen=True, slots=True)
class SelectedSnapshot:
    spec: SnapshotSpec
    snapshot_root: Path
    manifest: Mapping[str, object]
    tables: dict[str, tuple[str, ...]]


def _classify(table_name: str) -> str:
    if table_name == "quarantine":
        return "quarantine"
    kind = table_name.split("__", 1)[0]
    if kind in KNOWN_KIND_PREFIXES:
        return "known"
    return "unknown"


def resolve_snapshots(
    settings: WarehouseSettings, snapshot_ids: tuple[str, ...],
    *, registry: SnapshotRegistry | None = None,
) -> tuple[SelectedSnapshot, ...]:
    """Validate and resolve an explicit, approved snapshot selection.

    ``snapshot_ids`` must name snapshots already marked ``approved`` in the
    registry -- there is no "build everything on disk" mode. An unknown id,
    an unapproved status, a schema-version mismatch, a missing snapshot
    directory, or a failed manifest re-validation each stop the build before
    any DuckDB file is touched.
    """

    if not snapshot_ids:
        return ()
    active_registry = registry or load_snapshot_registry(settings.snapshots_path)
    resolved: list[SelectedSnapshot] = []
    seen: set[tuple[str, str]] = set()
    for snapshot_id in snapshot_ids:
        try:
            spec = active_registry.get(snapshot_id)
        except SnapshotRegistryError as exc:
            # Unknown/stale ids fail like every other selection problem here.
            raise SelectionError(str(exc)) from exc
        if spec.status != "approved":
            raise SelectionError(f"snapshot {snapshot_id!r} is not approved")
        key = (spec.source, spec.run_id)
        if key in seen:
            raise SelectionError(f"duplicate source/run selection: {key}")
        seen.add(key)
        snapshot_root = settings.snapshot_root(spec.source, spec.run_id)
        if not snapshot_root.is_dir():
            raise SelectionError(f"canonical snapshot missing on disk: {snapshot_root}")
        try:
            manifest = validate_canonical_manifest(snapshot_root)
        except NormalizationError as exc:
            raise SelectionError(f"snapshot {snapshot_id!r} failed manifest validation: {exc}") from exc
        if manifest["schema_version"] != spec.schema_version:
            raise SelectionError(
                f"snapshot {snapshot_id!r} schema_version mismatch: "
                f"registry declares {spec.schema_version!r}, manifest has {manifest['schema_version']!r}"
            )
        if spec.schema_version != settings.schema_version:
            raise SelectionError(
                f"snapshot {snapshot_id!r} declares unsupported schema_version "
                f"{spec.schema_version!r}; this warehouse build only supports "
                f"{settings.schema_version!r}"
            )
        if manifest["source"] != spec.source or manifest["run_id"] != spec.run_id:
            raise SelectionError(
                f"snapshot {snapshot_id!r} identity mismatch against registry"
            )
        tables = discover_tables(manifest)
        classified = {name: _classify(name) for name in tables}
        unknown = tuple(sorted(name for name, kind in classified.items() if kind == "unknown"))
        if unknown:
            raise SelectionError(
                f"snapshot {snapshot_id!r} declares unrecognized dataset(s) {unknown}; "
                "teach warehouse.schema.KIND_TABLES about them before building"
            )
        dependent = tuple(k for k in DEPENDENT_KINDS if k in tables)
        if dependent and PLANNING_KIND not in tables:
            raise SelectionError(
                f"snapshot {snapshot_id!r} carries {dependent} without {PLANNING_KIND!r}; "
                "these kinds are filtered against the activities their own snapshot's "
                "pl produces, so every row would be quarantined as an orphan "
                "without failing the build (#161)"
            )
        resolved.append(SelectedSnapshot(
            spec=spec, snapshot_root=snapshot_root, manifest=manifest, tables=tables,
        ))

    # Checked across the SELECTION, not per snapshot. The guarantee is
    # unchanged -- a build still cannot be missing a source kind -- but it is
    # the build that must be complete, not every contributor to it.
    #
    # This sat inside the loop above, which read as "each snapshot must carry
    # all five kinds". That was always stronger than the design:
    # `build_warehouse.py` accepts repeated --snapshot-id, and
    # `build._merge_gram_panchayat` exists specifically to merge contributions
    # across snapshots in one build. It also made a single-source snapshot
    # illegal, so adding any new kind -- gp_profile (#123), voucher (#129) --
    # would have retroactively invalidated every snapshot already approved,
    # including the one production is about to serve.
    covered = {name for snapshot in resolved for name in snapshot.tables}
    missing_kinds = tuple(kind for kind in KNOWN_KIND_PREFIXES if kind not in covered)
    if missing_kinds:
        # A kind absent entirely (never requested at normalization time) is
        # not the same as a kind present with zero rows.
        raise SelectionError(
            f"selection {tuple(snapshot_ids)!r} is missing required source-kind "
            f"dataset(s) {missing_kinds}; a partial normalization cannot be built"
        )
    return tuple(resolved)
