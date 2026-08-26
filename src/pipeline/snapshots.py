"""Approved source snapshot registry (configuration only; no data access)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


class SnapshotRegistryError(ValueError):
    """Raised for an invalid or unapproved snapshot registry."""


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    snapshot_id: str
    source: str
    run_id: str
    schema_version: str
    status: str
    description: str = ""


class SnapshotRegistry:
    def __init__(self, snapshots: tuple[SnapshotSpec, ...], *, version: int = 1) -> None:
        self.version = version
        self.snapshots = snapshots
        ids = [snapshot.snapshot_id for snapshot in snapshots]
        if len(ids) != len(set(ids)):
            raise SnapshotRegistryError("snapshot ids must be unique")
        if any(snapshot.status != "approved" for snapshot in snapshots):
            raise SnapshotRegistryError("registry may contain only approved snapshots")

    def get(self, snapshot_id: str) -> SnapshotSpec:
        for snapshot in self.snapshots:
            if snapshot.snapshot_id == snapshot_id:
                return snapshot
        raise SnapshotRegistryError(f"unknown approved snapshot: {snapshot_id}")

    def for_source(self, source: str) -> tuple[SnapshotSpec, ...]:
        return tuple(snapshot for snapshot in self.snapshots if snapshot.source == source)


def load_snapshot_registry(path: str | Path) -> SnapshotRegistry:
    registry_path = Path(path)
    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SnapshotRegistryError(f"cannot read snapshot registry: {registry_path}") from exc
    if not isinstance(raw, Mapping):
        raise SnapshotRegistryError("snapshot registry must be a mapping")
    version = raw.get("version", 1)
    records = raw.get("snapshots")
    if not isinstance(version, int) or not isinstance(records, list):
        raise SnapshotRegistryError("registry requires integer version and snapshots list")
    snapshots: list[SnapshotSpec] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise SnapshotRegistryError(f"snapshots[{index}] must be a mapping")
        required = {"id", "source", "run_id", "schema_version", "status"}
        missing = required - set(record)
        if missing:
            raise SnapshotRegistryError(f"snapshots[{index}] missing {sorted(missing)}")
        values = {key: record[key] for key in required}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise SnapshotRegistryError(f"snapshots[{index}] has an empty field")
        snapshots.append(
            SnapshotSpec(
                snapshot_id=values["id"],
                source=values["source"],
                run_id=values["run_id"],
                schema_version=values["schema_version"],
                status=values["status"],
                description=str(record.get("description", "")),
            )
        )
    return SnapshotRegistry(tuple(snapshots), version=version)
