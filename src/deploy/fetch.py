"""Obtain exactly the pinned snapshot, or refuse to become healthy.

An ECS task calls :func:`fetch_snapshot` before it reports ready. The helper
downloads the manifest's object version to a temporary path, proves the bytes,
proves the contents, and only then renames the file into place. Every failure
raises; there is no path that publishes an unverified file.

Order matters. Bytes are hashed after the download completes rather than during
it, because boto3's multipart download writes ranges out of order through
`seek` — a hash fed by a streaming wrapper would be computed over a permuted
byte order and would be wrong in a way that still looks like a clean digest.

Publication is a single `os.replace` inside the destination directory, so a
task that dies mid-download leaves a stray temporary file and never a truncated
database at the path the application opens.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import expectations as expectations_module
from .errors import (
    KnownAnswerError,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotManifestError,
    SnapshotStorageError,
    SnapshotUnavailableError,
)
from .expectations import Expectations
from .manifest import SnapshotManifest, attach_read_only, digest_file, relations_of, utc_now

_log = logging.getLogger(__name__)

# Free space required before downloading, as a multiple of the artifact size.
# The temporary file and the previous snapshot can coexist during publication.
DEFAULT_STORAGE_HEADROOM = 2.2


def _s3_error_types() -> tuple[type[BaseException], ...]:
    """Errors that genuinely mean "S3 could not serve this".

    OSError is excluded on purpose: a full or read-only task volume reported as
    SnapshotUnavailableError sends an operator to bucket policy and IAM for a
    local storage problem.
    """
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - boto3 is a declared dependency
        return ()
    return (BotoCoreError, ClientError)


_S3_ERRORS = _s3_error_types()


@dataclass(frozen=True)
class SnapshotIdentity:
    """What a task reports it is serving, safe to emit in health output."""

    label: str
    bucket: str
    key: str
    version_id: str
    sha256: str
    byte_size: int
    path: str
    verified_at: str
    # Defaulted so an external constructor keeps working, and False is the
    # conservative reading: assume the gate did not run unless told otherwise.
    aggregates_verified: bool = False

    def describe(self) -> str:
        gate = "aggregates=verified" if self.aggregates_verified else "aggregates=SKIPPED"
        return (
            f"{self.label} s3://{self.bucket}/{self.key}?versionId={self.version_id} "
            f"sha256={self.sha256} bytes={self.byte_size} {gate}"
        )


def _check_free_space(directory: Path, byte_size: int, headroom: float) -> None:
    required = int(byte_size * headroom)
    free = shutil.disk_usage(directory).free
    if free < required:
        raise SnapshotStorageError(
            f"{directory} has {free} bytes free; the snapshot needs about {required} "
            f"({byte_size} bytes at {headroom}x headroom)"
        )


def _head(s3_client: Any, manifest: SnapshotManifest) -> dict[str, Any]:
    try:
        response = s3_client.head_object(
            Bucket=manifest.bucket, Key=manifest.key, VersionId=manifest.version_id
        )
    except _S3_ERRORS as exc:
        raise SnapshotUnavailableError(
            f"cannot reach pinned snapshot {manifest.identity}: {exc}"
        ) from exc

    content_length = response.get("ContentLength")
    if content_length != manifest.byte_size:
        raise SnapshotIntegrityError(
            f"pinned object reports {content_length} bytes, manifest pins {manifest.byte_size}"
        )

    returned_version = response.get("VersionId")
    if returned_version is not None and returned_version != manifest.version_id:
        raise SnapshotIntegrityError(
            f"S3 returned object version {returned_version!r}, manifest pins "
            f"{manifest.version_id!r}"
        )
    return response


def _download(s3_client: Any, manifest: SnapshotManifest, target: Path) -> None:
    try:
        with open(target, "wb") as handle:
            s3_client.download_fileobj(
                manifest.bucket,
                manifest.key,
                handle,
                ExtraArgs={"VersionId": manifest.version_id},
            )
    except _S3_ERRORS as exc:
        raise SnapshotUnavailableError(
            f"download of {manifest.identity} failed: {exc}"
        ) from exc
    except OSError as exc:
        raise SnapshotStorageError(
            f"cannot write the snapshot to {target}: {exc}"
        ) from exc


def _verify_contents(
    path: Path, manifest: SnapshotManifest, expectations: Expectations | None
) -> None:
    try:
        with attach_read_only(path) as conn:
            relations = relations_of(conn)
            if relations != manifest.relations:
                missing = sorted(set(manifest.relations) - set(relations))
                extra = sorted(set(relations) - set(manifest.relations))
                raise SnapshotIntegrityError(
                    "snapshot relations do not match the manifest "
                    f"(missing: {missing or 'none'}; unexpected: {extra or 'none'})"
                )
            if expectations is not None:
                _run_expectations(conn, expectations)
    except SnapshotError:
        raise
    except Exception as exc:  # duckdb raises its own IOException on a bad file
        import duckdb

        raise SnapshotIntegrityError(
            f"snapshot could not be opened as a DuckDB database: {exc}. "
            f"The SHA-256 already matched, so the bytes are the pinned artifact; "
            f"suspect a storage-format mismatch before substitution "
            f"(manifest recorded DuckDB {manifest.duckdb_library_version}, "
            f"running {duckdb.__version__})"
        ) from exc


def _run_expectations(conn: Any, expectations: Expectations) -> None:
    """Evaluate the aggregate gate, keeping contract faults distinguishable.

    A typo in the expectations SQL is a broken contract, not evidence that the
    artifact was substituted -- and SnapshotIntegrityError is documented to mean
    the latter. Reporting one as the other would send an operator hunting a
    supply-chain problem that does not exist.
    """
    try:
        expectations_module.verify(conn, expectations)
    except SnapshotError:
        raise
    except Exception as exc:
        # Deliberately no exception text: DuckDB quotes the failing statement
        # in full, and the gate SQL lives in the private expectations object.
        raise KnownAnswerError(
            "expectations could not be evaluated against the snapshot; the gate SQL "
            "is not valid for this artifact"
        ) from exc


def load_expectations(s3_client: Any, manifest: SnapshotManifest) -> Expectations | None:
    """Read the private aggregate expectations that sit beside the artifact."""
    if manifest.expectations_key is None:
        return None
    try:
        # Pinned by version: the manifest must describe its own gate exactly,
        # or a rollback would validate an old snapshot against a newer contract.
        response = s3_client.get_object(
            Bucket=manifest.bucket,
            Key=manifest.expectations_key,
            VersionId=manifest.expectations_version_id,
        )
        body = response["Body"].read()
    except _S3_ERRORS as exc:
        raise SnapshotUnavailableError(
            f"cannot read expectations at s3://{manifest.bucket}/{manifest.expectations_key}: {exc}"
        ) from exc

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotManifestError(
            f"expectations at s3://{manifest.bucket}/{manifest.expectations_key} are not UTF-8"
        ) from exc
    return expectations_module.loads(text)


def fetch_snapshot(
    manifest: SnapshotManifest,
    destination: str | Path,
    *,
    s3_client: Any,
    expectations: Expectations | None = None,
    allow_missing_expectations: bool = False,
    storage_headroom: float = DEFAULT_STORAGE_HEADROOM,
) -> SnapshotIdentity:
    """Download, verify and atomically publish the pinned snapshot.

    Returns the identity the task should report. Raises on every failure: a
    missing or unreadable object, a byte count or SHA-256 that disagrees with
    the manifest, a relation inventory that disagrees with the manifest, or a
    failed aggregate expectation.
    """
    if (
        manifest.expectations_key is not None
        and expectations is None
        and not allow_missing_expectations
    ):
        # Otherwise a caller that simply forgot to load them gets a file
        # published and logged as "verified" with the aggregate gate skipped.
        raise SnapshotManifestError(
            f"manifest pins expectations at {manifest.expectations_key!r} but none were "
            "supplied; load them or pass allow_missing_expectations=True deliberately"
        )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_free_space(destination.parent, manifest.byte_size, storage_headroom)

    _head(s3_client, manifest)

    # The temporary file shares the destination directory so publication is a
    # rename within one filesystem, which is atomic.
    staging = destination.parent / f".{destination.name}.partial-{uuid4().hex}"
    try:
        _download(s3_client, manifest, staging)

        actual_size = staging.stat().st_size
        if actual_size != manifest.byte_size:
            raise SnapshotIntegrityError(
                f"downloaded {actual_size} bytes, manifest pins {manifest.byte_size}"
            )

        actual_sha, _ = digest_file(staging)
        if actual_sha != manifest.sha256:
            raise SnapshotIntegrityError(
                f"downloaded SHA-256 {actual_sha} does not match pinned {manifest.sha256}"
            )

        _verify_contents(staging, manifest, expectations)

        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)

    identity = SnapshotIdentity(
        label=manifest.label,
        bucket=manifest.bucket,
        key=manifest.key,
        version_id=manifest.version_id,
        sha256=manifest.sha256,
        byte_size=manifest.byte_size,
        path=str(destination),
        verified_at=utc_now(),
        aggregates_verified=expectations is not None,
    )
    _log.info("snapshot verified and published: %s", identity.describe())
    return identity
