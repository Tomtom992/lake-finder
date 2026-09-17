"""
map.py -- Ausgabe: CSV, GeoJSON, Folium-Karte und mobile Weboberflaeche.

Zwei Kartenprodukte:

* ``build_folium_map``  -- klassische Folium/Leaflet-Karte mit Layer-Control,
  Popups und optionalen Pufferringen. Gut fuer den Desktop-Blick.
* ``build_app``         -- eigenstaendige, mobile-first HTML-Oberflaeche
  (eine Datei, Daten eingebettet): Vollbildkarte, Bottom-Sheet mit
  Ergebnisliste, Filter-Schieberegler, Score-Aufschluesselung,
  Routen-/Satelliten-Links. Laeuft ohne Server, also auch offline vom
  Handy-Dateisystem aus.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

if False:  # nur fuer Typpruefer -- geopandas wird erst in den Funktionen geladen
    import geopandas as gpd

LOG = logging.getLogger("lake_finder.map")

WGS84 = "EPSG:4326"

# Spalten fuer results.csv (Reihenfolge = Ausgabereihenfolge)
CSV_COLUMNS = [
    "name",
    "osm_type",
    "osm_id",
    "latitude",
    "longitude",
    "area_ha",
    "perimeter_m",
    "shore_complexity",
    "wilderness_score",
    "visual_seclusion_score",
    "access_score",
    "privacy_exposure_score",
    "campsite_suitability_score",
    "safety_score",
    "data_confidence_score",
    "overall_spot_score",
    "overall_coverage",
    "score_base",
    "score_penalties",
    "passes_filters",
    "filter_status",
    "confidence",
    "tree_cover_100m",
    "tree_cover_250m",
    "tree_cover_500m",
    "tree_cover_1000m",
    "shore_tree_cover",
    "natural_land_percent",
    "built_up_percent",
    "cropland_percent",
    "grassland_percent",
    "shrubland_percent",
    "building_count_100m",
    "building_count_250m",
    "building_count_500m",
    "building_count_1000m",
    "building_osm_count",
    "building_overture_count",
    "building_official_count",
    "building_source_count",
    "building_sources_disagree",
    "shore_segment_count",
    "shore_length_m",
    "shore_segment_mean_score",
    "shore_segment_median_score",
    "shore_segment_p10_score",
    "shore_segment_min_score",
    "developed_shore_percent",
    "natural_shore_percent",
    "longest_developed_section_m",
    "longest_natural_section_m",
    "distance_nearest_building_m",
    "distance_major_road_m",
    "distance_minor_road_m",
    "distance_any_road_m",
    "distance_soft_way_m",
    "distance_railway_m",
    "distance_residential_m",
    "distance_campsite_m",
    "distance_marina_m",
    "distance_parking_m",
    "has_campsite",
    "has_marina",
    "has_accommodation",
    "has_residential",
    "has_parking",
    "has_railway",
    "air_distance_km",
    "drive_time_min",
    "drive_distance_km",
    "drive_time_source",
    "drive_access_lat",
    "drive_access_lon",
    "walking_distance_to_lake_m",
    "walking_distance_source",
    "access_road_class",
    "access_road_name",
    "access_via_private_road",
    "access_point_missing",
    "quality_flags",
    "filter_unknown_reasons",
    "filter_reasons",
    "landcover_source",
]

# Properties, die in die Karte eingebettet werden
APP_PROPS = [
    "name",
    "osm_type",
    "osm_id",
    "latitude",
    "longitude",
    "area_ha",
    "wilderness_score",
    "visual_seclusion_score",
    "access_score",
    "privacy_exposure_score",
    "campsite_suitability_score",
    "safety_score",
    "data_confidence_score",
    "overall_spot_score",
    "overall_coverage",
    "score_base",
    "score_penalties",
    "score_breakdown",
    "passes_filters",
    "filter_status",
    "filter_reasons",
    "filter_unknown_reasons",
    "quality_flags",
    "confidence",
    "shore_tree_cover",
    "tree_cover_100m",
    "tree_cover_500m",
    "tree_cover_1000m",
    "natural_land_percent",
    "built_up_percent",
    "cropland_percent",
    "building_count_100m",
    "building_count_500m",
    "building_count_1000m",
    "building_osm_count",
    "building_overture_count",
    "building_official_count",
    "building_source_count",
    "building_sources_disagree",
    "shore_segment_count",
    "shore_length_m",
    "shore_segment_mean_score",
    "shore_segment_median_score",
    "shore_segment_p10_score",
    "shore_segment_min_score",
    "developed_shore_percent",
    "natural_shore_percent",
    "longest_developed_section_m",
    "longest_natural_section_m",
    "distance_nearest_building_m",
    "distance_major_road_m",
    "distance_residential_m",
    "distance_campsite_m",
    "distance_marina_m",
    "has_campsite",
    "has_marina",
    "has_accommodation",
    "has_residential",
    "drive_time_min",
    "drive_distance_km",
    "drive_time_source",
    "air_distance_km",
]


def _jsonable(v: Any) -> Any:
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if np.isnan(f) else round(f, 4)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


# --------------------------------------------------------------------------
# Tabellen-/GeoJSON-Ausgabe
# --------------------------------------------------------------------------


def write_tables(gdf: "gpd.GeoDataFrame", cfg: dict) -> dict[str, Path]:
    """Schreibt results.csv und results.geojson."""
    import geopandas as gpd

    ocfg = cfg.get("output", {})
    outdir = Path(ocfg.get("dir", "output"))
    outdir.mkdir(parents=True, exist_ok=True)

    df = gdf.copy()
    if not ocfg.get("keep_filtered", True):
        df = df[df["passes_filters"]].copy()

    # CSV
    csv_path = outdir / ocfg.get("csv", "results.csv")
    cols = [c for c in CSV_COLUMNS if c in df.columns]
    extra = [
        c
        for c in df.columns
        if c not in cols
        and c not in ("geometry", "tags", "score_breakdown", "score_missing_metrics")
        and not c.endswith("_censored")
    ]
    tab = pd.DataFrame(df[cols + extra]).copy()
    # Verschachtelte Werte (Listen von Gruenden, Dicts mit Aufschluesselungen)
    # gehoeren nicht in eine CSV-Zelle: Listen werden zu "a; b", alles andere
    # Verschachtelte zu kompaktem JSON.
    for c in tab.columns:
        if tab[c].map(lambda v: isinstance(v, (list, tuple, set))).any():
            tab[c] = tab[c].apply(
                lambda v: "; ".join(str(x) for x in v) if isinstance(v, (list, tuple, set)) else v
            )
        elif tab[c].map(lambda v: isinstance(v, dict)).any():
            tab[c] = tab[c].apply(
                lambda v: json.dumps(_jsonable(v), ensure_ascii=False) if isinstance(v, dict) else v
            )
    tab.to_csv(csv_path, index=False, encoding="utf-8")
    LOG.info("results.csv geschrieben: %s (%d Zeilen, %d Spalten)", csv_path, len(tab), tab.shape[1])

    # GeoJSON (volle Geometrie, WGS84)
    gj_path = outdir / ocfg.get("geojson", "results.geojson")
    g = df.to_crs(WGS84).copy()
    # Verschachtelte Strukturen vertragen die GeoJSON-Treiber nicht: die
    # Score-Aufschluesselung wird als JSON-String mitgegeben, Listen werden
    # zu Text. Die volle Struktur steckt in der Weboberflaeche (app.html).
    if "score_breakdown" in g.columns:
        g["score_breakdown"] = g["score_breakdown"].apply(
            lambda v: json.dumps(_jsonable(v), ensure_ascii=False) if isinstance(v, list) else ""
        )
    for c in ("tags",):
        if c in g.columns:
            g = g.drop(columns=[c])
    # GeoJSON-Treiber koennen nur flache Attributwerte. Listen -> Text,
    # Dicts und Listen von Dicts -> JSON-String. So geht nichts verloren,
    # und die Datei bleibt mit jedem GIS lesbar.
    def _flatten(v):
        if isinstance(v, dict):
            return json.dumps(_jsonable(v), ensure_ascii=False)
        if isinstance(v, (list, tuple, set)):
            items = list(v)
            if items and isinstance(items[0], dict):
                return json.dumps(_jsonable(items), ensure_ascii=False)
            return "; ".join(str(x) for x in items)
        return _jsonable(v)

    for c in g.columns:
        if c == "geometry":
            continue
        g[c] = g[c].apply(_flatten)
    g.to_file(gj_path, driver="GeoJSON")
    LOG.info("results.geojson geschrieben: %s", gj_path)

    paths = {"csv": csv_path, "geojson": gj_path}

    # Pufferringe als eigene Datei
    if ocfg.get("write_buffers", False):
        dists = [int(d) for d in ocfg.get("buffer_layers_m", [100, 500, 1000])]
        recs = []
        for _, r in df.iterrows():
            for d in dists:
                try:
                    ring = r.geometry.buffer(d).difference(r.geometry)
                except Exception:
                    continue
                recs.append({"lake_uid": r["lake_uid"], "buffer_m": d, "geometry": ring})
        if recs:
            bg = gpd.GeoDataFrame(recs, geometry="geometry", crs=df.crs).to_crs(WGS84)
            bpath = outdir / "buffers.geojson"
            bg.to_file(bpath, driver="GeoJSON")
            paths["buffers"] = bpath
            LOG.info("buffers.geojson geschrieben: %s", bpath)

    return paths


# --------------------------------------------------------------------------
# GeoJSON fuer die eingebettete Karte
# --------------------------------------------------------------------------


def build_app_geojson(
    gdf: "gpd.GeoDataFrame", cfg: dict, full_props: bool = False
) -> dict:
    """Kompaktes FeatureCollection-Dict fuer die Weboberflaeche.

    ``full_props=True`` legt ALLE skalaren Spalten hinein statt nur der
    Anzeige-Properties. Das braucht der precomputed-Modus: dort wird aus
    genau dieser Datei spaeter neu bewertet, ohne die Rohdaten erneut zu
    holen -- dafuer muessen alle Metriken drinstehen, die die config
    referenzieren kann.
    """
    import geopandas as gpd

    ocfg = cfg.get("output", {})
    limit = int(ocfg.get("max_features_in_app", 400))
    simplify_m = float(ocfg.get("app_simplify_m", 8.0))
    with_buffers = bool(ocfg.get("write_buffers", True))
    buf_d = [int(d) for d in ocfg.get("buffer_layers_m", [100, 500, 1000])]

    df = gdf.sort_values("wilderness_score", ascending=False).head(limit).copy()
    metric = df.crs

    from shapely.geometry import mapping

    # Alle Geometrien in einem Rutsch umprojizieren statt je See einzeln.
    shapes = gpd.GeoSeries(
        [g.simplify(simplify_m, preserve_topology=True) for g in df.geometry], crs=metric
    ).to_crs(WGS84)

    ring_series: dict[int, list] = {}
    if with_buffers:
        for d in buf_d:
            rings = []
            for g in df.geometry:
                try:
                    rings.append(
                        g.buffer(d).difference(g).simplify(
                            max(simplify_m, d / 12.0), preserve_topology=True
                        )
                    )
                except Exception:  # pragma: no cover
                    rings.append(None)
            ring_series[d] = list(
                gpd.GeoSeries([r if r is not None else df.geometry.iloc[0] for r in rings],
                              crs=metric).to_crs(WGS84)
            )

    skip = {"geometry", "tags", "filter_checks", "access_candidates",
            "score_breakdowns", "sources_available", "worst_shore_segment"}
    prop_cols = (
        [c for c in df.columns if c not in skip] if full_props
        else [c for c in APP_PROPS if c in df.columns]
    )

    feats = []
    for i, (_, r) in enumerate(df.iterrows()):
        props = {k: _jsonable(r[k]) for k in prop_cols}
        if with_buffers:
            props["buffers"] = {
                str(d): mapping(ring_series[d][i])
                for d in buf_d
                if ring_series.get(d) and ring_series[d][i] is not None
            }
        feats.append(
            {
                "type": "Feature",
                "geometry": mapping(shapes.iloc[i]),
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": feats}


# --------------------------------------------------------------------------
# Mobile Oberflaeche
# --------------------------------------------------------------------------


SPOT_PROPS = [
    "spot_id", "lake_uid", "lake_name", "lat", "lon",
    "overall_spot_score", "campsite_suitability_score", "safety_score",
    "privacy_exposure_score", "distance_to_water_m", "flat_area_m2",
    "mean_slope_deg", "max_slope_deg", "relief_cm", "dryness_score",
    "tent_fit", "best_tent_orientation_deg", "tent_size_m",
    "dem_resolution_m", "dem_source", "allowed_area_checked",
    "safety_flags", "height_above_water_m", "lake_visibility_percent",
    "terrain_screening_percent", "vegetation_screening_percent",
    "visible_buildings_count", "exposure_from_roads", "exposure_from_paths",
]


def spots_by_lake(spots, limit_per_lake: int = 8) -> dict[str, list[dict]]:
    """Spot-Kandidaten nach See gruppieren, fuer die Kartenebene 2."""
    out: dict[str, list[dict]] = {}
    if spots is None or len(spots) == 0:
        return out
    cols = [c for c in SPOT_PROPS if c in spots.columns]
    for uid, grp in spots.groupby("lake_uid"):
        grp = grp.sort_values("overall_spot_score", ascending=False, na_position="last")
        out[str(uid)] = [
            {k: _jsonable(v) for k, v in rec.items()}
            for rec in grp.head(limit_per_lake)[cols].to_dict("records")
        ]
    return out


def app_meta(
    data: dict,
    cfg: dict,
    banner: str = "",
    note: str = "",
    spots=None,
    extra: dict | None = None,
) -> dict:
    """Baut den META-Block der Weboberflaeche. Reines Python.

    Getrennt von der Geometrie, damit der precomputed-Modus die Karte aus
    einer fertigen Datei erzeugen kann -- ohne geopandas, ohne Netzwerk.
    """
    feats = data.get("features", [])
    if feats:
        lats = [f["properties"].get("latitude") for f in feats]
        lons = [f["properties"].get("longitude") for f in feats]
        lats = [v for v in lats if v is not None]
        lons = [v for v in lons if v is not None]
        center = [sum(lats) / len(lats), sum(lons) / len(lons)] if lats else [53.33, 13.30]
    else:
        center = [53.33, 13.30]

    tcfg = cfg.get("travel", {}) or {}
    lc = cfg.get("landcover", {}) or {}
    default_note = (
        f"Datenstand: OpenStreetMap, "
        f"ESA WorldCover {lc.get('year', 2021)} {lc.get('version', 'v200')} (10 m). "
        "Distanzen ab Uferlinie, gerechnet in einem metrischen CRS. "
        f"Infrastruktur wurde im Umkreis von {(cfg.get('osm', {}) or {}).get('feature_radius_m', 3000)} m "
        "gesucht; größere Abstände sind abgeschnitten."
    )
    meta = {
        "center": center,
        "zoom": 10,
        "origin_label": tcfg.get("origin_label", ""),
        "banner": banner,
        "note": note or default_note,
        "spots": spots_by_lake(spots) if spots is not None and not isinstance(spots, dict) else (spots or {}),
    }
    if extra:
        meta.update(extra)
    return meta


def build_app_from_data(
    data: dict,
    meta: dict,
    out_path: str | Path,
    title: str = "Waldseen-Finder",
) -> Path:
    """Schreibt die mobile Oberflaeche aus einer fertigen FeatureCollection.

    Braucht nur json und das Template -- kein geopandas, kein shapely.
    Das ist der Pfad, den der precomputed-Modus und der Smoke-Test gehen.
    """
    tpl_path = Path(__file__).parent / "templates" / "viewer.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    html = (
        tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__META__", json.dumps(meta, ensure_ascii=False))
        .replace("__TITLE__", title)
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    LOG.info(
        "Weboberflaeche geschrieben: %s (%.1f MB, %d Seen)",
        out_path, out_path.stat().st_size / 1e6, len(data.get("features", [])),
    )
    return out_path


def build_app(
    gdf: "gpd.GeoDataFrame",
    cfg: dict,
    out_path: str | Path | None = None,
    title: str = "Waldseen-Finder",
    banner: str = "",
    note: str = "",
    spots=None,
) -> Path:
    """Mobile Oberflaeche aus einem GeoDataFrame (Live-Pfad)."""
    data = build_app_geojson(gdf, cfg)
    meta = app_meta(data, cfg, banner=banner, note=note, spots=spots)
    if out_path is None:
        out_path = Path(cfg.get("output", {}).get("dir", "output")) / cfg.get("output", {}).get(
            "app_html", "app.html"
        )
    return build_app_from_data(data, meta, out_path, title)


# --------------------------------------------------------------------------
# Folium-Karte
# --------------------------------------------------------------------------


def build_folium_map(
    gdf: "gpd.GeoDataFrame", cfg: dict, out_path: str | Path | None = None
) -> Path:
    """Klassische Folium-Karte mit Layer-Control und Popups."""
    import folium
    import geopandas as gpd

    ocfg = cfg.get("output", {})
    if out_path is None:
        out_path = Path(ocfg.get("dir", "output")) / ocfg.get("map_html", "map.html")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    g = gdf.to_crs(WGS84).copy()
    if g.empty:
        m = folium.Map(location=[53.33, 13.30], zoom_start=9)
        m.save(str(out_path))
        return out_path

    center = [float(g["latitude"].mean()), float(g["longitude"].mean())]
    m = folium.Map(location=center, zoom_start=10, tiles=None, control_scale=True)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Satellit (Esri)",
        attr="Esri, Maxar, Earthstar Geographics",
        max_zoom=19,
    ).add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer(
        "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        name="OpenTopoMap",
        attr="OpenStreetMap, SRTM | OpenTopoMap (CC-BY-SA)",
        max_zoom=17,
    ).add_to(m)

    def color(s: float) -> str:
        return (
            "#22c55e" if s >= 80 else
            "#84cc16" if s >= 65 else
            "#eab308" if s >= 50 else
            "#f97316" if s >= 35 else
            "#ef4444"
        )

    lakes_fg = folium.FeatureGroup(name="Seen", show=True).add_to(m)
    for _, r in g.iterrows():
        s = float(r.get("wilderness_score", 0) or 0)
        popup = folium.Popup(_popup_html(r, cfg), max_width=330)
        folium.GeoJson(
            r.geometry.__geo_interface__,
            style_function=lambda _f, c=color(s): {
                "color": c, "weight": 2, "fillColor": c, "fillOpacity": 0.4
            },
        ).add_child(popup).add_to(lakes_fg)
        folium.CircleMarker(
            [r["latitude"], r["longitude"]],
            radius=6 + 12 * (s / 100.0) ** 2,
            color="#111", weight=1, fill=True, fill_color=color(s), fill_opacity=0.95,
            tooltip=f"{r.get('name') or 'ohne Namen'} · Score {s:.0f}",
            popup=popup,
        ).add_to(lakes_fg)

    if ocfg.get("write_buffers", False):
        metric = gdf.crs
        for d, col in zip([100, 500, 1000], ["#fbbf24", "#a78bfa", "#38bdf8"]):
            fg = folium.FeatureGroup(name=f"{d}-m-Ring", show=False).add_to(m)
            for _, r in gdf.iterrows():
                try:
                    ring = r.geometry.buffer(d).difference(r.geometry)
                    ring = gpd.GeoSeries([ring], crs=metric).to_crs(WGS84).iloc[0]
                except Exception:
                    continue
                folium.GeoJson(
                    ring.__geo_interface__,
                    style_function=lambda _f, c=col: {
                        "color": c, "weight": 1, "dashArray": "4,4",
                        "fillColor": c, "fillOpacity": 0.08
                    },
                ).add_to(fg)

    folium.LayerControl(collapsed=True).add_to(m)
    m.fit_bounds([[g["latitude"].min(), g["longitude"].min()], [g["latitude"].max(), g["longitude"].max()]])
    m.save(str(out_path))
    LOG.info("Folium-Karte geschrieben: %s", out_path)
    return out_path


def _popup_html(r: pd.Series, cfg: dict) -> str:
    def v(key: str, digits: int = 0, unit: str = "") -> str:
        x = r.get(key)
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "–"
        return f"{float(x):.{digits}f}{unit}"

    osm = f"https://www.openstreetmap.org/{r.get('osm_type')}/{r.get('osm_id')}"
    gm = (
        f"https://www.google.com/maps/@?api=1&map_action=map"
        f"&center={r['latitude']},{r['longitude']}&zoom=15&basemap=satellite"
    )
    flags = r.get("quality_flags") or []
    rows = [
        ("Fläche", v("area_ha", 1, " ha")),
        ("Wildnis-Score", v("wilderness_score", 1)),
        ("Wald am Ufer (100 m)", v("shore_tree_cover", 0, " %")),
        ("Wald 500 m", v("tree_cover_500m", 0, " %")),
        ("Wald 1000 m", v("tree_cover_1000m", 0, " %")),
        ("Siedlungsanteil", v("built_up_percent", 2, " %")),
        ("Gebäude 500 m", v("building_count_500m", 0)),
        ("nächstes Gebäude", v("distance_nearest_building_m", 0, " m")),
        ("nächste Hauptstraße", v("distance_major_road_m", 0, " m")),
    ]
    if r.get("drive_time_min") is not None:
        rows.append(("Fahrzeit", v("drive_time_min", 0, " min")))
    body = "".join(
        f"<tr><td style='color:#555;padding:1px 8px 1px 0'>{k}</td>"
        f"<td style='text-align:right;font-weight:600'>{val}</td></tr>"
        for k, val in rows
    )
    return f"""
    <div style="font:13px/1.45 -apple-system,sans-serif;min-width:250px">
      <div style="font-size:15px;font-weight:700;margin-bottom:6px">
        {r.get('name') or 'See ohne Namen'}</div>
      <table style="width:100%;border-collapse:collapse">{body}</table>
      <div style="margin-top:6px;color:#92400e;font-size:11px">
        {'⚠ ' + ', '.join(flags) if flags else ''}</div>
      <div style="margin-top:8px">
        <a href="{osm}" target="_blank">OpenStreetMap</a> ·
        <a href="{gm}" target="_blank">Satellitenbild</a>
      </div>
    </div>"""
