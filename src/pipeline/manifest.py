"""Immutable raw-run manifests and atomic publication.

The publisher writes into a sibling staging directory and renames that
directory into place only after the manifest and every file digest are ready.
An existing run is never replaced.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Mapping
from uuid import uuid4

from .settings import validate_component

TERMINAL_STATES = frozenset({"complete", "failed", "aborted"})


class ManifestError(ValueError):
    """Raised for malformed, incomplete, or tampered raw runs."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _check_timestamp(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field_name} must be a non-empty ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field_name} is not ISO-8601") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_relative(path: str | Path, root: Path) -> Path:
    candidate = Path(path)
    if not str(path).strip() or candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ManifestError(f"unsafe relative path: {path}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ManifestError(f"path escapes run directory: {path}") from exc
    return candidate


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The complete identity and audit contract for one raw extraction."""

    source: str
    run_id: str
    schema_version: str
    code_sha: str
    config_hash: str
    requested_scope: Mapping[str, Any]
    observed_scope: Mapping[str, Any]
    started_at: str
    finished_at: str | None
    terminal_state: str
    counts: Mapping[str, int]
    files: Mapping[str, Mapping[str, Any]]
    failures: tuple[Any, ...] = ()
    parent_run_id: str | None = None
    resume_id: str | None = None
    privacy_class: str = "restricted"
    parent_resume_id: str | None = None

    def __post_init__(self) -> None:
        validate_component(self.source, "source")
        validate_component(self.run_id, "run_id")
        if self.terminal_state not in TERMINAL_STATES:
            raise ManifestError(
                f"terminal_state must be one of {sorted(TERMINAL_STATES)}"
            )
        for field_name in ("schema_version", "code_sha", "config_hash", "privacy_class"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"{field_name} must be a non-empty string")
        _check_timestamp(self.started_at, "started_at")
        if self.finished_at is not None:
            _check_timestamp(self.finished_at, "finished_at")
        if self.terminal_state in TERMINAL_STATES and self.finished_at is None:
            raise ManifestError("a terminal run must have finished_at")
        if not isinstance(self.requested_scope, Mapping) or not isinstance(
            self.observed_scope, Mapping
        ):
            raise ManifestError("requested_scope and observed_scope must be objects")
        if not isinstance(self.files, Mapping):
            raise ManifestError("files must be an object")
        for name, record in self.files.items():
            if not isinstance(name, str) or not isinstance(record, Mapping):
                raise ManifestError("each files entry must be a path and object")
            if not isinstance(record.get("sha256"), str) or len(record["sha256"]) != 64:
                raise ManifestError(f"files[{name!r}].sha256 must be a SHA-256 hex digest")
            if any(character not in "0123456789abcdef" for character in record["sha256"].lower()):
                raise ManifestError(f"files[{name!r}].sha256 must be a SHA-256 hex digest")
            if not isinstance(record.get("bytes"), int) or record["bytes"] < 0:
                raise ManifestError(f"files[{name!r}].bytes must be non-negative")
        for key, count in self.counts.items():
            if not isinstance(key, str) or not isinstance(count, int) or count < 0:
                raise ManifestError(f"counts[{key!r}] must be a non-negative integer")

    @property
    def file_hashes(self) -> dict[str, str]:
        return {name: str(record["sha256"]) for name, record in self.files.items()}

    @property
    def file_bytes(self) -> dict[str, int]:
        return {name: int(record["bytes"]) for name, record in self.files.items()}

    def to_dict(self) -> dict[str, Any]:
        files = {
            name: {"sha256": str(record["sha256"]), "bytes": int(record["bytes"])}
            for name, record in sorted(self.files.items())
        }
        parent_resume = self.parent_resume_id or self.parent_run_id or self.resume_id
        return {
            "source": self.source,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "code_sha": self.code_sha,
            "config_hash": self.config_hash,
            "requested_scope": dict(self.requested_scope),
            "observed_scope": dict(self.observed_scope),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_state": self.terminal_state,
            "counts": dict(self.counts),
            "files": files,
            "file_hashes": {name: row["sha256"] for name, row in files.items()},
            "file_bytes": {name: row["bytes"] for name, row in files.items()},
            "failures": list(self.failures),
            "parent_run_id": self.parent_run_id,
            "resume_id": self.resume_id,
            "parent_resume_id": parent_resume,
            "privacy_class": self.privacy_class,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunManifest":
        required = {
            "source", "run_id", "schema_version", "code_sha", "config_hash",
            "requested_scope", "observed_scope", "started_at", "finished_at",
            "terminal_state", "counts", "failures", "privacy_class",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ManifestError(f"manifest missing required fields: {', '.join(missing)}")
        files = value.get("files")
        if files is None:
            hashes = value.get("file_hashes", {})
            byte_counts = value.get("file_bytes", {})
            files = {
                name: {"sha256": digest, "bytes": byte_counts.get(name)}
                for name, digest in hashes.items()
            }
        if not isinstance(files, Mapping):
            raise ManifestError("files must be an object")
        for name, record in files.items():
            if not isinstance(name, str) or not isinstance(record, Mapping) or "sha256" not in record or "bytes" not in record:
                raise ManifestError(f"files[{name!r}] must contain sha256 and bytes")
        return cls(
            source=str(value["source"]),
            run_id=str(value["run_id"]),
            schema_version=str(value["schema_version"]),
            code_sha=str(value["code_sha"]),
            config_hash=str(value["config_hash"]),
            requested_scope=value["requested_scope"],
            observed_scope=value["observed_scope"],
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            terminal_state=value["terminal_state"],
            counts=value["counts"],
            files=files,
            failures=tuple(value["failures"]),
            parent_run_id=value.get("parent_run_id"),
            resume_id=value.get("resume_id"),
            parent_resume_id=value.get("parent_resume_id"),
            privacy_class=str(value["privacy_class"]),
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    run_path: Path
    manifest: RunManifest
    checked_files: int

    @property
    def valid(self) -> bool:
        return True

    def __bool__(self) -> bool:
        return self.valid


class RunPublisher:
    """Build and atomically publish one immutable raw run."""

    def __init__(
        self,
        raw_root: str | Path,
        source: str,
        run_id: str,
        *,
        schema_version: str = "1",
        code_sha: str = "unknown",
        config_hash: str = "unknown",
        requested_scope: Mapping[str, Any] | None = None,
        privacy_class: str = "restricted",
        parent_run_id: str | None = None,
        resume_id: str | None = None,
        parent_resume_id: str | None = None,
    ) -> None:
        self.raw_root = Path(raw_root).resolve()
        self.source = validate_component(source, "source")
        self.run_id = validate_component(run_id, "run_id")
        self.schema_version = schema_version
        self.code_sha = code_sha
        self.config_hash = config_hash
        self.requested_scope = dict(requested_scope or {})
        self.privacy_class = privacy_class
        self.parent_run_id = parent_run_id
        self.resume_id = resume_id
        self.parent_resume_id = parent_resume_id
        self.started_at = utc_now()
        self._staging: Path | None = None
        self._published = False

    @property
    def run_path(self) -> Path:
        return self.raw_root / self.source / self.run_id

    def __enter__(self) -> "RunPublisher":
        source_root = self.raw_root / self.source
        source_root.mkdir(parents=True, exist_ok=True)
        staging = source_root / f".{self.run_id}.staging-{uuid4().hex}"
        staging.mkdir()
        (staging / "payloads").mkdir()
        (staging / "audit.jsonl").touch()
        self._staging = staging
        return self

    def _require_open(self) -> Path:
        if self._staging is None or self._published:
            raise ManifestError("run publisher is not open")
        return self._staging

    def write_payload(self, relative_path: str | Path, payload: bytes | str | BinaryIO) -> Path:
        staging = self._require_open()
        rel = _safe_relative(relative_path, staging / "payloads")
        target = staging / "payloads" / rel
        # Refused here rather than in the CLI, because every writer routes
        # through this method: two --payload-tree roots sharing a relative
        # path, a tree path colliding with a --payload name, or a repeated
        # --payload all land on the same target. os.replace below would take
        # the last one silently, leaving the manifest inventorying fewer
        # files than the caller counted -- source bytes lost, with a green
        # run to show for it. A run publishes each path exactly once.
        if target.exists():
            raise ManifestError(
                f"payload already written: {rel}; a raw run publishes each path once "
                "(overlapping --payload-tree roots, or a --payload of the same name)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                if hasattr(payload, "read"):
                    while chunk := payload.read(1024 * 1024):
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        handle.write(chunk)
                else:
                    data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
                    handle.write(data)
            os.replace(temporary, target)
        except BaseException:
            # A stream that raises mid-read must not leave a partial temp
            # file behind in payloads/: publish() would otherwise inventory
            # and publish it under its random temp name.
            #
            # The replace is INSIDE this scope (#110). It used to sit after
            # the handler, so a failing rename left the fully-written temp
            # file in payloads/ under its random name -- and a caller that
            # caught the error and carried on to publish() would inventory it
            # as though it were a source file.
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        return target

    def append_audit(self, event: Mapping[str, Any]) -> None:
        staging = self._require_open()
        with (staging / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def publish(
        self,
        *,
        terminal_state: str = "complete",
        observed_scope: Mapping[str, Any] | None = None,
        counts: Mapping[str, int] | None = None,
        failures: list[Any] | tuple[Any, ...] | None = None,
    ) -> Path:
        staging = self._require_open()
        if terminal_state not in TERMINAL_STATES:
            raise ManifestError(f"unsupported terminal state: {terminal_state}")
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(staging.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            rel = path.relative_to(staging).as_posix()
            digest, byte_count = sha256_file(path)
            files[rel] = {"sha256": digest, "bytes": byte_count}
        manifest = RunManifest(
            source=self.source,
            run_id=self.run_id,
            schema_version=self.schema_version,
            code_sha=self.code_sha,
            config_hash=self.config_hash,
            requested_scope=self.requested_scope,
            observed_scope=dict(observed_scope or {}),
            started_at=self.started_at,
            finished_at=utc_now(),
            terminal_state=terminal_state,
            counts=dict(counts or {}),
            files=files,
            failures=tuple(failures or ()),
            parent_run_id=self.parent_run_id,
            resume_id=self.resume_id,
            parent_resume_id=self.parent_resume_id,
            privacy_class=self.privacy_class,
        )
        manifest_path = staging / "manifest.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=staging, delete=False) as handle:
            json.dump(manifest.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, manifest_path)
        destination = self.run_path
        if destination.exists():
            raise ManifestError(f"run already exists and is immutable: {destination}")
        os.replace(staging, destination)
        self._published = True
        self._staging = None
        return destination

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)


def load_manifest(run_path: str | Path) -> RunManifest:
    path = Path(run_path)
    try:
        value = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest at {path}") from exc
    return RunManifest.from_dict(value)


def validate_run(run_path: str | Path) -> ValidationReport:
    """Validate terminal state, file inventory, byte counts, and SHA-256s."""

    path = Path(run_path).resolve()
    if not path.is_dir():
        raise ManifestError(f"run directory does not exist: {path}")
    manifest = load_manifest(path)
    if manifest.source != path.parent.name or manifest.run_id != path.name:
        raise ManifestError("manifest source/run_id do not match run path")
    listed = set(manifest.files)
    actual = {
        child.relative_to(path).as_posix()
        for child in path.rglob("*")
        if child.is_file() and child.name != "manifest.json"
    }
    if actual != listed:
        missing = sorted(listed - actual)
        extra = sorted(actual - listed)
        raise ManifestError(f"file inventory mismatch (missing={missing}, extra={extra})")
    for rel, record in manifest.files.items():
        _safe_relative(rel, path)
        digest, byte_count = sha256_file(path / rel)
        if digest != record["sha256"] or byte_count != record["bytes"]:
            raise ManifestError(f"hash or byte count mismatch: {rel}")
    return ValidationReport(path, manifest, len(listed))


def approve_run(run_path: str | Path) -> ValidationReport:
    """Validate a run and permit only a complete run for downstream stages.

    Approval is represented by the returned, verified report; the immutable
    raw run is never modified with a mutable marker file.
    """

    report = validate_run(run_path)
    if report.manifest.terminal_state != "complete":
        raise ManifestError("only a complete, hash-verified run may be approved")
    return report
