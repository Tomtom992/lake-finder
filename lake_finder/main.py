#!/usr/bin/env python3
"""
main.py -- Pipeline: Seen finden, Umgebung bewerten, Ergebnisse ausgeben.

Beispiele
---------
    # Testlauf auf der voreingestellten 30x30-km-Region
    python main.py

    # andere Voreinstellung
    python main.py --preset stechlin

    # freie Bounding Box
    python main.py --bbox 13.05 53.20 13.55 53.47

    # Mittelpunkt + Kantenlaenge, mit echtem Routing statt Luftlinie
    python main.py --center 13.03 53.15 --size-km 40 --travel-mode osrm

    # nur Karte aus vorhandenen Ergebnissen neu bauen
    python main.py --rebuild-map-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

# Absichtlich nur leichte Importe auf Modulebene: main.py muss sich auch
# dann importieren lassen, wenn geopandas/rasterio fehlen -- sonst koennte
# weder "--version" noch der Smoke-Test etwas ueber die CLI aussagen, und
# eine fehlende Abhaengigkeit wuerde als nackter ImportError enden statt
# als verstaendliche Meldung.
from src import VERSION
from src import deployment as deploy_mod
from src.utils import Bbox, JsonCache, OverpassClient, pick_metric_crs, setup_logging

GEO_HINT = (
    "Der Geo-Stack fehlt oder ist unvollständig: {exc}\n"
    "Installation:  pip install -r requirements.txt\n"
    "Ohne geopandas/shapely/rasterio kann die Kommandozeile nichts berechnen. "
    "Vorberechnete Ergebnisse anschauen geht trotzdem:  streamlit run app.py"
)


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_region(cfg: dict, args: argparse.Namespace) -> tuple[Bbox, str]:
    """Ermittelt die Region aus CLI-Argumenten bzw. config.yaml."""
    if args.bbox:
        return Bbox.from_list(args.bbox), "CLI-BBox"
    if args.center:
        return Bbox.from_center(args.center[0], args.center[1], args.size_km or 30.0), "CLI-Center"

    rcfg = cfg.get("region", {}) or {}
    preset_name = args.preset or rcfg.get("preset")
    if preset_name:
        presets = cfg.get("presets", {}) or {}
        if preset_name not in presets:
            raise SystemExit(
                f"Unbekannte Voreinstellung '{preset_name}'. "
                f"Verfuegbar: {', '.join(sorted(presets))}"
            )
        p = presets[preset_name]
        label = p.get("label", preset_name)
        if "bbox" in p:
            return Bbox.from_list(p["bbox"]), label
        return Bbox.from_center(p["center"][0], p["center"][1], float(p.get("size_km", 30))), label
    if "bbox" in rcfg:
        return Bbox.from_list(rcfg["bbox"]), "config bbox"
    if "center" in rcfg:
        return Bbox.from_center(
            rcfg["center"][0], rcfg["center"][1], float(rcfg.get("size_km", 30))
        ), "config center"
    raise SystemExit("Keine Region definiert (region.preset / region.bbox / region.center).")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def run_data(cfg: dict, bbox: Bbox, region_label: str, log, deployment=None):
    """Datenstufe: alle Metriken, noch ohne Bewertung.

    Bewusst getrennt von der Bewertung: der Datenabruf ist teuer und wird
    gecacht, das Verschieben von Gewichten und Schwellen ist billig. Die
    Weboberflaeche nutzt genau diese Trennung fuer ihre Live-Regler.

    Rueckgabe: (GeoDataFrame ohne Scores, Kontext-Dict)
    """
    import pandas as pd

    from src import landcover as lc_mod
    from src import lakes as lakes_mod
    from src import osm_features as osm_mod
    from src import travel as travel_mod

    # Harte Sperre: im precomputed-Modus darf hier nichts nach draussen gehen.
    if deployment is not None:
        deploy_mod.guard_live(deployment, "Overpass-/WorldCover-Abruf")

    t_start = time.time()
    metric_crs = pick_metric_crs(bbox, (cfg.get("crs", {}) or {}).get("metric"))
    log.info("Region: %s  %s", region_label, bbox)
    log.info("Metrisches CRS fuer Buffer/Distanzen/Flaechen: %s", metric_crs)

    ocfg = cfg.get("osm", {}) or {}
    cache = JsonCache(
        ocfg.get("cache_dir", "data/cache/overpass"),
        enabled=bool(ocfg.get("cache_enabled", True)),
        ttl_days=float(ocfg.get("cache_ttl_days", 30)),
    )
    client = OverpassClient(
        endpoints=ocfg.get("endpoints", ["https://overpass-api.de/api/interpreter"]),
        timeout_s=int(ocfg.get("timeout_s", 300)),
        max_retries=int(ocfg.get("max_retries", 4)),
        min_interval_s=float(ocfg.get("min_interval_s", 1.5)),
        user_agent=str(ocfg.get("user_agent", "lake-finder/1.0")),
        cache=cache,
    )

    import geopandas as gpd

    from src import access as access_mod
    from src import buildings as bld_mod
    from src import shoreline as shore_mod
    from src import spots as spots_mod

    distances = [int(d) for d in cfg.get("buffers", {}).get("distances_m", [100, 250, 500, 1000])]
    cap = float((cfg.get("osm", {}) or {}).get("feature_radius_m", 3000))
    ctx: dict = {"metric_crs": metric_crs, "region": region_label, "bbox": bbox.as_list()}

    # 1 -- Seen
    log.info("--- Schritt 1/8: Seen aus OpenStreetMap ---")
    lakes = lakes_mod.fetch_lakes(bbox, cfg, client, metric_crs)
    if lakes.empty:
        log.warning("Keine Seen in der Region gefunden. Ende.")
        return lakes, ctx

    # 2 -- Infrastruktur
    log.info("--- Schritt 2/8: menschliche Infrastruktur ---")
    feats = osm_mod.fetch_features(bbox, lakes, cfg, client, metric_crs)
    osm_ok = bool(len(feats))

    # 3 -- Gebaeude aus mehreren Quellen
    log.info("--- Schritt 3/8: Gebaeude (OSM + Overture + amtlich) ---")
    bld_layer = bld_mod.build_layer(bbox, feats, cfg, metric_crs)
    ctx["building_sources"] = bld_layer["sources_used"]
    ctx["building_sources_failed"] = bld_layer["sources_failed"]

    # 4 -- Distanzen/Zaehlungen ab Ufer
    log.info("--- Schritt 4/8: Distanzen und Zaehlungen ab Uferlinie ---")
    osm_metrics = osm_mod.compute_osm_metrics(lakes, feats, cfg)
    osm_metrics["osm_features_ok"] = osm_ok

    bld_rows = []
    for _, lake in lakes.iterrows():
        rec = {"lake_uid": lake["lake_uid"]}
        rec.update(bld_mod.lake_metrics(lake.geometry, bld_layer, distances, cap))
        bld_rows.append(rec)
    bld_df = pd.DataFrame(bld_rows)

    # 5 -- Land Cover
    log.info("--- Schritt 5/8: Land Cover (ESA WorldCover 10 m) ---")
    sampler = None
    try:
        sampler = lc_mod.build_region_sampler(cfg, bbox, metric_crs)
    except Exception as exc:
        log.warning("Land-Cover-Sampler nicht verfuegbar: %s", exc)
    try:
        lc = lc_mod.compute_landcover_metrics(lakes, cfg, bbox, sampler=sampler)
    except Exception as exc:
        log.error("Land-Cover-Auswertung fehlgeschlagen: %s", exc)
        log.error("Der Lauf geht ohne Land-Cover weiter -- die betroffenen Filter "
                  "werden UNKNOWN, nicht stillschweigend bestanden.")
        lc = pd.DataFrame({"lake_uid": lakes["lake_uid"], "landcover_ok": False})

    # 6 -- Segmentierte Uferanalyse
    shore_df = pd.DataFrame({"lake_uid": lakes["lake_uid"]})
    if (cfg.get("shore", {}) or {}).get("enabled", True):
        log.info("--- Schritt 6/8: segmentierte Uferanalyse ---")
        index = osm_mod.FeatureIndex(crs=str(lakes.crs)).build(feats)
        rows, seg_rows = [], []
        for i, (_, lake) in enumerate(lakes.iterrows(), 1):
            if i % 25 == 0 or i == len(lakes):
                log.info("  Ufersegmente: %d/%d Seen", i, len(lakes))
            try:
                agg, segs = shore_mod.analyse_lake_shoreline(
                    lake.geometry, index, sampler, cfg
                )
            except Exception as exc:
                log.warning("  Uferanalyse fuer %s fehlgeschlagen: %s", lake["lake_uid"], exc)
                agg, segs = {"shore_segments_ok": False}, []
            agg = dict(agg)
            agg["lake_uid"] = lake["lake_uid"]
            rows.append(agg)
            for s in segs:
                s["lake_uid"] = lake["lake_uid"]
            seg_rows.extend(segs)
        shore_df = pd.DataFrame(rows)
        ctx["shore_segments"] = seg_rows

    # 7 -- Zugangspunkte und Erreichbarkeit
    log.info("--- Schritt 7/8: Zugangspunkte und Fahrzeit ---")
    df = (
        lakes.merge(osm_metrics, on="lake_uid", how="left")
        .merge(bld_df, on="lake_uid", how="left")
        .merge(lc, on="lake_uid", how="left")
        .merge(shore_df, on="lake_uid", how="left")
    )
    df["latitude"] = df["centroid_lat"]
    df["longitude"] = df["centroid_lon"]

    if (cfg.get("access", {}) or {}).get("enabled", True):
        try:
            acc = access_mod.compute_access(lakes, feats, cfg, metric_crs)
            df = df.merge(acc, on="lake_uid", how="left")
        except Exception as exc:
            log.warning("Zugangspunkt-Analyse fehlgeschlagen: %s", exc)
            df = travel_mod.add_travel_metrics(df, cfg)
    else:
        df = travel_mod.add_travel_metrics(df, cfg)
    if "drive_time_min" in df.columns:
        df["travel_ok"] = df["drive_time_min"].notna()

    ctx["features"] = feats
    ctx["buildings_layer"] = bld_layer
    ctx["seconds"] = round(time.time() - t_start, 1)
    log.info("Datenstufe fertig in %.1f s.", ctx["seconds"])
    return gpd.GeoDataFrame(df, geometry="geometry", crs=metric_crs), ctx


def run(cfg: dict, bbox: Bbox, region_label: str, log, profile: str | None = None,
        deployment=None):
    """Datenstufe + Bewertung + optionale Spot-Analyse.

    Rueckgabe: (Seen-GeoDataFrame, Spot-DataFrame, Kontext-Dict)
    """
    import geopandas as gpd
    import pandas as pd

    from src import scoring as score_mod
    from src import spots as spots_mod

    df, ctx = run_data(cfg, bbox, region_label, log, deployment=deployment)
    if df is None or len(df) == 0:
        return df, pd.DataFrame(), ctx

    log.info("--- Bewertung: Teil-Scores, harte Filter (PASS/FAIL/UNKNOWN) ---")
    scored = score_mod.score_table(df, cfg, profile)
    scored = gpd.GeoDataFrame(scored, geometry="geometry", crs=ctx["metric_crs"])

    spot_df = pd.DataFrame()
    if (cfg.get("camp", {}) or {}).get("enabled", False):
        log.info("--- Zusatzstufe: Campingflaechen an den besten Seen ---")
        spot_df, spot_info = spots_mod.analyse_spots(
            scored, ctx.get("features"), ctx.get("buildings_layer"),
            cfg, ctx["metric_crs"], profile,
        )
        ctx["spots"] = spot_info

    return scored, spot_df, ctx


def print_summary(scored, cfg: dict, top: int = 15) -> None:
    from src import scoring as score_mod

    if scored is None or len(scored) == 0:
        print("\nKeine Ergebnisse.\n")
        return
    passed = scored[scored["passes_filters"]]
    print("\n" + "=" * 104)
    print(f"{len(scored)} Seen bewertet, {len(passed)} bestehen alle harten Filter.")
    if "filter_status" in scored.columns:
        vc = scored["filter_status"].value_counts().to_dict()
        print(
            f"  Filterstatus: PASS {vc.get('PASS', 0)} | FAIL {vc.get('FAIL', 0)} | "
            f"UNKNOWN {vc.get('UNKNOWN', 0)}   "
            "(UNKNOWN = Datengrundlage fehlt, gilt NICHT als bestanden)"
        )
    print("=" * 104)
    cols = [
        ("name", 24, "s"),
        ("area_ha", 7, ".1f"),
        ("wilderness_score", 6, ".0f"),
        ("visual_seclusion_score", 6, ".0f"),
        ("overall_spot_score", 6, ".0f"),
        ("data_confidence_score", 6, ".0f"),
        ("shore_tree_cover", 6, ".0f"),
        ("developed_shore_percent", 6, ".0f"),
        ("building_count_500m", 6, ".0f"),
        ("distance_nearest_building_m", 8, ".0f"),
        ("drive_time_min", 6, ".0f"),
    ]
    head = ["Name".ljust(24), "ha".rjust(7), "Wild".rjust(6), "Sicht".rjust(6),
            "Gesamt".rjust(6), "Konf".rjust(6), "Ufer%".rjust(6), "beb%".rjust(6),
            "Geb500".rjust(6), "dGeb m".rjust(8), "min".rjust(6)]
    print("  ".join(head))
    print("-" * 104)
    for _, r in scored.head(top).iterrows():
        cells = []
        for c, w, f in cols:
            v = r.get(c)
            if f == "s":
                s = (str(v) if v else "(ohne Namen)")[:w].ljust(w)
            else:
                s = ("–" if v is None or (isinstance(v, float) and v != v) else format(float(v), f)).rjust(w)
            cells.append(s)
        status = str(r.get("filter_status", ""))
        mark = ""
        if not r["passes_filters"]:
            mark = "  [FAIL]" if status == "FAIL" else "  [UNKNOWN: Datenluecke]"
            reasons = r.get("filter_reasons") or []
            if isinstance(reasons, list) and reasons:
                mark += " " + reasons[0][:44]
        print("  ".join(cells) + mark)
    print()
    best = scored.iloc[0]
    print(f"Score-Aufschluesselung fuer '{best.get('name') or 'See ohne Namen'}':")
    print(score_mod.explain(best))
    print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Findet naturbelassene, abgelegene Seen aus offenen Geodaten.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--preset", help="Name einer Voreinstellung aus config.yaml")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    ap.add_argument("--center", nargs=2, type=float, metavar=("LON", "LAT"))
    ap.add_argument("--size-km", type=float, default=None)
    ap.add_argument("--out", help="Ausgabeverzeichnis (ueberschreibt output.dir)")
    ap.add_argument("--travel-mode", choices=["haversine", "osrm", "off"])
    ap.add_argument("--no-cache", action="store_true", help="Overpass-Cache ignorieren")
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--no-map", action="store_true")
    ap.add_argument("--sentinel", action="store_true", help="Sentinel-2-Bildchips fuer Top-Treffer")
    ap.add_argument(
        "--profile",
        help="Gewichtungsprofil fuer den Gesamtscore "
             "(balanced | max_seclusion | easy_access | best_tent | best_view | max_privacy)",
    )
    ap.add_argument("--spots", action="store_true", help="Campingflaechen-Analyse einschalten")
    ap.add_argument(
        "--mode", choices=["live", "precomputed", "auto"],
        help="Betriebsart. Die Kommandozeile rechnet, also ist 'live' der Standard; "
             "'precomputed' bricht bewusst ab, weil dann nichts berechnet werden darf.",
    )
    ap.add_argument(
        "--export-precomputed", action="store_true",
        help="Nach dem Lauf ein vorberechnetes Buendel unter deployment.data_dir "
             "schreiben (fuer die Cloud-App).",
    )
    ap.add_argument("--bundle-slug", help="Verzeichnisname des Buendels (Standard: aus der Region)")
    ap.add_argument("--version", action="version", version=f"lake_finder {VERSION}")
    ap.add_argument(
        "--lenient", action="store_true",
        help="UNKNOWN-Filter nicht als durchgefallen werten "
             "(filters.fail_on_missing_critical_data = false)",
    )
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.out:
        cfg.setdefault("output", {})["dir"] = args.out
    if args.no_cache:
        cfg.setdefault("osm", {})["cache_enabled"] = False
    if args.travel_mode == "off":
        cfg.setdefault("travel", {})["enabled"] = False
    elif args.travel_mode:
        cfg.setdefault("travel", {})["mode"] = args.travel_mode
    if args.sentinel:
        cfg.setdefault("sentinel", {})["enabled"] = True
    if args.spots:
        cfg.setdefault("camp", {})["enabled"] = True
    if args.lenient:
        cfg.setdefault("filters", {})["fail_on_missing_critical_data"] = False

    rcfg = cfg.get("run", {}) or {}
    log = setup_logging(args.log_level or rcfg.get("log_level", "INFO"), rcfg.get("log_file"))
    log.info("lake_finder %s", VERSION)

    # Die Kommandozeile ist das Werkzeug, das RECHNET -- deshalb ist hier
    # 'live' der Standard, unabhaengig von deployment.mode in der config.
    # Wer ausdruecklich --mode precomputed sagt, bekommt einen klaren Abbruch
    # statt eines Laufs, der doch Daten holt.
    dep = deploy_mod.resolve(cfg, requested=args.mode or deploy_mod.LIVE)
    if dep.is_precomputed:
        print(
            "\nModus 'precomputed': die Kommandozeile berechnet Ergebnisse und "
            "darf das in diesem Modus nicht.\n"
            "  Zum Rechnen:   python main.py --mode live --preset stechlin\n"
            "  Zum Anschauen: streamlit run app.py\n"
        )
        return 2

    try:
        from src import map as map_mod
    except ImportError as exc:  # pragma: no cover - nur ohne Geo-Stack
        print(GEO_HINT.format(exc=exc))
        return 3

    bbox, label = resolve_region(cfg, args)
    try:
        scored, spots, ctx = run(cfg, bbox, label, log, profile=args.profile, deployment=dep)
    except ImportError as exc:
        print(GEO_HINT.format(exc=exc))
        return 3
    if scored is None or len(scored) == 0:
        return 1

    paths = map_mod.write_tables(scored, cfg)

    if spots is not None and len(spots):
        from pathlib import Path as _P

        outdir = _P(cfg.get("output", {}).get("dir", "output"))
        sp_csv = outdir / "spots.csv"
        spots.drop(columns=[c for c in ("safety_flags",) if c in spots.columns]).assign(
            safety_flags=spots.get("safety_flags", "").apply(
                lambda v: "; ".join(v) if isinstance(v, list) else v
            )
            if "safety_flags" in spots.columns
            else ""
        ).to_csv(sp_csv, index=False, encoding="utf-8")
        paths["spots_csv"] = sp_csv
        log.info("Spot-Kandidaten geschrieben: %s (%d Zeilen)", sp_csv, len(spots))
    if not args.no_map:
        try:
            paths["map"] = map_mod.build_folium_map(scored, cfg)
        except Exception as exc:
            log.warning("Folium-Karte fehlgeschlagen: %s", exc)
        paths["app"] = map_mod.build_app(
            scored, cfg, title=f"Waldseen – {label}",
            banner="", note="", spots=spots,
        )

    if (cfg.get("sentinel", {}) or {}).get("enabled"):
        try:
            from src import sentinel as sen

            sen.make_chips(scored, cfg)
        except Exception as exc:
            log.warning("Sentinel-2-Kontrolle fehlgeschlagen: %s", exc)

    if args.export_precomputed or (cfg.get("deployment", {}) or {}).get("auto_export", False):
        try:
            from src import store as store_mod

            bundle = store_mod.write_bundle(
                scored, spots, cfg, ctx, slug=args.bundle_slug, label=label
            )
            paths["bundle"] = bundle
        except Exception as exc:
            log.error("Vorberechnetes Buendel konnte nicht geschrieben werden: %s", exc)

    print_summary(scored, cfg)
    print("Ausgaben:")
    for k, v in paths.items():
        print(f"  {k:9s} {v}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
