#!/usr/bin/env python3
"""Emit the private aggregates a deployed task checks itself against (#95).

The manifest pins TWO objects: the artifact and this one. `src/deploy/fetch.py`
runs these row counts against the downloaded database at container startup and
refuses to serve if any disagrees, and `scripts/verify_deployment.py` fails the
deploy when a task reports `aggregates=SKIPPED`. So a rebuild that publishes a
new artifact without a matching expectations object cannot be deployed at all.

Nothing generated this file before -- `publish_snapshot.py` uploads only the
`.duckdb`, and `build_snapshot_manifest.py` accepts `--expectations-key` while
having no way to produce what it points at. The numbers were evidently written
by hand once, in August, and would silently reject every artifact built since.

It stays OUT of the repository on purpose: these are the proprietary aggregates
the deployment gate exists to protect. Publish it to S3 beside the artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

# Bookkeeping, not data. Its row count is a property of how dirty the input
# happened to be, so pinning it would fail a build that quarantined one row
# more than the last -- a false alarm on the gate that must not cry wolf.
EXCLUDED = frozenset({"quarantine"})


def relation_row_counts(db: Path) -> dict[str, int]:
    conn = duckdb.connect(str(db), read_only=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'main' order by table_name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for name in names:
            if name in EXCLUDED:
                continue
            # Quoted: a relation named like a keyword would otherwise parse wrong.
            counts[name] = conn.execute(f'select count(*) from "{name}"').fetchone()[0]
        return counts
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("artifact", type=Path, help="the .duckdb to describe")
    p.add_argument("--out", type=Path, default=None, help="write here instead of stdout")
    args = p.parse_args(argv)

    if not args.artifact.is_file():
        print(f"no such artifact: {args.artifact}", file=sys.stderr)
        return 2

    counts = relation_row_counts(args.artifact)
    if not counts:
        # An empty payload would let verify() run no checks and report success.
        print("the artifact declares no relations; refusing to emit an empty gate", file=sys.stderr)
        return 1

    payload = {
        "schema_version": 1,
        "relation_row_counts": counts,
        "known_answer_queries": [],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
        print(f"{args.out}: {len(counts)} relations", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
