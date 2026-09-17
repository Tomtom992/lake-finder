"""
elevation/states.py -- Auswahl der Landesquelle.

Die fünf ostdeutschen Länder sind je eigene Module, weil sich Portal,
Kachelschema, CRS und Bezugsweg unterscheiden -- und weil sich das bei
einem Land ändern kann, ohne die anderen zu berühren.
"""

from __future__ import annotations

import logging

from . import brandenburg, mecklenburg_vorpommern, sachsen, sachsen_anhalt, thueringen

LOG = logging.getLogger("lake_finder.elevation.states")

MODULES = {
    "brandenburg": brandenburg,
    "bb": brandenburg,
    "mecklenburg_vorpommern": mecklenburg_vorpommern,
    "mv": mecklenburg_vorpommern,
    "sachsen": sachsen,
    "sn": sachsen,
    "sachsen_anhalt": sachsen_anhalt,
    "st": sachsen_anhalt,
    "thueringen": thueringen,
    "th": thueringen,
}


def info(state: str) -> dict | None:
    m = MODULES.get(str(state).strip().lower())
    return None if m is None else dict(m.SOURCE_INFO)


def all_info() -> list[dict]:
    seen, out = set(), []
    for m in MODULES.values():
        if m.SOURCE_INFO["state"] in seen:
            continue
        seen.add(m.SOURCE_INFO["state"])
        out.append(dict(m.SOURCE_INFO))
    return out


def make_source(state: str, cfg: dict):
    m = MODULES.get(str(state).strip().lower())
    if m is None:
        LOG.warning(
            "Unbekanntes Bundesland '%s'. Verfügbar: %s",
            state, ", ".join(sorted({x.SOURCE_INFO["state"] for x in MODULES.values()})),
        )
        return None
    return m.make_source(cfg)
