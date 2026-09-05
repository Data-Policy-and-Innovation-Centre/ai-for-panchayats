"""What a running task reports about itself (#85).

FastAPI exists only inside the image, so `docker/serve.py` cannot be imported
here. Everything with a decision in it therefore lives in `src.deploy.identity`
and is tested directly; serve.py is wiring, and the assertions at the bottom of
this file pin the parts of that wiring which can go wrong silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.deploy.fetch import SnapshotIdentity
from src.deploy.identity import (
    BUILD_INFO_FIELDS,
    PUBLISHABLE_IDENTITY_FIELDS,
    deployment_payload,
    publishable_identity,
    write_identity,
)

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "docker" / "serve.py"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
BUILD_SH = ROOT / "docker" / "build.sh"


def _identity(**overrides) -> SnapshotIdentity:
    fields = dict(
        label="full_state", bucket="dpic-prdw-snapshots", key="duckdb/database.duckdb",
        version_id="VER123", sha256="a" * 64, byte_size=1011363840,
        path="/var/snapshot/database.duckdb", verified_at="2026-09-05T00:00:00Z",
        aggregates_verified=True,
    )
    fields.update(overrides)
    return SnapshotIdentity(**fields)


# --- what may be published -------------------------------------------------

def test_the_bucket_key_and_local_path_are_never_published():
    """The route is unauthenticated. The bucket name is the one part of a
    private artifact's address useful to someone who does not have it."""

    payload = publishable_identity(_identity())
    assert "dpic-prdw-snapshots" not in json.dumps(payload)
    assert "duckdb/database.duckdb" not in json.dumps(payload)
    assert "/var/snapshot" not in json.dumps(payload)
    assert set(payload) == set(PUBLISHABLE_IDENTITY_FIELDS)


def test_the_published_fields_are_the_ones_the_issue_names():
    payload = publishable_identity(_identity())
    assert payload["label"] == "full_state"
    assert payload["version_id"] == "VER123"
    assert payload["sha256"] == "a" * 64
    assert payload["byte_size"] == 1011363840
    assert payload["aggregates_verified"] is True


def test_a_skipped_aggregate_gate_is_reported_as_skipped_not_omitted():
    payload = publishable_identity(_identity(aggregates_verified=False))
    assert payload["aggregates_verified"] is False


# --- persistence -----------------------------------------------------------

def test_write_identity_creates_its_parent_and_round_trips(tmp_path: Path):
    destination = tmp_path / "nested" / "identity.json"
    write_identity(_identity(), destination)
    assert json.loads(destination.read_text()) == publishable_identity(_identity())


# --- the served payload ----------------------------------------------------

def _build_info(tmp_path: Path, **overrides) -> Path:
    values = {
        "repo_commit": "b" * 40, "consumer_commit": "c" * 40, "image_tag": "abc1234-0f70811-arm64",
    }
    values.update(overrides)
    path = tmp_path / "BUILD_INFO"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_the_payload_carries_build_info_and_snapshot_identity(tmp_path: Path):
    build = _build_info(tmp_path)
    identity = tmp_path / "identity.json"
    write_identity(_identity(), identity)

    payload = deployment_payload(build, identity)
    assert payload["build"]["repo_commit"] == "b" * 40
    assert payload["build"]["consumer_commit"] == "c" * 40
    assert payload["build"]["image_tag"] == "abc1234-0f70811-arm64"
    assert payload["snapshot"]["version_id"] == "VER123"


def test_the_payload_is_json_serialisable(tmp_path: Path):
    build = _build_info(tmp_path)
    identity = tmp_path / "identity.json"
    write_identity(_identity(), identity)
    json.dumps(deployment_payload(build, identity))  # must not raise


@pytest.mark.parametrize("state", ["absent", "malformed", "not_an_object"])
def test_an_unusable_identity_file_reports_unavailable_rather_than_raising(
    tmp_path: Path, state: str
):
    """A dev `docker run` with no S3 access produces no identity file. The
    route must still answer -- a 500 here would look like an outage."""

    build = _build_info(tmp_path)
    identity = tmp_path / "identity.json"
    if state == "malformed":
        identity.write_text("{not json", encoding="utf-8")
    elif state == "not_an_object":
        identity.write_text("[1, 2, 3]", encoding="utf-8")

    payload = deployment_payload(build, identity)
    assert payload["snapshot"] == "unavailable"
    assert payload["build"]["repo_commit"] == "b" * 40


def test_a_missing_build_info_reports_every_field_unavailable(tmp_path: Path):
    payload = deployment_payload(tmp_path / "absent", tmp_path / "absent.json")
    assert payload["build"] == dict.fromkeys(BUILD_INFO_FIELDS, "unavailable")


# --- the wiring that cannot be exercised without docker --------------------

def test_the_route_is_registered_before_the_static_mount():
    """Order decides whether /deployment.json is a route or a static lookup.

    A mount at "/" registered first would match every path, and the request
    would fall through to index.html with a 200 -- the failure this criterion
    exists to prevent, and one that looks like success.
    """

    source = SERVE.read_text(encoding="utf-8")
    route = source.index('@app.get("/deployment.json")')
    mount = source.index("app.mount(")
    assert route < mount, "the route must be registered before the StaticFiles mount"


def test_the_dockerfile_declares_all_three_build_args_and_labels_them():
    source = DOCKERFILE.read_text(encoding="utf-8")
    for arg in ("REPO_COMMIT", "CONSUMER_COMMIT", "IMAGE_TAG"):
        assert re.search(rf"^ARG {arg}$", source, re.MULTILINE), f"ARG {arg}"
        assert f'"${arg}"' in source, f"{arg} is declared but never used in a label"
    assert "org.opencontainers.image.revision" in source
    assert "/app/BUILD_INFO" in source


def test_build_sh_passes_the_resolved_consumer_commit_not_the_ref():
    """CONSUMER_REF may be an abbreviation or a branch; a label recording
    "master" identifies nothing. The clone's resolved HEAD is always 40 hex."""

    source = BUILD_SH.read_text(encoding="utf-8")
    assert 'CONSUMER_COMMIT="$(git -C "$CTX/consumer" rev-parse HEAD)"' in source
    assert '--build-arg "CONSUMER_COMMIT=$CONSUMER_COMMIT"' in source
    assert '--build-arg "REPO_COMMIT=$REPO_COMMIT"' in source
    assert '--build-arg "IMAGE_TAG=$TAG"' in source


def test_the_entrypoint_still_fails_closed_after_gaining_the_flag():
    """--identity-out must not soften the guarantee that a task which cannot
    prove its database never serves."""

    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in source
    fetch = next(line for line in source.splitlines() if "scripts.fetch_snapshot" in line)
    assert "--identity-out" in fetch
    # No `|| true`, no `if`, no `set +e` around it: the command must stay bare
    # so a non-zero exit still kills the task.
    assert "||" not in fetch and not fetch.strip().startswith("if")
    assert "set +e" not in source


def test_fetch_snapshot_exposes_identity_out_and_defaults_to_off():
    from scripts.fetch_snapshot import parse_args

    assert parse_args(["m.json", "d.duckdb"]).identity_out is None
    assert parse_args(["m.json", "d.duckdb", "--identity-out", "i.json"]).identity_out == Path(
        "i.json"
    )


def test_a_failed_verification_writes_no_identity_file(tmp_path: Path, monkeypatch):
    """The file's existence must mean the database was proven.

    An identity written before or regardless of verification would let a task
    that failed its snapshot check still advertise a snapshot -- and the
    post-deploy verification this endpoint exists for would believe it.
    """

    import scripts.fetch_snapshot as cli
    from src.deploy.errors import SnapshotIntegrityError

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    destination = tmp_path / "db.duckdb"
    identity_out = tmp_path / "identity.json"

    class _Manifest:
        expectations_key = "duckdb/x.expectations.json"

    monkeypatch.setattr(cli, "load_manifest", lambda _p: _Manifest())
    monkeypatch.setattr(cli, "load_expectations", lambda _c, _m: None)
    monkeypatch.setattr(cli, "write_identity", _unreachable_write)

    def _fail(*_args, **_kwargs):
        raise SnapshotIntegrityError("sha256 mismatch")

    monkeypatch.setattr(cli, "fetch_snapshot", _fail)
    monkeypatch.setitem(
        __import__("sys").modules, "boto3", _FakeBoto3(),
    )

    code = cli.main([str(manifest), str(destination), "--identity-out", str(identity_out)])

    assert code == 1
    assert not identity_out.exists(), "an unverified task must not advertise a snapshot"


def _unreachable_write(*_args, **_kwargs):  # pragma: no cover - asserts it is never called
    raise AssertionError("write_identity ran despite a failed verification")


class _FakeBoto3:
    def client(self, *_args, **_kwargs):
        return object()


def test_the_provenance_layers_sit_below_pip_install():
    """A value that changes every commit must not invalidate the pip layer.

    ARG *declaration* invalidates nothing; the first instruction that expands
    one invalidates that layer and everything below. With the LABEL above
    `pip install`, every commit would reinstall every pinned package to
    produce a byte-identical layer.
    """

    source = DOCKERFILE.read_text(encoding="utf-8")
    pip = source.index("pip install")
    assert pip < source.index("LABEL org.opencontainers"), "LABEL must sit below pip install"
    assert pip < source.index("/app/BUILD_INFO"), "BUILD_INFO must sit below pip install"


def test_build_info_is_written_after_the_image_drops_privileges():
    """Written as `app` into a directory that user already owns, so it needs
    no chown -- and so a later chown cannot be forgotten."""

    source = DOCKERFILE.read_text(encoding="utf-8")
    assert source.index("\nUSER app") < source.index("/app/BUILD_INFO")
