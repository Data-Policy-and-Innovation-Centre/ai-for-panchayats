#!/usr/bin/env python3
"""Time normalization at several input sizes, and fingerprint what it produced.

    uv run python scripts/benchmark_normalize.py --gps 20 --gps 100 --gps 500 \
        --out /tmp/bench.json

Two questions, one run. *How fast* is the obvious one. *Did the output
change* is the one that decides whether a speedup is allowed to ship: run
this on the branch and on the commit it branched from, and the ``fingerprint``
values must match exactly at every size.

The fingerprint is content, not files. Part-file boundaries are an
implementation detail -- ``write_rows`` already splits a batch by fiscal year,
so the number of files depends on how rows happen to be grouped -- but the
rows themselves, their values, and their ``row_id``s must not move. So each
table is read back as one dataset, sorted by ``row_id``, and hashed.

Reads the scraped tree read-only and writes everything else under --work, so
it is safe to run while another normalization is in progress.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
from loguru import logger

DEFAULT_TREE = Path("data/raw/eGramSwaraj_Data/Gram_Panchayat")


def build_subset(tree: Path, count: int, destination: Path) -> int:
    """Copy the first `count` GP folders, in sorted order so runs compare."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    folders = sorted(p for p in tree.iterdir() if p.is_dir())[:count]
    for folder in folders:
        shutil.copytree(folder, destination / folder.name)
    return len(folders)


def publish(work: Path, tree: Path, run_id: str) -> Path:
    subprocess.run(
        [
            "uv", "run", "python", "main.py", "ingest",
            "--raw-root", str(work / "raw"), "--source", "egramSwaraj",
            "--run-id", run_id, "--code-sha", "bench", "--config-hash", "bench",
            "--payload-tree", str(tree),
        ],
        check=True, capture_output=True, cwd=Path(__file__).resolve().parents[1],
    )
    return work / "raw" / "egramSwaraj" / run_id


def fingerprint(snapshot: Path) -> dict[str, dict[str, object]]:
    """Per-table row count and a content hash, independent of part layout."""

    out: dict[str, dict[str, object]] = {}
    for table_dir in sorted(p for p in snapshot.iterdir() if p.is_dir()):
        files = sorted(str(p) for p in table_dir.rglob("*.parquet"))
        if not files:
            continue
        frame = ds.dataset(files, format="parquet").to_table().to_pandas()
        if "row_id" in frame.columns:
            frame = frame.sort_values("row_id", kind="stable")
        frame = frame.reindex(columns=sorted(frame.columns)).reset_index(drop=True)
        digest = hashlib.sha256(
            pd.util.hash_pandas_object(frame, index=False).values.tobytes()
        ).hexdigest()
        out[table_dir.name] = {"rows": len(frame), "sha256": digest[:16], "parts": len(files)}
    return out


# Runs one normalization and reports what it cost, as JSON on stdout.
#
# In a child process, because peak memory is the whole point of the column
# and it cannot be measured in the parent. ``ru_maxrss`` is a high-water mark
# over the life of a process and never falls, so in a single process the
# figure for every size after the first is really the largest thing that has
# happened so far -- including ``fingerprint()`` reading whole tables into
# pandas between sizes. Measured that way, 2 GPs "peaks" higher than 4 GPs.
# A fresh child per size has no history to inherit.
CHILD = """
import json, resource, sys, time
sys.path.insert(0, {root!r})
from src.pipeline.normalize import normalize_egramswaraj

run_path, output_root, chunk_size = sys.argv[1], sys.argv[2], int(sys.argv[3])
# CPU as a delta around the call, so importing pyarrow is not charged to
# normalization. Peak RSS is not a delta: it is the child's high-water mark,
# and the interpreter and its imports are genuinely part of that.
before = resource.getrusage(resource.RUSAGE_SELF)
started = time.monotonic()
result = normalize_egramswaraj(run_path, output_root, chunk_size=chunk_size)
elapsed = time.monotonic() - started
usage = resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({{
    "seconds": elapsed,
    "user_s": usage.ru_utime - before.ru_utime,
    "sys_s": usage.ru_stime - before.ru_stime,
    "maxrss": usage.ru_maxrss,
    "max_buffered_rows": result.max_buffered_rows,
    "quarantined": result.quarantine_count,
    "snapshot": str(result.output_root),
}}))
"""


def measure(run_path: Path, output_root: Path, chunk_size: int) -> dict[str, object]:
    """Wall clock, CPU split, and peak RSS for one normalization.

    CPU is reported alongside wall clock because the ratio is the diagnosis,
    not decoration: a run that is ~90% user time is bound by parsing, and no
    amount of faster storage will help it.
    """

    if output_root.exists():
        shutil.rmtree(output_root)
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", CHILD.format(root=str(root)),
         str(run_path), str(output_root), str(chunk_size)],
        check=True, capture_output=True, text=True, cwd=root,
    )
    measured = json.loads(completed.stdout)

    elapsed = measured["seconds"]
    user, system = measured["user_s"], measured["sys_s"]
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return {
        "seconds": round(elapsed, 2),
        "user_s": round(user, 2),
        "sys_s": round(system, 2),
        "cpu_pct": round(100 * (user + system) / elapsed) if elapsed else 0,
        "user_share_pct": round(100 * user / (user + system)) if (user + system) else 0,
        "peak_mb": round(measured["maxrss"] / scale),
        "max_buffered_rows": measured["max_buffered_rows"],
        "quarantined": measured["quarantined"],
        "snapshot": measured["snapshot"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gps", action="append", type=int, default=[], required=True,
                        help="GP count to measure; repeatable")
    parser.add_argument("--tree", type=Path, default=DEFAULT_TREE)
    parser.add_argument("--work", type=Path, required=True, help="scratch directory")
    parser.add_argument("--chunk-size", type=int, default=100_000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    # Resolved because `publish` runs the ingest subprocess with cwd set to
    # the repo root: a relative --work would mean two different directories.
    args.work = args.work.expanduser().resolve()
    args.tree = args.tree.expanduser().resolve()

    if not args.tree.is_dir():
        logger.error("no scraped tree at {}", args.tree)
        return 1

    args.work.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"chunk_size": args.chunk_size, "sizes": {}}
    for count in args.gps:
        subset = args.work / f"subset-{count}"
        actual = build_subset(args.tree, count, subset)
        run_path = publish(args.work, subset, f"bench-{count}")
        measured = measure(run_path, args.work / f"canonical-{count}", args.chunk_size)
        measured["gps"] = actual
        measured["files"] = sum(1 for _ in subset.rglob("*.json"))
        measured["fingerprint"] = fingerprint(Path(measured.pop("snapshot")))
        report["sizes"][str(count)] = measured
        rows = sum(t["rows"] for t in measured["fingerprint"].values())
        # One line per size, as it completes. A run at 1,000 GPs takes
        # minutes, so a timestamped line arriving per size is the difference
        # between watching progress and wondering whether it has hung.
        logger.info(
            "{:>5} GPs  {:>7,} files  {:>8.2f}s wall  {:>7.2f}s user  "
            "{:>5.2f}s sys  {:>4}% cpu  {:>5} MB  {:>9,} rows",
            actual, measured["files"], measured["seconds"], measured["user_s"],
            measured["sys_s"], measured["cpu_pct"], measured["peak_mb"], rows,
        )

    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True))
        logger.info("wrote {}", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
