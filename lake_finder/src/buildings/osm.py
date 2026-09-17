"""
buildings/osm.py -- Gebaeude aus der bereits geholten OSM-Feature-Ebene.

Bewusst KEINE eigene Overpass-Abfrage: die Gebaeude stecken schon in dem
gebuendelten Feature-Abruf von ``osm_features.fetch_features``. Hier wird
nur noch herausgefiltert und auf das gemeinsame Schema gebracht.

Gemeinsames Schema aller Gebaeudequellen:
    source_id, source, height, geometry   (metrisches CRS)
"""

from __future__ import annotations

import logging

LOG = logging.getLogger("lake_finder.buildings.osm")


def load(features, cfg: dict | None = None):
    """Filtert die Gebaeude aus dem Feature-GeoDataFrame."""
    import geopandas as gpd

    if features is None or len(features) == 0:
        return gpd.GeoDataFrame(
            {"source_id": [], "source": [], "height": []}, geometry=[], crs=None
        )

    mask = features["categories"].apply(lambda c: "building" in c)
    sub = features.loc[mask].copy()
    if sub.empty:
        LOG.info("OSM: keine Gebaeude im Abrufbereich.")
        return gpd.GeoDataFrame(
            {"source_id": [], "source": [], "height": []}, geometry=[], crs=features.crs
        )

    out = gpd.GeoDataFrame(
        {
            "source_id": [f"osm:{t}/{i}" for t, i in zip(sub["osm_type"], sub["osm_id"])],
            "source": "osm",
            "height": None,
        },
        geometry=list(sub.geometry.values),
        crs=features.crs,
    )
    LOG.info("OSM: %d Gebaeude.", len(out))
    return out
