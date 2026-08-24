from __future__ import annotations

import ctypes
import gc
import logging
import shutil
from pathlib import Path

import pandas as pd


logger = logging.getLogger(__name__)


def release_memory() -> None:
    """
    Release Python/pandas memory.

    malloc_trim is useful for long-running pandas jobs under WSL.
    """

    gc.collect()

    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


def read_required_csv(
    path: Path,
    name: str,
    *,
    nrows: int | None = None,
    dtype: dict | None = None,
) -> pd.DataFrame:
    """
    Read a required CSV.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Required source '{name}' was not found:\n{path}"
        )

    logger.info(
        "Reading %s: %s%s",
        name,
        path,
        (
            f" [sample={nrows:,}]"
            if nrows is not None
            else ""
        ),
    )

    return pd.read_csv(
        path,
        nrows=nrows,
        low_memory=True,
        dtype=dtype,
    )


def read_optional_csv(
    path: Path,
    name: str,
    *,
    nrows: int | None = None,
    dtype: dict | None = None,
) -> pd.DataFrame | None:
    """
    Read optional CSV.
    """

    if not path.exists():

        logger.warning(
            "Optional source missing: %s",
            path,
        )

        return None

    logger.info(
        "Reading %s: %s%s",
        name,
        path,
        (
            f" [sample={nrows:,}]"
            if nrows is not None
            else ""
        ),
    )

    return pd.read_csv(
        path,
        nrows=nrows,
        low_memory=True,
        dtype=dtype,
    )


def iter_csv_chunks(
    path: Path,
    name: str,
    *,
    chunk_size: int,
    nrows: int | None = None,
    dtype: dict | None = None,
):
    """
    Stream a CSV using pandas chunks.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Required source '{name}' was not found:\n{path}"
        )

    logger.info(
        "Streaming %s in chunks of %s rows",
        name,
        f"{chunk_size:,}",
    )

    return pd.read_csv(
        path,
        chunksize=chunk_size,
        nrows=nrows,
        low_memory=True,
        dtype=dtype,
    )


def empty_table(
    columns: list[str],
) -> pd.DataFrame:

    return pd.DataFrame(
        columns=columns
    )


def append_csv(
    df: pd.DataFrame,
    path: Path,
    *,
    first_write: bool,
) -> bool:
    """
    Write/append dataframe to CSV.

    Returns new first_write state.
    """

    if df.empty:
        return first_write

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_csv(
        path,
        mode="w" if first_write else "a",
        header=first_write,
        index=False,
    )

    return False


def write_empty_csv(
    path: Path,
    columns: list[str],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    pd.DataFrame(
        columns=columns
    ).to_csv(
        path,
        index=False,
    )


def reset_directory(
    path: Path,
) -> None:
    """
    Delete and recreate staging directory.
    """

    if path.exists():
        shutil.rmtree(path)

    path.mkdir(
        parents=True,
        exist_ok=True,
    )