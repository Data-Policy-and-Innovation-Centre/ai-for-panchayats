"""Deployment-side packaging and retrieval of the immutable DuckDB snapshot.

The chatbot's workload is read-only and analytical, so the deployed database is
a single version-pinned DuckDB file rather than a client/server engine. This
package pins that file's identity (`manifest`) and gives an ECS task a
fail-closed way to obtain exactly that file before it reports healthy
(`fetch`).

Deliberately self-contained: it shares no code with ``src/pipeline`` or
``src/warehouse`` so that deploying the identified artifact never waits on the
raw-source rebuild. See issue #71.
"""

from __future__ import annotations

from .errors import (
    KnownAnswerError,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotManifestError,
    SnapshotStorageError,
    SnapshotUnavailableError,
)
from .expectations import Expectations, KnownAnswerQuery
from .fetch import SnapshotIdentity, fetch_snapshot, load_expectations
from .manifest import SnapshotManifest, build_manifest, load_manifest, read_relations

__all__ = [
    "Expectations",
    "KnownAnswerError",
    "KnownAnswerQuery",
    "SnapshotError",
    "SnapshotIdentity",
    "SnapshotIntegrityError",
    "SnapshotManifest",
    "SnapshotManifestError",
    "SnapshotStorageError",
    "SnapshotUnavailableError",
    "build_manifest",
    "fetch_snapshot",
    "load_expectations",
    "load_manifest",
    "read_relations",
]
