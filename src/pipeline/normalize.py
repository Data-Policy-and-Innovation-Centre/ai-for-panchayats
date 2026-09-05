"""Canonical Parquet normalization for validated raw runs.

This module has no source-adapter code.  It consumes the immutable raw-run
contract and currently implements the eGramSwaraj JSON shape only.
"""

from __future__ import annotations

import csv
import json
import hashlib
import os
import re
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .manifest import ManifestError, RunManifest, approve_run, load_manifest

KNOWN_ENVELOPE_KEYS = frozenset({"data", "response", "result", "records", "rows"})
SUPPORTED_KINDS = frozenset({"PL", "AA", "TA", "PP", "RE"})
# Not a source table: the sink for records no adapter could turn into rows.
QUARANTINE_TABLE = "quarantine"
KIND_RE = re.compile(
    r"(?i)(?P<year>\d{4}(?:-\d{2,4})?)[_-](?P<kind>PL|AA|TA|PP|RE)(?:$|[_-])"
)
GP_RE = re.compile(r"^LGD[_-]?(?P<code>\d+)[_-](?P<name>.+)$", re.IGNORECASE)
# Flat reference extracts, each published as its own raw run: one CSV, one
# canonical table, no per-GP folder tree and no fiscal year in the filename.
# Keyed by the raw run's `source` -- the same field the snapshot registry
# records -- so `normalize_run` picks the lane without a new CLI flag and
# without either lane having to sniff the other's payloads.
#
# The value is (source_kind, key_column). The key column stands in for the
# JSON lane's business id: these files carry no activityCd, and identity has
# to follow the row's own key rather than its line number (#110).
FLAT_CSV_SOURCES: dict[str, tuple[str, str]] = {
    "egramswaraj_profile": ("PROFILE", "basic_info_lgd"),
}
# Nested per-GP-per-year accounting JSON, published as its own raw run (#129).
# Keyed by the raw run's `source`, exactly as FLAT_CSV_SOURCES is, so
# `normalize_run` picks the lane from the manifest rather than a CLI flag.
#
# Not a `SUPPORTED_KINDS` entry, despite #129's wording. That set drives the
# eGramSwaraj lane, which finds a GP by parsing `LGD_<code>_<name>` out of a
# parent directory and a kind out of the filename. The accounting tree is
# `District/Block/GP/YYYY-YYYY.json` -- neither pattern matches, and every
# file would be skipped as unrecognized. The GP is carried *inside* the
# payload as `gp_lgd_code`, which is also the safer source: 505 GP names are
# shared, so a name-derived path could not identify a GP at all.
NESTED_JSON_SOURCES: dict[str, str] = {
    "egramswaraj_accounting": "VOUCHER",
}
# The two arrays a single accounting file carries, and the `direction` each
# one means. The warehouse's `voucher.direction` CHECK admits exactly these
# two spellings.
VOUCHER_DIRECTIONS: tuple[tuple[str, str], ...] = (
    ("receipts", "receipt"),
    ("payments", "payment"),
)
# Per-voucher fields kept from each array element. The file's sibling
# `receipt_count`/`payment_count`/`total_receipts`/`total_payments` keys are
# deliberately NOT here: they are per-GP-per-year aggregates repeated
# alongside every voucher, and #46 records that summing them once per voucher
# row is the specific mistake to avoid. They are recomputable from the rows
# themselves, so carrying them would add a second, disagreeing answer.
VOUCHER_FIELDS: tuple[str, ...] = (
    "month", "date", "voucher_no", "type", "amount", "voucher_id",
)
ID_KEYS = ("activityCd", "activity_cd", "activityId", "activity_id", "id")
# Identity keys for child collections, by the array's own JSON key (#163).
#
# `ID_KEYS` recognizes activity-style fields only, so every one of these
# collections fell back to hashing the whole element: an ordinary edit to an
# amount re-identified a logically unchanged row and moved every descendant
# prefix beneath it.
#
# Chosen by measurement, not by reading field names. Across 250 random GPs
# and every kind, each key below is present, non-blank, and unique within its
# array in 100% of the arrays observed:
#
#   fundList                                    76,902 arrays  (max len   4)
#   physicalProgressAssetStageWebService        28,502 arrays  (max len  41)
#   admApprovalSchemeWebService                 27,672 arrays  (max len   2)
#   physicalProgressAssetStageUploadWebService  23,773 arrays  (max len 177)
#   budgetaryAllocationSchemeWebService          1,500 arrays  (max len  58)
#
# Composites are used where the collection has a natural parent code, even
# though the component code alone was also unique in the sample: a component
# code is only meaningful under its scheme, and a key that is more specific
# than it needs to be costs nothing -- it moves only when a code moves, which
# is the same as the row being a different row.
#
# These feed `_record_identity` only. `business_id` is untouched.
CHILD_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "fundList": ("schemeCode", "componentCode"),
    "admApprovalSchemeWebService": ("wrkSchmCd", "wrkSchmCmpntCd"),
    "budgetaryAllocationSchemeWebService": ("schemeCode", "schemeComponentCode"),
    "physicalProgressAssetStageWebService": ("physclPrgrssAstStgCd",),
    "physicalProgressAssetStageUploadWebService": ("fileUploadId",),
}
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
    """The record's own business identifier, or None.

    Stripped, and blank-after-stripping is treated as absent -- the same test
    `_collection_key` applies to its parts. The two are alternatives in one
    expression (`_collection_key(...) or _business_id(...)`), so a `" "`
    accepted here while rejected there would be an identity that exists only
    because of which field it happened to come from.

    Stripping also matches the warehouse side rather than diverging from it:
    every `activity_code` column is written through `clean.to_code`, which
    strips. A padded id would otherwise make a parent's stripped
    `activity_code` and a child's unstripped one fail to match, and the child
    would be quarantined as an orphan of a row that is right there.

    No value in the scrape needs either: 420,821 id values sampled across 200
    GPs, none padded and none whitespace-only. This is here so the two
    spellings cannot disagree, not because they currently do.
    """

    for key in ID_KEYS:
        value = record.get(key)
        if value is None or isinstance(value, (list, dict)):
            continue
        text = value.strip() if isinstance(value, str) else str(value)
        if text:
            return text
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


def _canonical_json(value: Any) -> str:
    """A byte-stable serialisation of a record, nested content included.

    ``sort_keys`` makes mapping order irrelevant; list order is preserved,
    because for these payloads the order of a child array is content rather
    than presentation.
    """

    return json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    )


def _occurrence_key(identity: str, occurrence: int) -> str:
    """Identity plus its occurrence, as two components that cannot be confused.

    The obvious spelling -- the identity alone when first, and the identity
    with a "#" and a counter appended after -- puts the counter inside the
    identity's own namespace. A second element with business id ``A`` then
    yields the same key as a *first* element whose business id genuinely is
    ``A#1``, so the two share a ``row_id`` and their nested descendants
    collide under the shared prefix. Encoding both as a JSON pair keeps them
    separate whatever the business id contains.

    The positional child ids this replaced could not collide, so getting it
    wrong would have traded an ordering bug for a uniqueness one.
    """

    return _canonical_json([identity, occurrence])


def _record_identity(record: Any, business_id: str | None) -> str:
    """A stable, content-derived identity for a record or a child element.

    Prefers a business identifier (e.g. activityCd) when present. Falling
    back to array position would let two records swap identities (and all
    child links) if the source ever returned them in a different order across
    runs, so the fallback hashes the record's own content instead. Genuine
    duplicates (identical business_id or identical content) are disambiguated
    deterministically by their order of appearance among their siblings, not
    by raw array position.

    The hash covers the **whole** record, nested arrays included. Hashing only
    the flattened scalars -- as this did until #110 -- gave the same identity
    to two records that differed solely in their children, so the occurrence
    suffix decided which was which and reordering the two swapped every
    `row_id` and child link between them.
    """

    if business_id is not None:
        return f"id:{business_id}"
    return "content:" + hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _collection_key(collection: str | None, element: Mapping[str, Any]) -> str | None:
    """The identity key for one element of a known child collection.

    Returns ``None`` unless EVERY part is present and non-blank. A partial
    composite is not an identity: two elements agreeing on the half that
    happens to be filled in would share it, and the caller would treat that
    as a stable key rather than falling back to content.

    The parts are joined as JSON rather than with a separator, so a value
    containing the separator cannot forge another element's key.
    """

    parts = CHILD_IDENTITY_KEYS.get(collection or "")
    if not parts:
        return None
    values: list[str] = []
    for part in parts:
        value = element.get(part)
        if value is None or isinstance(value, (list, dict)):
            return None
        # Stripped before the blank test, not after: `" "` is as absent as
        # `""`, and accepting it would hand a lone row a stable-looking key
        # that survives arbitrary content changes -- the opposite of what a
        # missing key is supposed to do here. Stripped in the key too, so
        # `"S1"` and `" S1 "` are the same scheme rather than two.
        text = value.strip() if isinstance(value, str) else str(value)
        if not text:
            return None
        values.append(text)
    return _canonical_json(values)


def _element_identity(element: Any, *, collection: str | None = None) -> str:
    """`_record_identity` for anything that may not be a mapping.

    Child arrays hold scalars as well as records, and a scalar has no
    business id to look up.

    ``collection`` is the array's own JSON key, which selects the child
    identity key. It is used for identity ONLY -- the `business_id`
    provenance column is computed separately by the caller and keeps its
    inherit-from-parent behaviour, because two loaders read that column as an
    activity code (`transform._base_identity`, `transform.admin_approval_scheme`).
    Letting a scheme code reach it would put a wrong value in a loaded column,
    which is worse than the instability this fixes (#163).
    """

    if not isinstance(element, Mapping):
        return _record_identity(element, None)
    return _record_identity(
        element, _collection_key(collection, element) or _business_id(element)
    )


def _repeated_identities(identities: Iterable[str]) -> frozenset[str]:
    """Identities that more than one sibling carries.

    The first of the two passes `_refine_identity` needs. Holds one entry per
    *distinct* identity, which is what the occurrence counter downstream
    already costs, so this adds no term to the memory the caller was paying.

    Takes identities rather than elements so that a lane whose key is not one
    of `ID_KEYS` -- the flat-CSV lane, whose key column is named by its
    caller -- reuses this rather than growing a second copy beside it.
    """

    seen: set[str] = set()
    repeated: set[str] = set()
    for identity in identities:
        if identity in seen:
            repeated.add(identity)
        seen.add(identity)
    return frozenset(repeated)


def _refine_identity(element: Any, identity: str, repeated: frozenset[str]) -> str:
    """Break a tie between siblings sharing an identity, using their content.

    Two elements carrying the same business id but differing in their other
    fields hash to the same identity, so the occurrence counter alone decided
    which was which -- and reversing the array swapped their row_ids and every
    descendant link beneath them. That is the defect the business id was meant
    to close, reappearing wherever the id is not unique among its siblings.

    Content stays a *tiebreaker*, deliberately. Folding it into every identity
    is the obvious spelling and is worse: a record's content includes its own
    child arrays in order, so reordering a grandchild would move the parent
    and orphan the children underneath it. Only an identity a sibling
    duplicates gets refined, so an unambiguous record is untouched.

    Elements duplicated in both id and content are interchangeable; those
    still fall back to order of appearance, which is all that is left.
    """

    if identity not in repeated:
        return identity
    return _canonical_json([identity, _record_identity(element, None)])


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
        # Occurrence counts are per array, so two identical elements under the
        # same key are told apart while identical elements under *different*
        # keys do not interfere.
        occurrences: dict[str, int] = {}
        collection = str(key)
        repeated = _repeated_identities(
            _element_identity(e, collection=collection) for e in value
        )
        for position, element in enumerate(value):
            # The element's OWN business id, not the inherited one: inheriting
            # the parent's id here would give every sibling the same identity
            # and put us straight back on positional row_ids.
            own_business_id = (
                _business_id(element) if isinstance(element, Mapping) else None
            )
            identity = _refine_identity(
                element, _element_identity(element, collection=collection), repeated
            )
            seen = occurrences.get(identity, 0)
            occurrences[identity] = seen + 1
            identity_key = _occurrence_key(identity, seen)
            # Identity-derived rather than positional (#110). `pos` below still
            # carries the array index, so ordering is retained as metadata --
            # it just no longer decides which row is which. Truncated to 16 hex
            # (64 bits) because it only has to be unique among one parent's
            # children, and a full digest at every level makes a deeply nested
            # row_id unreadable without buying anything.
            token = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16]
            row_id = f"{row_id_prefix}/{_sanitize(str(key))}:{token}"
            business_id = own_business_id or inherited_business_id
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
        # A second call rather than a materialized list: `to_records` is a pure
        # function of the already-parsed payload, so it hands back a fresh
        # generator and the identity pass stays as streaming as the row pass.
        repeated = _repeated_identities(
            _element_identity(record) for record in to_records(payload)[0]
        )
        for record in records:
            identity = _refine_identity(
                record, _element_identity(record), repeated
            )
            occurrence = identity_counts.get(identity, 0)
            identity_counts[identity] = occurrence + 1
            identity_key = _occurrence_key(identity, occurrence)
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
    observed_max_buffered_rows = 0
    with AtomicParquetPublication(destination) as publication:
        # One pass over the input, one buffer per table, rather than a full
        # re-read and re-parse of every file for each table in turn.
        #
        # The old shape cost 1 schema-inference walk + one walk per table + a
        # quarantine walk. On the full state that is 12 walks of ~204,000
        # files -- ~118 GiB parsed to produce ~1.3 GiB of Parquet, with 88% of
        # wall-clock in json.loads. Eleven of those twelve walks parsed each
        # file only to discard it (#145).
        #
        # Memory is deliberately unchanged. Buffers share one budget: when the
        # rows held across every table reach chunk_size, the fullest buffers
        # are written out until the total is back under it. Giving each table
        # its own chunk_size threshold would have been simpler and would have
        # raised peak buffered rows by the number of tables.
        #
        # Fullest-first, and stopping at the budget rather than emptying every
        # buffer, is what protects the *small* tables. A table holding 40 rows
        # when a big table trips the budget would otherwise be written as a
        # 40-row Parquet file -- once per flush, so hundreds of near-empty
        # files per small table at full-state scale.
        #
        # It does not restore the old part sizes for the big tables, and does
        # not try to. Sharing one budget across N filling tables means the
        # fullest holds roughly chunk_size/N when it trips, so at 1,000 GPs
        # the run writes 718 part files where the old shape wrote 199, and
        # 227.4 MB where it wrote 218.0 (+4.3%, from per-file overhead).
        # Buying those back means a budget of N x chunk_size, which is a real
        # memory regression; --chunk-size is the knob for anyone who wants
        # that trade. Note it now bounds rows buffered in *total*, where it
        # used to bound rows per part per table.
        schemas = {name: _arrow_schema(types) for name, types in table_types.items()}
        schemas[QUARANTINE_TABLE] = _arrow_schema({
            "source_system": pa.string(), "source_run_id": pa.string(),
            "source_record_id": pa.string(), "schema_version": pa.string(),
            "source_file": pa.string(), "source_kind": pa.string(),
            "reason_code": pa.string(), "reason": pa.string(), "raw_value": pa.string(),
        })
        paths_by_table: dict[str, list[Path]] = {name: [] for name in schemas}
        buffers: dict[str, list[Mapping[str, Any]]] = {name: [] for name in schemas}
        buffered_rows = 0

        def flush(*, final: bool) -> None:
            nonlocal buffered_rows
            # Ties broken by name so the write order never depends on dict
            # insertion order, which depends on which file was read first.
            # (Order *within* a table is the walk order either way, so this
            # decides only which files rows land in, not their sequence.)
            for name in sorted(buffers, key=lambda n: (-len(buffers[n]), n)):
                if not final and buffered_rows < chunk_size:
                    return
                rows = buffers[name]
                if not rows:
                    continue
                paths_by_table[name].extend(publication.write_rows(
                    name, rows, chunk_size=chunk_size, schema=schemas[name],
                    partition_by_fiscal_year=name != QUARANTINE_TABLE,
                ))
                buffered_rows -= len(rows)
                rows.clear()

        for item_name, value in _iter_run_items(payload_root, manifest, wanted):
            if item_name is None:
                buffers[QUARANTINE_TABLE].append(value.as_row(manifest))
            else:
                # Present by construction: the inference pass above walked the
                # same input with the same `wanted`, so it saw every table name
                # this pass can produce.
                buffers[item_name].append(value)
            buffered_rows += 1
            observed_max_buffered_rows = max(observed_max_buffered_rows, buffered_rows)
            if buffered_rows >= chunk_size:
                flush(final=False)
        flush(final=True)

        for table_name in sorted(table_types):
            if not paths_by_table[table_name]:
                paths_by_table[table_name].extend(
                    publication.write_empty(table_name, schemas[table_name])
                )
        staged_tables = {name: paths_by_table[name] for name in sorted(table_types)}
        if quarantine_count:
            staged_tables[QUARANTINE_TABLE] = paths_by_table[QUARANTINE_TABLE]
        files = {
            table_name: {
                "row_count": table_counts.get(
                    table_name, quarantine_count if table_name == QUARANTINE_TABLE else 0
                ),
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


def normalize_flat_csv(
    run_path: str | Path,
    output_root: str | Path,
    *,
    source_kind: str,
    key_column: str,
    chunk_size: int = 100_000,
) -> NormalizationResult:
    """Normalize a validated one-row-per-entity CSV run into an atomic Parquet tree.

    A second lane rather than a CSV mode bolted onto the JSON one. Nothing
    the JSON lane does applies here: there is no `LGD_<code>_<name>` folder to
    parse a GP out of, no fiscal year in the filename, no envelope to unwrap
    and no nested child arrays to recurse into. What the two lanes do share is
    the *output* contract -- the same provenance columns, the same atomic
    publication, the same canonical manifest -- and that is the half a
    snapshot has to honour.

    The records are held in memory rather than streamed, which the identity
    pass requires anyway -- deciding which keys are duplicated needs the whole
    file before any row can be written. These extracts are one row per entity,
    so the ceiling is the number of GPs in Odisha (6,794 x 99 string columns,
    ~3 MB on disk); the JSON lane's chunked multi-table budget exists for a
    204,000-file walk and would be machinery with nothing to do here.
    `chunk_size` still bounds the part files that get written.
    """

    report = approve_run(run_path)
    manifest = report.manifest
    if chunk_size <= 0:
        raise NormalizationError("chunk_size must be positive")
    payload_root = Path(run_path).resolve() / "payloads"
    if not payload_root.is_dir():
        raise NormalizationError(f"raw run has no payloads directory: {payload_root}")
    csv_paths = sorted(payload_root.rglob("*.csv"))
    if len(csv_paths) != 1:
        # Not a convenience restriction. Two CSVs in one run would share a
        # run_id and a canonical table, so their rows would be
        # indistinguishable in provenance and their identities could collide.
        raise NormalizationError(
            f"a flat-CSV run must publish exactly one .csv payload; "
            f"{payload_root} has {len(csv_paths)}"
        )
    csv_path = csv_paths[0]
    source_file = csv_path.relative_to(payload_root).as_posix()
    table_name = source_kind.lower()

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or key_column not in reader.fieldnames:
            raise NormalizationError(
                f"{source_file} has no {key_column!r} column; "
                f"found {list(reader.fieldnames or ())}"
            )
        records = list(reader)

    # A blank key is not an error here -- 84 of the profile rows carry one, and
    # they must reach the warehouse's quarantine rather than be dropped where
    # nothing counts them. `_record_identity` falls back to content, so they
    # still get distinct row_ids.
    #
    # The key column stands in for the JSON lane's `_business_id`; everything
    # after that is the same two passes, using the same helpers. Mirroring
    # rather than re-spelling matters: the pre-#110 `f"{identity}#{n}"` form
    # put the counter inside the identity's own namespace, so a key that
    # literally read `X#1` collided with the second occurrence of `X`. LGD
    # codes are numeric and cannot, but this lane is generic -- #48's
    # spreadsheet brings its own key column.
    business_ids = [(r.get(key_column) or "").strip() or None for r in records]
    identities = [
        _record_identity(record, business_id)
        for record, business_id in zip(records, business_ids)
    ]
    repeated = _repeated_identities(identities)

    rows: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    for record, business_id, identity in zip(records, business_ids, identities):
        identity = _refine_identity(record, identity, repeated)
        seen = occurrences.get(identity, 0)
        occurrences[identity] = seen + 1
        root_key = hashlib.sha256("|".join(
            (manifest.source, source_kind, source_file, "", "",
             _occurrence_key(identity, seen))
        ).encode("utf-8")).hexdigest()
        row = _flatten_scalars(record)
        row.update(_provenance(
            manifest=manifest, source_file=source_file, source_kind=source_kind,
            year=None, gp_code=None, gp_name=None, row_id=root_key,
            source_record_id=root_key, parent_row_id=None, pos=None,
            business_id=business_id,
        ))
        rows.append(row)

    types = dict(PROVENANCE_SCHEMA)
    _merge_table_types(types, rows)
    schema = _arrow_schema(types)
    destination = Path(output_root).expanduser().resolve() / manifest.source / manifest.run_id
    with AtomicParquetPublication(destination) as publication:
        # No fiscal-year partitioning: the source has no fiscal year, so every
        # row would land in one `fiscal_year-unknown` directory that says
        # nothing and reads as missing data rather than as inapplicable.
        paths = publication.write_rows(
            table_name, rows, chunk_size=chunk_size, schema=schema,
            partition_by_fiscal_year=False,
        ) or publication.write_empty(table_name, schema)
        canonical = {
            "source": manifest.source,
            "run_id": manifest.run_id,
            "raw_manifest_sha256": _file_sha256(Path(run_path).resolve() / "manifest.json")[0],
            "raw_manifest_identity": {
                "source": manifest.source, "run_id": manifest.run_id,
                "code_sha": manifest.code_sha, "config_hash": manifest.config_hash,
            },
            "schema_version": manifest.schema_version,
            "tables": {
                table_name: {
                    "row_count": len(rows),
                    "files": [
                        _canonical_file_record(publication.staging_root, path)
                        for path in paths
                    ],
                },
            },
            "quarantine_count": 0,
            "terminal_state": "complete",
        }
        publication.write_canonical_manifest(canonical)
        validate_canonical_manifest(publication.staging_root)
        publication.publish()
    return NormalizationResult(
        destination, manifest.run_id,
        {table_name: tuple(destination / path for path in paths)},
        (), len(rows), 0,
    )


def normalize_accounting(
    run_path: str | Path,
    output_root: str | Path,
    *,
    source_kind: str,
    chunk_size: int = 100_000,
) -> NormalizationResult:
    """Normalize a nested per-GP-per-year accounting run to canonical Parquet (#129).

    A third lane, for the same reason the flat-CSV lane is a second one: the
    input shares nothing with the eGramSwaraj walk but its output contract.
    Each payload is one GP and one fiscal year, holding independent
    ``receipts`` and ``payments`` arrays; one canonical row is emitted per
    voucher, tagged with the ``direction`` its array means.

    **Money is carried as source text, not as a float.** ``json.load`` is
    given ``parse_float=str``, so ``"amount": 44280.0`` reaches Parquet as
    ``"44280.0"`` and the warehouse parses it with ``clean.to_decimal_money``
    into an exact ``Decimal``. This is not defensive habit: #46's acceptance
    is a rupee total matching to the paisa, and this source has ~3.7M rows.
    Binary floating point accumulated over that many addends does not
    reliably reproduce a decimal total, and the error would appear as a
    reconciliation failure with no bad row to point at.

    **The schema is declared, not inferred.** The other two lanes see every
    row before writing and can merge observed types. Streaming a 657 MB tree
    means writing the first batch before the last file is read, so the type
    of a column cannot depend on rows not yet seen -- a late file whose
    ``voucher_id`` happened to be numeric would otherwise change a column's
    type midway and produce parts that will not read back as one table.

    Rows are written in batches as files are read, so the whole tree is never
    resident. The identity pass is per file, which is sound here and is not
    in the flat-CSV lane: a canonical ``row_id`` already mixes in the source
    file, and one file is exactly one GP and fiscal year, so two vouchers can
    only collide if they collide *within* that pair -- which is precisely the
    natural key ``(gp_lgd_code, fiscal_year, voucher_no)`` that #46 verified
    unique. A repeat inside one file is still refined by content rather than
    silently deduplicated, because a genuine duplicate is a source defect for
    the warehouse's quarantine to count, not for this lane to hide.
    """

    report = approve_run(run_path)
    manifest = report.manifest
    if chunk_size <= 0:
        raise NormalizationError("chunk_size must be positive")
    payload_root = Path(run_path).resolve() / "payloads"
    if not payload_root.is_dir():
        raise NormalizationError(f"raw run has no payloads directory: {payload_root}")
    json_paths = sorted(payload_root.rglob("*.json"))
    if not json_paths:
        raise NormalizationError(f"accounting run publishes no .json payloads: {payload_root}")

    table_name = source_kind.lower()
    types = dict(PROVENANCE_SCHEMA)
    for column in ("direction", *VOUCHER_FIELDS):
        types[column] = pa.string()
    schema = _arrow_schema(types)

    destination = Path(output_root).expanduser().resolve() / manifest.source / manifest.run_id
    total_rows = 0
    paths: list[Path] = []
    pending: list[dict[str, Any]] = []

    with AtomicParquetPublication(destination) as publication:
        def flush() -> None:
            nonlocal pending
            if pending:
                paths.extend(publication.write_rows(
                    table_name, pending, chunk_size=chunk_size, schema=schema,
                ))
                pending = []

        for json_path in json_paths:
            source_file = json_path.relative_to(payload_root).as_posix()
            rows = _accounting_rows(json_path, source_file, manifest, source_kind)
            total_rows += len(rows)
            pending.extend(rows)
            # Batched by row count rather than by file: a per-file flush would
            # write ~38,600 tiny Parquet parts, and one flush at the end would
            # defeat the streaming this lane exists for.
            if len(pending) >= chunk_size:
                flush()
        flush()

        if not paths:
            paths.extend(publication.write_empty(table_name, schema))
        canonical = {
            "source": manifest.source,
            "run_id": manifest.run_id,
            "raw_manifest_sha256": _file_sha256(Path(run_path).resolve() / "manifest.json")[0],
            "raw_manifest_identity": {
                "source": manifest.source, "run_id": manifest.run_id,
                "code_sha": manifest.code_sha, "config_hash": manifest.config_hash,
            },
            "schema_version": manifest.schema_version,
            "tables": {
                table_name: {
                    "row_count": total_rows,
                    "files": [
                        _canonical_file_record(publication.staging_root, path)
                        for path in paths
                    ],
                },
            },
            "quarantine_count": 0,
            "terminal_state": "complete",
        }
        publication.write_canonical_manifest(canonical)
        validate_canonical_manifest(publication.staging_root)
        publication.publish()
    return NormalizationResult(
        destination, manifest.run_id,
        {table_name: tuple(destination / path for path in paths)},
        (), total_rows, 0,
    )


def _accounting_rows(
    json_path: Path, source_file: str, manifest: RunManifest, source_kind: str,
) -> list[dict[str, Any]]:
    """One canonical row per voucher in a single GP/fiscal-year payload."""

    try:
        # parse_float=str keeps the source's exact decimal text; see the
        # money note in `normalize_accounting`.
        payload = json.loads(json_path.read_text(encoding="utf-8"), parse_float=str)
    except (OSError, json.JSONDecodeError) as error:
        raise NormalizationError(f"unreadable accounting payload {source_file}: {error}") from error
    if not isinstance(payload, dict):
        raise NormalizationError(
            f"accounting payload {source_file} is {type(payload).__name__}, expected an object"
        )

    gp_code = _blank_to_none(payload.get("gp_lgd_code"))
    gp_name = _blank_to_none(payload.get("gp_name"))
    year = _blank_to_none(payload.get("year"))
    # A payload naming no GP cannot be attributed, and the file path cannot
    # stand in for it: the tree is keyed by GP *name*, and 505 of those are
    # shared. Loading such a row would attach real money to a guess.
    if gp_code is None:
        raise NormalizationError(f"accounting payload {source_file} carries no gp_lgd_code")
    if year is None:
        raise NormalizationError(f"accounting payload {source_file} carries no year")

    records: list[tuple[str, Mapping[str, Any]]] = []
    for array_key, direction in VOUCHER_DIRECTIONS:
        entries = payload.get(array_key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise NormalizationError(
                f"accounting payload {source_file} has {array_key!r} as "
                f"{type(entries).__name__}, expected a list"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise NormalizationError(
                    f"accounting payload {source_file} has a non-object entry in {array_key!r}"
                )
            records.append((direction, entry))

    business_ids = [_blank_to_none(entry.get("voucher_no")) for _, entry in records]
    identities = [
        _record_identity(entry, business_id)
        for (_, entry), business_id in zip(records, business_ids)
    ]
    repeated = _repeated_identities(identities)

    rows: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    for (direction, entry), business_id, identity in zip(records, business_ids, identities):
        identity = _refine_identity(entry, identity, repeated)
        seen = occurrences.get(identity, 0)
        occurrences[identity] = seen + 1
        row_id = hashlib.sha256("|".join(
            (manifest.source, source_kind, source_file, gp_code, year,
             _occurrence_key(identity, seen))
        ).encode("utf-8")).hexdigest()
        row: dict[str, Any] = {"direction": direction}
        for column in VOUCHER_FIELDS:
            row[column] = _blank_to_none(entry.get(column))
        row.update(_provenance(
            manifest=manifest, source_file=source_file, source_kind=source_kind,
            year=year, gp_code=gp_code, gp_name=gp_name, row_id=row_id,
            source_record_id=row_id, parent_row_id=None, pos=None,
            business_id=business_id,
        ))
        rows.append(row)
    return rows


def _blank_to_none(value: Any) -> str | None:
    """Source text, stripped; blank-after-stripping is absent.

    The same test `_business_id` and `_collection_key` apply, so a padded
    voucher number cannot become an identity that differs from its own
    stripped spelling in the warehouse (every code column goes through
    `clean.to_code`, which strips).
    """

    if value is None or isinstance(value, (list, dict)):
        return None
    text = value.strip() if isinstance(value, str) else str(value)
    return text or None


def normalize_run(
    run_path: str | Path, output_root: str | Path, **kwargs: Any,
) -> NormalizationResult:
    """Stable generic entry point; the raw run's own `source` picks the lane.

    Dispatching on the manifest rather than on a CLI flag keeps the choice
    where it cannot be got wrong: a run published as `egramswaraj_profile`
    cannot be normalized as if it were the JSON scrape, whatever the caller
    passes. Reading the manifest twice (here and again inside the lane) is
    cheap next to the walk that follows.
    """

    source = load_manifest(run_path).source.casefold()
    nested = NESTED_JSON_SOURCES.get(source)
    if nested is not None:
        kwargs.pop("kinds", None)
        return normalize_accounting(run_path, output_root, source_kind=nested, **kwargs)
    flat = FLAT_CSV_SOURCES.get(source)
    if flat is None:
        return normalize_egramswaraj(run_path, output_root, **kwargs)
    source_kind, key_column = flat
    kwargs.pop("kinds", None)
    return normalize_flat_csv(
        run_path, output_root, source_kind=source_kind, key_column=key_column, **kwargs,
    )
