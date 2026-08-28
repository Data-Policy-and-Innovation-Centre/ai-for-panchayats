"""Falsification tests for snapshot retrieval.

Each test asserts a *negative*: a snapshot that is wrong in some specific way
must not reach the path the application opens. Synthetic databases and an
in-memory S3 fake only — no AWS call, no protected data.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace

import pytest
from _deploy_helpers import FakeS3, make_snapshot

from src.deploy.errors import (
    KnownAnswerError,
    SnapshotIntegrityError,
    SnapshotStorageError,
    SnapshotUnavailableError,
)
from src.deploy.expectations import Expectations, KnownAnswerQuery
from src.deploy.fetch import fetch_snapshot, load_expectations
from src.deploy.manifest import build_manifest

BUCKET = "prdw-snapshots"
KEY = "duckdb/database_allgps.duckdb"
VERSION = "3HL4kqtJlcpXroDTDmJ.o4pFUrXUEFDT"


@pytest.fixture()
def deployment(tmp_path):
    """A pinned manifest, a populated fake S3, and an empty task-local path."""
    source = make_snapshot(tmp_path / "source.duckdb")
    manifest = build_manifest(source, bucket=BUCKET, key=KEY, version_id=VERSION)

    s3 = FakeS3()
    s3.put_file(BUCKET, KEY, VERSION, source)

    destination = tmp_path / "task" / "database.duckdb"
    return manifest, s3, destination


def _staging_files(destination):
    parent = destination.parent
    return sorted(p.name for p in parent.glob(f".{destination.name}.partial-*")) if parent.exists() else []


# ── the one path that must succeed ───────────────────────────────────────────


def test_verified_snapshot_is_published_and_identified(deployment):
    manifest, s3, destination = deployment

    identity = fetch_snapshot(manifest, destination, s3_client=s3)

    assert destination.is_file()
    assert destination.stat().st_size == manifest.byte_size
    assert identity.sha256 == manifest.sha256
    assert identity.version_id == VERSION
    assert identity.label == "provisional_full_state_snapshot"
    assert manifest.sha256 in identity.describe()
    assert _staging_files(destination) == [], "staging file left behind"


def test_two_tasks_report_the_same_identity(deployment, tmp_path):
    manifest, s3, destination = deployment
    other = tmp_path / "task-b" / "database.duckdb"

    first = fetch_snapshot(manifest, destination, s3_client=s3)
    second = fetch_snapshot(manifest, other, s3_client=s3)

    assert (first.sha256, first.version_id, first.byte_size) == (
        second.sha256,
        second.version_id,
        second.byte_size,
    )
    assert destination.read_bytes() == other.read_bytes()


def test_multipart_style_out_of_order_writes_still_verify(deployment):
    """boto3 writes ranges via seek; the digest is taken after, so this holds."""
    manifest, s3, destination = deployment
    s3.reverse_chunks = True

    identity = fetch_snapshot(manifest, destination, s3_client=s3)

    assert identity.sha256 == manifest.sha256
    assert destination.is_file()


# ── integrity failures ───────────────────────────────────────────────────────


def test_a_corrupt_byte_prevents_publication(deployment):
    manifest, s3, destination = deployment
    s3.corrupt_byte = manifest.byte_size // 2

    with pytest.raises(SnapshotIntegrityError, match="SHA-256"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert not destination.exists()
    assert _staging_files(destination) == []


def test_a_truncated_download_prevents_publication(deployment):
    manifest, s3, destination = deployment
    s3.truncate_to = manifest.byte_size - 512

    with pytest.raises(SnapshotIntegrityError, match="downloaded .* bytes"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert not destination.exists()
    assert _staging_files(destination) == []


def test_a_size_mismatch_is_caught_before_downloading(deployment):
    manifest, s3, destination = deployment
    s3.head_content_length = manifest.byte_size + 1

    with pytest.raises(SnapshotIntegrityError, match="manifest pins"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert s3.download_calls == [], "a size mismatch must not cost a 1 GB download"
    assert not destination.exists()


def test_a_different_object_version_is_rejected(deployment):
    manifest, s3, destination = deployment
    s3.head_version_override = "some-other-version"

    with pytest.raises(SnapshotIntegrityError, match="object version"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert not destination.exists()


def test_a_missing_object_version_fails_closed(deployment):
    manifest, s3, destination = deployment
    repinned = manifest.with_object_version(BUCKET, KEY, "version-that-was-deleted")

    with pytest.raises(SnapshotUnavailableError, match="cannot reach pinned snapshot"):
        fetch_snapshot(repinned, destination, s3_client=s3)

    assert not destination.exists()


def test_an_interrupted_download_leaves_nothing_behind(deployment):
    manifest, s3, destination = deployment
    s3.fail_download_after = manifest.byte_size // 3

    with pytest.raises(SnapshotUnavailableError, match="download of"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert not destination.exists()
    assert _staging_files(destination) == []


def test_a_failed_fetch_does_not_disturb_the_previous_snapshot(deployment):
    """Rollback safety: a bad release must not destroy the running database."""
    manifest, s3, destination = deployment
    fetch_snapshot(manifest, destination, s3_client=s3)
    previous = destination.read_bytes()

    s3.corrupt_byte = 0
    with pytest.raises(SnapshotIntegrityError):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert destination.read_bytes() == previous


def test_relations_that_disagree_with_the_manifest_are_rejected(deployment, tmp_path):
    """Right bytes for the wrong database is still the wrong database."""
    manifest, s3, destination = deployment

    substitute = make_snapshot(tmp_path / "substitute.duckdb", with_view=False)
    s3.put_file(BUCKET, KEY, VERSION, substitute)

    # Pin the substitute's bytes, but keep the relation inventory the real
    # warehouse has: the digest passes and the contents still betray it.
    substitute_manifest = build_manifest(substitute, bucket=BUCKET, key=KEY, version_id=VERSION)
    assert "v_plan" not in substitute_manifest.relations
    forged = replace(substitute_manifest, relations=manifest.relations)

    with pytest.raises(SnapshotIntegrityError, match=r"missing: \['v_plan'\]"):
        fetch_snapshot(forged, destination, s3_client=s3)

    assert not destination.exists()


# ── storage and aggregate gates ──────────────────────────────────────────────


def test_insufficient_task_storage_fails_before_downloading(deployment, monkeypatch):
    manifest, s3, destination = deployment
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(0, 0, manifest.byte_size)
    )

    with pytest.raises(SnapshotStorageError, match="bytes free"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert s3.head_calls == []
    assert not destination.exists()


def test_row_count_expectations_gate_publication(deployment):
    manifest, s3, destination = deployment
    wrong = Expectations(relation_row_counts={"gram_panchayat": 999})

    with pytest.raises(KnownAnswerError, match="gram_panchayat row count does not match"):
        fetch_snapshot(manifest, destination, s3_client=s3, expectations=wrong)

    assert not destination.exists()

    right = Expectations(relation_row_counts={"gram_panchayat": 3, "plan": 3})
    fetch_snapshot(manifest, destination, s3_client=s3, expectations=right)
    assert destination.is_file()


def test_known_answer_money_totals_honour_the_stated_contract(deployment):
    manifest, s3, destination = deployment
    total_sql = "SELECT round(sum(total_cost), 2) FROM snap.plan"
    exact_total = 1000.50 + 1001.50 + 1002.50

    drifted = Expectations(
        relation_row_counts={},
        queries=(KnownAnswerQuery(name="plan_total", sql=total_sql, expected=((exact_total + 0.05,),)),),
    )
    with pytest.raises(KnownAnswerError, match="plan_total"):
        fetch_snapshot(manifest, destination, s3_client=s3, expectations=drifted)
    assert not destination.exists()

    # A stated tolerance must still bound the drift, not wave it through.
    too_tight = Expectations(
        relation_row_counts={},
        queries=(
            KnownAnswerQuery(
                name="plan_total", sql=total_sql, expected=((exact_total + 0.05,),), tolerance=0.01
            ),
        ),
    )
    with pytest.raises(KnownAnswerError, match=r"plan_total.*tolerance"):
        fetch_snapshot(manifest, destination, s3_client=s3, expectations=too_tight)
    assert not destination.exists()

    tolerated = Expectations(
        relation_row_counts={},
        queries=(
            KnownAnswerQuery(
                name="plan_total", sql=total_sql, expected=((exact_total + 0.05,),), tolerance=0.1
            ),
        ),
    )
    fetch_snapshot(manifest, destination, s3_client=s3, expectations=tolerated)
    assert destination.is_file()


def test_expectations_must_state_a_tolerance_explicitly():
    from src.deploy import expectations as expectations_module
    from src.deploy.errors import SnapshotManifestError

    payload = {
        "schema_version": 1,
        "relation_row_counts": {},
        "known_answer_queries": [{"name": "total", "sql": "SELECT 1", "expected": [[1]]}],
    }
    with pytest.raises(SnapshotManifestError, match="must state a tolerance"):
        expectations_module.from_mapping(payload)


def test_expectations_are_loaded_from_private_s3_not_the_manifest(deployment):
    manifest, s3, _destination = deployment
    expectations_key = "duckdb/database_allgps.expectations.json"
    s3.put(
        BUCKET,
        expectations_key,
        "exp-v1",
        b'{"schema_version": 1, "relation_row_counts": {"plan": 3}}',
    )
    pinned = replace(
        manifest, expectations_key=expectations_key, expectations_version_id="exp-v1"
    )

    loaded = load_expectations(s3, pinned)

    assert loaded is not None
    assert loaded.relation_row_counts == {"plan": 3}
    assert "relation_row_counts" not in pinned.to_json()


def test_expectations_are_read_at_the_pinned_version(deployment):
    """A republished contract must not silently rebind an older manifest."""
    manifest, s3, _destination = deployment
    key = "duckdb/database_allgps.expectations.json"
    s3.put(BUCKET, key, "exp-v1", b'{"schema_version": 1, "relation_row_counts": {"plan": 3}}')
    s3.put(BUCKET, key, "exp-v2", b'{"schema_version": 1, "relation_row_counts": {"plan": 999}}')

    old = replace(manifest, expectations_key=key, expectations_version_id="exp-v1")
    assert load_expectations(s3, old).relation_row_counts == {"plan": 3}
    assert s3.get_calls[-1] == (BUCKET, key, "exp-v1")

    new = replace(manifest, expectations_key=key, expectations_version_id="exp-v2")
    assert load_expectations(s3, new).relation_row_counts == {"plan": 999}


def test_a_deleted_expectations_version_fails_closed(deployment):
    manifest, s3, _destination = deployment
    key = "duckdb/database_allgps.expectations.json"
    s3.put(BUCKET, key, "exp-v1", b'{"schema_version": 1, "relation_row_counts": {}}')
    pinned = replace(manifest, expectations_key=key, expectations_version_id="exp-gone")

    with pytest.raises(SnapshotUnavailableError, match="cannot read expectations"):
        load_expectations(s3, pinned)


def test_an_unpinned_expectations_key_is_rejected(deployment):
    """An unversioned gate makes the manifest non-deterministic."""
    from src.deploy.errors import SnapshotManifestError

    manifest, _s3, _destination = deployment
    with pytest.raises(SnapshotManifestError, match="requires expectations_version_id"):
        replace(manifest, expectations_key="duckdb/x.json")


def test_a_manifest_without_expectations_loads_none(deployment):
    manifest, s3, _destination = deployment
    assert load_expectations(s3, manifest) is None


def test_a_pinned_gate_cannot_be_skipped_by_omission(deployment):
    """A manifest that names expectations must not publish without them."""
    from src.deploy.errors import SnapshotManifestError

    manifest, s3, destination = deployment
    key = "duckdb/database_allgps.expectations.json"
    s3.put(BUCKET, key, "exp-v1", b'{"schema_version": 1, "relation_row_counts": {"plan": 3}}')
    pinned = replace(manifest, expectations_key=key, expectations_version_id="exp-v1")

    with pytest.raises(SnapshotManifestError, match="but none were supplied"):
        fetch_snapshot(pinned, destination, s3_client=s3)

    assert not destination.exists()
    assert s3.download_calls == []

    # Skipping stays possible, but only as a deliberate act.
    fetch_snapshot(pinned, destination, s3_client=s3, allow_missing_expectations=True)
    assert destination.is_file()


def test_a_file_that_is_not_a_duckdb_database_fails_as_an_integrity_error(deployment, tmp_path):
    """Not an unhandled duckdb exception escaping the SnapshotError hierarchy."""
    manifest, s3, destination = deployment
    junk = tmp_path / "junk.duckdb"
    payload = b"this is not a database" * 100
    junk.write_bytes(payload)
    s3.put_file(BUCKET, KEY, VERSION, junk)

    # Byte identity holds; only the contents are wrong.
    tampered = replace(
        manifest, byte_size=len(payload), sha256=hashlib.sha256(payload).hexdigest()
    )
    with pytest.raises(SnapshotIntegrityError, match="could not be opened as a DuckDB database"):
        fetch_snapshot(tampered, destination, s3_client=s3, allow_missing_expectations=True)

    assert not destination.exists()


def test_an_ungated_publish_is_distinguishable_from_a_verified_one(deployment):
    """Otherwise a skipped gate reports the same identity as a full check."""
    manifest, s3, destination = deployment

    ungated = fetch_snapshot(manifest, destination, s3_client=s3)
    assert ungated.aggregates_verified is False
    assert "aggregates=SKIPPED" in ungated.describe()

    gated = fetch_snapshot(
        manifest,
        destination,
        s3_client=s3,
        expectations=Expectations(relation_row_counts={"plan": 3}),
    )
    assert gated.aggregates_verified is True
    assert "aggregates=verified" in gated.describe()


def test_a_broken_expectations_query_is_not_reported_as_substitution(deployment):
    """A contract typo must not read as evidence the artifact was swapped."""
    manifest, s3, destination = deployment
    broken = Expectations(
        relation_row_counts={},
        queries=(
            KnownAnswerQuery(
                name="typo", sql="SELECT * FROM snap.no_such_table", expected=((1,),)
            ),
        ),
    )

    with pytest.raises(KnownAnswerError, match="could not be evaluated"):
        fetch_snapshot(manifest, destination, s3_client=s3, expectations=broken)

    assert not destination.exists()


def test_snapshot_identity_defaults_to_the_conservative_reading():
    """An external constructor must keep working, and assume no gate ran."""
    from src.deploy.fetch import SnapshotIdentity

    identity = SnapshotIdentity(
        label="l", bucket="b", key="k", version_id="v", sha256="a" * 64,
        byte_size=1, path="/tmp/x", verified_at="2026-08-27T00:00:00Z",
    )
    assert identity.aggregates_verified is False
    assert "aggregates=SKIPPED" in identity.describe()


def test_a_broken_gate_query_does_not_leak_its_sql(deployment):
    """The gate SQL lives in the private expectations object; logs are public."""
    manifest, s3, destination = deployment
    secret_sql = "SELECT secret_column FROM snap.no_such_table"
    broken = Expectations(
        relation_row_counts={},
        queries=(KnownAnswerQuery(name="probe", sql=secret_sql, expected=((1,),)),),
    )

    with pytest.raises(KnownAnswerError) as exc:
        fetch_snapshot(manifest, destination, s3_client=s3, expectations=broken)

    assert "secret_column" not in str(exc.value)
    assert "no_such_table" not in str(exc.value)
    assert not destination.exists()


def test_non_utf8_expectations_raise_a_typed_error(deployment):
    from src.deploy.errors import SnapshotManifestError

    manifest, s3, _destination = deployment
    key = "duckdb/bad.json"
    s3.put(BUCKET, key, "v1", b"\xff\xfe not utf-8")
    pinned = replace(manifest, expectations_key=key, expectations_version_id="v1")

    with pytest.raises(SnapshotManifestError, match="not UTF-8"):
        load_expectations(s3, pinned)


def test_a_local_write_failure_is_a_storage_error_not_an_s3_outage(deployment, monkeypatch):
    """Otherwise a full task volume sends an operator to IAM and bucket policy."""
    manifest, s3, destination = deployment
    real_open = open

    def failing_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("src.deploy.fetch.open", failing_open, raising=False)
    with pytest.raises(SnapshotStorageError, match="cannot write the snapshot"):
        fetch_snapshot(manifest, destination, s3_client=s3)

    assert not destination.exists()
