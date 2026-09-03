"""Manifest contract tests. Synthetic databases only."""

from __future__ import annotations

import hashlib
import json

import pytest
from _deploy_helpers import make_snapshot

from src.deploy.errors import SnapshotManifestError
from src.deploy.manifest import (
    PROVISIONAL_LABEL,
    SnapshotManifest,
    attach_read_only,
    build_manifest,
    digest_file,
    from_mapping,
    load_manifest,
    read_relations,
)


@pytest.fixture()
def snapshot(tmp_path):
    return make_snapshot(tmp_path / "snap.duckdb")


def _valid_kwargs(**overrides):
    kwargs = {
        "label": PROVISIONAL_LABEL,
        "byte_size": 1024,
        "sha256": "a" * 64,
        "relations": ("gram_panchayat", "plan", "v_plan"),
        "bucket": "snapshots",
        "key": "duckdb/allgps.duckdb",
        "version_id": "v1",
        "duckdb_library_version": "1.5.5",
        "created_at": "2026-08-27T00:00:00Z",
    }
    kwargs.update(overrides)
    return kwargs


def test_build_manifest_records_bytes_and_relations(snapshot):
    manifest = build_manifest(snapshot, bucket="b", key="k", version_id="v")

    expected_sha, expected_size = digest_file(snapshot)
    assert manifest.sha256 == expected_sha
    assert manifest.byte_size == expected_size
    assert manifest.byte_size == snapshot.stat().st_size
    assert manifest.relations == ("gram_panchayat", "plan", "v_plan")
    assert manifest.label == PROVISIONAL_LABEL


def test_build_manifest_rejects_a_missing_artifact(tmp_path):
    with pytest.raises(SnapshotManifestError, match="no snapshot artifact"):
        build_manifest(tmp_path / "absent.duckdb", bucket="b", key="k", version_id="v")


def test_digest_file_is_chunk_boundary_safe(tmp_path, monkeypatch):
    """The digest must not depend on how the read happens to be chunked."""
    from src.deploy import manifest as manifest_module

    payload = bytes(range(256)) * 64  # 16 KiB
    target = tmp_path / "blob.bin"
    target.write_bytes(payload)
    expected = (hashlib.sha256(payload).hexdigest(), len(payload))

    for chunk in (1, 255, len(payload) - 1, len(payload), len(payload) + 1):
        monkeypatch.setattr(manifest_module, "_DIGEST_CHUNK", chunk)
        assert digest_file(target) == expected


def test_digest_file_handles_an_empty_file(tmp_path):
    target = tmp_path / "empty.bin"
    target.write_bytes(b"")
    assert digest_file(target) == (hashlib.sha256(b"").hexdigest(), 0)


def test_relations_are_read_read_only(snapshot):
    assert read_relations(snapshot) == ("gram_panchayat", "plan", "v_plan")

    with attach_read_only(snapshot) as conn:
        with pytest.raises(Exception):
            conn.execute("INSERT INTO snap.gram_panchayat VALUES ('999999', 'nope')")


def test_json_round_trip(tmp_path, snapshot):
    manifest = build_manifest(snapshot, bucket="b", key="k", version_id="v")
    path = tmp_path / "manifest.json"
    path.write_text(manifest.to_json(), encoding="utf-8")

    assert load_manifest(path) == manifest


def test_manifest_json_carries_no_row_counts(snapshot):
    """The public manifest must stay structural: names, never aggregates."""
    manifest = build_manifest(snapshot, bucket="b", key="k", version_id="v")
    payload = json.loads(manifest.to_json())

    assert set(payload) == {
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
    assert all(isinstance(name, str) for name in payload["relations"])


def test_with_object_version_repins_without_mutating(snapshot):
    manifest = build_manifest(snapshot, bucket="b", key="k", version_id="placeholder")
    repinned = manifest.with_object_version("bucket2", "key2", "real-version")

    assert manifest.version_id == "placeholder"
    assert repinned.version_id == "real-version"
    assert repinned.sha256 == manifest.sha256


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sha256": "A" * 64}, "64 lowercase hexadecimal"),
        ({"sha256": "abc"}, "64 lowercase hexadecimal"),
        ({"byte_size": 0}, "positive integer"),
        ({"byte_size": True}, "positive integer"),
        ({"byte_size": "1024"}, "positive integer"),
        ({"relations": ()}, "at least one table or view"),
        ({"relations": ("plan", "gram_panchayat")}, "must be sorted"),
        ({"relations": ("plan", "plan")}, "must not repeat"),
        ({"relations": ("plan", "")}, "non-empty string"),
        ({"bucket": ""}, "bucket must be a non-empty string"),
        ({"version_id": "  "}, "version_id must be a non-empty string"),
        ({"created_at": "not-a-date"}, "ISO-8601"),
        ({"schema_version": 2}, "unsupported manifest schema_version"),
    ],
)
def test_invalid_manifests_are_rejected(overrides, message):
    with pytest.raises(SnapshotManifestError, match=message):
        SnapshotManifest(**_valid_kwargs(**overrides))


def test_from_mapping_rejects_unexpected_and_missing_fields():
    payload = SnapshotManifest(**_valid_kwargs()).to_mapping()

    with pytest.raises(SnapshotManifestError, match="unexpected fields: surprise"):
        from_mapping({**payload, "surprise": 1})

    del payload["sha256"]
    with pytest.raises(SnapshotManifestError, match="missing fields: sha256"):
        from_mapping(payload)


def test_load_manifest_rejects_malformed_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotManifestError, match="not valid JSON"):
        load_manifest(path)
