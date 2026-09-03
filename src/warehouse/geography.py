"""Static GP -> block/district/state geography from the LGD reference tree.

``gram_panchayat`` is the only dimension in the warehouse whose geography
cannot come from the canonical snapshots. Every source kind records the GP it
was scraped for, but it records it as a *folder name*: the scraper writes
``LGD_<code>_<name>/`` (``ingest.egramSwaraj_API.extractor``), so
``pipeline.normalize._gp_context`` can only ever recover those two facts. The
zilla and block it walked through to reach that GP are discarded before the
path is built.

They are not lost, though -- they are in the reference tree the scraper
iterated in the first place, ``ingest/egramSwaraj_API/lgd_codes.json``, which
is git-tracked and covers all 6,794 Odisha GPs. This module flattens that tree
into a lookup keyed by ``gp_lgd_code`` so ``transform.gram_panchayat`` can
join it on.

Two deliberate choices:

* **This is a conformed reference table, not run-scoped evidence.** It is not
  published as a snapshot and carries no ``source_run_id``, because the LGD
  hierarchy is not something a scrape observed -- it is the administrative
  fact the scrape was addressed by. Giving it a source kind would also add it
  to ``schema.KIND_TABLES``, which ``select.resolve_snapshots`` then demands
  of every snapshot, retroactively invalidating the ones already built.
* **The join key is the LGD code, never the name.** 6,794 GPs share only
  5,983 distinct ``gp_name`` values -- 505 names repeat -- so a name join
  would hand some GPs another GP's district with no error anywhere.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Mapping

# The scraper's own copy, imported rather than re-spelled so the two cannot
# drift apart: this is the same file, and the same 6,794 GPs, that produced
# the folder names the normalizer parses.
from src.ingest.egramSwaraj_API.config import LGD_DATA_FILE

# LGD state code 21 is Odisha. The reference tree carries the code but not the
# name, and this warehouse is single-state by construction (the scraper's
# endpoint is parameterised on state 21), so the name is stated here once
# rather than left as an all-NULL column the consumer's views select.
STATE_CODE = "21"
STATE_NAME = "Odisha"

# The columns this module contributes to ``gram_panchayat``, in DDL order.
GEOGRAPHY_COLUMNS = (
    "state_code", "state_name", "district_code", "zp_name", "block_code", "block_name",
)


class GeographyError(RuntimeError):
    """Raised when the LGD reference tree is missing or malformed."""


def _text(value: object, *, field: str, path: Path) -> str:
    if value is None:
        raise GeographyError(f"{path}: {field} is null")
    text = str(value).strip()
    if not text:
        raise GeographyError(f"{path}: {field} is blank")
    return text


def _objects(parent: object, key: str, *, path: Path) -> list[dict]:
    """The list of child objects at ``key``, or a loud error.

    ``GeographyError`` promises to cover a malformed tree, so a top-level
    array, a string where a zilla should be, or a dict-valued ``gps`` has to
    surface as that and not as an ``AttributeError`` from a bare ``.get()``.
    """

    if not isinstance(parent, dict):
        raise GeographyError(f"{path}: expected an object, got {type(parent).__name__}")
    children = parent.get(key)
    if children is None:
        return []
    if not isinstance(children, list):
        raise GeographyError(f"{path}: {key} must be a list, got {type(children).__name__}")
    for child in children:
        if not isinstance(child, dict):
            raise GeographyError(
                f"{path}: every entry in {key} must be an object, got {type(child).__name__}"
            )
    return children


@lru_cache(maxsize=1)
def gp_geography(path: str | Path | None = None) -> Mapping[str, Mapping[str, str]]:
    """Flatten the LGD tree to ``{gp_lgd_code: {column: value}}``.

    Cached: the tree is ~1.1 MB of static reference data and a build reads it
    once per snapshot. Codes are stringified to match ``clean.to_code``, which
    keeps identifiers as text precisely so a float round-trip cannot eat a
    leading zero.
    """

    tree_path = Path(path) if path is not None else Path(LGD_DATA_FILE)
    try:
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GeographyError(f"LGD reference tree not found: {tree_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeographyError(f"LGD reference tree unreadable: {tree_path}: {exc}") from exc

    if not isinstance(tree, dict):
        raise GeographyError(f"{tree_path}: expected a JSON object, got {type(tree).__name__}")
    state_code = _text(tree.get("state_code"), field="state_code", path=tree_path)
    if state_code != STATE_CODE:
        # This warehouse is single-state and STATE_NAME is a constant, so a
        # tree for another state would label every GP "Odisha". Refuse rather
        # than mislabel: whoever swaps the tree must revisit the constant.
        raise GeographyError(
            f"{tree_path}: state_code is {state_code!r}, but this warehouse is built for "
            f"{STATE_CODE!r} ({STATE_NAME}); STATE_NAME must be revisited before changing it"
        )
    lookup: dict[str, Mapping[str, str]] = {}
    for zilla in _objects(tree, "zillas", path=tree_path):
        zp_name = _text(zilla.get("zp_name"), field="zp_name", path=tree_path)
        district_code = _text(zilla.get("zp_lgd_code"), field="zp_lgd_code", path=tree_path)
        for block in _objects(zilla, "blocks", path=tree_path):
            block_name = _text(block.get("bp_name"), field="bp_name", path=tree_path)
            block_code = _text(block.get("bp_lgd_code"), field="bp_lgd_code", path=tree_path)
            for gp in _objects(block, "gps", path=tree_path):
                gp_code = _text(gp.get("gp_lgd_code"), field="gp_lgd_code", path=tree_path)
                # A duplicate code would mean one GP in two blocks, i.e. the
                # reference tree contradicts itself. Silently keeping the last
                # one would assign real GPs to the wrong block, so refuse.
                if gp_code in lookup:
                    raise GeographyError(
                        f"{tree_path}: gp_lgd_code {gp_code!r} appears in more than one block"
                    )
                lookup[gp_code] = {
                    "state_code": state_code,
                    "state_name": STATE_NAME,
                    "district_code": district_code,
                    "zp_name": zp_name,
                    "block_code": block_code,
                    "block_name": block_name,
                }
    if not lookup:
        raise GeographyError(f"{tree_path}: reference tree contains no gram panchayats")
    return lookup
