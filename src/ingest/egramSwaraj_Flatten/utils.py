"""Discovery, payload normalisation and frame helpers."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .config import ENVELOPE_KEYS, FILE_RE, FOLDER_RE, KINDS, LEADING_COLUMNS

logger = logging.getLogger(__name__)


def iter_json_files(
    input_dir: Path,
    kinds: tuple[str, ...] = KINDS,
    limit_gps: int | None = None,
) -> Iterator[tuple[Path, str]]:
    """Yield (path, kind) for every matching JSON, sorted. limit_gps samples N GPs."""
    wanted = set(kinds)
    gps = 0
    for gp_dir in sorted(input_dir.iterdir()):
        if not gp_dir.is_dir():
            continue
        for path in sorted(gp_dir.glob("*.json")):
            match = FILE_RE.match(path.stem)
            if match and match.group("kind") in wanted:
                yield path, match.group("kind")
        gps += 1
        if limit_gps is not None and gps >= limit_gps:
            return


def parse_gp_folder(folder_name: str) -> tuple[str | None, str | None]:
    """LGD_115550_Angarbandha -> ("115550", "Angarbandha")."""
    match = FOLDER_RE.match(folder_name)
    if not match:
        logger.warning("Unparseable GP folder name: %s", folder_name)
        return None, None
    return match.group("code"), match.group("name").replace("_", " ").strip()


def parse_plan_year(path: Path) -> str:
    """2021_PL.json and 2021-PL.json both give "2021"."""
    match = FILE_RE.match(path.stem)
    if match:
        return match.group("year")
    fallback = re.split(r"[_-]", path.stem, maxsplit=1)[0]
    logger.warning("Unparseable file name %s; plan_year=%s", path.name, fallback)
    return fallback


def load_json(path: Path) -> Any | None:
    """Read one JSON; None on any read/parse failure."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Corrupt JSON, skipped: %s (%s)", path, exc)
    except OSError as exc:
        logger.error("Unreadable file, skipped: %s (%s)", path, exc)
    return None


def _unwrap(payload: dict, key: str) -> list[dict]:
    """Rows of payload[key], with the scalar siblings broadcast onto each row."""
    header = {k: v for k, v in payload.items()
              if k != key and not isinstance(v, (list, dict))}
    return [{**header, **row} for row in payload[key]]


def to_records(payload: Any) -> list[dict]:
    """List of records from a list, a single dict, or a dict wrapping a record list.

    A dict is only unwrapped when it is unambiguously a response envelope:
    either it uses a known envelope key, or it holds exactly one list-of-dicts
    and no other nested value. Anything else is a domain record and is returned
    whole, so its arrays become child tables instead of being discarded.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []

    list_keys = [
        k for k, v in payload.items()
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
    ]
    if not list_keys:
        return [payload]

    named = [k for k in list_keys if k in ENVELOPE_KEYS]
    if len(named) == 1:
        return _unwrap(payload, named[0])
    if len(named) > 1:
        logger.warning(
            "Multiple envelope keys %s; treating payload as a single record", named)
        return [payload]

    nested = [k for k, v in payload.items() if isinstance(v, (list, dict))]
    if len(list_keys) == 1 and len(nested) == 1:
        return _unwrap(payload, list_keys[0])

    # Several nested members: a domain record carrying child arrays, not an
    # envelope. Unwrapping one would silently drop the siblings.
    logger.debug("Ambiguous payload (nested keys %s); kept as one record", nested)
    return [payload]


def _holds_list_of_dicts(mapping: dict) -> bool:
    return any(
        isinstance(v, list) and v and all(isinstance(i, dict) for i in v)
        for v in mapping.values()
    )


def coerce_nested(obj: Any) -> Any:
    """Wrap a bare nested object in a one-element list, recursively.

    The feed sends assetDetails as a list in some GPs and an object in others;
    coercing gives both one schema. Objects of plain scalars stay inline.
    """
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            value = coerce_nested(value)
            if isinstance(value, dict) and _holds_list_of_dicts(value):
                value = [value]
            out[key] = value
        return out
    if isinstance(obj, list):
        return [coerce_nested(item) for item in obj]
    return obj


def sanitise(name: str) -> str:
    """assetLocationDetails -> assetlocationdetails."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def list_columns(frame: pd.DataFrame) -> list[str]:
    """Columns holding at least one list of dicts."""
    return [
        col for col in frame.columns
        if frame[col].map(
            lambda v: isinstance(v, list) and any(isinstance(i, dict) for i in v)
        ).any()
    ]


def encode_leftovers(frame: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode surviving list/dict cells so CSVs hold no Python reprs."""
    for col in frame.columns:
        mask = frame[col].map(lambda v: isinstance(v, (list, dict)))
        if mask.any():
            frame.loc[mask, col] = frame.loc[mask, col].map(json.dumps)
    return frame


def restore_integers(frame: pd.DataFrame) -> pd.DataFrame:
    """Whole-number float columns back to Int64, so 404281.0 is written as 404281."""
    for col in frame.select_dtypes("number").columns:
        values = frame[col].dropna()
        if len(values) and (values % 1 == 0).all() and values.abs().max() < 2 ** 63:
            frame[col] = frame[col].astype("Int64")
    return frame


def order_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Key columns first, rest in place."""
    lead = [c for c in LEADING_COLUMNS if c in frame.columns]
    return frame[lead + [c for c in frame.columns if c not in lead]]


def tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Post-process one finished table."""
    return order_columns(restore_integers(encode_leftovers(frame)))


def concat_batch(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Union a batch, keeping columns only some files carry."""
    frame = frames[0] if len(frames) == 1 else pd.concat(
        frames, ignore_index=True, sort=False)
    return order_columns(restore_integers(frame))