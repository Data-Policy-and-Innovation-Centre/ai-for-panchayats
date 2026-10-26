"""Shared, source-independent pipeline contracts."""

from .manifest import (
    approve_run,
    ManifestError,
    RunManifest,
    RunPublisher,
    ValidationReport,
    validate_run,
)
from .settings import PipelineSettings, SettingsError, load_settings
from .snapshots import SnapshotRegistry, SnapshotRegistryError, SnapshotSpec, load_snapshot_registry
from .normalize import (
    AtomicParquetPublication,
    NormalizationError,
    NormalizationResult,
    normalize_egramswaraj,
    normalize_flat_csv,
    normalize_run,
)

__all__ = [
    "ManifestError",
    "AtomicParquetPublication",
    "NormalizationError",
    "NormalizationResult",
    "approve_run",
    "PipelineSettings",
    "RunManifest",
    "RunPublisher",
    "SettingsError",
    "SnapshotRegistry",
    "SnapshotRegistryError",
    "SnapshotSpec",
    "ValidationReport",
    "load_settings",
    "load_snapshot_registry",
    "normalize_egramswaraj",
    "normalize_flat_csv",
    "normalize_run",
    "validate_run",
]
