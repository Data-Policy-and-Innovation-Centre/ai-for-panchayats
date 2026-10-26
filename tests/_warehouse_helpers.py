"""Shared synthetic-fixture helpers for the warehouse test suite.

Not a test module itself (no ``test_`` prefix, so pytest does not collect
it). Everything here builds small, synthetic canonical snapshots and never
touches ``data/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from src.pipeline.manifest import RunPublisher
from src.pipeline.normalize import normalize_egramswaraj
from src.pipeline.snapshots import SnapshotRegistry, SnapshotSpec
from warehouse.config import WarehouseSettings, load_settings


def publish_raw_run(tmp_path: Path, run_id: str, payloads: dict[str, object], *,
                     source: str = "egramSwaraj") -> Path:
    """Publish an immutable raw run with JSON payloads, as the scraper would."""

    with RunPublisher(tmp_path / "raw", source, run_id) as publisher:
        for name, payload in payloads.items():
            value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            publisher.write_payload(name, value)
        return publisher.publish()


def normalize(run_path: Path, canonical_root: Path, **kwargs):
    return normalize_egramswaraj(run_path, canonical_root, **kwargs)


def make_settings(tmp_path: Path, *, canonical_root: Path | None = None,
                   db_path: Path | None = None) -> WarehouseSettings:
    return load_settings(
        project_root=tmp_path,
        canonical_root=canonical_root or (tmp_path / "canonical"),
        snapshots_path=tmp_path / "snapshots.yaml",
        db_path=db_path or (tmp_path / "out" / "panchayat.duckdb"),
    )


def registry(*specs: SnapshotSpec) -> SnapshotRegistry:
    return SnapshotRegistry(tuple(specs))


def approved(snapshot_id: str, source: str, run_id: str, schema_version: str = "1") -> SnapshotSpec:
    return SnapshotSpec(snapshot_id, source, run_id, schema_version, "approved")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_manual_snapshot(
    canonical_root: Path, *, source: str, run_id: str, schema_version: str = "1",
    tables: Mapping[str, list[dict]],
) -> Path:
    """Hand-build a valid canonical snapshot without going through the normalizer.

    Used for scenarios the gated eGramSwaraj normalizer cannot itself produce
    (a second, distinct source system, for cross-source provenance tests).
    Every invariant ``validate_canonical_manifest`` checks is satisfied: file
    hashes/byte counts/row counts match, and the file inventory is exact.
    """

    destination = canonical_root / source / run_id
    destination.mkdir(parents=True, exist_ok=True)
    table_records = {}
    for table_name, rows in tables.items():
        table_dir = destination / table_name
        table_dir.mkdir(parents=True, exist_ok=True)
        path = table_dir / "part-00000.parquet"
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        else:
            pq.write_table(pa.Table.from_pylist([], schema=pa.schema([])), path, compression="zstd")
        digest, size = _sha256_file(path)
        table_records[table_name] = {
            "row_count": len(rows),
            "files": [{"path": path.relative_to(destination).as_posix(), "sha256": digest, "bytes": size}],
        }
    manifest = {
        "source": source,
        "run_id": run_id,
        "raw_manifest_sha256": "0" * 64,
        "raw_manifest_identity": {"source": source, "run_id": run_id, "code_sha": "test", "config_hash": "test"},
        "schema_version": schema_version,
        "tables": table_records,
        "quarantine_count": 0,
        "terminal_state": "complete",
    }
    manifest_path = destination / "canonical_manifest.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination, delete=False) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        temporary = Path(handle.name)
    os.replace(temporary, manifest_path)
    return destination
