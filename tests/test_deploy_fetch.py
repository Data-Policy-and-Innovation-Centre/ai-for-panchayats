"""Falsification tests for snapshot retrieval.

Each test asserts a *negative*: a snapshot that is wrong in some specific way
must not reach the path the application opens. Synthetic databases and an
in-memory S3 fake only — no AWS call, no protected data.
"""

from __future__ import annotations

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

    with pytest.raises(KnownAnswerError, match="gram_panchayat has 3 rows, expected 999"):
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
    with pytest.raises(KnownAnswerError, match=r"plan_total.*> 0.01"):
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
    pinned = replace(manifest, expectations_key=expectations_key)

    loaded = load_expectations(s3, pinned)

    assert loaded is not None
    assert loaded.relation_row_counts == {"plan": 3}
    assert "relation_row_counts" not in pinned.to_json()


def test_a_manifest_without_expectations_loads_none(deployment):
    manifest, s3, _destination = deployment
    assert load_expectations(s3, manifest) is None
