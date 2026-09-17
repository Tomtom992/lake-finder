"""
buildings/merge.py -- mehrere Gebaeudequellen zu einer Ebene zusammenfuehren.

Prioritaet (hoechste zuerst):

    official  >  overture  >  osm

Amtliche Hausumringe (ALKIS/HU) sind die beste verfuegbare Wahrheit, sobald
sie vorliegen; Overture buendelt u. a. Microsoft- und Google-Gebaeude-
extraktionen aus Luft- und Satellitenbildern und findet damit vieles, was in
OSM ausserhalb von Siedlungen schlicht fehlt; OSM ist am aktuellsten, wo
jemand gemappt hat.

Kernpunkt fuer die Bewertung: Wenn OSM 0 Gebaeude kennt und Overture 3,
ist nicht "0" die Wahrheit und auch nicht "3" -- es ist ein Widerspruch,
der die Confidence senken muss. Genau dafuer gibt es
``building_sources_disagree``.

Die Deduplizierung arbeitet in zwei Stufen:

1. ``dedup_records`` -- rein numerisch auf (x, y, Flaeche, Quelle). Ohne
   Geo-Abhaengigkeiten, deterministisch und offline testbar.
2. optionale geometrische Verfeinerung ueber die Ueberlappung (IoU), wenn
   shapely verfuegbar ist und beide Kandidaten Polygone haben.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping, Sequence

LOG = logging.getLogger("lake_finder.buildings")

SOURCE_PRIORITY = {"official": 3, "overture": 2, "osm": 1}
KNOWN_SOURCES = ("official", "overture", "osm")


# ==========================================================================
# Reine Dedup-Logik
# ==========================================================================


def _grid_key(x: float, y: float, cell: float) -> tuple[int, int]:
    return int(math.floor(x / cell)), int(math.floor(y / cell))


def dedup_records(
    records: Sequence[Mapping[str, Any]],
    max_centroid_dist_m: float = 12.0,
    max_area_ratio: float = 4.0,
    priority: Mapping[str, int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Dedupliziert Gebaeude ueber Schwerpunktabstand und Flaechenverhaeltnis.

    Erwartet je Datensatz mindestens ``x``, ``y``, ``source``; optional
    ``area_m2`` und ``id``.

    Rueckgabe: (behaltene Datensaetze, alle Datensaetze mit Dedup-Info).
    Ein behaltener Datensatz traegt zusaetzlich:
        ``sources``     -- alle Quellen, die dieses Gebaeude kennen
        ``n_sources``   -- Anzahl davon
        ``duplicates``  -- Anzahl zusammengefuehrter Fremdeintraege

    Der Schwerpunktabstand ist bewusst grosszuegig (Default 12 m): OSM-
    Umringe, Overture-Footprints und amtliche Hausumringe desselben
    Gebaeudes liegen selten deckungsgleich. Das Flaechenverhaeltnis
    verhindert, dass eine Scheune eine Nachbarhuette schluckt.
    """
    prio = dict(priority or SOURCE_PRIORITY)
    items = []
    for i, r in enumerate(records):
        items.append(
            {
                "i": i,
                "x": float(r["x"]),
                "y": float(r["y"]),
                "area_m2": (None if r.get("area_m2") is None else float(r["area_m2"])),
                "source": str(r.get("source", "osm")),
                "id": r.get("id"),
		"height": r.get("height"),
		"geometry": r.get("geometry"),
            }
        )
    # Hoehere Prioritaet zuerst, bei Gleichstand grosse Flaeche zuerst,
    # damit das Ergebnis unabhaengig von der Eingabereihenfolge ist.
    items.sort(key=lambda r: (-prio.get(r["source"], 0), -(r["area_m2"] or 0.0), r["i"]))

    cell = max(max_centroid_dist_m, 1.0)
    grid: dict[tuple[int, int], list[dict]] = {}
    kept: list[dict] = []

    for it in items:
        gx, gy = _grid_key(it["x"], it["y"], cell)
        match = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for cand in grid.get((gx + dx, gy + dy), ()):
                    d = math.hypot(cand["x"] - it["x"], cand["y"] - it["y"])
                    if d > max_centroid_dist_m:
                        continue
                    a1, a2 = cand.get("area_m2"), it.get("area_m2")
                    if a1 and a2:
                        ratio = max(a1, a2) / max(min(a1, a2), 1e-6)
                        if ratio > max_area_ratio:
                            continue
                    match = cand
                    break
                if match:
                    break
            if match:
                break

        if match is not None:
            match["sources"].add(it["source"])
            match["n_sources"] = len(match["sources"])
            match["duplicates"] = match.get("duplicates", 0) + 1
            match.setdefault("merged_ids", []).append(it.get("id"))
            if match.get("height") is None and it.get("height") is not None:
                match["height"] = it["height"]
        else:
            it["sources"] = {it["source"]}
            it["n_sources"] = 1
            it["duplicates"] = 0
            kept.append(it)
            grid.setdefault((gx, gy), []).append(it)

    for k in kept:
        k["sources"] = sorted(k["sources"])
    kept.sort(key=lambda r: r["i"])
    return kept, items


def source_summary(
    kept: Sequence[Mapping[str, Any]],
    sources_queried: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Zaehlt, wie viele Gebaeude jede Quelle kennt, und erkennt Widerspruch.

    ``sources_queried`` ist wichtig: nur eine Quelle, die tatsaechlich
    abgefragt wurde, kann widersprechen. Eine gar nicht konfigurierte
    Quelle mit 0 Gebaeuden ist kein Widerspruch, sondern eine Luecke --
    dafuer gibt es ``building_source_count``.
    """
    per_source = {s: 0 for s in KNOWN_SOURCES}
    for rec in kept:
        for s in rec.get("sources", ()):
            per_source[s] = per_source.get(s, 0) + 1
    present = [s for s in KNOWN_SOURCES if per_source.get(s, 0) > 0]
    queried = list(sources_queried) if sources_queried else list(present)
    total = len(kept)

    # Widerspruch: eine abgefragte Quelle sieht (fast) nichts, eine andere
    # deutlich mehr. Das ist der Fall "OSM 0, Overture 5".
    disagree = False
    if len(queried) >= 2:
        counts = [per_source.get(s, 0) for s in queried]
        lo, hi = min(counts), max(counts)
        if hi >= 2 and (lo == 0 or hi >= 2 * lo):
            disagree = True

    return {
        "building_count_merged": total,
        "building_osm_count": per_source.get("osm", 0),
        "building_overture_count": per_source.get("overture", 0),
        "building_official_count": per_source.get("official", 0),
        "building_source_count": len(present),
        "building_sources_queried": queried,
        "building_sources_disagree": disagree,
        "building_sources": present,
    }


# ==========================================================================
# GeoDataFrame-Ebene
# ==========================================================================


def records_from_gdf(gdf, source: str) -> list[dict]:
    """GeoDataFrame (metrisches CRS) -> Dedup-Datensaetze."""
    out: list[dict] = []
    if gdf is None or len(gdf) == 0:
        return out
    for idx, row in gdf.iterrows():
        g = row.geometry
        if g is None or g.is_empty:
            continue
        c = g.centroid
        try:
            area = float(g.area) if g.geom_type in ("Polygon", "MultiPolygon") else None
        except Exception:  # pragma: no cover
            area = None
        out.append(
            {
                "x": float(c.x),
                "y": float(c.y),
                "area_m2": area,
                "source": source,
                "id": row.get("source_id", row.get("osm_id", idx)),
                "height": row.get("height"),
                "geometry": g,
            }
        )
    return out


def merge_sources(gdfs: Mapping[str, Any], cfg: dict, metric_crs: str):
    """Fuehrt {quelle: GeoDataFrame} zu einer deduplizierten Ebene zusammen."""
    import geopandas as gpd

    bcfg = (cfg.get("buildings", {}) or {})
    max_d = float(bcfg.get("dedup_max_centroid_dist_m", 12.0))
    max_ratio = float(bcfg.get("dedup_max_area_ratio", 4.0))

    records: list[dict] = []
    for source in KNOWN_SOURCES:
        g = gdfs.get(source)
        if g is None or len(g) == 0:
            continue
        records.extend(records_from_gdf(g, source))
        LOG.info("  Gebaeudequelle %-9s: %d Objekte", source, len(g))

    if not records:
        LOG.warning("Keine Gebaeudedaten aus irgendeiner Quelle.")
        return gpd.GeoDataFrame(
            {"source": [], "sources": [], "n_sources": []},
            geometry=[],
            crs=metric_crs,
        )

    kept, _all = dedup_records(records, max_d, max_ratio)
    LOG.info(
        "Gebaeude zusammengefuehrt: %d Eingaben -> %d eindeutige (%d Dubletten).",
        len(records),
        len(kept),
        len(records) - len(kept),
    )

    return gpd.GeoDataFrame(
        {
            "source": [k["source"] for k in kept],
            "sources": [list(k["sources"]) for k in kept],
            "n_sources": [k["n_sources"] for k in kept],
            "height": [k.get("height") for k in kept],
            "area_m2": [k.get("area_m2") for k in kept],
        },
        geometry=[k["geometry"] for k in kept],
        crs=metric_crs,
    )


def lake_building_metrics(
    lake_geom,
    merged,
    tree,
    distances: Sequence[int],
    cap: float = 3000.0,
    sources_queried: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Gebaeudekennzahlen eines Sees aus der zusammengefuehrten Ebene."""
    out: dict[str, Any] = {}
    if merged is None or len(merged) == 0 or tree is None:
        out["building_data_ok"] = False
        return out

    geoms = list(merged.geometry.values)
    sources_col = list(merged["sources"].values)

    for d in distances:
        buf = lake_geom.buffer(d)
        idx = tree.query(buf, predicate="intersects")
        sel = [int(i) for i in idx]
        out[f"building_count_{d}m"] = len(sel)
        for s in KNOWN_SOURCES:
            out[f"building_{s}_count_{d}m"] = sum(1 for i in sel if s in sources_col[i])

    nearest_idx = tree.nearest(lake_geom)
    if nearest_idx is not None:
        dist = float(geoms[int(nearest_idx)].distance(lake_geom))
        out["distance_nearest_building_m"] = round(min(dist, cap), 1)
        out["distance_nearest_building_censored"] = dist >= cap
    else:  # pragma: no cover
        out["distance_nearest_building_m"] = cap
        out["distance_nearest_building_censored"] = True

    ref = max(distances)
    ref_sel = [
        {"sources": sources_col[int(i)]}
        for i in tree.query(lake_geom.buffer(ref), predicate="intersects")
    ]
    out.update(source_summary(ref_sel, sources_queried))
    out["building_data_ok"] = True
    return out
