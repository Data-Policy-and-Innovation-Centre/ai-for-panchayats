"""Pin the identity of one deployable DuckDB snapshot.

The manifest is committed to this **public** repository, so it carries only
structural identity: byte size, SHA-256, S3 coordinates, and the names of the
relations the file must contain. Aggregate expectations — row counts, money
totals, known-answer results — are derived from protected source data and live
in a separate object stored beside the artifact in private S3, referenced here
by key only.

A manifest describes exactly one immutable object version. Republishing means
writing a new manifest, never editing this one, so a rollback is a manifest
revert rather than a mutation of an S3 object.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import SnapshotManifestError

SCHEMA_VERSION = 1

# The label the artifact ships under until #43/#49, #61 and #62 are resolved.
PROVISIONAL_LABEL = "provisional_full_state_snapshot"

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DIGEST_CHUNK = 8 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotManifestError(f"{field_name} must be a non-empty string")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    # bool is an int subclass; a True byte size must never read as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SnapshotManifestError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True)
class SnapshotManifest:
    """Structural, publicly shareable identity of one deployable snapshot."""

    label: str
    byte_size: int
    sha256: str
    relations: tuple[str, ...]
    bucket: str
    key: str
    version_id: str
    duckdb_library_version: str
    created_at: str
    expectations_key: str | None = None
    expectations_version_id: str | None = None
    known_exceptions: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SnapshotManifestError(
                f"unsupported manifest schema_version {self.schema_version!r}; "
                f"this build understands {SCHEMA_VERSION}"
            )
        for field_name in ("label", "bucket", "key", "version_id", "duckdb_library_version"):
            _require_text(getattr(self, field_name), field_name)
        _require_positive_int(self.byte_size, "byte_size")

        if not isinstance(self.sha256, str) or not _SHA256_RE.match(self.sha256):
            raise SnapshotManifestError("sha256 must be 64 lowercase hexadecimal characters")

        if not self.relations:
            raise SnapshotManifestError("relations must name at least one table or view")
        for name in self.relations:
            _require_text(name, "relation name")
        if len(set(self.relations)) != len(self.relations):
            raise SnapshotManifestError("relations must not repeat a name")
        if list(self.relations) != sorted(self.relations):
            raise SnapshotManifestError("relations must be sorted for stable diffs")

        if self.expectations_key is not None:
            _require_text(self.expectations_key, "expectations_key")
            # Without a pinned version the gate is whatever happens to be
            # current, so an old manifest could be validated against a newer
            # contract -- or fail against it during a rollback.
            if self.expectations_version_id is None:
                raise SnapshotManifestError(
                    "expectations_key requires expectations_version_id: an unpinned "
                    "expectations object makes the manifest non-deterministic"
                )
            _require_text(self.expectations_version_id, "expectations_version_id")
        elif self.expectations_version_id is not None:
            raise SnapshotManifestError(
                "expectations_version_id without expectations_key"
            )
        for note in self.known_exceptions:
            _require_text(note, "known_exceptions entry")

        try:
            datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise SnapshotManifestError("created_at is not an ISO-8601 timestamp") from exc

    @property
    def identity(self) -> str:
        """A short, log-safe description of which artifact this is."""
        return f"s3://{self.bucket}/{self.key}?versionId={self.version_id} sha256={self.sha256}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "label": self.label,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "relations": list(self.relations),
            "bucket": self.bucket,
            "key": self.key,
            "version_id": self.version_id,
            "duckdb_library_version": self.duckdb_library_version,
            "created_at": self.created_at,
            "expectations_key": self.expectations_key,
            "expectations_version_id": self.expectations_version_id,
            "known_exceptions": list(self.known_exceptions),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), indent=2, sort_keys=True) + "\n"

    def with_object_version(self, bucket: str, key: str, version_id: str) -> SnapshotManifest:
        """Return a copy pinned to the S3 object version an upload produced."""
        return replace(
            self,
            bucket=_require_text(bucket, "bucket"),
            key=_require_text(key, "key"),
            version_id=_require_text(version_id, "version_id"),
        )


def from_mapping(payload: Mapping[str, Any]) -> SnapshotManifest:
    if not isinstance(payload, Mapping):
        raise SnapshotManifestError("manifest payload must be a JSON object")

    known = {
        "schema_version",
        "label",
        "byte_size",
        "sha256",
        "relations",
        "bucket",
        "key",
        "version_id",
        "duckdb_library_version",
        "created_at",
        "expectations_key",
        "expectations_version_id",
        "known_exceptions",
    }
    unexpected = sorted(set(payload) - known)
    if unexpected:
        raise SnapshotManifestError(f"manifest has unexpected fields: {', '.join(unexpected)}")

    optional = {"expectations_key", "expectations_version_id", "known_exceptions", "schema_version"}
    missing = sorted(known - optional - set(payload))
    if missing:
        raise SnapshotManifestError(f"manifest is missing fields: {', '.join(missing)}")

    relations = payload["relations"]
    if not isinstance(relations, list):
        raise SnapshotManifestError("relations must be a JSON array")
    exceptions = payload.get("known_exceptions", [])
    if not isinstance(exceptions, list):
        raise SnapshotManifestError("known_exceptions must be a JSON array")

    return SnapshotManifest(
        schema_version=payload.get("schema_version", SCHEMA_VERSION),
        label=payload["label"],
        byte_size=payload["byte_size"],
        sha256=payload["sha256"],
        relations=tuple(relations),
        bucket=payload["bucket"],
        key=payload["key"],
        version_id=payload["version_id"],
        duckdb_library_version=payload["duckdb_library_version"],
        created_at=payload["created_at"],
        expectations_key=payload.get("expectations_key"),
        expectations_version_id=payload.get("expectations_version_id"),
        known_exceptions=tuple(exceptions),
    )


def load_manifest(path: str | Path) -> SnapshotManifest:
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotManifestError(f"{path} is not valid JSON") from exc
    return from_mapping(payload)


def digest_file(path: str | Path) -> tuple[str, int]:
    """Stream `path` once, returning its SHA-256 and byte count.

    Bounded memory: a ~1 GB artifact is read in fixed-size chunks so this runs
    inside a small Fargate task.
    """
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


ATTACH_CATALOG = "snap"


@contextmanager
def attach_read_only(path: str | Path, *, catalog: str = ATTACH_CATALOG):
    """Yield an in-memory DuckDB connection with `path` attached read-only.

    This mirrors the consumer's ``DuckDBFileAdapter``, which connects to
    ``:memory:`` and attaches the analytical database READ_ONLY so the router's
    cache tables never write to it. A file that cannot be opened this way here
    cannot be served there either.
    """
    import duckdb

    escaped = str(Path(path)).replace("'", "''")
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"ATTACH '{escaped}' AS {catalog} (READ_ONLY)")
        yield conn
    finally:
        conn.close()


def relations_of(conn: Any, *, catalog: str = ATTACH_CATALOG) -> tuple[str, ...]:
    """Return the sorted table and view names in an attached snapshot."""
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = ? "
        "UNION SELECT view_name FROM duckdb_views() WHERE database_name = ?",
        [catalog, catalog],
    ).fetchall()
    return tuple(sorted(str(row[0]) for row in rows))


def read_relations(path: str | Path) -> tuple[str, ...]:
    """Return the sorted table and view names in a DuckDB file."""
    with attach_read_only(path) as conn:
        return relations_of(conn)


def build_manifest(
    artifact_path: str | Path,
    *,
    bucket: str,
    key: str,
    version_id: str,
    label: str = PROVISIONAL_LABEL,
    expectations_key: str | None = None,
    expectations_version_id: str | None = None,
    known_exceptions: tuple[str, ...] = (),
    created_at: str | None = None,
) -> SnapshotManifest:
    """Derive a manifest from a local DuckDB artifact.

    `version_id` is the S3 object version the artifact was (or will be) stored
    as. Callers that upload first should pass the real value; callers that
    manifest first should pass a placeholder and then call
    :meth:`SnapshotManifest.with_object_version`.
    """
    import duckdb

    path = Path(artifact_path)
    if not path.is_file():
        raise SnapshotManifestError(f"no snapshot artifact at {path}")

    sha256, byte_size = digest_file(path)
    return SnapshotManifest(
        label=label,
        byte_size=byte_size,
        sha256=sha256,
        relations=read_relations(path),
        bucket=bucket,
        key=key,
        version_id=version_id,
        duckdb_library_version=duckdb.__version__,
        created_at=created_at or utc_now(),
        expectations_key=expectations_key,
        expectations_version_id=expectations_version_id,
        known_exceptions=known_exceptions,
    )
