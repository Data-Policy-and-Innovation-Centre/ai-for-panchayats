"""Validated, side-effect-free settings for pipeline commands.

Settings deliberately do not create directories.  In particular, importing
this module can never create or inspect the repository's ``data/`` tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class SettingsError(ValueError):
    """Raised when a setting cannot be used safely."""


def _safe_component(value: str, label: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise SettingsError(f"{label} must be a non-empty path component")
    return value


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """Settings shared by all pipeline stages.

    ``raw_root`` is explicit in the CLI so test and development runs can use
    a temporary directory.  The API default mirrors the repository contract,
    but does not create it until a caller publishes a run.
    """

    project_root: Path
    raw_root: Path
    snapshots_path: Path
    schema_version: str = "1"
    default_privacy_class: str = "restricted"

    def __post_init__(self) -> None:
        if not self.schema_version.strip():
            raise SettingsError("schema_version cannot be empty")
        _safe_component(self.default_privacy_class, "default_privacy_class")
        object.__setattr__(self, "project_root", Path(self.project_root).resolve())
        object.__setattr__(self, "raw_root", Path(self.raw_root).resolve())
        object.__setattr__(self, "snapshots_path", Path(self.snapshots_path).resolve())

    def raw_source_root(self, source: str) -> Path:
        """Return a safe source directory below the configured raw root."""

        return self.raw_root / _safe_component(source, "source")

    def raw_run_root(self, source: str, run_id: str) -> Path:
        """Return a safe run directory below the configured raw root."""

        return self.raw_source_root(source) / _safe_component(run_id, "run_id")


def load_settings(
    *,
    project_root: str | Path | None = None,
    raw_root: str | Path | None = None,
    snapshots_path: str | Path | None = None,
) -> PipelineSettings:
    """Load settings from arguments, then environment, without filesystem IO."""

    root = Path(project_root or os.environ.get("PIPELINE_PROJECT_ROOT", Path.cwd()))
    configured_raw = raw_root or os.environ.get("PIPELINE_RAW_ROOT")
    raw = Path(configured_raw) if configured_raw else root / "data" / "raw"
    configured_snapshots = snapshots_path or os.environ.get("PIPELINE_SNAPSHOTS")
    snapshots = (
        Path(configured_snapshots)
        if configured_snapshots
        else root / "config" / "snapshots.yaml"
    )
    return PipelineSettings(project_root=root, raw_root=raw, snapshots_path=snapshots)


def validate_component(value: str, label: str = "value") -> str:
    """Public path-component validation used by CLI and publication APIs."""

    return _safe_component(value, label)
