"""Canonical Parquet normalization for validated raw runs.

This module has no source-adapter code.  It consumes the immutable raw-run
contract and currently implements the eGramSwaraj JSON shape only.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from .manifest import ManifestError, RunManifest, approve_run

KNOWN_ENVELOPE_KEYS = frozenset({"data", "response", "result", "records", "rows"})
SUPPORTED_KINDS = frozenset({"PL", "AA", "TA", "PP", "RE"})
KIND_RE = re.compile(
    r"(?i)(?P<year>\d{4}(?:-\d{2,4})?)[_-](?P<kind>PL|AA|TA|PP|RE)(?:$|[_-])"
)
GP_RE = re.compile(r"^LGD[_-]?(?P<code>\d+)[_-](?P<name>.+)$", re.IGNORECASE)
ID_KEYS = ("activityCd", "activity_cd", "activityId", "activity_id", "id")
PROVENANCE_COLUMNS = (
    "row_id", "parent_row_id", "pos", "source_system", "source_run_id",
    "source_record_id", "schema_version", "source_file", "source_kind",
    "gp_code", "gram_panchayat_name", "fiscal_year", "plan_year",
    "business_id", "mapping_status",
)
PROVENANCE_SCHEMA = {
    column: pa.string() for column in PROVENANCE_COLUMNS
}
PROVENANCE_SCHEMA["pos"] = pa.int64()


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
    max_buffered_rows: int = 0
    quarantine_count_value: int = 0

    @property
    def quarantine_count(self) -> int:
        return self.quarantine_count_value or len(self.quarantined)


class AtomicParquetPublication:
    """Publish a complete Parquet tree with stale-output removal."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().resolve()
        self._staging: Path | None = None
        self._published = False
        self._part_numbers: dict[str, int] = defaultdict(int)

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
        schema: pa.Schema | None = None,
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
                normalised = _normalise_rows(chunk)
                if schema is not None:
                    normalised = [_coerce_for_schema(row, schema) for row in normalised]
                table = pa.Table.from_pylist(normalised, schema=schema)
                part_number = self._part_numbers[safe_name]
                self._part_numbers[safe_name] += 1
                path = directory / f"part-{part_number:05d}.parquet"
                pq.write_table(table, path, compression="zstd")
                paths.append(path.relative_to(self.staging_root))
        return tuple(paths)

    def write_empty(self, table_name: str, schema: pa.Schema) -> tuple[Path, ...]:
        """Publish a schema-correct zero-row table for an explicitly requested kind."""

        safe_name = re.sub(r"[^0-9A-Za-z_]+", "_", table_name).strip("_").lower()
        directory = self.staging_root / safe_name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "part-00000.parquet"
        pq.write_table(pa.Table.from_pylist([], schema=schema), path, compression="zstd")
        return (path.relative_to(self.staging_root),)

    def write_canonical_manifest(self, value: Mapping[str, Any]) -> Path:
        """Write and validate the publication manifest while still staged."""

        target = self.staging_root / "canonical_manifest.json"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.staging_root, delete=False
        ) as handle:
            json.dump(dict(value), handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, target)
        validate_canonical_manifest(self.staging_root)
        return target

    def publish(self) -> Path:
        staging = self.staging_root
        if self.output_root.exists():
            raise NormalizationError(f"canonical snapshot already exists and is immutable: {self.output_root}")
        os.replace(staging, self.output_root)
        self._published = True
        self._staging = None
        return self.output_root

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)


def _safe_partition(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value) or "unknown"


def _normalise_year(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d{4})(?:-(\d{2}|\d{4}))?", value)
    if not match:
        return value
    start = int(match.group(1))
    end = match.group(2)
    if end is None:
        return f"{start:04d}-{start + 1:04d}"
    if len(end) == 4:
        end_year = int(end)
    else:
        # Derive the century from the start year rather than assuming the
        # 2000s, so "1998-99" -> "1998-1999" instead of "1998-2099". A
        # two-digit end that would fall before the start year rolls over
        # into the next century, e.g. "1999-00" -> "1999-2000".
        century = (start // 100) * 100
        end_year = century + int(end)
        if end_year < start:
            end_year += 100
    return f"{start:04d}-{end_year:04d}"


def _value_type(value: Any) -> pa.DataType | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    return pa.string()


def _merge_type(left: pa.DataType | None, right: pa.DataType | None) -> pa.DataType | None:
    if left is None:
        return right
    if right is None or left == right:
        return left
    if {left, right} <= {pa.int64(), pa.float64()}:
        return pa.float64()
    return pa.string()


def _schema_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, pa.DataType]:
    result: dict[str, pa.DataType | None] = {}
    for row in rows:
        for column, value in row.items():
            result[column] = _merge_type(result.get(column), _value_type(value))
    return {column: value or pa.string() for column, value in result.items()}


def _arrow_schema(types: Mapping[str, pa.DataType]) -> pa.Schema:
    ordered = [column for column in PROVENANCE_COLUMNS if column in types]
    ordered += [column for column in types if column not in ordered]
    return pa.schema([pa.field(column, types[column]) for column in ordered])


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


def _coerce_for_schema(row: Mapping[str, Any], schema: pa.Schema) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for schema_field in schema:
        value = _scalar(row.get(schema_field.name))
        if value is None:
            result[schema_field.name] = None
        elif pa.types.is_string(schema_field.type):
            result[schema_field.name] = str(value)
        elif pa.types.is_float64(schema_field.type):
            result[schema_field.name] = float(value)
        elif pa.types.is_int64(schema_field.type):
            result[schema_field.name] = int(value)
        else:
            result[schema_field.name] = value
    return result


def _scalar(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def to_records(payload: Any) -> tuple[Iterable[dict[str, Any]], str | None]:
    """Return records and a reason code for malformed payloads.

    Only named, known envelopes are unwrapped.  Unknown mapping keys remain a
    domain record so a domain array is never silently promoted to top-level
    rows.  Empty known envelopes are valid and return zero records.

    The records are a lazy generator rather than a materialized list: the
    caller (`json.loads(path.read_text(...))`) already holds the whole
    parsed payload in memory, and eagerly building a second list of merged
    records here would double that for the duration of processing one file.
    Yielding lazily means at most one merged record is alive on top of the
    parsed payload at a time. This does not bound memory to chunk_size (the
    full payload is still parsed up front -- true streaming would need an
    incremental JSON parser), but it removes the extra full-size copy.
    """

    if isinstance(payload, list):
        if all(isinstance(item, Mapping) for item in payload):
            return (dict(item) for item in payload), None
        return (), "malformed_root_array"
    if not isinstance(payload, Mapping):
        return (), "malformed_root_value"
    present = [key for key in payload if key in KNOWN_ENVELOPE_KEYS]
    if any(not isinstance(payload[key], list) for key in present):
        return (), "malformed_known_envelope"
    named = present
    if len(named) == 1:
        values = payload[named[0]]
        if not values:
            return (), None
        if not all(isinstance(item, Mapping) for item in values):
            return (), "malformed_known_envelope"
        header = {key: value for key, value in payload.items()
                  if key != named[0] and not isinstance(value, (list, dict))}
        return ({**header, **dict(item)} for item in values), None
    return (dict(payload),), None


def _file_context(path: Path, payload_root: Path) -> tuple[str, str | None, str | None]:
    source_file = path.relative_to(payload_root).as_posix()
    match = KIND_RE.search(path.stem)
    if not match:
        return source_file, None, None
    return source_file, match.group("kind").upper(), _normalise_year(match.group("year"))


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


def _record_identity(record: Mapping[str, Any], business_id: str | None) -> str:
    """A stable, content-derived identity for a top-level record.

    Prefers a business identifier (e.g. activityCd) when present. Falling
    back to array position would let two records swap identities (and all
    child links) if the source ever returns them in a different order across
    runs, so the fallback instead hashes the record's own scalar content.
    Genuine duplicates (identical business_id or identical content) are
    disambiguated deterministically by their order of appearance within the
    same file, not by raw array position.
    """
    if business_id is not None:
        return f"id:{business_id}"
    content = json.dumps(
        _flatten_scalars(record), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    )
    return "content:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _record_rows(
    record: Mapping[str, Any],
    *,
    manifest: RunManifest,
    source_file: str,
    source_kind: str,
    year: str | None,
    gp_code: str | None,
    gp_name: str | None,
    identity_key: str,
    table_name: str,
):
    root_key = "|".join(
        (manifest.source, source_kind, source_file, gp_code or "", year or "", identity_key)
    )
    root_key = hashlib.sha256(root_key.encode("utf-8")).hexdigest()
    business_id = _business_id(record)
    parent = _flatten_scalars(record)
    parent.update(_provenance(
        manifest=manifest, source_file=source_file, source_kind=source_kind,
        year=year, gp_code=gp_code, gp_name=gp_name, row_id=root_key,
        source_record_id=root_key, parent_row_id=None, pos=None, business_id=business_id,
    ))
    yield table_name, parent
    yield from _child_rows(
        record, parent_row_id=root_key, source_record_id=root_key, row_id_prefix=root_key,
        manifest=manifest, source_file=source_file, source_kind=source_kind, year=year,
        gp_code=gp_code, gp_name=gp_name, table_name=table_name,
        inherited_business_id=business_id,
    )


def _child_rows(
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
    table_name: str,
    inherited_business_id: str | None,
):
    for key, value in record.items():
        if not isinstance(value, list):
            continue
        child_table = f"{table_name}__{_sanitize(str(key))}"
        for position, element in enumerate(value):
            row_id = f"{row_id_prefix}/{_sanitize(str(key))}:{position}"
            business_id = (
                _business_id(element) if isinstance(element, Mapping) else None
            ) or inherited_business_id
            provenance = _provenance(
                manifest=manifest, source_file=source_file, source_kind=source_kind,
                year=year, gp_code=gp_code, gp_name=gp_name, row_id=row_id,
                source_record_id=source_record_id, parent_row_id=parent_row_id,
                pos=position, business_id=business_id,
            )
            if isinstance(element, Mapping):
                # Apply provenance AFTER flattening source fields: a source
                # record containing a reserved name (row_id, source_file, ...)
                # must not silently overwrite the generated provenance value,
                # or child rows would reference an ID the parent no longer
                # exposes.
                row = _flatten_scalars(element)
                row.update(provenance)
                yield child_table, row
                yield from _child_rows(
                    element, parent_row_id=row_id, source_record_id=source_record_id,
                    row_id_prefix=row_id, manifest=manifest, source_file=source_file,
                    source_kind=source_kind, year=year, gp_code=gp_code, gp_name=gp_name,
                    table_name=child_table,
                    inherited_business_id=business_id,
                )
            else:
                row = dict(provenance)
                row.update({"value": element, "value_kind": "scalar"})
                yield child_table, row


def _sanitize(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower() or "unnamed"


def _iter_run_items(
    payload_root: Path,
    manifest: RunManifest,
    wanted: set[str],
):
    """Replay one raw run, yielding one row or quarantine record at a time."""

    for path in sorted(payload_root.rglob("*.json")):
        source_file, source_kind, year = _file_context(path, payload_root)
        if source_kind is None:
            yield None, QuarantineRecord(
                source_file, "unknown_source_kind", "filename does not identify a supported kind"
            )
            continue
        if source_kind not in wanted:
            # A recognized kind that simply was not requested (e.g. --kinds
            # PL excluding AA/TA/PP/RE) is an intentional exclusion, not a
            # malformed input -- skip it silently rather than quarantining a
            # perfectly valid file.
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            yield None, QuarantineRecord(source_file, "malformed_json", str(exc))
            continue
        records, reason = to_records(payload)
        if reason is not None:
            yield None, QuarantineRecord(source_file, reason, "payload shape is invalid")
            continue
        gp_code, gp_name = _gp_context(path, payload_root)
        table_name = source_kind.lower()
        identity_counts: dict[str, int] = {}
        for record in records:
            business_id = _business_id(record)
            identity = _record_identity(record, business_id)
            occurrence = identity_counts.get(identity, 0)
            identity_counts[identity] = occurrence + 1
            identity_key = identity if occurrence == 0 else f"{identity}#{occurrence}"
            yield from _record_rows(
                record, manifest=manifest, source_file=source_file, source_kind=source_kind,
                year=year, gp_code=gp_code, gp_name=gp_name, identity_key=identity_key,
                table_name=table_name,
            )


def _merge_table_types(
    target: dict[str, pa.DataType], rows: Iterable[Mapping[str, Any]]
) -> None:
    for column, value_type in _schema_from_rows(rows).items():
        target[column] = _merge_type(target.get(column), value_type) or pa.string()


def _canonical_file_record(root: Path, relative: Path) -> dict[str, Any]:
    digest, byte_count = _file_sha256(root / relative)
    return {"path": relative.as_posix(), "sha256": digest, "bytes": byte_count}


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_canonical_manifest(snapshot_root: str | Path) -> Mapping[str, Any]:
    """Validate a staged or published canonical snapshot before consumption."""

    root = Path(snapshot_root).resolve()
    try:
        value = json.loads((root / "canonical_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"cannot read canonical manifest at {root}") from exc
    required = {
        "source", "run_id", "raw_manifest_sha256", "raw_manifest_identity",
        "schema_version", "tables", "quarantine_count", "terminal_state",
    }
    missing = sorted(required - set(value))
    if missing:
        raise NormalizationError(f"canonical manifest missing fields: {', '.join(missing)}")
    if value["terminal_state"] != "complete":
        raise NormalizationError("canonical snapshot is not complete")
    identity = value["raw_manifest_identity"]
    if not isinstance(identity, Mapping) or identity.get("source") != value["source"] \
            or identity.get("run_id") != value["run_id"]:
        raise NormalizationError("canonical manifest raw identity does not match snapshot")
    if not isinstance(value["raw_manifest_sha256"], str) or len(value["raw_manifest_sha256"]) != 64:
        raise NormalizationError("canonical manifest has invalid raw manifest hash")
    files: list[Mapping[str, Any]] = []
    table_files: dict[str, list[Mapping[str, Any]]] = {}
    for table_name, table in value["tables"].items():
        if not isinstance(table, Mapping) or not isinstance(table.get("row_count"), int):
            raise NormalizationError(f"invalid canonical table metadata: {table_name}")
        table_records: list[Mapping[str, Any]] = []
        for record in table.get("files", []):
            if not isinstance(record, Mapping) or not {"path", "sha256", "bytes"} <= set(record):
                raise NormalizationError(f"invalid canonical file metadata: {table_name}")
            files.append(record)
            table_records.append(record)
        table_files[table_name] = table_records
    listed = set()
    for record in files:
        relative = Path(str(record["path"]))
        if relative.is_absolute() or ".." in relative.parts or relative.name == "canonical_manifest.json":
            raise NormalizationError(f"unsafe canonical file path: {relative}")
        actual = root / relative
        if not actual.is_file():
            raise NormalizationError(f"canonical file missing: {relative}")
        digest, byte_count = _file_sha256(actual)
        if digest != record["sha256"] or byte_count != record["bytes"]:
            raise NormalizationError(f"canonical file hash or byte count mismatch: {relative}")
        listed.add(relative.as_posix())
    for table_name, table_records in table_files.items():
        actual_row_count = sum(
            pq.ParquetFile(root / Path(str(record["path"]))).metadata.num_rows
            for record in table_records
        )
        declared_row_count = value["tables"][table_name]["row_count"]
        if actual_row_count != declared_row_count:
            raise NormalizationError(
                f"canonical table row_count mismatch: {table_name} "
                f"(declared {declared_row_count}, actual {actual_row_count})"
            )
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.parquet")
    }
    if actual_files != listed:
        raise NormalizationError("canonical manifest file inventory mismatch")
    if not isinstance(value["quarantine_count"], int) or value["quarantine_count"] < 0:
        raise NormalizationError("invalid quarantine count")
    return value


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
    if manifest.source.casefold() not in {"egramswaraj", "egramswaraj_api"}:
        raise NormalizationError(
            f"no eGramSwaraj normalizer is registered for source {manifest.source!r}"
        )
    wanted = {str(kind).upper() for kind in kinds}
    if not wanted <= SUPPORTED_KINDS:
        raise NormalizationError(f"unsupported eGramSwaraj kinds: {sorted(wanted - SUPPORTED_KINDS)}")
    payload_root = Path(run_path).resolve() / "payloads"
    if not payload_root.is_dir():
        raise NormalizationError(f"raw run has no payloads directory: {payload_root}")
    if chunk_size <= 0:
        raise NormalizationError("chunk_size must be positive")
    table_types: dict[str, dict[str, pa.DataType]] = {
        kind.lower(): dict(PROVENANCE_SCHEMA) for kind in wanted
    }
    table_counts: dict[str, int] = {kind.lower(): 0 for kind in wanted}
    quarantine_count = 0
    for table_name, value in _iter_run_items(payload_root, manifest, wanted):
        if table_name is None:
            quarantine_count += 1
            continue
        table_counts[table_name] = table_counts.get(table_name, 0) + 1
        _merge_table_types(table_types.setdefault(table_name, dict(PROVENANCE_SCHEMA)), [value])

    destination = Path(output_root).expanduser().resolve() / manifest.source / manifest.run_id
    staged_tables: dict[str, list[Path]] = {}
    observed_max_buffered_rows = 0
    with AtomicParquetPublication(destination) as publication:
        for table_name in sorted(table_types):
            schema = _arrow_schema(table_types[table_name])
            paths: list[Path] = []
            buffer: list[Mapping[str, Any]] = []
            for item_name, value in _iter_run_items(payload_root, manifest, wanted):
                if item_name != table_name:
                    continue
                buffer.append(value)
                observed_max_buffered_rows = max(observed_max_buffered_rows, len(buffer))
                if len(buffer) >= chunk_size:
                    paths.extend(publication.write_rows(
                        table_name, buffer, chunk_size=chunk_size, schema=schema,
                    ))
                    buffer.clear()
            if buffer:
                paths.extend(publication.write_rows(
                    table_name, buffer, chunk_size=chunk_size, schema=schema,
                ))
                buffer.clear()
            if not paths:
                paths.extend(publication.write_empty(table_name, schema))
            staged_tables[table_name] = paths
        if quarantine_count:
            qschema = _arrow_schema({
                "source_system": pa.string(), "source_run_id": pa.string(),
                "source_record_id": pa.string(), "schema_version": pa.string(),
                "source_file": pa.string(), "source_kind": pa.string(),
                "reason_code": pa.string(), "reason": pa.string(), "raw_value": pa.string(),
            })
            paths = []
            buffer = []
            for item_name, value in _iter_run_items(payload_root, manifest, wanted):
                if item_name is not None:
                    continue
                buffer.append(value.as_row(manifest))
                observed_max_buffered_rows = max(observed_max_buffered_rows, len(buffer))
                if len(buffer) >= chunk_size:
                    paths.extend(publication.write_rows(
                        "quarantine", buffer, chunk_size=chunk_size,
                        partition_by_fiscal_year=False, schema=qschema,
                    ))
                    buffer.clear()
            if buffer:
                paths.extend(publication.write_rows(
                    "quarantine", buffer, chunk_size=chunk_size,
                    partition_by_fiscal_year=False, schema=qschema,
                ))
            staged_tables["quarantine"] = paths
        files = {
            table_name: {
                "row_count": table_counts.get(table_name, quarantine_count if table_name == "quarantine" else 0),
                "files": [_canonical_file_record(publication.staging_root, path) for path in paths],
            }
            for table_name, paths in staged_tables.items()
        }
        canonical = {
            "source": manifest.source,
            "run_id": manifest.run_id,
            "raw_manifest_sha256": _file_sha256(Path(run_path).resolve() / "manifest.json")[0],
            "raw_manifest_identity": {
                "source": manifest.source, "run_id": manifest.run_id,
                "code_sha": manifest.code_sha, "config_hash": manifest.config_hash,
            },
            "schema_version": manifest.schema_version,
            "tables": files,
            "quarantine_count": quarantine_count,
            "terminal_state": "complete",
        }
        publication.write_canonical_manifest(canonical)
        validate_canonical_manifest(publication.staging_root)
        publication.publish()
    published_tables = {
        name: tuple(destination / relative_path for relative_path in paths)
        for name, paths in staged_tables.items()
    }
    # Quarantine rows are intentionally not retained in memory after staging.
    return NormalizationResult(
        destination, manifest.run_id, published_tables, (), observed_max_buffered_rows, quarantine_count
    )


def normalize_run(*args: Any, **kwargs: Any) -> NormalizationResult:
    """Stable generic entry point; eGramSwaraj is the first supported source."""

    return normalize_egramswaraj(*args, **kwargs)
