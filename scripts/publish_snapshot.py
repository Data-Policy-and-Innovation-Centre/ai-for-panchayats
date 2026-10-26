"""Upload a snapshot, pin its exact version, and open the manifest PR (#95).

Publishing today is prose in infra/snapshots/README.md: an `aws s3 cp` that
must name the CMK or fail AccessDenied, a `head-object` read-back that returns
whatever version is *current* and must be cross-checked by byte size before it
can be trusted, then a manual manifest build and a hand-made PR. Every step is
a place to pin the wrong version.

This is the one part of delivery that stays a local command -- the artifact
comes from proprietary DVC-backed data and cannot be produced on CI at all --
so it should be a single reliable one.

    uv run python scripts/publish_snapshot.py data/interim/panchayat.duckdb --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_BUCKET = "dpic-prdw-snapshots"
DEFAULT_CMK_ALIAS = "alias/dpic-prdw-snapshots"
DEFAULT_REGION = "ap-south-1"


class PublishError(RuntimeError):
    """Refused before anything was uploaded, or after an upload could not be trusted."""


def resolve_cmk(client, alias: str) -> str:
    """The CMK's key id, or refuse.

    The bucket policy denies any upload that does not name the CMK explicitly,
    so an unresolvable alias means the upload would fail with AccessDenied
    after transferring a gigabyte. Failing here costs nothing.
    """

    try:
        described = client.describe_key(KeyId=alias)
    except Exception as error:  # noqa: BLE001 - botocore raises many shapes here
        raise PublishError(
            f"cannot resolve {alias!r}. The bucket policy denies any upload that does not "
            f"name the snapshot CMK, so this would fail AccessDenied after the transfer: {error}"
        ) from error
    return described["KeyMetadata"]["KeyId"]


def verify_uploaded(s3, bucket: str, key: str, version_id: str, local_bytes: int) -> int:
    """Read the exact version back and prove it is the file we just sent.

    head-object WITHOUT a version id returns whatever is current, which is how
    a manifest ends up pinning a version nobody uploaded. This reads the exact
    version and then cross-checks the size, because a version id alone proves
    only that *something* was written.
    """

    head = s3.head_object(Bucket=bucket, Key=key, VersionId=version_id)
    remote_bytes = head["ContentLength"]
    if remote_bytes != local_bytes:
        raise PublishError(
            f"uploaded object version {version_id} is {remote_bytes:,} bytes but the local "
            f"artifact is {local_bytes:,}. Do not pin this version."
        )
    return remote_bytes


def build_expectations_payload(artifact: Path) -> bytes:
    """The private aggregates, regenerated from the artifact being published.

    Regenerated rather than carried alongside, for the same reason the manifest
    is: a gate copied from a previous build describes a file it was not built
    from, and would either reject a good artifact or -- worse -- pass a wrong
    one whose counts happened to match.
    """

    result = subprocess.run(
        [sys.executable, "scripts/build_expectations.py", str(artifact)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PublishError(f"could not build the aggregates: {result.stderr.strip()}")
    return result.stdout.encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artifact", type=Path, help="the local .duckdb to publish")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--key", default=None, help="defaults to duckdb/<artifact name>")
    parser.add_argument("--cmk", default=DEFAULT_CMK_ALIAS)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--manifest", type=Path, default=Path("infra/snapshots/full_state.json"))
    parser.add_argument("--label", default="full_state")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report exactly what would be uploaded and pinned, and upload nothing",
    )
    parser.add_argument("--no-pr", action="store_true", help="write the manifest but open no PR")
    args = parser.parse_args(argv)

    if not args.artifact.is_file():
        print(f"no such artifact: {args.artifact}", file=sys.stderr)
        return 1
    key = args.key or f"duckdb/{args.artifact.name}"
    local_bytes = args.artifact.stat().st_size

    import boto3

    kms = boto3.client("kms", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    try:
        # Resolved BEFORE the upload, always -- including on a dry run, because
        # "the CMK is not resolvable" is the failure a dry run most needs to
        # surface.
        cmk = resolve_cmk(kms, args.cmk)
        # Beside the artifact, named after it: the convention the deployed
        # manifest already follows (duckdb/database_allgps.expectations.json).
        expectations_key = key.rsplit(".", 1)[0] + ".expectations.json"

        print(f"artifact   {args.artifact} ({local_bytes:,} bytes)")
        print(f"target     s3://{args.bucket}/{key}")
        print(f"encryption aws:kms {args.cmk} -> {cmk}")
        print(f"manifest   {args.manifest} (label {args.label})")
        print(f"aggregates s3://{args.bucket}/{expectations_key}")

        if args.dry_run:
            print("\ndry run: nothing uploaded, no manifest written, no PR opened")
            return 0

        # upload_file, not put_object. A single PUT is capped at 5 GiB and the
        # full-state artifact is 6.4 GB, so put_object fails on exactly the
        # file this command exists to publish. upload_file switches to
        # multipart automatically.
        #
        # The cost is that it returns no response, so the VersionId has to be
        # read back. head_object without a version returns whatever is
        # CURRENT -- the trap infra/snapshots/README.md documents -- so this
        # reads current, then re-heads that exact version and cross-checks the
        # byte count. If someone else published to the same key in the
        # intervening second, a same-size artifact would still slip through
        # here; the backstop is that build_snapshot_manifest recomputes sha256
        # from the LOCAL file, so a mismatched pin fails at the first
        # fetch_snapshot rather than being served.
        s3.upload_file(
            str(args.artifact), args.bucket, key,
            ExtraArgs={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": cmk},
        )
        current = s3.head_object(Bucket=args.bucket, Key=key)
        version_id = current["VersionId"]
        verify_uploaded(s3, args.bucket, key, version_id, local_bytes)
        print(f"uploaded   versionId={version_id}, size confirmed against the local artifact")

        # The manifest pins TWO objects. src/deploy/fetch.py runs these row
        # counts against the downloaded database at startup and refuses to
        # serve on a mismatch, and verify_deployment.py fails the deploy when a
        # task reports aggregates=SKIPPED. Publishing the artifact alone was
        # therefore not a partial success -- it produced a manifest that could
        # not be deployed at all. Small object, so put_object is correct here.
        expectations_body = build_expectations_payload(args.artifact)
        put = s3.put_object(
            Bucket=args.bucket, Key=expectations_key, Body=expectations_body,
            ContentType="application/json",
            ServerSideEncryption="aws:kms", SSEKMSKeyId=cmk,
        )
        expectations_version_id = put["VersionId"]
        print(
            f"aggregates s3://{args.bucket}/{expectations_key} "
            f"versionId={expectations_version_id}"
        )
    except PublishError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    # Regenerated from the local artifact rather than edited: sha256, byte size
    # and the relation inventory are all recomputed, so a manifest cannot
    # describe a file it was not built from.
    build = subprocess.run(
        [
            sys.executable, "scripts/build_snapshot_manifest.py",
            str(args.artifact), "--bucket", args.bucket, "--key", key,
            "--version-id", version_id, "--label", args.label,
            "--expectations-key", expectations_key,
            "--expectations-version-id", expectations_version_id,
            # --out, not --output. build_snapshot_manifest.py declares "--out";
            # argparse rejects the long form with exit 2, and this subprocess
            # runs AFTER the ~6.4 GB upload, so the wrong spelling means every
            # real publish pays for the upload and then fails to write the
            # manifest it exists to produce.
            "--out", str(args.manifest),
        ],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        print(build.stdout, file=sys.stderr)
        print(build.stderr, file=sys.stderr)
        print(
            "the object is uploaded but the manifest was not written. Re-run "
            f"build_snapshot_manifest.py with --version-id {version_id} rather than "
            "re-uploading, or the next upload becomes a second version to choose between.",
            file=sys.stderr,
        )
        return 1
    print(f"manifest   written from the local artifact, pinning {version_id}")

    if args.no_pr:
        return 0

    branch = f"chore/snapshot-{args.label}-{version_id[:8]}"
    for command in (
        ["git", "checkout", "-b", branch],
        ["git", "add", str(args.manifest)],
        ["git", "commit", "-m", f"chore: pin {args.label} snapshot {version_id[:12]}"],
        ["git", "push", "-u", "origin", branch],
    ):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return 1
    # Only the manifest is in the diff, so the credential scan has nothing to
    # allowlist and #94's check runs against the new pin.
    subprocess.run(
        ["gh", "pr", "create", "--base", "dev", "--fill"], check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
