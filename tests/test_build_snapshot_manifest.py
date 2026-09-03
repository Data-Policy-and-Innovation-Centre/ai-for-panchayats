"""The guard that stops a sample build becoming a deployable snapshot.

A 20-GP smoke build is structurally identical to a full-state one -- same 19
tables, same schema, same green conformance run. The only thing separating
them is row counts, and the only place that matters is here: the deployment
reads infra/snapshots/full_state.json, and this script is what writes it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from scripts.build_snapshot_manifest import DEFAULT_EXCEPTIONS, main
from warehouse.conformance import EXPECTED_GP_COUNT, MIN_GP_COVERAGE
from warehouse.geography import gp_geography
from warehouse.schema import CREATE_ORDER, DDL

FULL_STATE = len(gp_geography())


def _artifact(path: Path, gp_count: int, *, extra: int = 0) -> Path:
    """A structurally complete warehouse holding `gp_count` real GPs.

    ``extra`` appends GP codes the reference tree does not know, which is the
    only way to build an artifact with *more* rows than the roster -- slicing
    past its end just gives the whole roster back.
    """

    rows = [
        (code, f"GP {code}", geo["state_code"], geo["state_name"],
         geo["district_code"], geo["zp_name"], geo["block_code"], geo["block_name"])
        for code, geo in list(gp_geography().items())[:gp_count]
    ]
    rows += [
        (f"9{index:08d}", f"Unknown GP {index}", "21", "Odisha", "303", "Anugul", "3639", "Anugul")
        for index in range(extra)
    ]
    con = duckdb.connect(str(path))
    try:
        for table in CREATE_ORDER:
            con.execute(DDL[table])
        if rows:  # executemany rejects an empty parameter list
            con.executemany(
                "INSERT INTO gram_panchayat (gp_lgd_code, gp_name, state_code, state_name, "
                "district_code, zp_name, block_code, block_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        con.close()
    return path


def _pin(artifact: Path, out: Path) -> int:
    return main([
        str(artifact), "--bucket", "b", "--key", "k", "--version-id", "v", "--out", str(out),
    ])


@pytest.mark.parametrize("gp_count", [0, 1, 20, 500, int(FULL_STATE * MIN_GP_COVERAGE) - 1])
def test_a_partial_build_cannot_be_pinned(tmp_path: Path, gp_count: int, capsys):
    """A sample is refused, up to the edge of the coverage band.

    The band exists because a GP whose payloads are all empty produces no rows
    and never reaches the dimension, so exact equality would refuse a
    genuinely complete build (verified: three scraped folders, one dataless,
    yields two rows)."""

    out = tmp_path / "manifest.json"
    assert _pin(_artifact(tmp_path / "partial.duckdb", gp_count), out) == 1
    assert "must not be pinned" in capsys.readouterr().err
    assert not out.exists()


def test_a_build_with_gps_outside_the_roster_cannot_be_pinned(tmp_path: Path, capsys):
    """The other direction. More rows than the roster means the build and the
    reference tree disagree about what Odisha is -- and since that tree is
    also what fills the geography columns, the disagreement is already in the
    data."""

    artifact = _artifact(tmp_path / "extra.duckdb", FULL_STATE, extra=3)
    out = tmp_path / "manifest.json"
    assert _pin(artifact, out) == 1
    assert "6,797 gram_panchayat rows" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("gp_count", [FULL_STATE, FULL_STATE - 1, int(FULL_STATE * 0.95)])
def test_a_full_state_build_is_pinned(tmp_path: Path, gp_count: int):
    """The control. A guard that refuses everything pins nothing -- and a
    build missing a handful of dataless GPs is still the full state."""

    out = tmp_path / "manifest.json"
    assert _pin(_artifact(tmp_path / "full.duckdb", gp_count), out) == 0
    assert out.exists()


def test_the_roster_size_agrees_with_the_conformance_literal():
    """Two sources of truth, deliberately: this guard derives the roster size
    from lgd_codes.json, while conformance carries an independent literal
    written against the spec -- so a corrupted reference tree cannot satisfy
    both. That only works if a divergence is noticed."""

    assert FULL_STATE == EXPECTED_GP_COUNT


def test_an_artifact_without_gram_panchayat_is_refused(tmp_path: Path, capsys):
    """Scope that cannot be established is not scope that can be assumed."""

    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    assert _pin(path, tmp_path / "manifest.json") == 1
    assert "no gram_panchayat table" in capsys.readouterr().err


def test_an_unreadable_artifact_is_refused_not_traced(tmp_path: Path, capsys):
    path = tmp_path / "not-a-database.duckdb"
    path.write_text("certainly not duckdb")
    assert _pin(path, tmp_path / "manifest.json") == 1
    assert "cannot read" in capsys.readouterr().err


def test_geography_is_no_longer_a_default_known_exception():
    """#61 is fixed, so an artifact this pipeline builds must stop claiming
    its geography is blank. Pinning the older externally-built artifact means
    passing --known-exception explicitly."""

    assert not any("#61" in exception for exception in DEFAULT_EXCEPTIONS)
    assert any("#62" in exception for exception in DEFAULT_EXCEPTIONS)


# --------------------------------------------------------------------- prod vs staging


def _mode_env(name: str) -> dict[str, str]:
    """Parse a mode file the way both Make and `uv run --env-file` do."""

    text = (Path(__file__).resolve().parents[1] / "config" / f"{name}.env").read_text()
    return dict(
        line.split("=", 1)
        for line in (raw.strip() for raw in text.splitlines())
        if line and not line.startswith("#")
    )


def test_prod_and_staging_share_no_paths():
    """The whole point of the two mode files: a staging run must not be able
    to overwrite production's registry, canonical Parquet or database."""

    prod, staging = _mode_env("prod"), _mode_env("staging")
    # Every path the pipeline writes to, including the raw-run root: both
    # modes published to the same data/raw/<source>/<run-id> until that was
    # separated, which contradicted this file's own promise and made a
    # same-day second run copy ~204,000 files before dying on "destination
    # exists".
    assert set(prod) == set(staging) == {
        "PIPELINE_RAW_ROOT", "PANCHAYAT_CANONICAL_ROOT",
        "PIPELINE_SNAPSHOTS", "PANCHAYAT_DB_PATH",
    }
    for key in prod:
        assert prod[key] != staging[key], f"{key} is the same in both modes"
    assert not set(prod.values()) & set(staging.values())


def test_both_mode_registries_exist_and_are_separate_files():
    root = Path(__file__).resolve().parents[1]
    for name in ("prod", "staging"):
        registry = root / _mode_env(name)["PIPELINE_SNAPSHOTS"]
        assert registry.is_file(), f"{name} registry missing: {registry}"
