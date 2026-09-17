#!/usr/bin/env python3
"""
make_demo_data.py -- erzeugt einen SYNTHETISCHEN Beispieldatensatz.

Zweck: die Weboberflaeche (src/templates/viewer.html) laesst sich damit
vollstaendig ausprobieren, ohne dass Overpass oder WorldCover erreichbar
sein muessen -- z.B. in einer Umgebung ohne Netzzugang.

WICHTIG: Die Seen, Namen und Messwerte in diesem Datensatz sind ERFUNDEN.
Sie sind keine Analyseergebnisse und beschreiben keine realen Orte. Die
Geometrien sind generierte Blobs, keine OSM-Polygone. Die Datei traegt
deshalb ueberall den Zusatz "Demo" und die Oberflaeche zeigt ein Banner.

Was echt ist: der Scoring-Code. Die Punkte werden mit src/scoring.py und
der echten config.yaml gerechnet -- der Datensatz testet also die
Bewertungslogik und die Oberflaeche, nur eben mit Phantasie-Eingaben.

Aufruf:
    python tools/make_demo_data.py              # schreibt web/demo.html
    python tools/make_demo_data.py --json out.geojson
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from src.filters import evaluate as evaluate_filters
from src.scoring import quality_flags, score_all

random.seed(20260917)

CENTER = (13.30, 53.33)  # Mueritz-NP Ost / Feldberger Seenlandschaft


def blob(lon: float, lat: float, radius_m: float, n: int = 26, rough: float = 0.28):
    """Unregelmaessiges Polygon um einen Punkt (Grad), grob kreisfoermig."""
    mlat = 111_320.0
    mlon = 111_320.0 * math.cos(math.radians(lat))
    phases = [random.uniform(0, 6.28) for _ in range(3)]
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = radius_m * (
            1
            + rough * 0.6 * math.sin(2 * a + phases[0])
            + rough * 0.3 * math.sin(3 * a + phases[1])
            + rough * 0.2 * math.sin(5 * a + phases[2])
        )
        pts.append([round(lon + r * math.cos(a) / mlon, 6), round(lat + r * math.sin(a) / mlat, 6)])
    pts.append(pts[0])
    return pts


def ring(lon: float, lat: float, radius_m: float, dist_m: float):
    """Pufferring als Polygon mit Loch (Demo-Naeherung, kein echter Buffer)."""
    outer = blob(lon, lat, radius_m + dist_m, rough=0.20)
    inner = blob(lon, lat, radius_m, rough=0.20)
    return {"type": "Polygon", "coordinates": [outer, list(reversed(inner))]}


# (Name, Radius m, Ufer-Wald %, Wald 500 m, Wald 1000 m, Siedlung %, Acker %,
#  Geb 100/500/1000, dGeb m, dStrasse m, dWohn m, Camping, Marina, Fahrzeit min)
SPEC = [
    ("Demo 01 – Tiefer Waldsee",   260, 98, 94, 90, 0.00,  1, 0, 0, 0, 2400, 2100, 3000, 0, 0, 102),
    ("Demo 02 – Schwarzer Moorsee",180, 96, 91, 84, 0.00,  3, 0, 0, 1, 1750, 1400, 3000, 0, 0,  95),
    ("Demo 03 – Kesselsee Nord",   140, 94, 88, 79, 0.00,  6, 0, 0, 2, 1320,  980, 2600, 0, 0, 118),
    ("Demo 04 – Stiller Pfuhl",    110, 92, 86, 72, 0.05,  9, 0, 1, 4,  760,  850, 2100, 0, 0,  88),
    ("Demo 05 – Buchenhaussee",    340, 88, 83, 76, 0.10, 11, 0, 1, 6,  640, 1250, 1800, 0, 0, 110),
    ("Demo 06 – Erlenbruchsee",    200, 86, 79, 70, 0.20, 14, 0, 2, 9,  520,  700, 1500, 0, 0, 126),
    ("Demo 07 – Forsthaussee",     420, 81, 74, 66, 0.45, 16, 1, 3, 14, 210,  620, 1250, 0, 0,  99),
    ("Demo 08 – Mühlenteich",      160, 74, 68, 61, 0.80, 22, 1, 5, 21, 140,  430,  900, 0, 0, 105),
    ("Demo 09 – Zeltplatzsee",     520, 78, 71, 63, 0.60, 12, 0, 4, 26, 300,  380,  850, 1, 0,  92),
    ("Demo 10 – Bootshaussee",     600, 71, 64, 58, 1.10, 15, 2, 8, 34, 120,  260,  600, 0, 1,  97),
    ("Demo 11 – Ackerrandsee",     240, 52, 41, 33, 0.30, 46, 0, 2, 11, 480,  540, 1400, 0, 0,  84),
    ("Demo 12 – Dorfsee",          380, 44, 36, 30, 3.20, 28, 6, 24, 78,  40,  120,  150, 0, 0,  76),
    ("Demo 13 – Badeanstaltsee",   700, 62, 55, 47, 2.10, 18, 3, 17, 55,  70,  180,  320, 1, 1,  81),
    ("Demo 14 – Kiesgrubensee",    300, 35, 28, 24, 1.60, 34, 1, 9, 30, 190,  240,  700, 0, 0,  70),
]


def build(cfg: dict) -> dict:
    feats = []
    for i, s in enumerate(SPEC):
        (name, rad, shore, t500, t1000, built, crop,
         b100, b500, b1000, dbuild, droad, dres, camp, marina, drive) = s

        ang = 2 * math.pi * i / len(SPEC)
        spread = 0.055 + 0.030 * ((i * 7) % 5)
        lon = CENTER[0] + spread * math.cos(ang) * 1.6
        lat = CENTER[1] + spread * math.sin(ang)
        area_ha = round(math.pi * rad * rad / 10_000.0, 1)

        props = {
            "name": name,
            "osm_type": "way",
            "osm_id": 900000000 + i,
            "lake_uid": f"w{900000000 + i}",
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "area_ha": area_ha,
            "shore_tree_cover": float(shore),
            "tree_cover_100m": float(shore),
            "tree_cover_500m": float(t500),
            "tree_cover_1000m": float(t1000),
            "natural_land_percent": round(min(99.5, t1000 + 4.0), 1),
            "built_up_percent": float(built),
            "cropland_percent": float(crop),
            "building_count_100m": int(b100),
            "building_count_250m": int(b100 + (b500 - b100) // 2),
            "building_count_500m": int(b500),
            "building_count_1000m": int(b1000),
            "distance_nearest_building_m": float(dbuild),
            "distance_major_road_m": float(droad),
            "distance_residential_m": float(dres),
            "distance_campsite_m": 800.0 if camp else 3000.0,
            "distance_marina_m": 600.0 if marina else 3000.0,
            "has_campsite": bool(camp),
            "has_marina": bool(marina),
            "has_accommodation": bool(camp or marina),
            "has_residential": dres < 1000,
            "has_parking": b500 > 3,
            "has_railway": False,
            "drive_time_min": float(drive),
            "drive_distance_km": round(drive * 75 / 60.0, 1),
            "drive_time_source": "demo",
            "air_distance_km": round(drive * 75 / 60.0 / 1.3, 1),
            "landcover_ref_distance_m": 1000,
            "landcover_ok": True,
            "shore_segment_p10_score": float(max(5, min(98, shore - 6))),
            "shore_segment_median_score": float(max(5, min(98, shore))),
            "shore_segment_min_score": float(max(2, shore - 25 - 3 * b500)),
            "developed_shore_percent": float(min(60, 2 * b500 + (20 if camp else 0))),
            "natural_shore_percent": float(max(20, 100 - 3 * b500 - (20 if camp else 0))),
            "longest_developed_section_m": float(25 * (b500 + (4 if camp else 0))),
            "shore_segment_count": 40,
            "nodata_percent_1000m": 0.0,
            "valid_pixels_1000m": 40000,
        }

        # Datengrundlagen als vorhanden markieren (Demo hat alle Werte)
        props.update({"landcover_ok": True, "building_data_ok": True,
                      "osm_features_ok": True, "travel_ok": True,
                      "shore_segments_ok": True})
        flags, conf = quality_flags(props, cfg)
        props["quality_flags"] = flags
        sc = score_all(props, cfg)
        outcome = evaluate_filters(props, cfg)
        props.update(
            {
                "wilderness_score": sc["wilderness_score"],
                "visual_seclusion_score": sc.get("visual_seclusion_score"),
                "access_score": sc.get("access_score"),
                "data_confidence_score": sc.get("data_confidence_score"),
                "overall_spot_score": sc.get("overall_spot_score"),
                "overall_coverage": sc.get("overall_coverage"),
                "score_base": sc["score_base"],
                "score_penalties": sc["score_penalties"],
                "score_breakdown": sc["score_breakdown"],
                "passes_filters": outcome.passed,
                "filter_status": outcome.status,
                "filter_reasons": outcome.fail_reasons + [
                    f"[unbekannt] {r}" for r in outcome.unknown_reasons],
                "quality_flags": flags + ["DEMO-DATEN"],
                "confidence": conf,
                "buffers": {
                    "100": ring(lon, lat, rad, 100),
                    "500": ring(lon, lat, rad, 500),
                    "1000": ring(lon, lat, rad, 1000),
                },
            }
        )
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [blob(lon, lat, rad)]},
                "properties": props,
            }
        )
    feats.sort(key=lambda f: -f["properties"]["wilderness_score"])
    return {"type": "FeatureCollection", "features": feats}


BANNER = (
    "<b>Demo-Datensatz.</b> Diese Seen, Namen und Messwerte sind erfunden und "
    "beschreiben keine realen Orte – die Oberfläche zeigt, wie echte Ergebnisse "
    "aussehen. Für echte Daten <code>python main.py</code> laufen lassen."
)

NOTE = (
    "Demo-Modus: synthetische Eingabewerte, aber echte Scoring-Logik aus "
    "<code>src/scoring.py</code> und den Gewichten in <code>config.yaml</code>. "
    "Distanzen wären im Echtbetrieb ab Uferlinie in einem metrischen CRS gerechnet, "
    "Waldanteile aus ESA WorldCover 10 m (Stand 2021)."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=str(ROOT / "web" / "demo.html"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument(
        "--bundle",
        nargs="?",
        const=str(ROOT / "data" / "processed" / "demo-mueritz"),
        default=None,
        help="zusätzlich ein vorberechnetes Bündel schreiben (für den "
             "precomputed-Modus und den Smoke-Test)",
    )
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = build(cfg)

    if args.json:
        Path(args.json).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"GeoJSON: {args.json}")

    tpl = (ROOT / "src" / "templates" / "viewer.html").read_text(encoding="utf-8")
    meta = {
        "center": [CENTER[1], CENTER[0]],
        "zoom": 10,
        "origin_label": "Berlin",
        "banner": BANNER,
        "note": NOTE,
    }
    html = (
        tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__META__", json.dumps(meta, ensure_ascii=False))
        .replace("__TITLE__", "Waldseen-Finder – Demo")
    )
    out = Path(args.html)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"HTML:    {out}  ({out.stat().st_size/1024:.0f} kB)")

    if args.bundle:
        _write_demo_bundle(Path(args.bundle), data)

    passed = sum(1 for f in data["features"] if f["properties"]["passes_filters"])
    print(f"{len(data['features'])} Demo-Seen, {passed} bestehen die harten Filter.")
    for f in data["features"][:5]:
        p = f["properties"]
        print(f"  {p['wilderness_score']:5.1f}  {p['name']}")
    return 0


def _write_demo_bundle(target: Path, data: dict) -> None:
    """Schreibt ein vorberechnetes Bündel aus den Demo-Daten.

    Bewusst ohne geopandas: der Demo-Datensatz ist bereits kartenfertig.
    Das Manifest trägt ``is_demo: true`` -- die Oberfläche zeigt dann das
    Demo-Banner, und niemand hält erfundene Zahlen für Messwerte.
    """
    from datetime import datetime, timezone

    from src import BUNDLE_FORMAT_VERSION, VERSION
    from src.store import APP_DATA, MANIFEST

    target.mkdir(parents=True, exist_ok=True)
    (target / APP_DATA).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    feats = data.get("features", [])
    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "lake_finder_version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slug": target.name,
        "label": "DEMO – synthetische Daten (Müritz-Region)",
        "region_bbox": [13.05, 53.20, 13.55, 53.47],
        "metric_crs": "EPSG:32633",
        "is_demo": True,
        "counts": {
            "lakes": len(feats),
            "passes": sum(1 for f in feats if f["properties"].get("passes_filters")),
            "spots": 0,
        },
        "sources": {"buildings": ["demo"], "landcover": "DEMO – keine echten Daten"},
        "files": {"app_data": APP_DATA},
    }
    (target / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Bündel:  {target}  ({len(feats)} Demo-Seen, is_demo=true)")


if __name__ == "__main__":
    raise SystemExit(main())
