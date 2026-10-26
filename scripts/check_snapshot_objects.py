"""Prove a manifest's pinned objects exist, before a merge rather than at task start (#94).

`infra/snapshots/full_state.json` pins an S3 object version and a separately
version-pinned expectations object. Both are private and neither is
reproducible on a runner, so nothing checked that either was really there
until an ECS task tried to start -- at which point the failure costs a full
rollout and its 420s grace period.

The specific trap this closes is documented in `infra/snapshots/README.md`:
`head-object` WITHOUT a version id returns whatever version is *current*, so a
manifest naming a version that was never uploaded still looks correct in
review. Every call here passes `VersionId` explicitly.

    uv run python scripts/check_snapshot_objects.py infra/snapshots/full_state.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 1


class ManifestObjectError(RuntimeError):
    """A pinned object is missing, deleted, or not the size the manifest claims."""


def _head(client: Any, bucket: str, key: str, version_id: str) -> dict[str, Any]:
    """Metadata for one exact version. Never the current one."""

    from botocore.exceptions import ClientError

    try:
        return client.head_object(Bucket=bucket, Key=key, VersionId=version_id)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        # 404 is "no such version"; 405 is what S3 returns for a DELETED
        # version, which is a different failure with the same consequence and
        # would otherwise read as a transport error.
        if code in {"404", "NoSuchKey", "NoSuchVersion"} or status == 404:
            raise ManifestObjectError(
                f"s3://{bucket}/{key} has no version {version_id!r}. "
                "A manifest can name a version that was never uploaded and still look "
                "correct in review, because head-object without a version id returns "
                "whatever is current."
            ) from error
        if status == 405 or code == "MethodNotAllowed":
            raise ManifestObjectError(
                f"s3://{bucket}/{key} version {version_id!r} is a delete marker or was "
                "deleted. The bucket expires noncurrent versions after 180 days, so a "
                "pin can rot without anything changing in git."
            ) from error
        if status == 403 or code in {"403", "AccessDenied"}:
            raise ManifestObjectError(
                f"access denied reading s3://{bucket}/{key}. This check must be able to "
                "distinguish 'not there' from 'not allowed'; it cannot, so it fails."
            ) from error
        # A version id that is not merely absent but MALFORMED comes back 400,
        # not 404 -- found by running it against a fabricated value, which is
        # the exact case this check exists to catch. Without this branch the
        # tool died with a botocore traceback, and a traceback in a workflow
        # log is the thing a reviewer has to decode instead of reading a
        # verdict.
        if status == 400 or code in {"400", "InvalidArgument", "BadRequest"}:
            raise ManifestObjectError(
                f"s3://{bucket}/{key} was pinned to {version_id!r}, which S3 rejects as "
                "a malformed version id. A hand-edited or invented pin looks exactly "
                "like this."
            ) from error
        # Anything else is still ours to report as a failure rather than a
        # crash: this runs in CI, where an unhandled exception is indistinguishable
        # from the tool being broken.
        raise ManifestObjectError(
            f"could not read s3://{bucket}/{key} version {version_id!r}: {error}"
        ) from error


def verify_manifest(manifest: dict[str, Any], client: Any) -> list[str]:
    """Check every pinned object. Returns the lines to report on success."""

    bucket = manifest["bucket"]
    lines: list[str] = []

    head = _head(client, bucket, manifest["key"], manifest["version_id"])
    actual = head["ContentLength"]
    expected = manifest["byte_size"]
    if actual != expected:
        raise ManifestObjectError(
            f"s3://{bucket}/{manifest['key']} version {manifest['version_id']} is "
            f"{actual:,} bytes but the manifest says {expected:,}. The version id and "
            "the byte size disagree, so at least one of them was copied from a "
            "different upload."
        )
    lines.append(f"snapshot     {manifest['key']} @ {manifest['version_id']} ({actual:,} bytes)")

    expectations_key = manifest.get("expectations_key")
    if expectations_key is None:
        lines.append("expectations not pinned by this manifest; nothing to check")
        return lines

    expectations_version = manifest.get("expectations_version_id")
    if not expectations_version:
        raise ManifestObjectError(
            f"manifest pins expectations_key {expectations_key!r} with no "
            "expectations_version_id, so the gate would read whatever is current"
        )
    head = _head(client, bucket, expectations_key, expectations_version)
    # Deliberately no size assertion: the manifest records no expected size for
    # this object, and inventing one here would fail every legitimate re-publish.
    lines.append(
        f"expectations {expectations_key} @ {expectations_version} "
        f"({head['ContentLength']:,} bytes)"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--region", default=None)
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read {args.manifest}: {error}", file=sys.stderr)
        return EXIT_INVALID

    import boto3

    client = boto3.client("s3", region_name=args.region)
    try:
        lines = verify_manifest(manifest, client)
    except ManifestObjectError as error:
        print(f"manifest pins an object that is not there: {error}", file=sys.stderr)
        return EXIT_INVALID
    except KeyError as error:
        print(f"manifest is missing required field {error}", file=sys.stderr)
        return EXIT_INVALID

    # Never the object body, never the expectations content, never an aggregate.
    # This check exists to prove existence and size; printing anything else would
    # put private values into a public workflow log.
    for line in lines:
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
