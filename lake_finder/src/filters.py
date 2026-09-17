"""
filters.py -- Harte Filter mit drei Zustaenden: PASS / FAIL / UNKNOWN.

Warum das noetig wurde
----------------------
Im echten Stechlin-Lauf bestand der Kleine Zermittensee die harten Filter,
obwohl 60 m vom Ufer ein Gebaeude steht, ein Campingplatz, ein Wohngebiet
und ein Parkplatz in Reichweite liegen. Ursache: die alte Filterliste
pruefte gar keinen Gebaeudeabstand und keine has_*-Flags, und ein
fehlender Wert galt stillschweigend als bestanden.

Beides ist hier korrigiert:

* Es gibt Checks auf Distanzen, Zaehlungen je Ring UND auf Ausschluss-
  Flags (Wohngebiet, Campingplatz, Marina, Unterkunft, Parkplatz).
* Ein Check kennt drei Ergebnisse. UNKNOWN ist kein PASS. Ob UNKNOWN am
  Ende durchfaellt, entscheidet ``filters.fail_on_missing_critical_data``
  (Standard: true -- fuer eine hochwertige Suche will man keine Treffer,
  deren Grundlage fehlt).

Datenverfuegbarkeit
-------------------
Ein Check kann ueber ``requires: [buildings]`` an eine Datenquelle
gekoppelt werden. Fehlt die Quelle fuer diesen See (z. B. Land Cover nicht
lesbar), ist der Check UNKNOWN -- unabhaengig davon, ob zufaellig ein Wert
in der Zeile steht.

Das Modul kommt ohne geopandas/shapely aus und ist isoliert testbar.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

LOG = logging.getLogger("lake_finder.filters")

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

# Datenquellen, an die sich Checks binden koennen
SOURCE_LANDCOVER = "landcover"
SOURCE_BUILDINGS = "buildings"
SOURCE_OSM = "osm_features"
SOURCE_SHORE = "shore_segments"
SOURCE_TRAVEL = "travel"
SOURCE_TERRAIN = "terrain"

ALL_SOURCES = (
    SOURCE_LANDCOVER,
    SOURCE_BUILDINGS,
    SOURCE_OSM,
    SOURCE_SHORE,
    SOURCE_TRAVEL,
    SOURCE_TERRAIN,
)


# --------------------------------------------------------------------------
# Ergebnisobjekte
# --------------------------------------------------------------------------


@dataclass
class FilterCheck:
    name: str
    label: str
    status: str
    value: Any = None
    detail: str = ""
    critical: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "value": self.value,
            "detail": self.detail,
            "critical": self.critical,
        }


@dataclass
class FilterOutcome:
    status: str = PASS
    passed: bool = True
    checks: list[FilterCheck] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)
    unknown_reasons: list[str] = field(default_factory=list)

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)

    @property
    def n_unknown(self) -> int:
        return sum(1 for c in self.checks if c.status == UNKNOWN)


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _get(row: Mapping[str, Any], key: str) -> Any:
    v = row.get(key, None)
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def data_availability(row: Mapping[str, Any], cfg: dict | None = None) -> dict[str, bool]:
    """Welche Datengrundlagen liegen fuer diesen See vor?

    Bewusst konservativ: Wenn ein ``*_ok``-Flag explizit auf False steht,
    gilt die Quelle als nicht verfuegbar. Fehlt das Flag ganz, wird aus
    dem Vorhandensein charakteristischer Spalten geschlossen -- und wenn
    auch die fehlen, ist die Quelle nicht verfuegbar (nicht "vorhanden").
    """
    def flag(name: str, probes: Iterable[str]) -> bool:
        v = _get(row, name)
        if v is not None:
            return bool(v)
        return any(_get(row, p) is not None for p in probes)

    return {
        SOURCE_LANDCOVER: flag(
            "landcover_ok", ("shore_tree_cover", "tree_cover_500m", "built_up_percent")
        ),
        SOURCE_BUILDINGS: flag(
            "building_data_ok", ("building_count_500m", "distance_nearest_building_m")
        ),
        SOURCE_OSM: flag(
            "osm_features_ok", ("distance_major_road_m", "has_campsite", "has_residential")
        ),
        SOURCE_SHORE: flag(
            "shore_segments_ok", ("shore_segment_p10_score", "developed_shore_fraction")
        ),
        SOURCE_TRAVEL: flag("travel_ok", ("drive_time_min",)),
        SOURCE_TERRAIN: flag("terrain_ok", ("mean_slope_deg", "dryness_score")),
    }


# --------------------------------------------------------------------------
# Migration alter Konfigurationen
# --------------------------------------------------------------------------

_LEGACY_SOURCE_HINTS = {
    "tree_cover": SOURCE_LANDCOVER,
    "shore_tree": SOURCE_LANDCOVER,
    "built_up": SOURCE_LANDCOVER,
    "cropland": SOURCE_LANDCOVER,
    "natural_land": SOURCE_LANDCOVER,
    "building": SOURCE_BUILDINGS,
    "drive_time": SOURCE_TRAVEL,
    "shore_segment": SOURCE_SHORE,
    "developed_shore": SOURCE_SHORE,
    "slope": SOURCE_TERRAIN,
    "dryness": SOURCE_TERRAIN,
}


def _infer_source(metric: str) -> list[str]:
    for hint, src in _LEGACY_SOURCE_HINTS.items():
        if hint in metric:
            return [src]
    return [SOURCE_OSM]


def collect_checks(cfg: dict) -> dict[str, dict]:
    """Liefert die Checkdefinitionen -- neu (filters.checks) und alt gemischt.

    ``scoring.hard_filters`` aus aelteren Konfigurationen bleibt gueltig;
    fehlende ``requires``-Angaben werden aus dem Metriknamen abgeleitet.
    Definitionen unter ``filters.checks`` haben bei Namensgleichheit Vorrang.
    """
    merged: dict[str, dict] = {}
    legacy = (cfg.get("scoring", {}) or {}).get("hard_filters", {}) or {}
    for name, spec in legacy.items():
        if not isinstance(spec, dict):
            continue
        s = dict(spec)
        s.setdefault("metric", name)
        s.setdefault("requires", _infer_source(str(s["metric"])))
        s.setdefault("critical", True)
        s.setdefault("legacy", True)
        merged[name] = s
    for name, spec in ((cfg.get("filters", {}) or {}).get("checks", {}) or {}).items():
        if not isinstance(spec, dict):
            continue
        s = dict(spec)
        if "flag" not in s:
            s.setdefault("metric", name)
            s.setdefault("requires", _infer_source(str(s["metric"])))
        else:
            s.setdefault("requires", [SOURCE_OSM])
        s.setdefault("critical", True)
        merged[name] = s
    return merged


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------


def evaluate_check(
    name: str,
    spec: dict,
    row: Mapping[str, Any],
    avail: Mapping[str, bool],
) -> FilterCheck:
    """Ein einzelner Check. Gibt PASS, FAIL oder UNKNOWN zurueck."""
    label = str(spec.get("label", spec.get("metric", spec.get("flag", name))))
    critical = bool(spec.get("critical", True))

    missing_sources = [s for s in spec.get("requires", []) if not avail.get(s, False)]
    if missing_sources:
        return FilterCheck(
            name=name,
            label=label,
            status=UNKNOWN,
            detail=f"Datengrundlage fehlt: {', '.join(missing_sources)}",
            critical=critical,
        )

    # Flag-Check (Ausschlusskriterium)
    if "flag" in spec:
        raw = _get(row, str(spec["flag"]))
        if raw is None:
            return FilterCheck(name, label, UNKNOWN, None, "Flag nicht gesetzt", critical)
        must_be = bool(spec.get("must_be", False))
        ok = bool(raw) == must_be
        return FilterCheck(
            name,
            label,
            PASS if ok else FAIL,
            bool(raw),
            "" if ok else f"{label}: vorhanden",
            critical,
        )

    metric = str(spec.get("metric", name))
    val = _num(_get(row, metric))
    if val is None:
        return FilterCheck(name, label, UNKNOWN, None, f"{label}: Wert fehlt", critical)

    # Zensierte Distanzen sind nach unten sicher: ">= cap" erfuellt jedes
    # Mindestkriterium, verletzt aber kein Hoechstkriterium.
    # Zensur-Flags heissen je nach Erzeuger "<metrik>_censored" oder
    # "<metrik ohne _m>_censored" -- beide Schreibweisen akzeptieren.
    base = metric[:-2] if metric.endswith("_m") else metric
    censored = bool(_get(row, f"{metric}_censored") or _get(row, f"{base}_censored"))

    problems = []
    if "min" in spec and val < float(spec["min"]):
        problems.append(f"{label} {val:g} < {float(spec['min']):g}")
    if "max" in spec and val > float(spec["max"]):
        if censored and "distance" in metric:
            pass  # abgeschnittene Distanz kann ein Maximum nicht sinnvoll verletzen
        else:
            problems.append(f"{label} {val:g} > {float(spec['max']):g}")

    if problems:
        return FilterCheck(name, label, FAIL, val, "; ".join(problems), critical)
    return FilterCheck(name, label, PASS, val, "", critical)


def evaluate(row: Mapping[str, Any], cfg: dict) -> FilterOutcome:
    """Alle harten Filter auf eine Zeile anwenden."""
    fcfg = cfg.get("filters", {}) or {}
    fail_on_missing = bool(fcfg.get("fail_on_missing_critical_data", True))
    avail = data_availability(row, cfg)
    checks_def = collect_checks(cfg)

    out = FilterOutcome()
    for name, spec in checks_def.items():
        if spec.get("enabled", True) is False:
            continue
        # Legacy-Option aus scoring.hard_filters
        if spec.get("legacy") and spec.get("fail_on_missing") is True:
            spec = dict(spec, critical=True)
        chk = evaluate_check(name, spec, row, avail)
        out.checks.append(chk)
        if chk.status == FAIL:
            out.fail_reasons.append(chk.detail or chk.label)
        elif chk.status == UNKNOWN and chk.critical:
            out.unknown_reasons.append(chk.detail or f"{chk.label}: unbekannt")

    if out.fail_reasons:
        out.status = FAIL
    elif out.unknown_reasons:
        out.status = UNKNOWN
    else:
        out.status = PASS

    out.passed = out.status == PASS or (out.status == UNKNOWN and not fail_on_missing)
    return out


def outcome_columns(outcome: FilterOutcome) -> dict[str, Any]:
    """Spalten fuer den Ergebnis-DataFrame (rueckwaertskompatibel)."""
    reasons = list(outcome.fail_reasons)
    reasons += [f"[unbekannt] {r}" for r in outcome.unknown_reasons]
    return {
        "passes_filters": outcome.passed,
        "filter_status": outcome.status,
        "filter_reasons": reasons,
        "filter_fail_reasons": list(outcome.fail_reasons),
        "filter_unknown_reasons": list(outcome.unknown_reasons),
        "filter_n_fail": outcome.n_fail,
        "filter_n_unknown": outcome.n_unknown,
        "filter_checks": [c.as_dict() for c in outcome.checks],
    }
