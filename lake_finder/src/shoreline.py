"""
shoreline.py -- segmentierte Uferanalyse.

Warum
-----
``shore_tree_cover`` ist ein Flaechenmittel des gesamten Ufergürtels. Ein
See mit 95 % Wald und 5 % Dorf am Ufer bekommt damit fast denselben Wert
wie ein See mit 100 % Wald -- obwohl der erste genau dort, wo man steht,
auf ein Bootshaus schaut.

Deshalb wird die Uferlinie hier in Segmente von 20-25 m zerlegt und jedes
Segment einzeln bewertet. Anschliessend interessieren nicht nur Mittelwert
und Median, sondern vor allem das SCHLECHTESTE Zehntel (P10) und die
Laenge des laengsten zusammenhaengenden gestoerten Abschnitts.

Aufbau
------
* ``segment_shoreline``    -- Geometrie: Uferlinie -> Segmente (shapely)
* ``score_segments``       -- je Segment ein 0-100-Wert (nutzt scoring.py)
* ``aggregate_segments``   -- Mittel/Median/P10/Min, Anteile, laengste Laeufe

Die Aggregation ist bewusst frei von geopandas/shapely: sie arbeitet auf
Listen von (score, laenge) und ist damit offline testbar. Genau dort sitzt
die Logik, die man leicht falsch macht (Ringschluss beim laengsten Lauf,
laengengewichtete Perzentile).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

LOG = logging.getLogger("lake_finder.shoreline")


# ==========================================================================
# Reine Aggregationslogik -- ohne Geo-Abhaengigkeiten, voll testbar
# ==========================================================================


def weighted_percentile(values: Sequence[float], weights: Sequence[float], q: float) -> float | None:
    """Laengengewichtetes Perzentil (q in 0..100).

    Ufersegmente sind nicht exakt gleich lang (das letzte Stueck eines
    Rings ist kuerzer, und Inseln erzeugen eigene Ringe). Ein ungewichtetes
    Perzentil wuerde kurze Segmente ueberbewerten.
    """
    pairs = [
        (float(v), float(w))
        for v, w in zip(values, weights)
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and w and w > 0
    ]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    target = total * (q / 100.0)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= target:
            return v
    return pairs[-1][0]


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    pairs = [
        (float(v), float(w))
        for v, w in zip(values, weights)
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and w and w > 0
    ]
    if not pairs:
        return None
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None


def longest_run_length(mask: Sequence[bool], lengths: Sequence[float], circular: bool = True) -> float:
    """Laenge des laengsten zusammenhaengenden True-Abschnitts.

    ``circular=True`` schliesst den Ring: ein gestoerter Abschnitt, der
    ueber den willkuerlichen Startpunkt der Uferlinie hinweggeht, wird als
    ein Abschnitt gezaehlt und nicht in zwei zerlegt.
    """
    n = len(mask)
    if n == 0:
        return 0.0
    if all(mask):
        return float(sum(lengths))

    order = range(n)
    if circular:
        try:
            start = next(i for i in range(n) if not mask[i])
        except StopIteration:  # pragma: no cover - durch all(mask) abgedeckt
            return float(sum(lengths))
        order = [(start + k) % n for k in range(n)]

    best = cur = 0.0
    for i in order:
        if mask[i]:
            cur += float(lengths[i])
            best = max(best, cur)
        else:
            cur = 0.0
    return best


def aggregate_segments(
    scores: Sequence[float],
    lengths: Sequence[float],
    developed_threshold: float = 55.0,
    natural_threshold: float = 75.0,
    circular: bool = True,
) -> dict[str, Any]:
    """Kennzahlen des Ufers aus den Segmentwerten.

    ``developed`` heisst hier: das Segment erreicht den Schwellenwert nicht
    -- also Bebauung, Strasse, Steg, Acker oder offenes Gelaende direkt am
    Wasser. Es ist eine Bewertung, keine Klassifikation eines Katasters.
    """
    n = len(scores)
    if n == 0 or len(lengths) != n:
        return {
            "shore_segment_count": 0,
            "shore_length_m": 0.0,
            "shore_segment_mean_score": None,
            "shore_segment_median_score": None,
            "shore_segment_p10_score": None,
            "shore_segment_min_score": None,
            "developed_shore_fraction": None,
            "developed_shore_percent": None,
            "natural_shore_fraction": None,
            "natural_shore_percent": None,
            "longest_developed_section_m": None,
            "longest_natural_section_m": None,
            "worst_shore_segment_index": None,
        }

    total_len = float(sum(lengths))
    dev_mask = [(s is None) or (float(s) < developed_threshold) for s in scores]
    nat_mask = [(s is not None) and (float(s) >= natural_threshold) for s in scores]
    dev_len = sum(l for m, l in zip(dev_mask, lengths) if m)
    nat_len = sum(l for m, l in zip(nat_mask, lengths) if m)

    valid = [(i, float(s)) for i, s in enumerate(scores) if s is not None]
    worst_idx = min(valid, key=lambda t: t[1])[0] if valid else None

    dev_frac = dev_len / total_len if total_len else None
    nat_frac = nat_len / total_len if total_len else None

    return {
        "shore_segment_count": n,
        "shore_length_m": round(total_len, 1),
        "shore_segment_mean_score": _r(weighted_mean(scores, lengths)),
        "shore_segment_median_score": _r(weighted_percentile(scores, lengths, 50)),
        "shore_segment_p10_score": _r(weighted_percentile(scores, lengths, 10)),
        "shore_segment_min_score": _r(min((s for s in scores if s is not None), default=None)),
        "developed_shore_fraction": _r(dev_frac, 4),
        "developed_shore_percent": _r(None if dev_frac is None else dev_frac * 100.0),
        "natural_shore_fraction": _r(nat_frac, 4),
        "natural_shore_percent": _r(None if nat_frac is None else nat_frac * 100.0),
        "longest_developed_section_m": round(longest_run_length(dev_mask, lengths, circular), 1),
        "longest_natural_section_m": round(longest_run_length(nat_mask, lengths, circular), 1),
        "worst_shore_segment_index": worst_idx,
    }


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(float(v), nd)


# ==========================================================================
# Geometrie und Bewertung -- braucht shapely/geopandas
# ==========================================================================


def segment_shoreline(geom: Any, spacing_m: float = 25.0, min_segments: int = 8) -> list[dict]:
    """Zerlegt die AEUSSERE Uferlinie in Segmente von ~``spacing_m``.

    Rueckgabe je Segment: Mittelpunkt, Laenge, Aussennormale, Ringindex.
    Loecher (Inseln) werden bewusst ignoriert -- ein Ufer, an dem man
    stehen kann, ist die Aussengrenze des Wasserkoerpers.
    """
    from shapely.geometry import LineString, Point

    rings: list[Any] = []
    if geom.geom_type == "Polygon":
        rings = [geom.exterior]
    elif geom.geom_type == "MultiPolygon":
        rings = [g.exterior for g in geom.geoms]
    else:  # pragma: no cover
        return []

    segments: list[dict] = []
    for ring_i, ring in enumerate(rings):
        line = LineString(ring.coords)
        total = line.length
        if total <= 0:
            continue
        n = max(min_segments, int(round(total / max(spacing_m, 1.0))))
        step = total / n
        for k in range(n):
            d0 = k * step
            d1 = min((k + 1) * step, total)
            mid = line.interpolate((d0 + d1) / 2.0)
            p0 = line.interpolate(d0)
            p1 = line.interpolate(d1)
            dx, dy = p1.x - p0.x, p1.y - p0.y
            norm = math.hypot(dx, dy) or 1.0
            # Normale nach aussen: Ringe sind durch orient(sign=1) gegen den
            # Uhrzeigersinn orientiert, damit zeigt (dy, -dx) nach aussen.
            nx, ny = dy / norm, -dx / norm
            segments.append(
                {
                    "ring": ring_i,
                    "index": len(segments),
                    "x": mid.x,
                    "y": mid.y,
                    "length_m": d1 - d0,
                    "nx": nx,
                    "ny": ny,
                    "point": Point(mid.x, mid.y),
                }
            )
    return segments


def _segment_scoring_cfg(cfg: dict) -> dict:
    """Default-Bewertung eines Ufersegments, sofern die config nichts sagt."""
    sh = cfg.get("shore", {}) or {}
    if sh.get("segment_score"):
        return sh["segment_score"]
    return {
        "components": {
            "tree": {"label": "Wald am Segment", "metric": "seg_tree_cover",
                     "weight": 3.0, "ramp": [30, 95], "missing": 0.2},
            "natural": {"label": "naturnah", "metric": "seg_natural_land",
                        "weight": 1.5, "ramp": [40, 99], "missing": 0.2},
            "d_build": {"label": "Abstand Gebäude", "metric": "seg_distance_building_m",
                        "weight": 3.0, "ramp": [30, 600], "missing": 0.2},
            "d_major": {"label": "Abstand Hauptstraße", "metric": "seg_distance_major_road_m",
                        "weight": 1.5, "ramp": [50, 800], "missing": 0.2},
            "d_minor": {"label": "Abstand Nebenstraße", "metric": "seg_distance_minor_road_m",
                        "weight": 1.0, "ramp": [30, 400], "missing": 0.3},
            "d_soft": {"label": "Abstand Waldweg", "metric": "seg_distance_soft_way_m",
                       "weight": 0.4, "ramp": [5, 150], "missing": 0.5},
        },
        "penalties": {
            "built": {"label": "Siedlungsfläche am Segment", "metric": "seg_built_up",
                      "threshold": 0.0, "points_per_unit": 2.0, "max_points": 40},
            "crop": {"label": "Acker am Segment", "metric": "seg_cropland",
                     "threshold": 20.0, "points_per_unit": 0.4, "max_points": 15},
            "human": {"label": "Menschliche Objekte im Umkreis", "metric": "seg_human_feature_count",
                      "points_per_unit": 6.0, "max_points": 30},
            "marina": {"label": "Steg / Marina nah", "flag": "seg_marina_near", "points": 25},
            "campsite": {"label": "Campingplatz nah", "flag": "seg_campsite_near", "points": 25},
        },
    }


def score_segments(seg_records: Sequence[Mapping[str, Any]], cfg: dict) -> list[float]:
    """Bewertet jedes Segment mit der normalen Score-Engine (0-100)."""
    from .scoring import score_row

    scfg = {"scoring": _segment_scoring_cfg(cfg)}
    return [float(score_row(r, scfg)["wilderness_score"]) for r in seg_records]


def analyse_lake_shoreline(
    lake_geom: Any,
    feature_index: Any,
    landcover_sampler: Any,
    cfg: dict,
) -> tuple[dict[str, Any], list[dict]]:
    """Komplette Uferanalyse eines Sees.

    ``feature_index``      -- osm_features.FeatureIndex (metrisches CRS)
    ``landcover_sampler``  -- Objekt mit ``sample(x, y, radius_m) -> dict``
                              oder None, wenn kein Raster verfuegbar ist.

    Rueckgabe: (Kennzahlen je See, Segmentliste mit Einzelwerten)
    """
    sh = cfg.get("shore", {}) or {}
    spacing = float(sh.get("segment_spacing_m", 25.0))
    radius = float(sh.get("primary_radius_m", 100.0))
    near_radius = float(sh.get("near_radius_m", 150.0))
    dev_thr = float(sh.get("developed_threshold_score", 55.0))
    nat_thr = float(sh.get("natural_threshold_score", 75.0))

    segments = segment_shoreline(lake_geom, spacing)
    if not segments:
        return aggregate_segments([], []), []

    records: list[dict] = []
    for seg in segments:
        pt = seg["point"]
        rec: dict[str, Any] = {
            "index": seg["index"],
            "ring": seg["ring"],
            "x": seg["x"],
            "y": seg["y"],
            "length_m": seg["length_m"],
        }
        if feature_index is not None:
            for cat, key in (
                ("building", "seg_distance_building_m"),
                ("major_road", "seg_distance_major_road_m"),
                ("minor_road", "seg_distance_minor_road_m"),
                ("soft_way", "seg_distance_soft_way_m"),
                ("parking", "seg_distance_parking_m"),
                ("campsite", "seg_distance_campsite_m"),
                ("marina", "seg_distance_marina_m"),
                ("residential_area", "seg_distance_residential_m"),
            ):
                d, censored = feature_index.nearest_distance(cat, pt, cap=1500.0)
                rec[key] = round(d, 1)
                rec[key.replace("_m", "") + "_censored"] = censored
            disc = pt.buffer(near_radius)
            rec["seg_human_feature_count"] = sum(
                feature_index.count_within(c, disc)
                for c in ("building", "man_made", "parking", "marina", "campsite", "accommodation")
            )
            rec["seg_marina_near"] = rec.get("seg_distance_marina_m", 9e9) < near_radius
            rec["seg_campsite_near"] = rec.get("seg_distance_campsite_m", 9e9) < near_radius

        if landcover_sampler is not None:
            try:
                lc = landcover_sampler.sample(seg["x"], seg["y"], radius)
                rec["seg_tree_cover"] = lc.get("tree_cover_percent")
                rec["seg_natural_land"] = lc.get("natural_land_percent")
                rec["seg_built_up"] = lc.get("built_up_percent")
                rec["seg_cropland"] = lc.get("cropland_percent")
                rec["seg_valid_pixels"] = lc.get("valid_pixels")
            except Exception as exc:  # pragma: no cover
                LOG.debug("Land-Cover-Sampling fuer Segment %s fehlgeschlagen: %s", seg["index"], exc)
        records.append(rec)

    scores = score_segments(records, cfg)
    for rec, sc in zip(records, scores):
        rec["segment_score"] = round(sc, 1)
        rec["developed"] = sc < dev_thr

    lengths = [r["length_m"] for r in records]
    agg = aggregate_segments(scores, lengths, dev_thr, nat_thr, circular=True)

    wi = agg.get("worst_shore_segment_index")
    if wi is not None and 0 <= wi < len(records):
        w = records[wi]
        agg["worst_shore_segment"] = {
            "index": wi,
            "score": w.get("segment_score"),
            "x": w.get("x"),
            "y": w.get("y"),
            "distance_building_m": w.get("seg_distance_building_m"),
            "distance_minor_road_m": w.get("seg_distance_minor_road_m"),
            "tree_cover": w.get("seg_tree_cover"),
        }
    agg["shore_segments_ok"] = True
    agg["shore_segment_spacing_m"] = spacing
    return agg, records
