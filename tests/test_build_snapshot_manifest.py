"""The guard that stops a sample build becoming a deployable snapshot.

A 20-GP smoke build is structurally identical to a full-state one -- same 19
tables, same schema, same green conformance run. The only thing separating
them is row counts, and the only place that matters is here: the deployment
reads infra/snapshots/full_state.json, and this script is what writes it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest

from scripts.build_snapshot_manifest import DEFAULT_EXCEPTIONS, main
from warehouse.conformance import EXPECTED_GP_COUNT, GEOGRAPHY_COLUMNS, MIN_GP_COVERAGE
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


def test_a_full_size_build_with_swapped_geography_cannot_be_pinned(tmp_path: Path, capsys):
    """Right shape, wrong rows: the failure cardinality cannot see.

    Two GPs in different districts trade their geography. Every column is
    still populated, the district and block counts are unchanged, and the row
    count is exactly the roster -- so the coverage band and
    check_geography_completeness both pass. This is the shape a join on
    `gp_name` produces, and 505 GP names are shared, so it is not
    hypothetical (#136).
    """

    artifact = _artifact(tmp_path / "swapped.duckdb", FULL_STATE)
    con = duckdb.connect(str(artifact))
    try:
        pair = con.execute(
            "SELECT gp_lgd_code, district_code, zp_name, block_code, block_name "
            "FROM gram_panchayat ORDER BY zp_name LIMIT 1"
        ).fetchall() + con.execute(
            "SELECT gp_lgd_code, district_code, zp_name, block_code, block_name "
            "FROM gram_panchayat ORDER BY zp_name DESC LIMIT 1"
        ).fetchall()
        (first, *first_geo), (second, *second_geo) = pair
        assert first_geo != second_geo, "fixture needs two GPs in different districts"
        for code, geo in ((first, second_geo), (second, first_geo)):
            con.execute(
                "UPDATE gram_panchayat SET district_code = ?, zp_name = ?, "
                "block_code = ?, block_name = ? WHERE gp_lgd_code = ?",
                [*geo, code],
            )
    finally:
        con.close()

    assert _pin(artifact, tmp_path / "out.json") == 1
    stderr = capsys.readouterr().err
    assert "disagrees with the LGD reference tree" in stderr
    # Two rows are wrong, each on four columns. Counting the columns would
    # report eight, and on a stale-tree artifact would report six times the
    # table's own row count.
    assert "has 2 gram_panchayat row(s)" in stderr
    assert first in stderr or second in stderr
    assert not (tmp_path / "out.json").exists()


def test_a_missing_geography_column_is_named_not_raised_as_a_binder_error(
    tmp_path: Path, capsys,
):
    """The mapping check must not mask conformance's own columns finding.

    Selecting the six geography columns from an artifact that lacks one is a
    binder error, and it would be reported through the catch-all in main() as
    "cannot read <artifact>" -- blaming the file rather than naming the
    missing column. So the row comparison only runs once completeness passes.
    """

    # Built without the column rather than altered afterwards: the foreign
    # keys pointing at gram_panchayat make DROP COLUMN a DependencyException.
    artifact = tmp_path / "no-zp-name.duckdb"
    con = duckdb.connect(str(artifact))
    try:
        for table in CREATE_ORDER:
            ddl = DDL[table]
            if table == "gram_panchayat":
                ddl = ddl.replace("zp_name       VARCHAR,", "", 1)
                assert "zp_name" not in ddl, "DDL changed; this fixture no longer drops it"
            con.execute(ddl)
        con.executemany(
            "INSERT INTO gram_panchayat (gp_lgd_code, gp_name, state_code, state_name, "
            "district_code, block_code, block_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (code, f"GP {code}", geo["state_code"], geo["state_name"],
                 geo["district_code"], geo["block_code"], geo["block_name"])
                for code, geo in gp_geography().items()
            ],
        )
    finally:
        con.close()

    assert _pin(artifact, tmp_path / "out.json") == 1
    stderr = capsys.readouterr().err
    assert "Binder Error" not in stderr
    assert "zp_name" in stderr
    assert not (tmp_path / "out.json").exists()


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
    its geography is blank. What makes dropping the caveat honest is the guard
    below, which refuses any artifact whose geography is not actually there --
    so the older externally-built artifact is refused outright rather than
    pinned with an exception."""

    assert not any("#61" in exception for exception in DEFAULT_EXCEPTIONS)
    assert any("#62" in exception for exception in DEFAULT_EXCEPTIONS)


@pytest.mark.parametrize("blank", list(GEOGRAPHY_COLUMNS) + [None])
def test_a_full_size_build_with_blank_geography_cannot_be_pinned(
    tmp_path: Path, blank: str | None, capsys,
):
    """The #61 artifact itself: the full roster of rows, no geography behind
    them.

    Row count alone cannot tell it apart from a good build, and this script is
    what removes the #61 caveat from the manifest -- so pinning it would
    publish a known-bad database as having resolved the very issue it is the
    example of. ``blank=None`` nulls every geography column at once (the
    literal #61 case); the rest null one column at a time, because a
    hand-picked subset of columns is how #61 passed a green check the first
    time.
    """

    artifact = _artifact(tmp_path / "blank.duckdb", FULL_STATE)
    columns = GEOGRAPHY_COLUMNS if blank is None else (blank,)
    con = duckdb.connect(str(artifact))
    try:
        for column in columns:
            con.execute(f"UPDATE gram_panchayat SET {column} = NULL")
    finally:
        con.close()

    out = tmp_path / "manifest.json"
    assert _pin(artifact, out) == 1
    assert "geography is not complete" in capsys.readouterr().err
    assert not out.exists()


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


def test_the_sample_recipe_survives_pipefail_on_a_full_size_tree(tmp_path: Path):
    """`make sample` must not die on the tree it exists for.

    The Makefile sets `.SHELLFLAGS := -euo pipefail`, so any pipeline whose
    reader stops early takes the recipe down with it: `ls | head -20` over
    6,794 GP folders writes past the 64KB pipe buffer, `head` exits, and `ls`
    dies of SIGPIPE with status 141. The sample target would then fail on a
    real tree while passing on every small fixture.

    The recipe is extracted with `make -n` and run under those exact flags
    rather than via `make` itself, because `.SHELLFLAGS` only exists in GNU
    Make >= 3.82 -- on the 3.81 that ships with macOS the recipe runs without
    pipefail and the bug is invisible.

    400 directories with long names clear the pipe buffer (~75KB of listing),
    which is the property under test; building all 6,794 would only be slower.
    """

    tree = tmp_path / "tree"
    padding = "N" * 180
    for index in range(400):
        (tree / f"LGD_{index}{padding}").mkdir(parents=True)
        (tree / f"LGD_{index}{padding}" / "2021_PL.json").write_text("{}")
    listing = subprocess.run(
        ["ls", str(tree)], capture_output=True, text=True, check=True
    ).stdout
    assert len(listing) > 64 * 1024, "fixture too small to reach the pipe buffer"

    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "out"
    recipe = subprocess.run(
        ["make", "-n", "sample", f"PIPELINE_TREE={tree}", f"SAMPLE_TREE={out}"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", recipe], capture_output=True, text=True,
    )
    assert result.returncode == 0, f"sample recipe failed: {result.stderr}"
    # Whole GP folders, contents intact -- a trailing-slash source would copy
    # the contents into the sample root instead of the directory itself.
    assert len(list(out.iterdir())) == 20
    assert len(list(out.glob("*/2021_PL.json"))) == 20


def test_make_run_stamps_real_provenance_on_the_raw_run():
    """`ingest` defaults code_sha and config_hash to "unknown", and
    normalization copies those into raw_manifest_identity -- so two artifacts
    built from different code would be indistinguishable. The recipe must
    supply both, and the two modes must not share a config hash.

    Read out of `make -n` rather than by running the pipeline: this asserts
    what the recipe passes, which is the thing that regressed.
    """

    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]

    def flags(mode: str) -> dict[str, str]:
        out = subprocess.run(
            ["make", "-n", "run", f"MODE={mode}"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        found = dict(re.findall(r"--(code-sha|config-hash) (\S+)", out))
        assert set(found) == {"code-sha", "config-hash"}, out
        return found

    prod, staging = flags("prod"), flags("staging")
    for mode, found in (("prod", prod), ("staging", staging)):
        for key, value in found.items():
            assert value != "unknown", f"{mode} passes a literal 'unknown' for --{key}"
            assert value, f"{mode} passes an empty --{key}"
    # The mode file decides every path the run touches, so its hash is what a
    # rebuild would need; sharing one between modes would make them look alike.
    assert prod["config-hash"] != staging["config-hash"]
    assert prod["code-sha"] == staging["code-sha"]  # same tree, same code
