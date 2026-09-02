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
from src.pipeline.snapshots import SnapshotRegistry, SnapshotSpec, load_snapshot_registry

from .config import WarehouseSettings
from .load import discover_tables
from .schema import KIND_TABLES

KNOWN_KIND_PREFIXES = tuple(kind.lower() for kind in KIND_TABLES)


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
        spec = active_registry.get(snapshot_id)
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
        missing_kinds = tuple(kind for kind in KNOWN_KIND_PREFIXES if kind not in tables)
        if missing_kinds:
            # A kind absent entirely (never requested at normalization time)
            # is not the same as a kind present with zero rows.
            raise SelectionError(
                f"snapshot {snapshot_id!r} is missing required source-kind dataset(s) "
                f"{missing_kinds}; a partial normalization cannot be built"
            )
        resolved.append(SelectedSnapshot(
            spec=spec, snapshot_root=snapshot_root, manifest=manifest, tables=tables,
        ))
    return tuple(resolved)
