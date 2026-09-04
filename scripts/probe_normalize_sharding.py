#!/usr/bin/env python3
"""Measure what sharding normalization across processes would actually buy.

    uv run python scripts/probe_normalize_sharding.py \
        --tree data/raw/eGramSwaraj_Data/Gram_Panchayat --gps 1000 --shards 8 \
        --work /tmp/shard-probe

A probe, not a feature. Sharding a raw run across worker processes is the
obvious way to go faster, but it is not free: `AtomicParquetPublication`
hands out part numbers from one counter, `canonical_manifest.json` would
need merging, and `publish()`'s atomic staging rename -- the property that a
failed run leaves nothing behind rather than half a snapshot -- does not
survive N independent writers. That is a real amount of work on the most
safety-critical part of the pipeline.

So the question worth answering first is how much there is to win, and this
answers it without building any of that. It splits the GP folders into N
disjoint raw runs and normalizes them (a) one after another and (b) all at
once, and reports the ratio. Disjoint runs need no merging, so parallel
efficiency is measured on its own, uncontaminated by machinery that does not
exist yet.

Serial total is also compared against one unsharded run over the same GPs,
which prices the per-shard fixed cost -- each shard re-reads its own manifest
and re-runs schema inference over its own slice.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]

# Run the documented way -- `uv run python scripts/probe_normalize_sharding.py`
# -- Python puts `scripts/` on sys.path, not the repository root, so the
# sibling import below fails with ModuleNotFoundError without this.
# `scripts/build_warehouse.py` carries the same line for the same reason,
# and `tests/test_build_warehouse_cli.py` documents why a pytest run does
# not notice: pythonpath = ["src", "."] already puts both roots on the path.
sys.path.append(str(ROOT))

from scripts.benchmark_normalize import _positive  # noqa: E402


def build_shards(tree: Path, gps: int, shards: int, work: Path) -> list[Path]:
    """Split the first `gps` GP folders into `shards` disjoint trees."""

    folders = sorted(p for p in tree.iterdir() if p.is_dir())[:gps]
    roots: list[Path] = []
    for index in range(shards):
        destination = work / f"shard-{index}"
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        # Strided rather than blocked, so no shard gets all the large GPs and
        # the parallel run is not paced by one unlucky worker.
        for folder in folders[index::shards]:
            shutil.copytree(folder, destination / folder.name)
        roots.append(destination)
    return roots


def publish(work: Path, tree: Path, run_id: str) -> Path:
    destination = work / "raw" / "egramSwaraj" / run_id
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(
        ["uv", "run", "python", "main.py", "ingest",
         "--raw-root", str(work / "raw"), "--source", "egramSwaraj",
         "--run-id", run_id, "--code-sha", "probe", "--config-hash", "probe",
         "--payload-tree", str(tree)],
        check=True, capture_output=True, cwd=ROOT,
    )
    return destination


def normalize_one(args: tuple[str, str]) -> tuple[float, int]:
    """Normalize one run in this process; returns (seconds, rows)."""

    sys.path.insert(0, str(ROOT))
    from src.pipeline.normalize import normalize_egramswaraj

    run_path, output_root = args
    if Path(output_root).exists():
        shutil.rmtree(output_root)
    started = time.monotonic()
    result = normalize_egramswaraj(Path(run_path), Path(output_root))
    return time.monotonic() - started, sum(
        table["row_count"] for table in json.loads(
            (result.output_root / "canonical_manifest.json").read_text()
        )["tables"].values()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True)
    # Both are counts this probe multiplies work by, and both are
    # expensive to get wrong: `--gps -1` slices to every folder but the
    # last, so a typo meant to shrink the run normalizes the whole tree
    # twice instead, and `--shards 0` runs the full unsharded pass before
    # ProcessPoolExecutor rejects max_workers=0. Reusing the benchmark's
    # type rather than restating it: same rule, same message, one place.
    parser.add_argument("--gps", type=_positive, default=1000)
    parser.add_argument("--shards", type=_positive, default=8)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)

    work = args.work.expanduser().resolve()
    tree = args.tree.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)

    logger.info("building {} shards of {} GPs", args.shards, args.gps)
    roots = build_shards(tree, args.gps, args.shards, work)
    runs = [publish(work, root, f"shard-{i}") for i, root in enumerate(roots)]

    logger.info("--- one unsharded run over the same {} GPs ---", args.gps)
    whole_tree = work / "whole"
    if whole_tree.exists():
        shutil.rmtree(whole_tree)
    whole_tree.mkdir(parents=True)
    for folder in sorted(p for p in tree.iterdir() if p.is_dir())[:args.gps]:
        shutil.copytree(folder, whole_tree / folder.name)
    whole_run = publish(work, whole_tree, "whole")
    whole_s, whole_rows = normalize_one((str(whole_run), str(work / "out-whole")))
    logger.info("unsharded: {:.2f}s  {:,} rows", whole_s, whole_rows)

    logger.info("--- {} shards, one after another ---", args.shards)
    serial_started = time.monotonic()
    serial_rows = 0
    for index, run in enumerate(runs):
        seconds, rows = normalize_one((str(run), str(work / f"out-serial-{index}")))
        serial_rows += rows
        logger.info("  shard {}: {:.2f}s  {:,} rows", index, seconds, rows)
    serial_s = time.monotonic() - serial_started

    logger.info("--- {} shards, all at once ---", args.shards)
    parallel_started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.shards) as pool:
        results = list(pool.map(
            normalize_one,
            [(str(run), str(work / f"out-par-{i}")) for i, run in enumerate(runs)],
        ))
    parallel_s = time.monotonic() - parallel_started
    parallel_rows = sum(rows for _, rows in results)
    for index, (seconds, rows) in enumerate(results):
        logger.info("  shard {}: {:.2f}s  {:,} rows", index, seconds, rows)

    logger.info("=" * 66)
    logger.info("unsharded, one process      {:8.2f}s   {:>10,} rows", whole_s, whole_rows)
    logger.info("{} shards, serial            {:8.2f}s   {:>10,} rows  "
                "(shard overhead {:+.1f}%)", args.shards, serial_s, serial_rows,
                100 * (serial_s - whole_s) / whole_s)
    logger.info("{} shards, parallel          {:8.2f}s   {:>10,} rows",
                args.shards, parallel_s, parallel_rows)
    logger.info("speedup vs unsharded         {:8.2f}x", whole_s / parallel_s)
    logger.info("parallel efficiency          {:8.0f}% of {} cores",
                100 * (serial_s / parallel_s) / args.shards, args.shards)
    if not (whole_rows == serial_rows == parallel_rows):
        logger.error("ROW COUNTS DISAGREE -- sharding lost or duplicated rows")
        return 1
    logger.info("row counts agree across all three arrangements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
