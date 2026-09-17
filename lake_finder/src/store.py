"""
store.py -- vorberechnete Ergebnisbuendel lesen und schreiben.

Ein Buendel ist ein Verzeichnis unter ``data/processed/<slug>/``:

    manifest.json      Region, Zeitstempel, Zaehlungen, Versionen, Quellen
    app_data.json      kartenfertige FeatureCollection mit ALLEN Metriken
    results.geojson    vollstaendige Ergebnisse fuer GIS (optional)
    results.csv        Tabelle (optional)
    spots.csv          Campingflaechen (optional)

Der Punkt an ``app_data.json``: darin stecken die Geometrien bereits
vereinfacht, die Pufferringe bereits gerechnet und **alle** Metriken als
Properties. Damit laesst sich im precomputed-Modus

  * die Karte neu bauen,
  * mit anderen Schwellen neu filtern,
  * mit anderen Gewichten neu bewerten,

und zwar ausschliesslich mit ``json`` und ``pandas`` -- ohne geopandas,
ohne shapely, ohne eine einzige Netzwerkanfrage. Das ist genau das, was
eine gehostete App beim Oeffnen braucht.

Lesen und Schreiben sind getrennt: ``write_bundle`` braucht den vollen
Geo-Stack (es kommt aus der Analyse), alles Lesende nicht.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import BUNDLE_FORMAT_VERSION, VERSION

LOG = logging.getLogger("lake_finder.store")

MANIFEST = "manifest.json"
APP_DATA = "app_data.json"
RESULTS_GEOJSON = "results.geojson"
RESULTS_CSV = "results.csv"
SPOTS_CSV = "spots.csv"

REQUIRED_FEATURE_PROPS = ("lake_uid", "latitude", "longitude", "wilderness_score")


def slugify(text: str) -> str:
    s = (text or "region").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "region"


# ==========================================================================
# Lesen -- reines Python
# ==========================================================================


@dataclass
class Bundle:
    """Ein geladenes Ergebnisbuendel."""

    path: Path
    manifest: dict
    data: dict
    spots: Any = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return str(self.manifest.get("slug", self.path.name))

    @property
    def label(self) -> str:
        return str(self.manifest.get("label", self.slug))

    @property
    def created(self) -> str:
        return str(self.manifest.get("created_utc", "unbekannt"))

    @property
    def is_demo(self) -> bool:
        return bool(self.manifest.get("is_demo", False))

    @property
    def features(self) -> list[dict]:
        return self.data.get("features", []) or []

    @property
    def n_lakes(self) -> int:
        return len(self.features)

    def describe(self) -> str:
        c = self.manifest.get("counts", {}) or {}
        parts = [f"{self.label}", f"{self.n_lakes} Seen"]
        if c.get("passes") is not None:
            parts.append(f"{c['passes']} bestanden (Stand der Vorberechnung)")
        parts.append(f"berechnet {self.created[:16].replace('T', ' ')} UTC")
        parts.append(f"lake_finder {self.manifest.get('lake_finder_version', '?')}")
        return " · ".join(parts)


def bundle_dirs(directory: str | Path) -> list[Path]:
    """Alle Buendelverzeichnisse. Kein Laden, nur Dateisystem."""
    d = Path(directory)
    if not d.exists():
        return []
    out = []
    if (d / MANIFEST).exists():
        out.append(d)
    out.extend(sorted(p.parent for p in d.glob("*/" + MANIFEST)))
    return out


def read_manifest(path: str | Path) -> dict | None:
    """Manifest eines Buendels lesen. Nie eine Ausnahme nach aussen."""
    p = Path(path)
    mp = p if p.name == MANIFEST else p / MANIFEST
    try:
        with open(mp, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        m.setdefault("slug", mp.parent.name)
        m["_path"] = str(mp.parent)
        return m
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Manifest %s unlesbar: %s", mp, exc)
        return None


def list_bundles(directory: str | Path) -> list[dict]:
    """Manifeste aller Buendel, neuestes zuerst."""
    out = [m for d in bundle_dirs(directory) if (m := read_manifest(d)) is not None]
    out.sort(key=lambda m: str(m.get("created_utc", "")), reverse=True)
    return out


def load_bundle(path: str | Path) -> Bundle:
    """Laedt Manifest und Kartendaten eines Buendels."""
    p = Path(path)
    manifest = read_manifest(p)
    if manifest is None:
        raise FileNotFoundError(f"Kein Manifest in {p}")

    data_file = p / str((manifest.get("files", {}) or {}).get("app_data", APP_DATA))
    with open(data_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    spots: Any = {}
    spot_file = p / str((manifest.get("files", {}) or {}).get("spots_json", "spots.json"))
    if spot_file.exists():
        try:
            with open(spot_file, "r", encoding="utf-8") as fh:
                spots = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            LOG.warning("Spots in %s unlesbar: %s", spot_file, exc)

    return Bundle(path=p, manifest=manifest, data=data, spots=spots)


def validate_bundle(bundle: Bundle) -> list[str]:
    """Prueft ein Buendel auf Brauchbarkeit. Liefert die Probleme als Text."""
    problems: list[str] = []
    fmt = bundle.manifest.get("format_version")
    if fmt is None:
        problems.append("manifest.json ohne format_version")
    elif int(fmt) > BUNDLE_FORMAT_VERSION:
        problems.append(
            f"Bündelformat {fmt} ist neuer als dieser Code ({BUNDLE_FORMAT_VERSION}). "
            "lake_finder aktualisieren."
        )

    if bundle.data.get("type") != "FeatureCollection":
        problems.append("app_data.json ist keine FeatureCollection")
    feats = bundle.features
    if not feats:
        problems.append("app_data.json enthält keine Seen")
        return problems

    missing = [
        k for k in REQUIRED_FEATURE_PROPS
        if k not in (feats[0].get("properties") or {})
    ]
    if missing:
        problems.append("Pflicht-Properties fehlen: " + ", ".join(missing))
    if not feats[0].get("geometry"):
        problems.append("Features ohne Geometrie")
    return problems


# ==========================================================================
# Neu bewerten -- reines pandas, kein Geo-Stack
# ==========================================================================


def bundle_dataframe(bundle: Bundle):
    """Properties des Buendels als DataFrame -- Grundlage fuer neues Scoring."""
    import pandas as pd

    rows = [dict(f.get("properties") or {}) for f in bundle.features]
    return pd.DataFrame(rows)


def rescore_bundle(bundle: Bundle, cfg: dict, profile: str | None = None) -> tuple[dict, Any]:
    """Bewertet ein Buendel mit der aktuellen config neu.

    Rueckgabe: (FeatureCollection mit aktualisierten Properties, DataFrame).

    Hier passiert der eigentliche Trick des precomputed-Modus: die teuren
    Rohdaten sind eingefroren, Schwellen und Gewichte aber nicht. Der Nutzer
    schiebt Regler, und es wird nur gerechnet -- nichts geholt.
    """
    from . import scoring as scoring_mod

    df = bundle_dataframe(bundle)
    if df.empty:
        return bundle.data, df

    scored = scoring_mod.score_table(df, cfg, profile)
    by_uid = {}
    for rec in scored.to_dict("records"):
        uid = rec.get("lake_uid")
        if uid is not None:
            by_uid[uid] = rec

    out_feats = []
    for f in bundle.features:
        props = dict(f.get("properties") or {})
        rec = by_uid.get(props.get("lake_uid"))
        if rec:
            props.update({k: _plain(v) for k, v in rec.items()})
        out_feats.append({**f, "properties": props})

    return {"type": "FeatureCollection", "features": out_feats}, scored


def _plain(v: Any) -> Any:
    """numpy/pandas-Typen zu JSON-faehigen Werten."""
    try:
        import numpy as np

        if isinstance(v, np.generic):
            v = v.item()
        if isinstance(v, float) and np.isnan(v):
            return None
    except ImportError:  # pragma: no cover
        pass
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def sort_features(data: dict, key: str = "overall_spot_score", fallback: str = "wilderness_score") -> dict:
    """Sortiert die Features absteigend nach einem Score."""
    feats = list(data.get("features", []))

    def val(f):
        p = f.get("properties") or {}
        v = p.get(key)
        if v is None:
            v = p.get(fallback)
        return -(float(v) if isinstance(v, (int, float)) else -1e9)

    feats.sort(key=val)
    return {"type": "FeatureCollection", "features": feats}


# ==========================================================================
# Schreiben -- braucht den vollen Geo-Stack
# ==========================================================================


def write_bundle(
    scored,
    spots,
    cfg: dict,
    ctx: dict | None = None,
    out_dir: str | Path | None = None,
    slug: str | None = None,
    label: str | None = None,
    is_demo: bool = False,
) -> Path:
    """Schreibt ein vorberechnetes Buendel aus einem fertigen Lauf."""
    from . import map as map_mod

    ctx = ctx or {}
    base = Path(out_dir or ((cfg.get("deployment", {}) or {}).get("data_dir", "data/processed")))
    slug = slug or slugify(str(ctx.get("region") or label or "region"))
    target = base / slug
    target.mkdir(parents=True, exist_ok=True)

    # Kartenfertige Daten MIT allen Metriken -- Grundlage fuers Neubewerten
    data = map_mod.build_app_geojson(scored, cfg, full_props=True)
    with open(target / APP_DATA, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)

    files = {"app_data": APP_DATA}

    # Vollstaendige Ergebnisse zusaetzlich, damit das Buendel auch fuer
    # QGIS und Tabellenarbeit taugt.
    try:
        import copy as _copy

        cfg_out = _copy.deepcopy(cfg)
        cfg_out.setdefault("output", {})["dir"] = str(target)
        written = map_mod.write_tables(scored, cfg_out)
        for key, path in written.items():
            files[key] = Path(path).name
    except Exception as exc:  # pragma: no cover - Ausgabe ist Beiwerk
        LOG.warning("results.csv/geojson im Bündel nicht geschrieben: %s", exc)

    n_spots = 0
    if spots is not None and len(spots):
        n_spots = len(spots)
        try:
            spot_records = map_mod.spots_by_lake(spots, limit_per_lake=50)
            with open(target / "spots.json", "w", encoding="utf-8") as fh:
                json.dump(spot_records, fh, ensure_ascii=False)
            files["spots_json"] = "spots.json"
        except Exception as exc:  # pragma: no cover
            LOG.warning("Spots im Bündel nicht geschrieben: %s", exc)

    passes = None
    try:
        passes = int(scored["passes_filters"].sum())
    except Exception:  # pragma: no cover
        pass

    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "lake_finder_version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slug": slug,
        "label": label or str(ctx.get("region") or slug),
        "region_bbox": ctx.get("bbox"),
        "metric_crs": ctx.get("metric_crs"),
        "is_demo": bool(is_demo),
        "counts": {"lakes": len(data.get("features", [])), "passes": passes, "spots": n_spots},
        "sources": {
            "buildings": ctx.get("building_sources", []),
            "buildings_failed": list((ctx.get("building_sources_failed") or {}).keys()),
            "landcover": f"ESA WorldCover {(cfg.get('landcover', {}) or {}).get('year', 2021)} "
                         f"{(cfg.get('landcover', {}) or {}).get('version', 'v200')}",
        },
        "files": files,
    }
    with open(target / MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    LOG.info(
        "Vorberechnetes Bündel geschrieben: %s (%d Seen, %d Spots)",
        target, manifest["counts"]["lakes"], n_spots,
    )
    return target
