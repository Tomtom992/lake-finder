"""
buildings -- Gebaeude aus mehreren Quellen, zusammengefuehrt und bewertet.

    official  >  overture  >  osm

``build_layer`` ist der einzige Einstiegspunkt, den die Pipeline braucht.
Faellt eine Quelle aus, laeuft der Rest weiter -- der Verlust wird im
Rueckgabewert vermerkt und landet spaeter in der Confidence.
"""

from __future__ import annotations

import logging
from typing import Any

from ..utils import Bbox
from . import merge as merge_mod
from . import official as official_mod
from . import osm as osm_mod
from . import overture as overture_mod

LOG = logging.getLogger("lake_finder.buildings")

KNOWN_SOURCES = merge_mod.KNOWN_SOURCES
dedup_records = merge_mod.dedup_records
source_summary = merge_mod.source_summary


def build_layer(bbox: Bbox, features, cfg: dict, metric_crs: str) -> dict[str, Any]:
    """Baut die zusammengefuehrte Gebaeudeebene.

    Rueckgabe:
        {"merged": GeoDataFrame, "tree": STRtree|None,
         "sources_used": [...], "sources_failed": {name: grund}}
    """
    from shapely.strtree import STRtree

    gdfs: dict[str, Any] = {}
    failed: dict[str, str] = {}

    try:
        gdfs["osm"] = osm_mod.load(features, cfg)
    except Exception as exc:  # pragma: no cover
        failed["osm"] = str(exc)
        LOG.warning("OSM-Gebaeude fehlgeschlagen: %s", exc)

    try:
        g = overture_mod.load(bbox, cfg, metric_crs)
        if g is not None and len(g):
            gdfs["overture"] = g
    except Exception as exc:
        failed["overture"] = str(exc)
        LOG.warning("Overture-Gebaeude nicht verfuegbar: %s", exc)

    try:
        g = official_mod.load(bbox, cfg, metric_crs)
        if g is not None and len(g):
            gdfs["official"] = g
    except Exception as exc:
        failed["official"] = str(exc)
        LOG.warning("Amtliche Gebaeude nicht verfuegbar: %s", exc)

    merged = merge_mod.merge_sources(gdfs, cfg, metric_crs)
    geoms = [g for g in merged.geometry.values if g is not None and not g.is_empty]
    tree = STRtree(geoms) if geoms else None

    return {
        "merged": merged,
        "tree": tree,
        "sources_used": [s for s in KNOWN_SOURCES if s in gdfs and len(gdfs[s])],
        "sources_failed": failed,
    }


def lake_metrics(lake_geom, layer: dict, distances, cap: float = 3000.0) -> dict[str, Any]:
    """Gebaeudekennzahlen eines Sees aus der zusammengefuehrten Ebene."""
    return merge_mod.lake_building_metrics(
        lake_geom,
        layer.get("merged"),
        layer.get("tree"),
        distances,
        cap,
        sources_queried=layer.get("sources_used"),
    )
