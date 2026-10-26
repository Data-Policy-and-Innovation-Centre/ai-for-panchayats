"""Side-effect-free settings for the DuckDB warehouse build.

Mirrors ``pipeline.settings``: every path is explicit or environment
overridable, and constructing settings never touches the filesystem.  In
particular importing or instantiating this module must never create,
inspect, or list anything under ``data/`` -- tests always inject their own
temporary roots.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class WarehouseSettingsError(ValueError):
    """Raised when a warehouse setting cannot be used safely."""


@dataclass(frozen=True, slots=True)
class WarehouseSettings:
    """Settings shared by the warehouse build and validation commands."""

    project_root: Path
    canonical_root: Path
    snapshots_path: Path
    db_path: Path
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.schema_version.strip():
            raise WarehouseSettingsError("schema_version cannot be empty")
        object.__setattr__(self, "project_root", Path(self.project_root).resolve())
        object.__setattr__(self, "canonical_root", Path(self.canonical_root).resolve())
        object.__setattr__(self, "snapshots_path", Path(self.snapshots_path).resolve())
        object.__setattr__(self, "db_path", Path(self.db_path).resolve())

    def snapshot_root(self, source: str, run_id: str) -> Path:
        """The canonical snapshot directory published by the normalizer.

        Mirrors ``normalize_egramswaraj``'s own publication layout:
        ``canonical_root / source / run_id``.
        """

        return self.canonical_root / source / run_id


def load_settings(
    *,
    project_root: str | Path | None = None,
    canonical_root: str | Path | None = None,
    snapshots_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> WarehouseSettings:
    """Load settings from arguments, then environment, without filesystem IO."""

    root = Path(project_root or os.environ.get("PIPELINE_PROJECT_ROOT", Path.cwd()))
    configured_canonical = canonical_root or os.environ.get("PANCHAYAT_CANONICAL_ROOT")
    canonical = Path(configured_canonical) if configured_canonical else root / "data" / "interim" / "canonical"
    configured_snapshots = snapshots_path or os.environ.get("PIPELINE_SNAPSHOTS")
    snapshots = (
        Path(configured_snapshots)
        if configured_snapshots
        else root / "config" / "snapshots.yaml"
    )
    configured_db = db_path or os.environ.get("PANCHAYAT_DB_PATH")
    db = Path(configured_db) if configured_db else root / "data" / "interim" / "panchayat.duckdb"
    return WarehouseSettings(
        project_root=root,
        canonical_root=canonical,
        snapshots_path=snapshots,
        db_path=db,
    )
