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
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow.dataset as ds
from loguru import logger

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.pipeline.normalize import normalize_egramswaraj  # noqa: E402

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


def measure(run_path: Path, output_root: Path, chunk_size: int) -> dict[str, object]:
    """Wall clock, CPU split, and peak RSS for one normalization.

    CPU is reported alongside wall clock because the ratio is the diagnosis,
    not decoration: a run that is ~90% user time is bound by parsing, and no
    amount of faster storage will help it. Measured on this process with
    getrusage, so it excludes the subprocess that published the run.
    """

    if output_root.exists():
        shutil.rmtree(output_root)
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    result = normalize_egramswaraj(run_path, output_root, chunk_size=chunk_size)
    elapsed = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_SELF)

    user = after.ru_utime - before.ru_utime
    system = after.ru_stime - before.ru_stime
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return {
        "seconds": round(elapsed, 2),
        "user_s": round(user, 2),
        "sys_s": round(system, 2),
        "cpu_pct": round(100 * (user + system) / elapsed) if elapsed else 0,
        "user_share_pct": round(100 * user / (user + system)) if (user + system) else 0,
        "peak_mb": round(max(after.ru_maxrss, before.ru_maxrss) / scale),
        "max_buffered_rows": result.max_buffered_rows,
        "quarantined": result.quarantine_count,
        "snapshot": str(result.output_root),
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
