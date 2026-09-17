"""
access.py -- echte Zugangspunkte statt Routing auf den See-Mittelpunkt.

Das Problem mit dem alten Verfahren
-----------------------------------
Die Fahrzeit wurde zum Repraesentativpunkt des Seepolygons gerechnet. Der
liegt im Wasser. Ein Router setzt so einen Punkt auf die naechstgelegene
Strasse -- manchmal auf die richtige Seite des Sees, manchmal auf die
falsche, manchmal auf eine Privatzufahrt. Ergebnis: eine Zahl, die
plausibel aussieht und nichts misst.

Was hier stattdessen passiert
-----------------------------
1. Strassen im Umkreis des Sees einsammeln (aus den bereits geholten
   OSM-Features, kein zusaetzlicher Abruf).
2. Je Strasse den zum Ufer naechstgelegenen Punkt als Kandidaten nehmen;
   zusaetzlich entlang langer Strassen in festen Abstaenden abtasten.
3. Kandidaten bewerten: befahrbar? oeffentlich? wie weit zu Fuss ans Ufer?
4. Mehrere gute Kandidaten routen lassen und den besten behalten.

Ehrlichkeitsgrenze
------------------
Die Fusswegdistanz ist standardmaessig eine Luftlinienschaetzung mit
Umwegfaktor, KEIN Routing auf dem Wegenetz -- der oeffentliche
OSRM-Demoserver bietet nur das Auto-Profil. Die Spalte
``walking_distance_source`` sagt, was es war. Mit einem lokalen
OSRM/Valhalla mit Fussprofil (``access.foot_router_url``) wird daraus
echtes Routing.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

LOG = logging.getLogger("lake_finder.access")

# Wie gut ist eine Strassenkategorie zum Anfahren und Parken?
DRIVABLE = {"major_road": 1.0, "minor_road": 1.0, "soft_way": 0.45}


# ==========================================================================
# Reine Auswahl-Logik -- offline testbar
# ==========================================================================


def candidate_score(cand: Mapping[str, Any], cfg: dict) -> float:
    """Bewertet einen Zugangspunkt (0-100). Hoeher ist besser.

    Bewusst NICHT nur "kurzester Fussweg": ein Punkt 40 m vom Ufer auf
    einer Privatzufahrt ist schlechter als 700 m auf einem oeffentlichen
    Waldparkplatz.
    """
    acfg = cfg.get("access", {}) or {}
    max_walk = float(acfg.get("max_walk_m", 2500.0))
    w_walk = float(acfg.get("weight_walk", 3.0))
    w_class = float(acfg.get("weight_road_class", 1.5))
    private_penalty = float(acfg.get("private_penalty", 45.0))
    track_penalty = float(acfg.get("track_penalty", 8.0))

    walk = float(cand.get("walk_m", max_walk))
    walk_score = max(0.0, 1.0 - walk / max(max_walk, 1.0))
    cls = float(DRIVABLE.get(str(cand.get("road_class", "minor_road")), 0.5))

    score = 100.0 * (w_walk * walk_score + w_class * cls) / max(w_walk + w_class, 1e-9)
    if cand.get("private"):
        score -= private_penalty
    if cand.get("road_class") == "soft_way":
        score -= track_penalty
    if walk > max_walk:
        score -= 25.0
    return max(0.0, min(100.0, score))


def select_best_access(
    candidates: Sequence[Mapping[str, Any]], cfg: dict
) -> tuple[dict | None, list[dict]]:
    """Bewertet alle Kandidaten und liefert (bester, sortierte Liste).

    Private Zufahrten werden nicht geloescht, sondern nur abgewertet --
    wenn es nichts anderes gibt, soll der Nutzer das sehen und selbst
    entscheiden (Flag ``access_via_private_road``).
    """
    scored = []
    for c in candidates:
        d = dict(c)
        d["candidate_score"] = round(candidate_score(c, cfg), 1)
        scored.append(d)
    scored.sort(key=lambda c: -c["candidate_score"])
    return (scored[0] if scored else None), scored


def walk_estimate(straight_m: float, cfg: dict) -> float:
    """Luftlinie -> geschaetzte Gehstrecke. Im Wald eher optimistisch."""
    f = float((cfg.get("access", {}) or {}).get("walk_detour_factor", 1.35))
    return round(float(straight_m) * f, 1)


# ==========================================================================
# Geometrie -- braucht shapely
# ==========================================================================


def _is_private(row: Mapping[str, Any]) -> bool:
    vals = {
        str(row.get("access", "") or "").lower(),
        str(row.get("motor_vehicle", "") or "").lower(),
        str(row.get("vehicle", "") or "").lower(),
    }
    return bool(vals & {"private", "no", "permit", "customers", "forestry", "agricultural"})


def _road_class(cats: Any) -> str | None:
    for c in ("major_road", "minor_road", "soft_way"):
        if c in cats:
            return c
    return None


def find_access_candidates(lake_geom, features, cfg: dict) -> list[dict]:
    """Zugangspunkt-Kandidaten aus dem Strassennetz um den See."""
    from shapely.ops import nearest_points

    acfg = cfg.get("access", {}) or {}
    search_r = float(acfg.get("search_radius_m", 2500.0))
    sample_step = float(acfg.get("sample_step_m", 250.0))
    max_per_road = int(acfg.get("max_samples_per_road", 6))
    allow_tracks = bool(acfg.get("allow_tracks", True))

    if features is None or len(features) == 0:
        return []

    area = lake_geom.buffer(search_r)
    cands: list[dict] = []

    sub = features[features["categories"].apply(lambda c: bool(c & set(DRIVABLE)))]
    for _, row in sub.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty or not geom.intersects(area):
            continue
        cls = _road_class(row["categories"])
        if cls is None or (cls == "soft_way" and not allow_tracks):
            continue
        private = _is_private(row)

        try:
            p_road, _p_lake = nearest_points(geom, lake_geom)
        except Exception:  # pragma: no cover
            continue
        points = [p_road]

        # Lange Strassen zusaetzlich abtasten: der naechste Punkt ist nicht
        # immer der beste (Steilufer, Schilfguertel, Privatgrundstueck).
        try:
            if geom.geom_type in ("LineString", "MultiLineString") and geom.length > sample_step:
                n = min(max_per_road, int(geom.length // sample_step))
                for k in range(1, n + 1):
                    points.append(geom.interpolate(k * geom.length / (n + 1)))
        except Exception:  # pragma: no cover
            pass

        for p in points:
            straight = float(p.distance(lake_geom))
            if straight > search_r:
                continue
            cands.append(
                {
                    "x": float(p.x),
                    "y": float(p.y),
                    "road_class": cls,
                    "private": private,
                    "road_name": row.get("name", ""),
                    "highway": row.get("highway", ""),
                    "straight_to_shore_m": round(straight, 1),
                    "walk_m": walk_estimate(straight, cfg),
                }
            )

    # Nahe beieinanderliegende Kandidaten ausduennen
    grid_cell = float(acfg.get("dedup_cell_m", 120.0))
    seen: set[tuple[int, int]] = set()
    thinned = []
    for c in sorted(cands, key=lambda c: c["walk_m"]):
        key = (int(c["x"] // grid_cell), int(c["y"] // grid_cell))
        if key in seen:
            continue
        seen.add(key)
        thinned.append(c)
    return thinned


def compute_access(lakes, features, cfg: dict, metric_crs: str):
    """Zugangspunkte und Fahrzeiten je See."""
    import geopandas as gpd
    import pandas as pd
    from pyproj import Transformer

    from .travel import osrm_table, straight_line_estimate
    from .utils import JsonCache

    acfg = cfg.get("access", {}) or {}
    tcfg = cfg.get("travel", {}) or {}
    if not acfg.get("enabled", True):
        return pd.DataFrame({"lake_uid": lakes["lake_uid"]})

    to_wgs = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)
    top_k = int(acfg.get("route_top_k", 3))
    origin = tcfg.get("origin")
    origin = (float(origin[0]), float(origin[1])) if origin else None
    mode = str(tcfg.get("mode", "haversine")).lower()

    rows: list[dict] = []
    route_targets: list[tuple[float, float]] = []
    route_map: list[tuple[int, int]] = []  # (Zeilenindex, Kandidatenindex)

    n = len(lakes)
    for i, (_, lake) in enumerate(lakes.iterrows(), 1):
        if i % 25 == 0 or i == n:
            LOG.info("  Zugangspunkte: %d/%d Seen", i, n)
        cands = find_access_candidates(lake.geometry, features, cfg)
        best, scored = select_best_access(cands, cfg)
        rec: dict[str, Any] = {"lake_uid": lake["lake_uid"]}
        if not best:
            rec.update(
                {
                    "access_point_missing": True,
                    "access_via_private_road": None,
                    "walking_distance_to_lake_m": None,
                    "walking_distance_source": None,
                    "access_candidate_count": 0,
                }
            )
            rows.append(rec)
            continue

        keep = scored[:top_k]
        row_i = len(rows)
        for ci, c in enumerate(keep):
            lon, lat = to_wgs.transform(c["x"], c["y"])
            c["lon"], c["lat"] = lon, lat
            route_targets.append((lon, lat))
            route_map.append((row_i, ci))

        rec.update(
            {
                "access_point_missing": False,
                "access_via_private_road": bool(best.get("private")),
                "access_road_class": best.get("road_class"),
                "access_road_name": best.get("road_name", ""),
                "walking_distance_to_lake_m": best.get("walk_m"),
                "walking_distance_source": "luftlinie_x_umwegfaktor",
                "access_candidate_count": len(cands),
                "_candidates": keep,
            }
        )
        rows.append(rec)

    df = pd.DataFrame(rows)
    if not route_targets or origin is None:
        return _finalise_access(df, cfg, to_wgs)

    # Fahrzeit fuer alle Kandidaten aller Seen in wenigen Requests
    est_min, est_km = straight_line_estimate(
        origin,
        route_targets,
        float(tcfg.get("detour_factor", 1.3)),
        float(tcfg.get("avg_speed_kmh", 75.0)),
    )
    src = ["schaetzung"] * len(route_targets)
    if mode == "osrm":
        cache = JsonCache(tcfg.get("cache_dir", "data/cache/osrm"), True, 90)
        r_min, r_km = osrm_table(
            origin,
            route_targets,
            base_url=str(tcfg.get("osrm_url", "https://router.project-osrm.org")),
            chunk=int(tcfg.get("osrm_chunk", 80)),
            min_interval_s=float(tcfg.get("osrm_min_interval_s", 1.2)),
            cache=cache,
        )
        for k, v in enumerate(r_min):
            if v is not None:
                est_min[k] = v
                if r_km[k] is not None:
                    est_km[k] = r_km[k]
                src[k] = "osrm"

    for k, (row_i, ci) in enumerate(route_map):
        cands = df.at[row_i, "_candidates"]
        cands[ci]["drive_time_min"] = est_min[k]
        cands[ci]["drive_distance_km"] = est_km[k]
        cands[ci]["drive_time_source"] = src[k]

    return _finalise_access(df, cfg, to_wgs)


def _finalise_access(df, cfg: dict, to_wgs):
    """Waehlt je See den Kandidaten mit der besten Kombination aus."""
    acfg = cfg.get("access", {}) or {}
    w_time = float(acfg.get("weight_drive_time", 1.0))

    out_rows = []
    for _, r in df.iterrows():
        rec = {k: v for k, v in r.items() if k != "_candidates"}
        cands = r.get("_candidates")
        if isinstance(cands, list) and cands:
            def total(c):
                t = c.get("drive_time_min")
                t_pen = 0.0 if t is None else min(60.0, float(t) / 4.0) * w_time
                return c.get("candidate_score", 0.0) - t_pen

            best = max(cands, key=total)
            rec.update(
                {
                    "drive_access_lat": round(best.get("lat", 0.0), 6),
                    "drive_access_lon": round(best.get("lon", 0.0), 6),
                    "drive_time_min": best.get("drive_time_min"),
                    "drive_distance_km": best.get("drive_distance_km"),
                    "drive_time_source": best.get("drive_time_source"),
                    "walking_distance_to_lake_m": best.get("walk_m"),
                    "access_road_class": best.get("road_class"),
                    "access_road_name": best.get("road_name", ""),
                    "access_via_private_road": bool(best.get("private")),
                    "access_candidates": [
                        {
                            "lat": c.get("lat"),
                            "lon": c.get("lon"),
                            "walk_m": c.get("walk_m"),
                            "road_class": c.get("road_class"),
                            "private": c.get("private"),
                            "drive_time_min": c.get("drive_time_min"),
                            "score": c.get("candidate_score"),
                        }
                        for c in cands
                    ],
                    "travel_ok": best.get("drive_time_min") is not None,
                }
            )
        out_rows.append(rec)

    import pandas as pd

    return pd.DataFrame(out_rows)
