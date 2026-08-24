#!/usr/bin/env python3

from __future__ import annotations

import logging
import sys

import pandas as pd

from schema_tables import config
from schema_tables import transform

from schema_tables.export import (
    export_tables,
    print_export_summary,
)

from schema_tables.utils import (
    empty_table,
    release_memory,
)


logger = logging.getLogger(
    "build_dimension_tables"
)


def build() -> int:

    if not config.CODE_LOOKUP_XLSX.exists():

        raise FileNotFoundError(
            config.CODE_LOOKUP_XLSX
        )

    logger.info(
        "Reading lookup workbook: %s",
        config.CODE_LOOKUP_XLSX,
    )

    workbook = pd.ExcelFile(
        config.CODE_LOOKUP_XLSX,
        engine="openpyxl",
    )

    logger.info(
        "Workbook sheets: %s",
        workbook.sheet_names,
    )

    sheets = set(
        workbook.sheet_names
    )

    # ---------------------------------------------------------
    # dim_code
    # ---------------------------------------------------------

    if "Code Descriptions" in sheets:

        raw = workbook.parse(
            "Code Descriptions"
        )

        dim_code = transform.dim_code(
            raw
        )

    else:

        dim_code = empty_table(
            transform.DIM_CODE_COLUMNS
        )

    # ---------------------------------------------------------
    # dim_welfare_scheme
    # ---------------------------------------------------------

    if "Welfare Scheme Master" in sheets:

        raw = workbook.parse(
            "Welfare Scheme Master"
        )

        dim_welfare_scheme = (
            transform.dim_welfare_scheme(
                raw
            )
        )

    else:

        dim_welfare_scheme = (
            empty_table(
                transform.DIM_WELFARE_SCHEME_COLUMNS
            )
        )

    # ---------------------------------------------------------
    # dim_lsdg_theme
    # ---------------------------------------------------------

    if "FocusArea to LSDG Theme" in sheets:

        raw = workbook.parse(
            "FocusArea to LSDG Theme"
        )

        dim_lsdg_theme = (
            transform.dim_lsdg_theme(
                raw
            )
        )

    else:

        dim_lsdg_theme = (
            empty_table(
                transform.DIM_LSDG_THEME_COLUMNS
            )
        )

    report = export_tables(
        {
            "dim_code":
                dim_code,

            "dim_welfare_scheme":
                dim_welfare_scheme,

            "dim_lsdg_theme":
                dim_lsdg_theme,
        }
    )

    print_export_summary(
        report
    )

    release_memory()

    return 0


def main() -> int:

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)-8s | "
            "%(message)s"
        ),
    )

    return build()


if __name__ == "__main__":
    sys.exit(main())