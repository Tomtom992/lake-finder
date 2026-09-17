"""
osm_pbf.py -- Alternative zu Overpass fuer grosse Regionen.

Warum ueberhaupt: Overpass ist ein gemeinsam genutzter, gedrosselter
Dienst. Fuer eine 30x30-km-Region ist er ideal; fuer ein ganzes
Bundesland oder mehrere zehntausend Quadratkilometer ist er die falsche
Schnittstelle -- man laedt dann einmal den regionalen Extrakt von
Geofabrik und verarbeitet ihn lokal.

Voraussetzung:
    pip install pyrosm

Konfiguration (config.yaml):
    osm:
      backend: pbf
      pbf:
        enabled: true
        url:  https://download.geofabrik.de/europe/germany/brandenburg-latest.osm.pbf
        path: data/pbf/brandenburg-latest.osm.pbf

Die zurueckgegebenen GeoDataFrames haben dieselbe Struktur wie die
Overpass-Varianten, sodass der Rest der Pipeline unveraendert bleibt.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .utils import Bbox, retry

LOG = logging.getLogger("lake_finder.pbf")

WGS84 = "EPSG:4326"


@retry(times=3, delay=5.0)
def ensure_pbf(cfg: dict) -> Path:
    """Laedt den regionalen OSM-Extrakt, falls noch nicht vorhanden."""
    import requests

    p = (cfg.get("osm", {}) or {}).get("pbf", {}) or {}
    path = Path(p.get("path", "data/pbf/region-latest.osm.pbf"))
    if path.exists() and path.stat().st_size > 1_000_000:
        LOG.info("PBF vorhanden: %s (%.0f MB)", path, path.stat().st_size / 1e6)
        return path
    url = p.get("url")
    if not url:
        raise RuntimeError("osm.pbf.url ist nicht gesetzt.")
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Lade OSM-Extrakt %s ...", url)
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=(30, 1800)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=4 << 20):
                fh.write(chunk)
    tmp.replace(path)
    LOG.info("PBF gespeichert: %s (%.0f MB)", path, path.stat().st_size / 1e6)
    return path


def _osm(cfg: dict, bbox: Bbox):
    from pyrosm import OSM

    return OSM(str(ensure_pbf(cfg)), bounding_box=bbox.as_list())


def fetch_lakes_pbf(bbox: Bbox, cfg: dict, metric_crs: str):
    """Seen aus dem PBF-Extrakt -- Rueckgabe wie lakes.fetch_lakes."""
    import geopandas as gpd

    from .lakes import is_lake

    osm = _osm(cfg, bbox)
    gdf = osm.get_data_by_custom_criteria(
        custom_filter={"natural": ["water"], "landuse": ["reservoir"]},
        filter_type="keep",
        keep_nodes=False,
        keep_ways=True,
        keep_relations=True,
    )
    if gdf is None or gdf.empty:
        LOG.warning("Keine Wasserflaechen im PBF-Ausschnitt.")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84).to_crs(metric_crs)

    lcfg = cfg.get("lakes", {})
    rows = []
    for _, r in gdf.iterrows():
        tags = {k: v for k, v in r.items() if isinstance(v, str) and k != "geometry"}
        if isinstance(r.get("tags"), dict):
            tags.update(r["tags"])
        ok, _reason = is_lake(tags, lcfg)
        if not ok:
            continue
        geom = r.geometry
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        rows.append(
            {
                "osm_type": "relation" if geom.geom_type == "MultiPolygon" else "way",
                "osm_id": int(r.get("id", 0) or 0),
                "name": r.get("name") or "",
                "water_tag": tags.get("water", ""),
                "tags": tags,
                "geometry": geom,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(geometry=[], crs=WGS84).to_crs(metric_crs)

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84)
    reps = out.geometry.representative_point()
    out["centroid_lon"] = reps.x
    out["centroid_lat"] = reps.y
    out = out.to_crs(metric_crs)
    out["geometry"] = out.geometry.buffer(0)
    out["area_ha"] = out.geometry.area / 10_000.0
    out["perimeter_m"] = out.geometry.length
    b = out.geometry.bounds
    out["bbox_minx"], out["bbox_miny"] = b["minx"], b["miny"]
    out["bbox_maxx"], out["bbox_maxy"] = b["maxx"], b["maxy"]
    out["shore_complexity"] = out["perimeter_m"] / (
        2.0 * (3.141592653589793 * out["area_ha"] * 10_000.0) ** 0.5
    )
    amin = float(lcfg.get("min_area_ha", 1.0))
    amax = float(lcfg.get("max_area_ha", 100000.0))
    out = out[(out["area_ha"] >= amin) & (out["area_ha"] <= amax)].copy()
    out = out.sort_values("area_ha", ascending=False).reset_index(drop=True)
    out["lake_uid"] = out["osm_type"].str[0] + out["osm_id"].astype(str)
    LOG.info("PBF: %d Seen nach Filter.", len(out))
    return out


def fetch_features_pbf(bbox: Bbox, cfg: dict, metric_crs: str):
    """Infrastruktur aus dem PBF-Extrakt -- Rueckgabe wie osm_features.fetch_features."""
    import geopandas as gpd
    import pandas as pd

    from .osm_features import categorize

    radius = float((cfg.get("osm", {}) or {}).get("feature_radius_m", 3000))
    big = bbox.buffered(radius)
    osm = _osm(cfg, big)

    parts = []
    getters = [
        ("buildings", lambda: osm.get_buildings()),
        ("landuse", lambda: osm.get_landuse()),
        ("pois", lambda: osm.get_pois(
            custom_filter={"tourism": True, "amenity": ["parking"], "leisure": ["marina", "slipway"]}
        )),
        ("network", lambda: osm.get_network(network_type="all")),
        ("man_made", lambda: osm.get_data_by_custom_criteria(
            custom_filter={"man_made": True, "power": True, "railway": True, "harbour": True},
            filter_type="keep", keep_nodes=True, keep_ways=True, keep_relations=True)),
    ]
    for name, fn in getters:
        try:
            g = fn()
            if g is not None and not g.empty:
                parts.append(g)
                LOG.info("  PBF %s: %d Objekte", name, len(g))
        except Exception as exc:
            LOG.warning("  PBF %s fehlgeschlagen: %s", name, exc)

    if not parts:
        return gpd.GeoDataFrame(
            columns=["osm_type", "osm_id", "categories", "highway", "name", "geometry"],
            geometry="geometry", crs=WGS84,
        ).to_crs(metric_crs)

    gdf = pd.concat(parts, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=WGS84)

    cats = []
    for _, r in gdf.iterrows():
        tags = {k: v for k, v in r.items() if isinstance(v, str) and k != "geometry"}
        if isinstance(r.get("tags"), dict):
            tags.update(r["tags"])
        cats.append(categorize(tags))
    gdf["categories"] = cats
    gdf = gdf[gdf["categories"].apply(bool)].copy()
    gdf["osm_type"] = "way"
    gdf["osm_id"] = gdf.get("id", 0)
    gdf["highway"] = gdf.get("highway", "")
    gdf["name"] = gdf.get("name", "")
    gdf = gdf[["osm_type", "osm_id", "categories", "highway", "name", "geometry"]]
    gdf = gdf.to_crs(metric_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    LOG.info("PBF: %d Infrastruktur-Objekte mit Kategorie.", len(gdf))
    return gdf
