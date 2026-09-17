#!/usr/bin/env python3
"""
smoke_test.py -- Deployment-Rauchtest fuer lake_finder.

Prueft in wenigen Sekunden, ob eine Installation grundsaetzlich arbeitsfaehig
ist. Gedacht fuer: nach dem Klonen, vor dem Deployment, nach jedem Update.

Geprueft wird:

  1. Python-Version und Paketumgebung
  2. alle Module lassen sich importieren
  3. config.yaml wird gefunden, ist gueltig und vollstaendig
  4. app.py: Syntax und alle Modulimporte aufloesbar
  5. Betriebsart laesst sich aufloesen (live / precomputed / auto)
  6. vorberechnete Buendel sind lesbar und gueltig
  7. Neubewertung eines Buendels funktioniert (reines Rechnen, kein Netz)
  8. die Karte laesst sich erzeugen

Aufruf:
    python tools/smoke_test.py
    python tools/smoke_test.py --strict     # fehlender Geo-Stack = Fehler
    python tools/smoke_test.py --data-dir data/processed

Exitcode 0 = alles bestanden (Warnungen erlaubt), 1 = mindestens ein Fehler.

Der Test macht bewusst KEINE Netzwerkanfragen. Er sagt aus, ob die
Installation lauffaehig ist -- nicht, ob Overpass gerade erreichbar ist.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
SYMBOL = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL ", SKIP: " übersprungen "}

# Pakete, ohne die nur die Live-Analyse ausfaellt -- der precomputed-Modus
# laeuft ohne sie weiter. Fehlen sie, gibt es SKIP statt FAIL (ausser --strict).
GEO_PACKAGES = {"geopandas", "shapely", "rasterio", "pyproj", "fiona", "osgeo", "folium"}
# Optional insgesamt: ohne diese laeuft der precomputed-Modus weiter.
OPTIONAL_PACKAGES = GEO_PACKAGES | {"streamlit", "duckdb", "pystac_client", "PIL", "pyrosm"}

# Module, die immer importierbar sein muessen (kein Geo-Stack noetig)
CORE_MODULES = [
    "src", "src.deployment", "src.store", "src.utils", "src.filters",
    "src.scoring", "src.shoreline", "src.map", "src.terrain", "src.camp",
    "src.viewshed", "src.access", "src.buildings", "src.buildings.merge",
    "src.elevation.common", "src.elevation.states",
]
# Module, die den vollen Geo-Stack brauchen
GEO_MODULES = [
    "src.lakes", "src.osm_features", "src.landcover", "src.spots",
    "src.osm_pbf", "src.sentinel", "main",
]

REQUIRED_CONFIG_KEYS = [
    "region", "presets", "lakes", "buffers", "osm", "landcover",
    "scoring", "filters", "output", "deployment",
]


class Report:
    def __init__(self, strict: bool = False):
        self.rows: list[tuple[str, str, str]] = []
        self.strict = strict

    def add(self, status: str, name: str, detail: str = "") -> str:
        if status == SKIP and self.strict:
            status = FAIL
        self.rows.append((status, name, detail))
        line = f"[{SYMBOL[status]:^14}] {name}"
        if detail:
            line += f"\n{' ' * 18}{detail}"
        print(line, flush=True)
        return status

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == WARN)

    @property
    def skipped(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == SKIP)


def _missing_package(exc: BaseException) -> str | None:
    name = getattr(exc, "name", None) or str(exc)
    for pkg in GEO_PACKAGES:
        if pkg in str(name):
            return pkg
    return None


# --------------------------------------------------------------------------
# Einzelpruefungen
# --------------------------------------------------------------------------


def check_python(rep: Report) -> None:
    v = sys.version_info
    detail = f"Python {v.major}.{v.minor}.{v.micro} · {sys.executable}"
    if v < (3, 10):
        rep.add(FAIL, "Python-Version", detail + "  (mindestens 3.10 nötig)")
    else:
        rep.add(OK, "Python-Version", detail)

    for pkg in ("numpy", "pandas", "yaml"):
        try:
            importlib.import_module(pkg)
        except ImportError as exc:
            rep.add(FAIL, f"Paket {pkg}", f"{exc}  ->  pip install -r requirements.txt")
            return
    rep.add(OK, "Grundpakete", "numpy, pandas, PyYAML vorhanden")

    present = []
    for pkg in ("geopandas", "shapely", "rasterio", "pyproj", "folium", "streamlit"):
        try:
            importlib.import_module(pkg)
            present.append(pkg)
        except ImportError:
            pass
    missing = [p for p in ("geopandas", "shapely", "rasterio", "pyproj", "folium", "streamlit")
               if p not in present]
    if missing:
        rep.add(SKIP, "Geo-/Web-Stack",
                f"nicht installiert: {', '.join(missing)} — Live-Analyse und/oder "
                "Streamlit stehen nicht zur Verfügung")
    else:
        rep.add(OK, "Geo-/Web-Stack", ", ".join(present))


def check_imports(rep: Report) -> None:
    bad = []
    for mod in CORE_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            bad.append(f"{mod}: {type(exc).__name__}: {exc}")
    if bad:
        rep.add(FAIL, "Kernmodule importierbar", "\n".join(" " * 18 + b for b in bad).strip())
    else:
        rep.add(OK, "Kernmodule importierbar", f"{len(CORE_MODULES)} Module ohne Geo-Stack")

    skipped, broken = [], []
    for mod in GEO_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            pkg = _missing_package(exc)
            (skipped if pkg else broken).append(f"{mod} ({exc})")
        except Exception as exc:  # pragma: no cover
            broken.append(f"{mod} ({type(exc).__name__}: {exc})")
    if broken:
        rep.add(FAIL, "Analysemodule importierbar", "; ".join(broken))
    elif skipped:
        rep.add(SKIP, "Analysemodule importierbar",
                f"{len(skipped)} Module brauchen den Geo-Stack")
    else:
        rep.add(OK, "Analysemodule importierbar", f"{len(GEO_MODULES)} Module")


def check_config(rep: Report) -> dict:
    import yaml

    path = ROOT / "config.yaml"
    if not path.exists():
        rep.add(FAIL, "config.yaml gefunden", f"fehlt unter {path}")
        return {}
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        rep.add(FAIL, "config.yaml lesbar", str(exc))
        return {}

    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
    if missing:
        rep.add(FAIL, "config.yaml vollständig", "fehlende Abschnitte: " + ", ".join(missing))
    else:
        rep.add(OK, "config.yaml vollständig",
                f"{len(cfg)} Abschnitte · {len(cfg.get('presets', {}))} Regionen · "
                f"{len((cfg.get('filters', {}) or {}).get('checks', {}))} harte Filter")

    # Die Vertragspruefung aus den Tests hier noch einmal kurz: eine config,
    # die nicht berechnete Metriken referenziert, ist ein stiller Fehler.
    try:
        sys.path.insert(0, str(ROOT / "tests"))
        from test_config_contract import PLANNED_NOT_YET_COMPUTED, produced_columns

        produced = produced_columns() | PLANNED_NOT_YET_COMPUTED
        unknown = []
        scoring = cfg.get("scoring", {}) or {}
        entries = list((scoring.get("components", {}) or {}).items())
        for block in (scoring.get("scores", {}) or {}).values():
            entries += list(((block or {}).get("components", {}) or {}).items())
        for name, spec in entries:
            key = (spec or {}).get("metric", name)
            if key not in produced:
                unknown.append(key)
        if unknown:
            rep.add(WARN, "config referenziert nur berechnete Metriken",
                    "unbekannt: " + ", ".join(sorted(set(unknown))))
        else:
            rep.add(OK, "config referenziert nur berechnete Metriken")
    except Exception as exc:  # pragma: no cover
        rep.add(WARN, "config-Vertragsprüfung", f"nicht durchführbar: {exc}")
    return cfg


def check_app_file(rep: Report) -> None:
    """app.py laesst sich parsen und alle Modulimporte sind aufloesbar.

    Importieren selbst geht nicht: app.py IST das Streamlit-Skript und
    wuerde beim Import die Oberflaeche aufbauen. Deshalb AST-Analyse.
    """
    path = ROOT / "app.py"
    if not path.exists():
        rep.add(FAIL, "app.py vorhanden", f"fehlt unter {path}")
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        rep.add(FAIL, "app.py fehlerfrei", f"Zeile {exc.lineno}: {exc.msg}")
        return

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)

    missing, geo_missing = [], []
    for mod in sorted(modules):
        try:
            importlib.import_module(mod)
        except ImportError as exc:
            optional = _missing_package(exc) or mod.split(".")[0] in OPTIONAL_PACKAGES
            (geo_missing if optional else missing).append(mod)
        except Exception as exc:  # pragma: no cover
            missing.append(f"{mod} ({type(exc).__name__})")

    if missing:
        rep.add(FAIL, "app.py: Imports auflösbar", "nicht importierbar: " + ", ".join(missing))
    elif geo_missing:
        rep.add(SKIP, "app.py: Imports auflösbar",
                f"syntaktisch in Ordnung; {', '.join(geo_missing)} nicht installiert")
    else:
        rep.add(OK, "app.py: Imports auflösbar", f"{len(modules)} Module")


def check_mode(rep: Report, cfg: dict, data_dir: str | None) -> "object":
    from src import deployment as deploy_mod

    try:
        dep = deploy_mod.resolve(cfg, data_dir=data_dir)
    except Exception as exc:
        rep.add(FAIL, "Betriebsart auflösbar", str(exc))
        return None
    rep.add(OK, f"Betriebsart: {dep.mode}",
            f"{dep.source} · Datenverzeichnis {dep.data_dir} · "
            f"{dep.bundles_available} Bündel")

    # Die Sperre muss wirklich sperren.
    if dep.is_precomputed:
        try:
            deploy_mod.guard_live(dep, "Testzugriff")
            rep.add(FAIL, "Live-Sperre im precomputed-Modus", "guard_live hat NICHT gesperrt")
        except deploy_mod.LiveModeDisabled:
            rep.add(OK, "Live-Sperre im precomputed-Modus", "greift")
    return dep


def check_bundles(rep: Report, cfg: dict, data_dir: str | None) -> "object":
    from src import deployment as deploy_mod
    from src import store as store_mod

    directory = deploy_mod.data_dir_from(cfg, data_dir)
    manifests = store_mod.list_bundles(directory)
    if not manifests:
        rep.add(WARN, "Vorberechnete Daten vorhanden",
                f"keine unter {directory} — für den Cloud-Betrieb nötig. Erzeugen mit: "
                "python main.py --mode live --preset stechlin --export-precomputed")
        return None

    first = None
    for m in manifests:
        try:
            bundle = store_mod.load_bundle(m["_path"])
        except Exception as exc:
            rep.add(FAIL, f"Bündel lesbar: {m.get('slug')}", str(exc))
            continue
        problems = store_mod.validate_bundle(bundle)
        if problems:
            rep.add(FAIL, f"Bündel gültig: {bundle.slug}", "; ".join(problems))
            continue
        demo = "  [DEMO-DATEN]" if bundle.is_demo else ""
        rep.add(OK, f"Bündel gültig: {bundle.slug}", bundle.describe() + demo)
        first = first or bundle
    return first


def check_rescore(rep: Report, cfg: dict, bundle) -> "object":
    if bundle is None:
        rep.add(SKIP, "Neubewertung aus Bündel", "kein Bündel vorhanden")
        return None
    from src import store as store_mod

    try:
        data, scored = store_mod.rescore_bundle(bundle, cfg)
    except Exception as exc:
        rep.add(FAIL, "Neubewertung aus Bündel", f"{type(exc).__name__}: {exc}")
        return None
    if scored is None or len(scored) == 0:
        rep.add(FAIL, "Neubewertung aus Bündel", "keine Zeilen")
        return None

    need = {"wilderness_score", "passes_filters", "filter_status", "data_confidence_score"}
    missing = sorted(need - set(scored.columns))
    if missing:
        rep.add(FAIL, "Neubewertung aus Bündel", "fehlende Spalten: " + ", ".join(missing))
        return None
    counts = scored["filter_status"].value_counts().to_dict()
    rep.add(OK, "Neubewertung aus Bündel (ohne Netzwerk)",
            f"{len(scored)} Seen · " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    return data


def check_map(rep: Report, cfg: dict, data) -> None:
    from src import map as map_mod

    if data is None:
        rep.add(SKIP, "Karte erzeugbar", "keine Daten")
        return
    try:
        meta = map_mod.app_meta(data, cfg, banner="Smoke-Test")
        with tempfile.TemporaryDirectory() as tmp:
            out = map_mod.build_app_from_data(data, meta, Path(tmp) / "app.html", "Smoke-Test")
            size = out.stat().st_size
            html = out.read_text(encoding="utf-8")
    except Exception as exc:
        rep.add(FAIL, "Karte erzeugbar", f"{type(exc).__name__}: {exc}")
        return

    problems = []
    if "__DATA__" in html or "__META__" in html or "__TITLE__" in html:
        problems.append("Platzhalter im Template nicht ersetzt")
    if size < 5000:
        problems.append(f"verdächtig klein ({size} Bytes)")
    if "leaflet" not in html.lower():
        problems.append("Leaflet wird nicht eingebunden")
    if problems:
        rep.add(FAIL, "Karte erzeugbar", "; ".join(problems))
    else:
        rep.add(OK, "Karte erzeugbar",
                f"{size // 1024} kB · {len(data.get('features', []))} Seen eingebettet")


def check_outputs(rep: Report) -> None:
    """Hinweis auf die Artefakte des letzten CLI-Laufs -- kein Fehler, wenn sie fehlen."""
    out = ROOT / "output"
    found = [f for f in ("results.csv", "results.geojson", "map.html", "app.html")
             if (out / f).exists()]
    if found:
        rep.add(OK, "Ausgaben des letzten Laufs", ", ".join(found))
    else:
        rep.add(WARN, "Ausgaben des letzten Laufs",
                "noch keine in output/ — nach 'python main.py --preset stechlin' erwartet")


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deployment-Rauchtest fuer lake_finder")
    ap.add_argument("--data-dir", default=None, help="Verzeichnis mit vorberechneten Bündeln")
    ap.add_argument("--strict", action="store_true",
                    help="übersprungene Prüfungen als Fehler werten")
    ap.add_argument("--json", action="store_true", help="Ergebnis zusätzlich als JSON ausgeben")
    args = ap.parse_args(argv)

    try:
        from src import VERSION
    except Exception as exc:  # pragma: no cover
        print(f"src ist nicht importierbar: {exc}")
        return 1

    print("=" * 78)
    print(f"  lake_finder {VERSION} — Deployment-Rauchtest")
    print(f"  Projektverzeichnis: {ROOT}")
    print("=" * 78)

    rep = Report(strict=args.strict)
    check_python(rep)
    check_imports(rep)
    cfg = check_config(rep)
    check_app_file(rep)
    check_mode(rep, cfg, args.data_dir)
    bundle = check_bundles(rep, cfg, args.data_dir)
    data = check_rescore(rep, cfg, bundle)
    check_map(rep, cfg, data)
    check_outputs(rep)

    print("=" * 78)
    total = len(rep.rows)
    if rep.failed:
        print(f"  ERGEBNIS: {rep.failed} von {total} Prüfungen fehlgeschlagen.")
    else:
        print(f"  ERGEBNIS: alle {total} Prüfungen bestanden"
              f" ({rep.warned} Warnung(en), {rep.skipped} übersprungen).")
    print("=" * 78)

    if args.json:
        print(json.dumps(
            {"version": VERSION, "failed": rep.failed, "warned": rep.warned,
             "skipped": rep.skipped,
             "rows": [{"status": s, "name": n, "detail": d} for s, n, d in rep.rows]},
            ensure_ascii=False, indent=2,
        ))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
