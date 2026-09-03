"""Synthetic fixtures for the deployment snapshot tests.

Every database here is generated in a temp directory. No protected source data,
no real artifact, and no AWS call is involved.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError


def make_snapshot(path: Path, *, gp_rows: int = 3, with_view: bool = True) -> Path:
    """Write a small DuckDB file shaped loosely like the warehouse."""
    import duckdb

    conn = duckdb.connect(str(path))
    try:
        conn.execute("CREATE TABLE gram_panchayat(gp_lgd_code VARCHAR, gp_name VARCHAR)")
        conn.execute("CREATE TABLE plan(plan_code VARCHAR, gp_lgd_code VARCHAR, total_cost DOUBLE)")
        for index in range(gp_rows):
            conn.execute(
                "INSERT INTO gram_panchayat VALUES (?, ?)",
                [f"{100000 + index}", f"GP-{index}"],
            )
            conn.execute(
                "INSERT INTO plan VALUES (?, ?, ?)",
                [f"PLAN-{index}", f"{100000 + index}", 1000.50 + index],
            )
        if with_view:
            conn.execute(
                "CREATE VIEW v_plan AS SELECT p.plan_code, g.gp_name, p.total_cost "
                "FROM plan p JOIN gram_panchayat g USING (gp_lgd_code)"
            )
    finally:
        conn.close()
    return path


class FakeS3:
    """An in-memory stand-in for the subset of S3 the fetch helper uses.

    Deliberately hand-written rather than mocked from the implementation: a
    fake generated from the same assumptions as the code cannot falsify it.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str, str], bytes] = {}
        self.head_calls: list[tuple[str, str, str]] = []
        self.download_calls: list[tuple[str, str, str]] = []
        self.get_calls: list[tuple[str, str, str]] = []
        # Test hooks.
        self.head_content_length: int | None = None
        self.head_version_override: str | None = None
        self.truncate_to: int | None = None
        self.corrupt_byte: int | None = None
        self.fail_download_after: int | None = None
        self.reverse_chunks: bool = False

    def put(self, bucket: str, key: str, version_id: str, body: bytes) -> None:
        self._objects[(bucket, key, version_id)] = body

    def put_file(self, bucket: str, key: str, version_id: str, path: Path) -> None:
        self.put(bucket, key, version_id, path.read_bytes())

    def _lookup(self, bucket: str, key: str, version_id: str) -> bytes:
        try:
            return self._objects[(bucket, key, version_id)]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchVersion", "Message": "version not found"}},
                "HeadObject",
            ) from None

    def head_object(self, *, Bucket: str, Key: str, VersionId: str) -> dict[str, Any]:
        self.head_calls.append((Bucket, Key, VersionId))
        body = self._lookup(Bucket, Key, VersionId)
        return {
            "ContentLength": self.head_content_length
            if self.head_content_length is not None
            else len(body),
            "VersionId": self.head_version_override or VersionId,
        }

    def download_fileobj(
        self, bucket: str, key: str, fileobj: Any, ExtraArgs: dict[str, Any] | None = None
    ) -> None:
        version_id = (ExtraArgs or {}).get("VersionId", "")
        self.download_calls.append((bucket, key, version_id))
        body = self._lookup(bucket, key, version_id)

        if self.corrupt_byte is not None:
            index = self.corrupt_byte
            mutated = bytearray(body)
            mutated[index] = (mutated[index] + 1) % 256
            body = bytes(mutated)
        if self.truncate_to is not None:
            body = body[: self.truncate_to]

        if self.fail_download_after is not None:
            fileobj.write(body[: self.fail_download_after])
            fileobj.flush()
            raise ClientError(
                {"Error": {"Code": "RequestTimeout", "Message": "connection reset"}},
                "GetObject",
            )

        if self.reverse_chunks:
            # boto3's multipart download writes ranges out of order via seek.
            chunk = max(1, len(body) // 4)
            ranges = [(start, body[start : start + chunk]) for start in range(0, len(body), chunk)]
            for start, payload in reversed(ranges):
                fileobj.seek(start)
                fileobj.write(payload)
            fileobj.flush()
            return

        fileobj.write(body)
        fileobj.flush()

    def get_object(self, *, Bucket: str, Key: str, VersionId: str) -> dict[str, Any]:
        self.get_calls.append((Bucket, Key, VersionId))
        body = self._lookup(Bucket, Key, VersionId)
        return {"Body": io.BytesIO(body), "VersionId": VersionId}
