"""
osm_features.py -- menschliche Infrastruktur aus OSM holen und je See auswerten.

Designentscheidungen:

* EINE gebuendelte Overpass-Abfrage pro Kachel bzw. pro See-Batch statt
  hunderter Einzelabfragen. Alle Kategorien (Gebaeude, Landuse, Tourismus,
  Strassen, Schienen, man_made ...) werden in einem Query-Block geholt.
* Zwei Modi:
    - ``around``: Features im Umkreis ``feature_radius_m`` um die
      See-Geometrien. Sehr datensparsam, ideal bei wenigen Seen.
    - ``bbox``:   Features in der (aufgeweiteten) Regionsbox. Robust und
      unabhaengig von der Seenzahl, dafuer mehr Daten.
    - ``auto``:   around bei <= ``around_max_lakes`` Seen, sonst bbox.
* Distanzen und Buffer ausschliesslich im metrischen CRS.
* Raeumlicher Index (shapely STRtree) statt O(n*m)-Schleifen.
* Distanzen sind bei ``feature_radius_m`` ZENSIERT: alles was weiter weg
  ist, wird als ">= radius" gefuehrt (Spalte ``*_censored``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.strtree import STRtree

from .lakes import _polygon_from_rings, _stitch
from .utils import Bbox, OverpassClient, chunked

LOG = logging.getLogger("lake_finder.osm")

WGS84 = "EPSG:4326"

# --------------------------------------------------------------------------
# Kategorien
# --------------------------------------------------------------------------

MAJOR_ROADS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
}
MINOR_ROADS = {"unclassified", "residential", "living_street", "service", "road"}
SOFT_WAYS = {"track", "path", "footway", "cycleway", "bridleway", "steps"}

ACTIVE_RAILWAY = {
    "rail",
    "light_rail",
    "subway",
    "tram",
    "narrow_gauge",
    "funicular",
    "monorail",
}

BUILT_LANDUSE = {"residential", "commercial", "industrial", "retail"}
RURAL_LANDUSE = {"farmyard", "quarry", "landfill", "military", "brownfield", "construction"}

CAMP_TOURISM = {"camp_site", "caravan_site", "camp_pitch"}
STAY_TOURISM = {
    "hotel",
    "chalet",
    "guest_house",
    "hostel",
    "motel",
    "apartment",
    "resort",
    "alpine_hut",
    "wilderness_hut",
}

# man_made-Werte, die wirklich fuer technische Ueberpraegung stehen.
# (man_made=survey_point, cairn o.ae. sind bewusst NICHT dabei.)
HARD_MAN_MADE = {
    "pier",
    "tower",
    "mast",
    "communications_tower",
    "chimney",
    "works",
    "wastewater_plant",
    "water_works",
    "water_tower",
    "storage_tank",
    "silo",
    "pipeline",
    "windmill",
    "wind_turbine",
    "bunker_silo",
    "gasometer",
    "crane",
    "breakwater",
    "groyne",
    "quay",
}

# Kategorien, fuer die pro Buffer gezaehlt wird
COUNT_CATEGORIES = [
    "building",
    "residential_area",
    "commercial_industrial",
    "campsite",
    "accommodation",
    "marina",
    "parking",
    "man_made",
    "major_road",
    "minor_road",
    "soft_way",
    "railway",
]

# Kategorien, fuer die die Distanz zum naechsten Objekt bestimmt wird
DISTANCE_CATEGORIES = COUNT_CATEGORIES + ["any_road"]


def categorize(tags: dict[str, str]) -> set[str]:
    """Ordnet ein OSM-Objekt einer oder mehreren Infrastrukturkategorien zu."""
    cats: set[str] = set()
    if not tags:
        return cats

    b = tags.get("building")
    if b and b not in ("no", "none"):
        cats.add("building")
    if tags.get("building:part") == "yes":
        cats.add("building")

    lu = tags.get("landuse")
    if lu == "residential":
        cats.add("residential_area")
    elif lu in ("commercial", "industrial", "retail"):
        cats.add("commercial_industrial")
    elif lu in RURAL_LANDUSE:
        cats.add("man_made")

    to = tags.get("tourism")
    if to in CAMP_TOURISM:
        cats.add("campsite")
    elif to in STAY_TOURISM:
        cats.add("accommodation")

    le = tags.get("leisure")
    if le in ("marina", "slipway"):
        cats.add("marina")
    if tags.get("harbour") and tags.get("harbour") != "no":
        cats.add("marina")
    if tags.get("mooring") in ("yes", "private", "public"):
        cats.add("marina")

    if tags.get("amenity") == "parking":
        cats.add("parking")

    mm = tags.get("man_made")
    if mm in HARD_MAN_MADE:
        cats.add("man_made")
    if tags.get("power") in ("plant", "substation", "generator", "line", "tower"):
        cats.add("man_made")

    rw = tags.get("railway")
    if rw in ACTIVE_RAILWAY:
        cats.add("railway")

    hw = tags.get("highway")
    if hw in MAJOR_ROADS:
        cats.add("major_road")
    elif hw in MINOR_ROADS:
        cats.add("minor_road")
    elif hw in SOFT_WAYS:
        cats.add("soft_way")

    return cats


# --------------------------------------------------------------------------
# Overpass-Queries
# --------------------------------------------------------------------------

_SELECTORS = [
    'nwr["building"]',
    'nwr["landuse"~"^(residential|commercial|industrial|retail|farmyard|quarry|landfill|military|brownfield|construction)$"]',
    'nwr["tourism"~"^(camp_site|caravan_site|camp_pitch|hotel|chalet|guest_house|hostel|motel|apartment|resort|alpine_hut|wilderness_hut)$"]',
    'nwr["leisure"~"^(marina|slipway)$"]',
    'nwr["harbour"]',
    'nwr["mooring"]',
    'nwr["amenity"="parking"]',
    'nwr["man_made"~"^(pier|tower|mast|communications_tower|chimney|works|wastewater_plant|water_works|water_tower|storage_tank|silo|pipeline|windmill|wind_turbine|bunker_silo|gasometer|crane|breakwater|groyne|quay)$"]',
    'nwr["power"~"^(plant|substation|generator|line|tower)$"]',
    'way["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|funicular|monorail)$"]',
    'way["highway"]',
]


def features_query_bbox(bbox: Bbox, timeout_s: int = 300) -> str:
    parts = "\n".join(f"  {sel}({bbox.overpass()});" for sel in _SELECTORS)
    return f"[out:json][timeout:{int(timeout_s)}];\n(\n{parts}\n);\nout geom qt;\n"


def features_query_around(
    lake_ids: Sequence[tuple[str, int]], radius_m: float, timeout_s: int = 300
) -> str:
    """Features im Umkreis um eine Menge von Seen (Way-/Relation-IDs)."""
    ways = [i for t, i in lake_ids if t == "way"]
    rels = [i for t, i in lake_ids if t == "relation"]
    setup = []
    if ways:
        setup.append(f"way(id:{','.join(str(i) for i in ways)})->.lw;")
    if rels:
        setup.append(f"relation(id:{','.join(str(i) for i in rels)})->.lr;")
    if ways and rels:
        setup.append("(.lw; .lr;)->.lakes;")
    elif ways:
        setup.append(".lw->.lakes;")
    else:
        setup.append(".lr->.lakes;")
    sel = "\n".join(f"  {s}(around.lakes:{radius_m:.0f});" for s in _SELECTORS)
    return (
        f"[out:json][timeout:{int(timeout_s)}];\n"
        + "\n".join(setup)
        + f"\n(\n{sel}\n);\nout geom qt;\n"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _geom_from_element(el: dict) -> Any | None:
    """Overpass-Element -> shapely-Geometrie (WGS84)."""
    t = el.get("type")
    if t == "node":
        if el.get("lon") is None:
            return None
        return Point(float(el["lon"]), float(el["lat"]))

    if t == "way":
        g = el.get("geometry")
        if not g or len(g) < 2:
            return None
        pts = [(float(p["lon"]), float(p["lat"])) for p in g]
        tags = el.get("tags", {}) or {}
        closed = pts[0] == pts[-1] and len(pts) >= 4
        area_like = bool(
            tags.get("building")
            or tags.get("landuse")
            or tags.get("leisure")
            or tags.get("tourism")
            or tags.get("amenity")
            or tags.get("area") == "yes"
        )
        if closed and area_like:
            try:
                poly = Polygon(pts)
                return poly if poly.is_valid else poly.buffer(0)
            except Exception:  # pragma: no cover
                return LineString(pts)
        return LineString(pts)

    if t == "relation":
        outers, inners, lines = [], [], []
        for m in el.get("members", []) or []:
            g = m.get("geometry")
            if m.get("type") != "way" or not g or len(g) < 2:
                continue
            pts = [(float(p["lon"]), float(p["lat"])) for p in g]
            if m.get("role") == "inner":
                inners.append(pts)
            else:
                outers.append(pts)
            lines.append(pts)
        if el.get("tags", {}).get("type") == "multipolygon" and outers:
            poly = _polygon_from_rings(_stitch(outers), _stitch(inners))
            if poly is not None:
                return poly
        if lines:
            try:
                return MultiLineString([LineString(p) for p in lines if len(p) >= 2])
            except Exception:  # pragma: no cover
                return None
    return None


@dataclass
class FeatureIndex:
    """Raeumliche Indizes je Kategorie im metrischen CRS."""

    crs: str
    geoms: dict[str, list] = field(default_factory=dict)
    trees: dict[str, STRtree] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def build(self, gdf: gpd.GeoDataFrame) -> "FeatureIndex":
        for cat in set(DISTANCE_CATEGORIES):
            if cat == "any_road":
                mask = gdf["categories"].apply(
                    lambda c: bool({"major_road", "minor_road", "soft_way"} & c)
                )
            else:
                mask = gdf["categories"].apply(lambda c, k=cat: k in c)
            sub = gdf.loc[mask, "geometry"]
            geoms = [g for g in sub.values if g is not None and not g.is_empty]
            self.geoms[cat] = geoms
            self.counts[cat] = len(geoms)
            self.trees[cat] = STRtree(geoms) if geoms else None
        return self

    def nearest_distance(self, cat: str, geom: Any, cap: float) -> tuple[float, bool]:
        """Distanz zum naechsten Objekt der Kategorie. (Wert, zensiert?)"""
        tree = self.trees.get(cat)
        if not tree:
            return cap, True
        idx = tree.nearest(geom)
        if idx is None:
            return cap, True
        try:
            d = float(self.geoms[cat][int(idx)].distance(geom))
        except Exception:  # pragma: no cover
            return cap, True
        if d >= cap:
            return cap, True
        return d, False

    def count_within(self, cat: str, buffer_geom: Any) -> int:
        tree = self.trees.get(cat)
        if not tree:
            return 0
        idx = tree.query(buffer_geom, predicate="intersects")
        return int(len(idx))


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------


def fetch_features(
    bbox: Bbox,
    lakes: gpd.GeoDataFrame,
    cfg: dict,
    client: OverpassClient,
    metric_crs: str,
) -> gpd.GeoDataFrame:
    """Holt alle relevanten Infrastruktur-Objekte als GeoDataFrame (metrisch)."""
    ocfg = cfg.get("osm", {})
    if str(ocfg.get("backend", "overpass")).lower() == "pbf":
        from .osm_pbf import fetch_features_pbf

        LOG.info("Backend 'pbf': lokale Verarbeitung eines Geofabrik-Extrakts.")
        return fetch_features_pbf(bbox, cfg, metric_crs)

    radius = float(ocfg.get("feature_radius_m", 3000))
    timeout_s = int(ocfg.get("timeout_s", 300))
    mode = str(ocfg.get("feature_mode", "auto")).lower()
    around_max = int(ocfg.get("around_max_lakes", 200))

    if mode == "auto":
        mode = "around" if 0 < len(lakes) <= around_max else "bbox"
    LOG.info("Feature-Abruf im Modus '%s' (Radius %.0f m).", mode, radius)

    elements: dict[tuple[str, int], dict] = {}

    if mode == "around" and len(lakes):
        ids = list(zip(lakes["osm_type"], lakes["osm_id"]))
        batch = int(ocfg.get("around_batch", 25))
        batches = list(chunked(ids, batch))
        for i, b in enumerate(batches, 1):
            ql = features_query_around(b, radius, timeout_s)
            data = client.query(ql, cache_tag=f"feat-around:{radius}:{sorted(b)}")
            got = data.get("elements", [])
            for el in got:
                elements[(el.get("type", "?"), int(el.get("id", 0)))] = el
            LOG.info("  Batch %d/%d: %d Elemente (gesamt %d)", i, len(batches), len(got), len(elements))
    else:
        big = bbox.buffered(radius)
        tiles = big.tiles(float(ocfg.get("tile_size_deg", 0.15)))
        LOG.info("  %d Kachel(n) fuer Feature-Abfrage.", len(tiles))
        for i, tile in enumerate(tiles, 1):
            ql = features_query_bbox(tile, timeout_s)
            data = client.query(ql, cache_tag=f"feat-bbox:{tile.overpass()}")
            got = data.get("elements", [])
            for el in got:
                elements[(el.get("type", "?"), int(el.get("id", 0)))] = el
            LOG.info("  Kachel %d/%d: %d Elemente (gesamt %d)", i, len(tiles), len(got), len(elements))

    LOG.info("Overpass lieferte %d eindeutige Infrastruktur-Objekte.", len(elements))

    rows: list[dict[str, Any]] = []
    for (otype, oid), el in elements.items():
        tags = el.get("tags", {}) or {}
        cats = categorize(tags)
        if not cats:
            continue
        geom = _geom_from_element(el)
        if geom is None or geom.is_empty:
            continue
        rows.append(
            {
                "osm_type": otype,
                "osm_id": oid,
                "categories": cats,
                "highway": tags.get("highway", ""),
                "name": tags.get("name", ""),
                # Zugangstags: entscheiden spaeter, ob ein Zugangspunkt
                # ueberhaupt oeffentlich befahrbar ist (src/access.py)
                "access": tags.get("access", ""),
                "motor_vehicle": tags.get("motor_vehicle", ""),
                "vehicle": tags.get("vehicle", ""),
                "surface": tags.get("surface", ""),
                "tracktype": tags.get("tracktype", ""),
                "geometry": geom,
            }
        )

    if not rows:
        LOG.warning("Keine Infrastruktur-Objekte gefunden - das ist bei OSM selten und verdaechtig.")
        return gpd.GeoDataFrame(
            columns=[
                "osm_type", "osm_id", "categories", "highway", "name",
                "access", "motor_vehicle", "vehicle", "surface", "tracktype", "geometry",
            ],
            geometry="geometry",
            crs=WGS84,
        ).to_crs(metric_crs)

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84).to_crs(metric_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    stats = {}
    for c in COUNT_CATEGORIES:
        stats[c] = int(gdf["categories"].apply(lambda s, k=c: k in s).sum())
    LOG.info("Kategorien: %s", ", ".join(f"{k}={v}" for k, v in stats.items() if v))
    return gdf


# --------------------------------------------------------------------------
# Auswertung je See
# --------------------------------------------------------------------------


def compute_osm_metrics(
    lakes: gpd.GeoDataFrame,
    features: gpd.GeoDataFrame,
    cfg: dict,
) -> pd.DataFrame:
    """Berechnet je See Zaehlungen und Distanzen -- immer ab UFER, nie ab Mittelpunkt."""
    distances = [int(d) for d in cfg.get("buffers", {}).get("distances_m", [100, 250, 500, 1000])]
    cap = float(cfg.get("osm", {}).get("feature_radius_m", 3000))
    flag_radius = int(cfg.get("osm", {}).get("flag_radius_m", 1000))

    index = FeatureIndex(crs=str(lakes.crs)).build(features)

    out: list[dict[str, Any]] = []
    n = len(lakes)
    for i, (_, lake) in enumerate(lakes.iterrows(), 1):
        if i % 25 == 0 or i == n:
            LOG.info("  OSM-Metriken: %d/%d Seen", i, n)
        geom = lake.geometry
        rec: dict[str, Any] = {"lake_uid": lake["lake_uid"]}

        # Buffer einmal bauen und wiederverwenden
        buffers = {d: geom.buffer(d) for d in distances}

        for d in distances:
            buf = buffers[d]
            for cat in COUNT_CATEGORIES:
                rec[f"{cat}_count_{d}m"] = index.count_within(cat, buf)

        for cat in DISTANCE_CATEGORIES:
            dist, censored = index.nearest_distance(cat, geom, cap)
            rec[f"distance_{cat}_m"] = round(dist, 1)
            rec[f"distance_{cat}_censored"] = bool(censored)

        fr = flag_radius if flag_radius in distances else distances[-1]
        rec["has_campsite"] = rec.get(f"campsite_count_{fr}m", 0) > 0
        rec["has_marina"] = rec.get(f"marina_count_{fr}m", 0) > 0
        rec["has_accommodation"] = rec.get(f"accommodation_count_{fr}m", 0) > 0
        rec["has_residential"] = rec.get(f"residential_area_count_{fr}m", 0) > 0
        rec["has_parking"] = rec.get(f"parking_count_{fr}m", 0) > 0
        rec["has_railway"] = rec.get(f"railway_count_{fr}m", 0) > 0
        rec["flag_radius_m"] = fr
        out.append(rec)

    df = pd.DataFrame(out)

    # Aliasspalten mit den im Auftrag geforderten Namen
    alias = {
        "distance_building_m": "distance_nearest_building_m",
        "distance_major_road_m": "distance_major_road_m",
        "distance_any_road_m": "distance_any_road_m",
        "distance_residential_area_m": "distance_residential_m",
        "distance_campsite_m": "distance_campsite_m",
        "distance_marina_m": "distance_marina_m",
    }
    for src, dst in alias.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
    for d in distances:
        if f"building_count_{d}m" in df.columns:
            df[f"building_count_{d}m"] = df[f"building_count_{d}m"].astype(int)
    return df
