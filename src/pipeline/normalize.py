"""Canonical Parquet normalization for validated raw runs.

This module has no source-adapter code.  It consumes the immutable raw-run
contract and currently implements the eGramSwaraj JSON shape only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from .manifest import ManifestError, RunManifest, approve_run

KNOWN_ENVELOPE_KEYS = frozenset({"data", "response", "result", "records", "rows"})
SUPPORTED_KINDS = frozenset({"PL", "AA", "TA", "PP", "RE"})
KIND_RE = re.compile(r"(?i)(?P<year>\d{4})[_-](?P<kind>PL|AA|TA|PP|RE)(?:$|[_-])")
GP_RE = re.compile(r"^LGD[_-]?(?P<code>\d+)[_-](?P<name>.+)$", re.IGNORECASE)
ID_KEYS = ("activityCd", "activity_cd", "activityId", "activity_id", "id")
PROVENANCE_COLUMNS = (
    "row_id", "parent_row_id", "pos", "source_system", "source_run_id",
    "source_record_id", "schema_version", "source_file", "source_kind",
    "gp_code", "gram_panchayat_name", "fiscal_year", "plan_year",
    "business_id", "mapping_status",
)


class NormalizationError(ManifestError):
    """Raised when a normalized dataset cannot be safely published."""


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    source_file: str
    reason_code: str
    reason: str
    source_record_id: str | None = None
    raw_value: str | None = None

    def as_row(self, manifest: RunManifest) -> dict[str, Any]:
        return {
            "source_system": manifest.source,
            "source_run_id": manifest.run_id,
            "source_record_id": self.source_record_id,
            "schema_version": manifest.schema_version,
            "source_file": self.source_file,
            "source_kind": "",
            "reason_code": self.reason_code,
            "reason": self.reason,
            "raw_value": self.raw_value,
        }


@dataclass(slots=True)
class NormalizationResult:
    output_root: Path
    run_id: str
    tables: dict[str, tuple[Path, ...]] = field(default_factory=dict)
    quarantined: tuple[QuarantineRecord, ...] = ()

    @property
    def quarantine_count(self) -> int:
        return len(self.quarantined)


class AtomicParquetPublication:
    """Publish a complete Parquet tree with stale-output removal."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self._staging: Path | None = None
        self._published = False

    def __enter__(self) -> "AtomicParquetPublication":
        self.output_root.parent.mkdir(parents=True, exist_ok=True)
        self._staging = Path(tempfile.mkdtemp(
            prefix=f".{self.output_root.name}.staging-",
            dir=self.output_root.parent,
        ))
        return self

    @property
    def staging_root(self) -> Path:
        if self._staging is None or self._published:
            raise NormalizationError("Parquet publication is not open")
        return self._staging

    def write_rows(
        self,
        table_name: str,
        rows: list[Mapping[str, Any]],
        *,
        chunk_size: int,
        partition_by_fiscal_year: bool = True,
    ) -> tuple[Path, ...]:
        if chunk_size <= 0:
            raise NormalizationError("chunk_size must be positive")
        if not rows:
            return ()
        safe_name = re.sub(r"[^0-9A-Za-z_]+", "_", table_name).strip("_").lower()
        if not safe_name:
            raise NormalizationError("table name cannot be empty")
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        if partition_by_fiscal_year:
            for row in rows:
                year = row.get("fiscal_year")
                groups[str(year) if year not in (None, "") else "unknown"].append(row)
        else:
            groups[""] = rows
        paths: list[Path] = []
        for partition, grouped in sorted(groups.items()):
            directory = self.staging_root / safe_name
            if partition:
                # Avoid Hive's implicit partition-column injection: fiscal_year
                # is retained in every Parquet row and remains directly readable
                # with pyarrow. The directory still makes the partition explicit.
                directory /= f"fiscal_year-{_safe_partition(partition)}"
            directory.mkdir(parents=True, exist_ok=True)
            for offset in range(0, len(grouped), chunk_size):
                chunk = grouped[offset : offset + chunk_size]
                table = pa.Table.from_pylist(_normalise_rows(chunk))
                path = directory / f"part-{len(paths):05d}.parquet"
                pq.write_table(table, path, compression="zstd")
                paths.append(path.relative_to(self.staging_root))
        return tuple(paths)

    def publish(self) -> Path:
        staging = self.staging_root
        backup: Path | None = None
        try:
            if self.output_root.exists():
                backup = self.output_root.parent / f".{self.output_root.name}.previous-{uuid4().hex}"
                os.replace(self.output_root, backup)
            os.replace(staging, self.output_root)
            self._published = True
            self._staging = None
            if backup is not None:
                shutil.rmtree(backup)
            return self.output_root
        except Exception:
            if self.output_root.exists() and backup is not None:
                shutil.rmtree(self.output_root, ignore_errors=True)
            if backup is not None and backup.exists() and not self.output_root.exists():
                os.replace(backup, self.output_root)
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)


def _safe_partition(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value) or "unknown"


def _normalise_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column in row:
            if column not in seen:
                columns.append(column)
                seen.add(column)
    return [{column: _scalar(row.get(column)) for column in columns} for row in rows]


def _scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def to_records(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Return records and a reason code for malformed payloads.

    Only named, known envelopes are unwrapped.  Unknown mapping keys remain a
    domain record so a domain array is never silently promoted to top-level
    rows.  Empty known envelopes are valid and return zero records.
    """

    if isinstance(payload, list):
        if all(isinstance(item, Mapping) for item in payload):
            return [dict(item) for item in payload], None
        return [], "malformed_root_array"
    if not isinstance(payload, Mapping):
        return [], "malformed_root_value"
    named = [
        key for key, value in payload.items()
        if key in KNOWN_ENVELOPE_KEYS and isinstance(value, list)
    ]
    if len(named) == 1:
        values = payload[named[0]]
        if not values:
            return [], None
        if not all(isinstance(item, Mapping) for item in values):
            return [], "malformed_known_envelope"
        header = {key: value for key, value in payload.items()
                  if key != named[0] and not isinstance(value, (list, dict))}
        return [{**header, **dict(item)} for item in values], None
    return [dict(payload)], None


def _file_context(path: Path, payload_root: Path) -> tuple[str, str | None, str | None]:
    source_file = path.relative_to(payload_root).as_posix()
    match = KIND_RE.search(path.stem)
    if not match:
        return source_file, None, None
    return source_file, match.group("kind").upper(), match.group("year")


def _gp_context(path: Path, payload_root: Path) -> tuple[str | None, str | None]:
    for parent in (path.parent, *path.parents):
        if parent == payload_root.parent:
            break
        match = GP_RE.match(parent.name)
        if match:
            return match.group("code"), match.group("name").replace("_", " ").strip()
    return None, None


def _business_id(record: Mapping[str, Any]) -> str | None:
    for key in ID_KEYS:
        value = record.get(key)
        if value not in (None, "") and not isinstance(value, (list, dict)):
            return str(value)
    return None


def _flatten_scalars(record: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}_{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            for child_key, child_value in _flatten_scalars(value, name).items():
                values[child_key] = child_value
        elif not isinstance(value, list):
            values[name] = value
    return values


def _provenance(
    *,
    manifest: RunManifest,
    source_file: str,
    source_kind: str,
    year: str | None,
    gp_code: str | None,
    gp_name: str | None,
    row_id: str,
    source_record_id: str,
    parent_row_id: str | None,
    pos: int | None,
    business_id: str | None,
) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "parent_row_id": parent_row_id,
        "pos": pos,
        "source_system": manifest.source,
        "source_run_id": manifest.run_id,
        "source_record_id": source_record_id,
        "schema_version": manifest.schema_version,
        "source_file": source_file,
        "source_kind": source_kind,
        "gp_code": str(gp_code) if gp_code is not None else None,
        "gram_panchayat_name": gp_name,
        "fiscal_year": str(year) if year is not None else None,
        "plan_year": str(year) if year is not None else None,
        "business_id": business_id,
        "mapping_status": "unmapped" if source_kind == "RE" else "mapped",
    }


def _emit_record(
    record: Mapping[str, Any],
    *,
    manifest: RunManifest,
    source_file: str,
    source_kind: str,
    year: str | None,
    gp_code: str | None,
    gp_name: str | None,
    root_pos: int,
    table_name: str,
    tables: dict[str, list[dict[str, Any]]],
) -> None:
    root_key = "|".join(
        (manifest.source, manifest.run_id, source_kind, source_file,
         gp_code or "", year or "", str(root_pos))
    )
    business_id = _business_id(record)
    parent = _provenance(
        manifest=manifest, source_file=source_file, source_kind=source_kind,
        year=year, gp_code=gp_code, gp_name=gp_name, row_id=root_key,
        source_record_id=root_key, parent_row_id=None, pos=None, business_id=business_id,
    )
    parent.update(_flatten_scalars(record))
    tables[table_name].append(parent)
    _emit_children(
        record, parent_row_id=root_key, source_record_id=root_key, row_id_prefix=root_key,
        manifest=manifest, source_file=source_file, source_kind=source_kind, year=year,
        gp_code=gp_code, gp_name=gp_name, tables=tables, table_name=table_name,
        inherited_business_id=business_id,
    )


def _emit_children(
    record: Mapping[str, Any],
    *,
    parent_row_id: str,
    source_record_id: str,
    row_id_prefix: str,
    manifest: RunManifest,
    source_file: str,
    source_kind: str,
    year: str | None,
    gp_code: str | None,
    gp_name: str | None,
    tables: dict[str, list[dict[str, Any]]],
    table_name: str,
    inherited_business_id: str | None,
) -> None:
    for key, value in record.items():
        if not isinstance(value, list):
            continue
        child_table = f"{table_name}__{_sanitize(str(key))}"
        for position, element in enumerate(value):
            row_id = f"{row_id_prefix}/{_sanitize(str(key))}:{position}"
            business_id = (
                _business_id(element) if isinstance(element, Mapping) else None
            ) or inherited_business_id
            row = _provenance(
                manifest=manifest, source_file=source_file, source_kind=source_kind,
                year=year, gp_code=gp_code, gp_name=gp_name, row_id=row_id,
                source_record_id=source_record_id, parent_row_id=parent_row_id,
                pos=position, business_id=business_id,
            )
            if isinstance(element, Mapping):
                row.update(_flatten_scalars(element))
                tables[child_table].append(row)
                _emit_children(
                    element, parent_row_id=row_id, source_record_id=source_record_id,
                    row_id_prefix=row_id, manifest=manifest, source_file=source_file,
                    source_kind=source_kind, year=year, gp_code=gp_code, gp_name=gp_name,
                    tables=tables, table_name=child_table,
                    inherited_business_id=business_id,
                )
            else:
                row.update({"value": element, "value_kind": "scalar"})
                tables[child_table].append(row)


def _sanitize(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower() or "unnamed"


def normalize_egramswaraj(
    run_path: str | Path,
    output_root: str | Path,
    *,
    chunk_size: int = 100_000,
    kinds: Iterable[str] = SUPPORTED_KINDS,
) -> NormalizationResult:
    """Normalize a validated eGramSwaraj raw run into an atomic Parquet tree."""

    report = approve_run(run_path)
    manifest = report.manifest
    wanted = {str(kind).upper() for kind in kinds}
    if not wanted <= SUPPORTED_KINDS:
        raise NormalizationError(f"unsupported eGramSwaraj kinds: {sorted(wanted - SUPPORTED_KINDS)}")
    payload_root = Path(run_path).resolve() / "payloads"
    if not payload_root.is_dir():
        raise NormalizationError(f"raw run has no payloads directory: {payload_root}")
    tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quarantined: list[QuarantineRecord] = []
    for path in sorted(payload_root.rglob("*.json")):
        source_file, source_kind, year = _file_context(path, payload_root)
        if source_kind not in wanted:
            quarantined.append(QuarantineRecord(
                source_file, "unknown_source_kind", "filename does not identify a supported kind"
            ))
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            quarantined.append(QuarantineRecord(source_file, "malformed_json", str(exc)))
            continue
        records, reason = to_records(payload)
        if reason is not None:
            quarantined.append(QuarantineRecord(source_file, reason, "payload shape is invalid"))
            continue
        gp_code, gp_name = _gp_context(path, payload_root)
        table_name = source_kind.lower()
        for position, record in enumerate(records):
            _emit_record(
                record, manifest=manifest, source_file=source_file, source_kind=source_kind,
                year=year, gp_code=gp_code, gp_name=gp_name, root_pos=position,
                table_name=table_name, tables=tables,
            )
    staged_tables: dict[str, tuple[Path, ...]] = {}
    with AtomicParquetPublication(output_root) as publication:
        for table_name, rows in sorted(tables.items()):
            staged_tables[table_name] = publication.write_rows(
                table_name, rows, chunk_size=chunk_size,
            )
        if quarantined:
            qrows = [record.as_row(manifest) for record in quarantined]
            staged_tables["quarantine"] = publication.write_rows(
                "quarantine", qrows, chunk_size=chunk_size, partition_by_fiscal_year=False,
            )
        destination = publication.publish()
    published_tables = {
        name: tuple(destination / relative_path for relative_path in paths)
        for name, paths in staged_tables.items()
    }
    return NormalizationResult(destination, manifest.run_id, published_tables, tuple(quarantined))


def normalize_run(*args: Any, **kwargs: Any) -> NormalizationResult:
    """Stable generic entry point; eGramSwaraj is the first supported source."""

    return normalize_egramswaraj(*args, **kwargs)
