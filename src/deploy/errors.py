"""Typed failures for snapshot packaging and retrieval.

Every failure mode here is fail-closed: the caller either receives a verified
snapshot or an exception. There is no partial success and no degraded mode,
because a task that serves a wrong or stale database answers plausibly rather
than visibly.
"""

from __future__ import annotations


class SnapshotError(Exception):
    """Base class for every snapshot packaging or retrieval failure."""


class SnapshotManifestError(SnapshotError, ValueError):
    """Raised for a malformed, incomplete, or internally inconsistent manifest."""


class SnapshotUnavailableError(SnapshotError):
    """Raised when the pinned object cannot be retrieved at all.

    Missing key, missing or deleted object version, or denied access. Distinct
    from an integrity failure: nothing was proven wrong, nothing was obtained.
    """


class SnapshotIntegrityError(SnapshotError):
    """Raised when retrieved bytes do not match the pinned identity.

    Wrong byte count, wrong SHA-256, or a relation inventory that disagrees
    with the manifest. Always treat as artifact substitution until proven
    otherwise.
    """


class SnapshotStorageError(SnapshotError):
    """Raised when task-local storage cannot hold the download plus the final file."""


class KnownAnswerError(SnapshotError):
    """Raised when a verified-by-bytes snapshot fails a known-answer query.

    Byte identity can hold while the artifact is still the wrong database, so
    this gate runs after hashing and before publication.
    """
