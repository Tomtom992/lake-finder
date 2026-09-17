"""
buildings/official.py -- amtliche Hausumringe (hoechste Prioritaet).

Seit Juni 2024 sind die Geobasisdaten der meisten Bundeslaender Open Data
(Datenlizenz Deutschland -- Namensnennung 2.0). Hausumringe (HU) bzw. die
Gebaeude aus ALKIS sind damit fuer Brandenburg, Mecklenburg-Vorpommern,
Sachsen, Sachsen-Anhalt und Thueringen frei verfuegbar -- allerdings ueber
fuenf verschiedene Portale, teils als Jahresabzug, teils hinter einer
Registrierung und in unterschiedlichen Formaten.

Deshalb bewusst KEIN automatischer Download: dieses Modul liest lokale
Dateien, die man einmal herunterlaedt, und klippt sie auf die Region.
Alles, was geopandas lesen kann, funktioniert -- GeoPackage, Shapefile,
FlatGeobuf, GeoJSON, GeoParquet.

config.yaml:

    buildings:
      official:
        enabled: true
        sources:
          - state: brandenburg
            path: data/official/bb_hausumringe.gpkg
            layer: null          # optional
          - state: mecklenburg_vorpommern
            path: data/official/mv_gebaeude/*.shp

Portale (Stand 2026):
    Brandenburg   Geobroker  https://geobroker.geobasis-bb.de/
    MV            GeoPortal.MV / GDI-MV
    Sachsen       GeoSN Open Data
    Sachsen-Anhalt https://www.lvermgeo.sachsen-anhalt.de/de/gdp-open-data.html
    Thueringen    Geoportal-Th / TLBG

Die genauen Download-Links aendern sich; sie gehoeren deshalb in die
config und nicht in den Code.
"""

from __future__ import annotations

import glob
import logging
from pathlib import Path

from ..utils import Bbox

LOG = logging.getLogger("lake_finder.buildings.official")


def load(bbox: Bbox, cfg: dict, metric_crs: str):
    """Liest alle konfigurierten amtlichen Gebaeudedateien fuer die Region."""
    import geopandas as gpd
    import pandas as pd

    ocfg = (cfg.get("buildings", {}) or {}).get("official", {}) or {}
    if not ocfg.get("enabled", False):
        return None
    sources = ocfg.get("sources", []) or []
    if not sources:
        LOG.info("buildings.official ist aktiv, aber es sind keine Quellen eingetragen.")
        return None

    parts = []
    for src in sources:
        pattern = str(src.get("path", ""))
        if not pattern:
            continue
        paths = sorted(glob.glob(pattern))
        if not paths:
            LOG.warning("Amtliche Gebaeudequelle ohne Treffer: %s", pattern)
            continue
        for p in paths:
            try:
                kw = {"layer": src["layer"]} if src.get("layer") else {}
                g = gpd.read_file(p, bbox=tuple(bbox.as_list()), **kw)
            except Exception as exc:
                LOG.warning("Amtliche Datei %s nicht lesbar: %s", Path(p).name, exc)
                continue
            if g is None or g.empty:
                continue
            if g.crs is None:
                crs = src.get("crs")
                if not crs:
                    LOG.warning("%s hat kein CRS und buildings.official[].crs fehlt - "
                                "Datei wird uebersprungen.", Path(p).name)
                    continue
                g = g.set_crs(crs)
            g = g.to_crs(metric_crs)
            g = g[g.geometry.notna() & ~g.geometry.is_empty]
            g = gpd.GeoDataFrame(
                {
                    "source_id": [
                        f"official:{src.get('state', '?')}:{i}" for i in range(len(g))
                    ],
                    "source": "official",
                    "height": g["height"] if "height" in g.columns else None,
                },
                geometry=list(g.geometry.values),
                crs=metric_crs,
            )
            parts.append(g)
            LOG.info("Amtlich (%s): %d Gebaeude aus %s", src.get("state", "?"), len(g), Path(p).name)

    if not parts:
        return None
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=metric_crs)
    LOG.info("Amtliche Gebaeude gesamt: %d", len(out))
    return out
