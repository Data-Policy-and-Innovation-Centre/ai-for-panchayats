"""The code dictionaries load, and the ways they could load wrongly fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.warehouse.dimensions import DimensionError, _load, dimension_frames


def test_the_committed_dictionaries_match_what_the_issue_documents():
    """Shapes and provenance mix, against #48's own counted figures.

    Not a tautology over whatever the files happen to hold: these numbers
    were counted from the source dictionary independently and written into
    #48 before this loader existed. If a future edit truncates a file or
    drops a column, the counts move and this fails.
    """

    frames = dimension_frames()
    assert [len(frames[name]) for name in
            ("dim_code", "dim_lsdg_theme", "dim_welfare_scheme")] == [717, 17, 12]

    dim_code = frames["dim_code"]
    assert dim_code["variable"].nunique() == 23, "dim_code decodes 23 coded columns"
    assert dim_code["source"].value_counts().to_dict() == {
        "Derived": 409, "Unresolved": 238, "Confirmed": 51,
        "User-supplied": 14, "Conflict": 4, "Candidate": 1,
    }
    # The point of carrying provenance at all: 94% of mappings are not
    # confirmed-high, and a label's trustworthiness has to survive the load.
    assert (dim_code["confidence"] == "high").sum() == 373
    assert (dim_code["source"] == "Confirmed").sum() == 51


def test_codes_stay_text_so_a_leading_zero_survives(tmp_path: Path):
    """`code` is VARCHAR in the DDL and joined with CAST(... AS VARCHAR).

    If it ever arrives as an integer, `01` becomes `1` and the join returns
    fewer rows with no error anywhere.

    To be clear about what this pins: the guarantee comes from
    `load_common.read_csv`, which defaults every untyped column to `string`,
    not from anything in `dimensions.py`. This module depends on that
    default, so the test guards the dependency -- it would catch the default
    changing under us, which is the way this would actually break.
    """

    csv = tmp_path / "dim_code.csv"
    csv.write_text(
        "variable,code,description,source,confidence\n"
        "focus_area,01,Leading zero,Confirmed,high\n",
        encoding="utf-8",
    )
    assert _load("dim_code", tmp_path)["code"].iloc[0] == "01"


def test_a_code_meaning_two_things_is_refused(tmp_path: Path):
    """(variable, code) is dim_code's PRIMARY KEY.

    Keeping the last of a duplicate pair silently picks one of two meanings
    for the same code -- the exact confusion this table exists to prevent.
    """

    csv = tmp_path / "dim_code.csv"
    csv.write_text(
        "variable,code,description,source,confidence\n"
        "focus_area,3,Sanitation,Confirmed,high\n"
        "focus_area,3,Drinking water,Derived,medium\n",
        encoding="utf-8",
    )
    with pytest.raises(DimensionError, match="variable/code is not unique"):
        _load("dim_code", tmp_path)


def test_a_collapsed_theme_mapping_is_labelled_as_collapsed():
    """focus_area -> LSDG theme is many-to-many, and this table is one row
    per focus area, so it is a reduction. The columns that say so have to
    survive the load, or a consumer cannot tell Sanitation (3 themes
    collapsed into 1) from Roads (genuinely 1).
    """

    frame = dimension_frames()["dim_lsdg_theme"]
    assert {"distinct_themes", "source_rows"} <= set(frame.columns)
    collapsed = frame[frame["distinct_themes"] > 1]
    assert len(collapsed) == 9, "9 of the 17 focus areas span several themes"
    assert frame.loc[frame["focus_area_name"] == "Roads", "distinct_themes"].iloc[0] == 1
    # Counts, not "3.0" -- they are rendered to users in provenance notes.
    assert str(frame["distinct_themes"].dtype) == "Int64"


def test_labels_are_stripped(tmp_path: Path):
    """The real file has "Theme 5 - Clean and Green Village " with a trailing
    space, and a trailing space in a label is visible to a user."""

    csv = tmp_path / "dim_lsdg_theme.csv"
    csv.write_text(
        "focus_area_name,lsdg_theme,distinct_themes,n_rows\n"
        "Sanitation,Theme 5 - Clean and Green Village ,3.0,350.0\n",
        encoding="utf-8",
    )
    frame = _load("dim_lsdg_theme", tmp_path)
    assert frame["lsdg_theme"].iloc[0] == "Theme 5 - Clean and Green Village"
    # n_rows is renamed to say what it counts; both counts are kept, because
    # they are what marks this table as a reduction rather than a mapping.
    assert list(frame.columns) == [
        "focus_area_name", "lsdg_theme", "distinct_themes", "source_rows",
    ]


def test_a_missing_dictionary_is_loud(tmp_path: Path):
    with pytest.raises(DimensionError, match="not found"):
        _load("dim_code", tmp_path)


def test_the_decode_join_needs_its_cast_and_not_for_the_reason_the_issue_gives():
    """#48 says the `CAST(... AS VARCHAR)` is load-bearing. It is. But the
    failure mode it describes -- "omitting the CAST returns zero rows with no
    error" -- is not what DuckDB does, and the difference matters.

    Measured on DuckDB 1.5.5: comparing VARCHAR to BIGINT makes DuckDB cast
    the *string to a number*, not the number to a string. So omitting the
    CAST does not quietly match nothing; it raises ConversionException,
    because `dim_code` holds one non-numeric code (`asset_coverage_code` =
    'A'). Loud, not silent.

    Keep the CAST. Both spellings are pinned here so that if DuckDB ever
    changes its comparison rules, this fails and the guidance gets revisited
    rather than being trusted from a comment.
    """

    import duckdb

    con = duckdb.connect()
    try:
        con.register("dim_code_frame", dimension_frames()["dim_code"])
        con.execute("CREATE TABLE dim_code AS SELECT * FROM dim_code_frame")
        # BIGINT, the way the fact tables actually declare their code columns.
        con.execute("CREATE TABLE fact (activity_code VARCHAR, focus_area BIGINT)")
        known = con.execute(
            "SELECT code FROM dim_code WHERE variable = 'focus_area' LIMIT 1"
        ).fetchone()[0]
        con.execute("INSERT INTO fact VALUES ('a-1', CAST(? AS BIGINT))", [known])

        decoded = con.execute("""
            SELECT d.description FROM fact f
            LEFT JOIN dim_code d
              ON d.variable = 'focus_area'
             AND d.code = CAST(f.focus_area AS VARCHAR)
        """).fetchall()
        assert decoded and decoded[0][0] is not None, "documented join must decode"

        # The `variable` predicate is the other load-bearing half: code 1
        # means different things in different columns, so dropping it
        # multiplies rows instead of decoding them.
        rows_without_variable = con.execute("""
            SELECT count(*) FROM fact f
            LEFT JOIN dim_code d ON d.code = CAST(f.focus_area AS VARCHAR)
        """).fetchone()[0]
        assert rows_without_variable > 1, (
            "one fact row should fan out across every variable sharing that "
            "code; if it does not, the fixture stopped exercising the hazard"
        )

        con.execute("CREATE TABLE fact2 (asset_coverage_code BIGINT)")
        con.execute("INSERT INTO fact2 VALUES (1)")
        with pytest.raises(duckdb.ConversionException, match="'A'"):
            con.execute("""
                SELECT count(*) FROM fact2 f
                LEFT JOIN dim_code d
                  ON d.variable = 'asset_coverage_code'
                 AND d.code = f.asset_coverage_code
            """).fetchall()
    finally:
        con.close()


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("2.6", "fractional: rounding to 3 overstates how many themes were collapsed"),
        ("n/a", "not a number: coercing to NULL hides that the mapping is collapsed"),
        ("", "blank: same as above"),
        ("inf", "non-finite: astype('Int64') would raise far from the cause"),
        ("0", "zero: a row exists because it was observed, so it counts >= 1"),
        ("-1", "negative: impossible provenance that every build check would pass"),
    ],
)
def test_a_theme_count_that_is_not_a_whole_number_is_refused(
    tmp_path: Path, value: str, why: str,
):
    """`distinct_themes` is the field that says whether a mapping was
    collapsed, so silently rounding or nulling it misrepresents exactly the
    thing it exists to report -- and the build still succeeds. Same defect
    family as #116 (fractional counts rounded) and #127 (malformed digit
    grouping rewritten). A reference file has no quarantine to fall back on.
    """

    csv = tmp_path / "dim_lsdg_theme.csv"
    csv.write_text(
        "focus_area_name,lsdg_theme,distinct_themes,n_rows\n"
        f"Sanitation,Theme 5,{value},350.0\n",
        encoding="utf-8",
    )
    with pytest.raises(DimensionError, match="whole number of at least 1"):
        _load("dim_lsdg_theme", tmp_path)


def test_a_whole_number_written_as_a_float_is_accepted(tmp_path: Path):
    """The real file spells them "3.0" and "350.0"; refusing those would
    reject the very data this loader exists to read."""

    csv = tmp_path / "dim_lsdg_theme.csv"
    csv.write_text(
        "focus_area_name,lsdg_theme,distinct_themes,n_rows\n"
        "Sanitation,Theme 5,3.0,350.0\n",
        encoding="utf-8",
    )
    frame = _load("dim_lsdg_theme", tmp_path)
    assert frame["distinct_themes"].iloc[0] == 3
    assert frame["source_rows"].iloc[0] == 350


def test_more_themes_than_the_activities_they_were_counted_over_is_refused(
    tmp_path: Path,
):
    """`distinct_themes` is counted *across* `source_rows` activities, so it
    cannot exceed them. Three themes supported by one activity is arithmetic
    that cannot have happened -- but it is a whole number and it is positive,
    so every other check in this loader waves it through and the warehouse
    publishes impossible provenance.
    """

    csv = tmp_path / "dim_lsdg_theme.csv"
    csv.write_text(
        "focus_area_name,lsdg_theme,distinct_themes,n_rows\n"
        "Sanitation,Theme 5,3.0,1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(DimensionError, match="cannot exceed"):
        _load("dim_lsdg_theme", tmp_path)


def test_a_count_equal_to_its_support_is_accepted(tmp_path: Path):
    """Four rows of the real file have distinct_themes == source_rows == 1.
    The bound is "cannot exceed", not "must be less than"."""

    csv = tmp_path / "dim_lsdg_theme.csv"
    csv.write_text(
        "focus_area_name,lsdg_theme,distinct_themes,n_rows\n"
        "Water,Theme 2,1.0,1.0\n",
        encoding="utf-8",
    )
    assert _load("dim_lsdg_theme", tmp_path)["distinct_themes"].iloc[0] == 1


@pytest.mark.parametrize(
    ("name", "header", "row", "column"),
    [
        ("dim_code", "variable,code,description,source,confidence",
         "focus_area,3,Sanitation,  ,high", "source"),
        ("dim_lsdg_theme", "focus_area_name,lsdg_theme,distinct_themes,n_rows",
         "Sanitation, ,3.0,350.0", "lsdg_theme"),
        ("dim_welfare_scheme", "scheme_code,scheme_name", "1,   ", "scheme_name"),
    ],
)
def test_a_blank_in_a_column_the_row_needs_is_refused(
    tmp_path: Path, name: str, header: str, row: str, column: str,
):
    """Whitespace-only survives `.str.strip()` as "", and the key check does
    not look at these columns. A row that loads with a blank `lsdg_theme` or
    `scheme_name` decodes to nothing; one with a blank `dim_code.source` is a
    label carrying no provenance, which is the single thing `source` exists
    to prevent (see the module docstring).
    """

    (tmp_path / f"{name}.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
    with pytest.raises(DimensionError, match="blank"):
        _load(name, tmp_path)


def test_an_unrecorded_confidence_still_loads(tmp_path: Path):
    """The counterpart to the test above, and the reason it is narrow: 253 of
    717 real dim_code rows have no `confidence` and 238 have `Unresolved`
    provenance. Rejecting those would reject the dictionary itself.
    """

    csv = tmp_path / "dim_code.csv"
    csv.write_text(
        "variable,code,description,source,confidence\n"
        "focus_area,3,,Unresolved,\n",
        encoding="utf-8",
    )
    frame = _load("dim_code", tmp_path)
    assert frame["confidence"].iloc[0] == ""
    assert frame["source"].iloc[0] == "Unresolved"
