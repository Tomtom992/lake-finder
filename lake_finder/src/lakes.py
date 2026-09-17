"""
lakes.py -- Seen aus OpenStreetMap holen und als echte Polygone aufbereiten.

Kernpunkte:
  * Ways UND Relationen (type=multipolygon) werden verarbeitet; Relationen
    werden aus ihren Member-Ways zu Ringen zusammengesetzt (inkl. Inseln
    als Loecher).
  * Filterung ueber ``natural=water`` + ``water=*`` mit konfigurierbaren
    Positiv-/Negativlisten.
  * Flaeche, Umfang, Mittelpunkt und Bounding Box werden IMMER im
    metrischen CRS berechnet, nie in EPSG:4326.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .utils import Bbox, JsonCache, OverpassClient

LOG = logging.getLogger("lake_finder.lakes")

WGS84 = "EPSG:4326"

# Werte von water=*, die nie ein "See" sind
DEFAULT_EXCLUDE_WATER = [
    "river",
    "canal",
    "ditch",
    "drain",
    "stream",
    "stream_pool",
    "wastewater",
    "fish_pass",
    "moat",
    "reflecting_pool",
    "swimming_pool",
    "salt_pool",
    "harbour",
    "fountain",
]


# --------------------------------------------------------------------------
# Overpass-Abfrage
# --------------------------------------------------------------------------


def lake_query(bbox: Bbox, timeout_s: int = 300, include_reservoirs: bool = False) -> str:
    """Overpass-QL fuer alle Wasserflaechen in der Box.

    Wir holen bewusst grosszuegig ``natural=water`` (plus optional
    ``landuse=reservoir``) und filtern die Tags anschliessend lokal --
    das ist eine Abfrage statt vieler und macht die Filterlogik
    nachvollziehbar und ohne neuen Netzwerkzugriff aenderbar.
    """
    extra = ""
    if include_reservoirs:
        extra = (
            '  way["landuse"="reservoir"]({bb});\n'
            '  relation["landuse"="reservoir"]({bb});\n'
        ).format(bb=bbox.overpass())
    return f"""[out:json][timeout:{int(timeout_s)}];
(
  way["natural"="water"]({bbox.overpass()});
  relation["natural"="water"]["type"="multipolygon"]({bbox.overpass()});
{extra});
out geom qt;
"""


# --------------------------------------------------------------------------
# Geometrie-Aufbau
# --------------------------------------------------------------------------


def _coords(geometry: Iterable[dict]) -> list[tuple[float, float]]:
    return [(float(p["lon"]), float(p["lat"])) for p in geometry if p]


def _ring_from_way(el: dict) -> list[tuple[float, float]] | None:
    geom = el.get("geometry")
    if not geom or len(geom) < 4:
        return None
    pts = _coords(geom)
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    if len(pts) < 4:
        return None
    return pts


def _stitch(segments: list[list[tuple[float, float]]], tol: float = 1e-9) -> list[list[tuple[float, float]]]:
    """Setzt offene Linienstuecke zu geschlossenen Ringen zusammen.

    Overpass liefert Multipolygon-Relationen als Sammlung von Member-Ways.
    Ein Aussenring kann aus mehreren Ways bestehen, die nur ueber ihre
    Endpunkte verbunden sind. Nicht schliessbare Reste werden verworfen
    (und gezaehlt -> Datenqualitaets-Flag).
    """
    open_segs = [list(s) for s in segments if len(s) >= 2]
    rings: list[list[tuple[float, float]]] = []

    # bereits geschlossene Segmente direkt uebernehmen
    rest: list[list[tuple[float, float]]] = []
    for s in open_segs:
        if s[0] == s[-1] and len(s) >= 4:
            rings.append(s)
        else:
            rest.append(s)

    def close_enough(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    while rest:
        cur = rest.pop(0)
        changed = True
        while changed and not close_enough(cur[0], cur[-1]):
            changed = False
            for i, seg in enumerate(rest):
                if close_enough(cur[-1], seg[0]):
                    cur = cur + seg[1:]
                elif close_enough(cur[-1], seg[-1]):
                    cur = cur + list(reversed(seg))[1:]
                elif close_enough(cur[0], seg[-1]):
                    cur = seg[:-1] + cur
                elif close_enough(cur[0], seg[0]):
                    cur = list(reversed(seg))[:-1] + cur
                else:
                    continue
                rest.pop(i)
                changed = True
                break
        if close_enough(cur[0], cur[-1]) and len(cur) >= 4:
            cur[-1] = cur[0]
            rings.append(cur)
        else:
            LOG.debug("Offener Ring mit %d Punkten verworfen.", len(cur))
    return rings


def _polygon_from_rings(
    outers: list[list[tuple[float, float]]], inners: list[list[tuple[float, float]]]
) -> Polygon | MultiPolygon | None:
    """Baut aus Aussen-/Innenringen ein (Multi-)Polygon und ordnet Loecher zu."""
    outer_polys = []
    for ring in outers:
        try:
            p = Polygon(ring)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                outer_polys.append(p)
        except Exception:  # pragma: no cover - defekte Geometrie
            continue
    if not outer_polys:
        return None

    inner_polys = []
    for ring in inners:
        try:
            p = Polygon(ring)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                inner_polys.append(p)
        except Exception:  # pragma: no cover
            continue

    built: list[Polygon] = []
    for op in outer_polys:
        holes = [ip.exterior.coords for ip in inner_polys if op.contains(ip.representative_point())]
        try:
            poly = Polygon(op.exterior.coords, holes)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                built.append(poly)
        except Exception:  # pragma: no cover
            built.append(op)

    if not built:
        return None
    geom = built[0] if len(built) == 1 else unary_union(built)
    if geom.geom_type == "Polygon":
        return orient(geom, sign=1.0)
    if geom.geom_type == "MultiPolygon":
        return MultiPolygon([orient(g, sign=1.0) for g in geom.geoms])
    return None


# --------------------------------------------------------------------------
# Tag-Filter
# --------------------------------------------------------------------------


def is_lake(tags: dict[str, str], cfg: dict) -> tuple[bool, str]:
    """Entscheidet, ob ein Wasserobjekt als See zaehlt. Gibt (ok, Grund) zurueck."""
    water = (tags.get("water") or "").strip().lower()
    natural = (tags.get("natural") or "").strip().lower()
    landuse = (tags.get("landuse") or "").strip().lower()

    accept = {v.lower() for v in cfg.get("accept_water_values", ["lake", "pond", "oxbow", "lagoon"])}
    exclude = {v.lower() for v in cfg.get("exclude_water_values", DEFAULT_EXCLUDE_WATER)}

    if cfg.get("exclude_intermittent", True) and tags.get("intermittent") in ("yes", "seasonal"):
        return False, "intermittent"
    if tags.get("salt") == "yes" and not cfg.get("include_salt", True):
        return False, "salt"

    if landuse == "reservoir" or water == "reservoir" or tags.get("water") == "basin":
        return (True, "reservoir") if cfg.get("include_reservoirs", False) else (False, "reservoir")

    if water in exclude:
        return False, f"water={water}"
    if water in accept:
        return True, f"water={water}"
    if not water and natural == "water":
        if cfg.get("include_untagged_natural_water", True):
            return True, "natural=water (ohne water=*)"
        return False, "natural=water ohne water=*"
    if water:
        # unbekannter water-Wert: konservativ ablehnen, aber protokollieren
        return False, f"water={water} (nicht in accept-Liste)"
    return False, "kein Wassertag"


# --------------------------------------------------------------------------
# Hauptfunktion
# --------------------------------------------------------------------------


def fetch_lakes(
    bbox: Bbox,
    cfg: dict,
    client: OverpassClient,
    metric_crs: str,
) -> gpd.GeoDataFrame:
    """Holt alle Seen der Region und liefert einen GeoDataFrame im metrischen CRS.

    Spalten: osm_type, osm_id, name, tags, area_ha, perimeter_m,
             centroid_lon/lat, bbox_* , geometry
    """
    if str((cfg.get("osm", {}) or {}).get("backend", "overpass")).lower() == "pbf":
        from .osm_pbf import fetch_lakes_pbf

        LOG.info("Backend 'pbf': lokale Verarbeitung eines Geofabrik-Extrakts.")
        return fetch_lakes_pbf(bbox, cfg, metric_crs)

    lcfg = cfg.get("lakes", {})
    tile_deg = float(cfg.get("osm", {}).get("lake_tile_size_deg", 0.5))
    tiles = bbox.tiles(tile_deg)
    LOG.info("Seen-Abfrage: %d Kachel(n) a max %.2f Grad.", len(tiles), tile_deg)

    elements: dict[tuple[str, int], dict] = {}
    for i, tile in enumerate(tiles, 1):
        ql = lake_query(
            tile,
            timeout_s=int(cfg.get("osm", {}).get("timeout_s", 300)),
            include_reservoirs=bool(lcfg.get("include_reservoirs", False)),
        )
        data = client.query(ql, cache_tag=f"lakes:{tile.overpass()}")
        got = data.get("elements", [])
        for el in got:
            elements[(el.get("type", "?"), int(el.get("id", 0)))] = el
        LOG.info("  Kachel %d/%d: %d Elemente (gesamt %d)", i, len(tiles), len(got), len(elements))

    LOG.info("Overpass lieferte %d eindeutige Wasser-Objekte.", len(elements))

    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    broken = 0

    for (otype, oid), el in elements.items():
        tags = el.get("tags", {}) or {}
        ok, reason = is_lake(tags, lcfg)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue

        geom = None
        if otype == "way":
            ring = _ring_from_way(el)
            if ring:
                geom = _polygon_from_rings([ring], [])
        elif otype == "relation":
            outers, inners = [], []
            for m in el.get("members", []) or []:
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = _coords(m["geometry"])
                if len(pts) < 2:
                    continue
                (inners if m.get("role") == "inner" else outers).append(pts)
            geom = _polygon_from_rings(_stitch(outers), _stitch(inners))

        if geom is None or geom.is_empty:
            broken += 1
            continue

        rows.append(
            {
                "osm_type": otype,
                "osm_id": oid,
                "name": tags.get("name") or tags.get("alt_name") or "",
                "water_tag": tags.get("water", ""),
                "tags": tags,
                "geometry": geom,
            }
        )

    if rejected:
        LOG.info(
            "Verworfen nach Tag-Filter: %s",
            ", ".join(f"{k}: {v}" for k, v in sorted(rejected.items(), key=lambda x: -x[1])[:8]),
        )
    if broken:
        LOG.warning("%d Objekte mit unbrauchbarer Geometrie verworfen.", broken)

    if not rows:
        LOG.warning("Keine Seen gefunden.")
        return gpd.GeoDataFrame(
            columns=["osm_type", "osm_id", "name", "water_tag", "tags", "geometry"],
            geometry="geometry",
            crs=WGS84,
        ).to_crs(metric_crs)

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84)

    # Mittelpunkt in WGS84 merken (fuer Karte/Links), bevor projiziert wird
    reps = gdf.geometry.representative_point()
    gdf["centroid_lon"] = reps.x
    gdf["centroid_lat"] = reps.y

    gdf = gdf.to_crs(metric_crs)
    gdf["geometry"] = gdf.geometry.buffer(0)  # Selbstueberschneidungen bereinigen
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()

    gdf["area_ha"] = gdf.geometry.area / 10_000.0
    gdf["perimeter_m"] = gdf.geometry.length
    bounds = gdf.geometry.bounds
    gdf["bbox_minx"] = bounds["minx"]
    gdf["bbox_miny"] = bounds["miny"]
    gdf["bbox_maxx"] = bounds["maxx"]
    gdf["bbox_maxy"] = bounds["maxy"]

    # Formindex: 1 = Kreis, >1 = zerklueftet. Hoher Wert = viel Uferlinie.
    gdf["shore_complexity"] = gdf["perimeter_m"] / (
        2.0 * (3.141592653589793 * gdf["area_ha"] * 10_000.0) ** 0.5
    )

    n_before = len(gdf)
    amin = float(lcfg.get("min_area_ha", 1.0))
    amax = float(lcfg.get("max_area_ha", 100000.0))
    gdf = gdf[(gdf["area_ha"] >= amin) & (gdf["area_ha"] <= amax)].copy()
    LOG.info(
        "%d Seen nach Flaechenfilter (%.1f - %.1f ha), vorher %d.",
        len(gdf),
        amin,
        amax,
        n_before,
    )

    gdf = gdf.sort_values("area_ha", ascending=False).reset_index(drop=True)
    gdf["lake_uid"] = gdf["osm_type"].str[0] + gdf["osm_id"].astype(str)
    return gdf


def dissolve_all_water(gdf_lakes: gpd.GeoDataFrame) -> Any:
    """Vereinigte Wasserflaeche aller Seen -- fuer Distanzen zu Nachbarseen."""
    if gdf_lakes.empty:
        return None
    return unary_union(list(gdf_lakes.geometry.values))
