"""What a running task says it is (#85).

A deployment could not be distinguished from the one before it by any
observation of the app: `SnapshotIdentity` was printed to stdout by
`fetch_snapshot` and discarded, `/health` belongs to the consumer, and
`serve.py` only mounted static files. Post-deploy verification needs to ask a
task what it is and get an answer.

The payload is assembled here rather than in `docker/serve.py` for one
practical reason: FastAPI exists only inside the image, so anything written
next to the route is untestable in this repository. Everything with a decision
in it lives in this module; `serve.py` stays wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from src.deploy.fetch import SnapshotIdentity

# The identity fields a task may publish over HTTP.
#
# `bucket`, `key` and `path` are deliberately NOT here. The route is
# unauthenticated -- it sits in front of CloudFront with no credential of any
# kind -- and the bucket name is the one piece of a private artifact's address
# that is useful to someone who does not already have it. `version_id` and
# `sha256` identify the artifact precisely for anyone who *does* have access,
# which is who the endpoint is for. `describe()` still logs the full address at
# startup, where it goes to CloudWatch rather than to the internet.
PUBLISHABLE_IDENTITY_FIELDS: tuple[str, ...] = (
    "label", "version_id", "sha256", "byte_size", "aggregates_verified", "verified_at",
)

BUILD_INFO_FIELDS: tuple[str, ...] = ("repo_commit", "consumer_commit", "image_tag")

UNAVAILABLE = "unavailable"


def publishable_identity(identity: SnapshotIdentity) -> dict[str, Any]:
    """The subset of a verified identity that is safe to serve."""

    return {field: getattr(identity, field) for field in PUBLISHABLE_IDENTITY_FIELDS}


def write_identity(identity: SnapshotIdentity, destination: Path) -> None:
    """Persist the publishable identity for the serving process to read.

    Written only after verification has already succeeded, so the file's
    presence means the database was proven -- never that a fetch was
    attempted.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(publishable_identity(identity), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """A JSON object from `path`, or None if it is absent or unusable.

    Absent is a normal state, not an error: `docker run` without S3 access
    produces no identity file, and the route should still answer with what it
    does know. A task that could not verify its snapshot never reaches the
    serving process at all -- `entrypoint.sh` runs under `set -e` -- so a
    missing file here cannot mean "verification failed silently".
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def deployment_payload(build_info_path: Path, identity_path: Path) -> dict[str, Any]:
    """Everything a task can say about itself, as one JSON-ready dict."""

    build_info = _read_json_object(build_info_path) or {}
    identity = _read_json_object(identity_path)
    return {
        "build": {field: build_info.get(field, UNAVAILABLE) for field in BUILD_INFO_FIELDS},
        "snapshot": identity if identity is not None else UNAVAILABLE,
    }


__all__ = [
    "BUILD_INFO_FIELDS",
    "PUBLISHABLE_IDENTITY_FIELDS",
    "deployment_payload",
    "publishable_identity",
    "write_identity",
]
