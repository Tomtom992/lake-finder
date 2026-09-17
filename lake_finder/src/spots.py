"""
spots.py -- Orchestrierung der Spot-Analyse: vom See zur Zeltfläche.

Diese Stufe ist teuer (Höhenraster, Distanztransformation, Viewshed je
Kandidat) und läuft deshalb NUR für die besten Seen aus der Seenstufe --
so, wie es die Anforderung vorsieht:

    Ostdeutschland -> alle Seen grob -> Top-Seen -> hochaufgelöste Spots

Ablauf je See
-------------
1. Höhenausschnitt um den See laden (DGM1 lokal, WCS oder Copernicus).
2. Abstand zum Wasser als Raster (exakte Distanztransformation).
3. Suchkorridor 20-250 m, optional auf die erlaubte Gebietskulisse
   beschränkt (``allowed_area.geojson``).
4. Kandidatenflächen suchen, Zeltgrundriss in mehreren Drehungen prüfen.
5. Je Kandidat Sichtbarkeit und Privacy rechnen.
6. Teil-Scores und Gesamtscore je Kandidat.

Was diese Stufe NICHT tut: sie trifft keine Aussage darüber, ob man dort
zelten darf. Ohne ``allowed_area.geojson`` steht in jedem Kandidaten
``allowed_area_checked: false``.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np

from . import camp, terrain, viewshed
from .scoring import overall_spot_score, score_block

LOG = logging.getLogger("lake_finder.spots")


def _rasterize(geoms: Sequence[Any], shape, transform) -> np.ndarray:
    from rasterio.features import rasterize

    geoms = [g for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return np.zeros(shape, dtype=bool)
    return rasterize(
        [(g, 1) for g in geoms], out_shape=shape, transform=transform, fill=0, dtype="uint8"
    ).astype(bool)


def load_allowed_area(cfg: dict, metric_crs: str):
    """Erlaubte Gebietskulisse laden. Ohne Datei: None (keine Annahme)."""
    import geopandas as gpd

    path = (cfg.get("camp", {}) or {}).get("allowed_area_geojson")
    if not path:
        return None
    try:
        g = gpd.read_file(path)
        if g.crs is None:
            g = g.set_crs("EPSG:4326")
        g = g.to_crs(metric_crs)
        LOG.info("Erlaubte Gebietskulisse geladen: %d Flächen aus %s", len(g), path)
        return g
    except Exception as exc:
        LOG.warning("Gebietskulisse %s nicht lesbar: %s", path, exc)
        return None


def analyse_lake_spots(
    lake,
    dem_source,
    features,
    buildings_layer: dict,
    cfg: dict,
    metric_crs: str,
    allowed_gdf=None,
) -> list[dict[str, Any]]:
    """Kandidatenflächen für einen See. Leere Liste, wenn nichts taugt."""
    ccfg = cfg.get("camp", {}) or {}
    pad = float(ccfg.get("corridor_max_m", 250.0)) + float(
        (cfg.get("viewshed", {}) or {}).get("max_radius_m", 800.0)
    )
    minx, miny, maxx, maxy = lake.geometry.bounds
    bounds = (minx - pad, miny - pad, maxx + pad, maxy + pad)

    win = dem_source.read(bounds, metric_crs)
    if win is None or win.array is None or min(win.array.shape) < 8:
        LOG.info("  %s: kein Höhenraster verfügbar.", lake.get("lake_uid"))
        return []

    dem = win.array
    px = win.resolution_m
    shape = dem.shape

    # Wasser und Abstand dazu
    water = _rasterize([lake.geometry], shape, win.transform)
    if not water.any():
        LOG.debug("  %s: See liegt nicht im Höhenausschnitt.", lake.get("lake_uid"))
        return []
    from .elevation.common import euclidean_distance_m

    dist_water = euclidean_distance_m(water, px)
    water_level = float(np.nanmedian(dem[water])) if water.any() else None

    allowed_mask = None
    if allowed_gdf is not None and len(allowed_gdf):
        allowed_mask = _rasterize(list(allowed_gdf.geometry.values), shape, win.transform)

    cands = camp.find_camp_candidates(
        dem, px, dist_water, cfg,
        allowed_mask=allowed_mask, water_level_m=water_level,
    )
    if not cands:
        return []

    # Infrastrukturraster für Sichtbarkeit und Nähe
    feature_rasters: dict[str, np.ndarray] = {}
    if features is not None and len(features):
        for key, cat in (
            ("major_road", "major_road"),
            ("minor_road", "minor_road"),
            ("soft_way", "soft_way"),
            ("parking", "parking"),
            ("campsite", "campsite"),
        ):
            sel = features[features["categories"].apply(lambda c, k=cat: k in c)]
            feature_rasters[key] = _rasterize(list(sel.geometry.values), shape, win.transform)
    merged = buildings_layer.get("merged") if buildings_layer else None
    if merged is not None and len(merged):
        feature_rasters["buildings"] = _rasterize(list(merged.geometry.values), shape, win.transform)

    dsm = None  # DOM: sobald eine Oberflächenmodell-Quelle konfiguriert ist

    # Einmal je Raster statt einmal je Kandidat: die Distanztransformation
    # und die Flussakkumulation sind die teuersten Schritte der Stufe.
    from .elevation.common import euclidean_distance_m as _edm

    dist_rasters = {
        key: (_edm(raster, px) if raster is not None and raster.any() else None)
        for key, raster in feature_rasters.items()
    }
    dry = terrain.dryness_from_terrain(dem, px, None, cfg)

    # Fussweg: bis zum Ufer kommt der Wert aus der Zugangspunktanalyse,
    # vom Ufer zum Platz die Luftlinie mal Umwegfaktor. Beides sind
    # Schaetzungen, kein Routing auf dem Wegenetz -- deshalb die Spalte
    # walking_distance_source am See.
    walk_to_lake = lake.get("walking_distance_to_lake_m")
    detour = float((cfg.get("access", {}) or {}).get("walk_detour_factor", 1.35))

    out: list[dict[str, Any]] = []
    for i, c in enumerate(cands):
        rc = (c["row"], c["col"])
        rec = dict(c)
        rec["spot_id"] = f"{lake.get('lake_uid')}-{i + 1:02d}"
        rec["lake_uid"] = lake.get("lake_uid")
        rec["lake_name"] = lake.get("name", "")
        x, y = win.xy(c["row"], c["col"])
        rec["x"], rec["y"] = x, y
        try:
            from pyproj import Transformer

            lon, lat = Transformer.from_crs(
                win.crs or metric_crs, "EPSG:4326", always_xy=True
            ).transform(x, y)
            rec["lon"], rec["lat"] = round(lon, 6), round(lat, 6)
        except Exception:  # pragma: no cover
            rec["lon"] = rec["lat"] = None

        # Abstände zu Infrastruktur am Spot (aus den Rastern, in Metern)
        dists: dict[str, float | None] = {}
        for key, d in dist_rasters.items():
            if d is None:
                dists[key] = None
                continue
            dists[key] = round(float(d[rc]), 1)
            rec[f"distance_{key}_m"] = dists[key]
        rec.update(viewshed.proximity_flags(dists, cfg))

        try:
            rec.update(
                viewshed.privacy_metrics(
                    dem, dsm, px, rc, feature_rasters, cfg, water_mask=water
                )
            )
        except Exception as exc:  # pragma: no cover
            LOG.debug("Viewshed für %s fehlgeschlagen: %s", rec["spot_id"], exc)

        # Höhe über Wasser wirkt nur als Faktor auf den fertigen Rasterwert --
        # das spart die Neuberechnung der Hydrologie je Kandidat.
        h = rec.get("height_above_water_m")
        h_factor = 1.0
        if h is not None:
            h_term = float(np.clip(1.0 - (float(h) / 3.0), 0.0, 1.0))
            h_factor = 1.0 - 0.35 * h_term
        rec["dryness_score"] = round(float(dry["dryness_score"][rc]) * h_factor, 1)
        rec["wetness_risk"] = round(float(dry["wetness_risk"][rc]), 3)
        rec["wetness_index"] = round(float(dry["wetness_index"][rc]), 2)
        rec["flood_risk_proxy"] = rec["wetness_risk"]

        if walk_to_lake is not None and rec.get("distance_to_water_m") is not None:
            try:
                rec["walking_distance_to_spot_m"] = round(
                    float(walk_to_lake) + float(rec["distance_to_water_m"]) * detour, 1
                )
            except (TypeError, ValueError):  # pragma: no cover
                pass

        rec["terrain_ok"] = True
        rec["dem_resolution_m"] = px
        rec["dem_source"] = win.source
        rec["tent_grade_dem"] = bool(win.resolution_m <= 2.0)
        if not rec["tent_grade_dem"]:
            rec["dgm_zu_grob_fuer_zeltflaeche"] = True

        # Sicherheits-Flags, bewusst als Hinweise formuliert
        flags = []
        if rec.get("wet_depression"):
            flags.append("wet_depression")
        if (rec.get("max_slope_deg") or 0) > float(ccfg.get("max_slope_deg", 5.0)):
            flags.append("steep_ground")
        if (rec.get("roughness_cm") or 0) > 20:
            flags.append("rough_ground")
        if rec.get("close_to_water"):
            flags.append("close_to_water")
        if (rec.get("walking_distance_to_spot_m") or 0) > 5000:
            flags.append("very_long_walkout")
        rec["safety_flags"] = flags

        for block in ("campsite_suitability", "safety", "privacy_exposure"):
            rec[f"{block}_score"] = score_block(rec, cfg, block)["score"]
        out.append(rec)

    return out


def analyse_spots(
    lakes,
    features,
    buildings_layer: dict,
    cfg: dict,
    metric_crs: str,
    profile: str | None = None,
):
    """Spot-Analyse für die besten Seen. Liefert (DataFrame, Zusammenfassung)."""
    import pandas as pd

    from .elevation import get_source

    ccfg = cfg.get("camp", {}) or {}
    if not ccfg.get("enabled", False):
        return pd.DataFrame(), {}

    dem_source = get_source(cfg)
    if dem_source is None:
        LOG.warning(
            "Spot-Analyse aktiv, aber keine Höhenquelle konfiguriert "
            "(elevation.enabled / elevation.source)."
        )
        return pd.DataFrame(), {"reason": "keine Höhenquelle"}

    top_n = int(ccfg.get("top_lakes", 15))
    sort_col = "overall_spot_score" if "overall_spot_score" in lakes.columns else "wilderness_score"
    pool = lakes[lakes["passes_filters"]] if "passes_filters" in lakes.columns else lakes
    if pool.empty:
        pool = lakes
    pool = pool.sort_values(sort_col, ascending=False, na_position="last").head(top_n)

    allowed = load_allowed_area(cfg, metric_crs)
    if allowed is None:
        LOG.warning(
            "Keine allowed_area.geojson gesetzt. Kandidaten werden als GEEIGNET, "
            "aber NICHT als erlaubt ausgewiesen (allowed_area_checked = false)."
        )

    rows: list[dict] = []
    for i, (_, lake) in enumerate(pool.iterrows(), 1):
        LOG.info("  Spots %d/%d: %s", i, len(pool), lake.get("name") or lake.get("lake_uid"))
        try:
            rows.extend(
                analyse_lake_spots(
                    lake, dem_source, features, buildings_layer, cfg, metric_crs, allowed
                )
            )
        except Exception as exc:
            LOG.warning("  Spot-Analyse für %s fehlgeschlagen: %s", lake.get("lake_uid"), exc)

    if not rows:
        return pd.DataFrame(), {"lakes_checked": len(pool), "spots": 0}

    df = pd.DataFrame(rows)
    # Gesamtscore je Spot: Seenwerte + Spotwerte zusammen
    lake_scores = lakes.set_index("lake_uid")
    overall = []
    for _, r in df.iterrows():
        base = {}
        if r["lake_uid"] in lake_scores.index:
            lr = lake_scores.loc[r["lake_uid"]]
            for k in ("wilderness_score", "visual_seclusion_score", "access_score"):
                if k in lake_scores.columns:
                    base[k] = lr[k]
        base.update(
            {
                "campsite_suitability_score": r.get("campsite_suitability_score"),
                "safety_score": r.get("safety_score"),
                "privacy_exposure_score": r.get("privacy_exposure_score"),
            }
        )
        overall.append(overall_spot_score(base, cfg, profile))
    for key in ("overall_spot_score", "overall_coverage", "overall_profile"):
        df[key] = [o.get(key) for o in overall]

    df = df.sort_values("overall_spot_score", ascending=False, na_position="last").reset_index(drop=True)
    LOG.info("Spot-Analyse fertig: %d Kandidaten an %d Seen.", len(df), df["lake_uid"].nunique())
    return df, {"lakes_checked": len(pool), "spots": len(df)}
