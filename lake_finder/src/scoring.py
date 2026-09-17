"""
scoring.py -- transparenter Wilderness-Score, harte Filter, Qualitaets-Flags.

Aufbau des Scores:

    base      = 100 * sum(w_i * ramp_i) / sum(w_i)      -> 0..100
    penalties = Summe der Abzuege (Punkte)
    score     = clamp(base - penalties, 0, 100)

Jede Komponente ist eine lineare Rampe zwischen zwei in der config.yaml
gesetzten Schwellen. ``[lo, hi]`` mit lo < hi heisst "mehr ist besser",
``[hi, lo]`` mit lo > hi heisst "weniger ist besser". Damit laesst sich
jedes Kriterium ohne Codeaenderung umgewichten oder abschalten.

Bewusste Eigenschaften:
  * Der Score haengt nie an einem einzigen Kriterium: die Gewichte gehen
    in einen gewichteten Mittelwert ein, und ein einzelner Ausreisser kann
    den Score nicht allein nach oben ziehen.
  * Fehlende Werte (z.B. Land Cover nicht lesbar) werden konservativ mit
    ``missing`` bewertet, NICHT mit dem Bestwert, und setzen ein Flag.
  * Harte Filter sind vom Score getrennt. Ein See, der durchfaellt, wird
    nicht geloescht, sondern markiert -- so bleibt sichtbar, warum.

Dieses Modul kommt ohne geopandas/shapely aus und ist damit isoliert
testbar.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

import pandas as pd

from .utils import clamp, ramp

LOG = logging.getLogger("lake_finder.scoring")


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------


def _get(row: Mapping[str, Any], key: str) -> Any:
    v = row.get(key, None)
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


# --------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------


def score_row(row: Mapping[str, Any], cfg: dict) -> dict[str, Any]:
    """Berechnet Score und Aufschluesselung fuer einen See."""
    scfg = cfg.get("scoring", {})
    comps: dict[str, dict] = scfg.get("components", {}) or {}
    pens: dict[str, dict] = scfg.get("penalties", {}) or {}

    breakdown: list[dict[str, Any]] = []
    wsum = 0.0
    acc = 0.0
    missing_components: list[str] = []

    for name, spec in comps.items():
        weight = float(spec.get("weight", 1.0))
        if weight <= 0:
            continue
        metric = spec.get("metric", name)
        lo, hi = spec.get("ramp", [0, 100])
        raw = _as_float(_get(row, metric))
        miss = float(spec.get("missing", 0.25))
        if raw is None:
            missing_components.append(metric)
            s = miss
        else:
            s = ramp(raw, float(lo), float(hi))
        acc += weight * s
        wsum += weight
        breakdown.append(
            {
                "kind": "component",
                "name": name,
                "metric": metric,
                "value": raw,
                "unit": spec.get("unit", ""),
                "weight": weight,
                "sub_score": round(s, 3),
                "points": round(100.0 * weight * s / max(wsum, 1e-9), 2),  # vorlaeufig
                "label": spec.get("label", name),
            }
        )

    base = 100.0 * acc / wsum if wsum else 0.0
    # Beitrag jeder Komponente am Endergebnis korrekt nachtragen
    for b in breakdown:
        if b["kind"] == "component":
            b["points"] = round(100.0 * b["weight"] * b["sub_score"] / wsum, 2) if wsum else 0.0

    penalty_total = 0.0
    for name, spec in pens.items():
        pts = 0.0
        detail: Any = None
        if "flag" in spec:
            flag = bool(_get(row, spec["flag"]))
            detail = flag
            if flag:
                pts = float(spec.get("points", 0.0))
        else:
            metric = spec.get("metric", name)
            val = _as_float(_get(row, metric))
            detail = val
            if val is not None:
                thr = float(spec.get("threshold", 0.0))
                over = max(0.0, val - thr)
                if "points_per_unit" in spec:
                    pts = over * float(spec["points_per_unit"])
                elif over > 0:
                    pts = float(spec.get("points", 0.0))
                pts = min(pts, float(spec.get("max_points", 1e9)))
        if pts:
            penalty_total += pts
            breakdown.append(
                {
                    "kind": "penalty",
                    "name": name,
                    "metric": spec.get("metric", spec.get("flag", name)),
                    "value": detail,
                    "points": -round(pts, 2),
                    "label": spec.get("label", name),
                }
            )

    score = clamp(base - penalty_total, 0.0, 100.0)
    return {
        "wilderness_score": round(score, 1),
        "score_base": round(base, 1),
        "score_penalties": round(penalty_total, 1),
        "score_breakdown": breakdown,
        "score_missing_metrics": missing_components,
    }


# --------------------------------------------------------------------------
# Harte Filter
# --------------------------------------------------------------------------


def check_hard_filters(row: Mapping[str, Any], cfg: dict) -> tuple[bool, list[str]]:
    """Prueft die konfigurierten Mindest-/Hoechstwerte. (bestanden?, Gruende)"""
    filters: dict[str, dict] = (cfg.get("scoring", {}) or {}).get("hard_filters", {}) or {}
    reasons: list[str] = []
    for name, spec in filters.items():
        if not spec or spec.get("enabled", True) is False:
            continue
        metric = spec.get("metric", name)
        val = _as_float(_get(row, metric))
        label = spec.get("label", metric)
        if val is None:
            if spec.get("fail_on_missing", False):
                reasons.append(f"{label}: Wert fehlt")
            continue
        if "min" in spec and val < float(spec["min"]):
            reasons.append(f"{label} {val:g} < {float(spec['min']):g}")
        if "max" in spec and val > float(spec["max"]):
            reasons.append(f"{label} {val:g} > {float(spec['max']):g}")
    return (not reasons), reasons


# --------------------------------------------------------------------------
# Datenqualitaet
# --------------------------------------------------------------------------


def quality_flags(row: Mapping[str, Any], cfg: dict) -> tuple[list[str], str]:
    """Erzeugt Datenqualitaets-Flags und eine grobe Vertrauensstufe.

    Wichtig: Ein guter Score bedeutet "in den verwendeten offenen Daten
    ist hier nichts Menschliches verzeichnet" -- nicht "hier ist definitiv
    nichts". Die Flags machen genau diesen Unterschied sichtbar.
    """
    flags: list[str] = []
    lc = cfg.get("landcover", {})

    if row.get("landcover_ok") is False:
        flags.append("landcover_fehlt")

    built = _as_float(_get(row, "built_up_percent"))
    ref = int(_get(row, "landcover_ref_distance_m") or 1000)
    b_count = _as_float(_get(row, f"building_count_{ref}m"))
    if built is not None and b_count is not None:
        if built >= 0.5 and b_count == 0:
            flags.append("osm_luecke_moeglich")  # Raster sieht Bebauung, OSM nicht
        if built < 0.05 and b_count and b_count > 5:
            flags.append("landcover_underestimates_development")

    # Mehrquellen-Gebaeude: widersprechen sich OSM und Overture/amtliche Daten?
    if bool(_get(row, "building_sources_disagree")):
        flags.append("building_sources_disagree")
    n_src = _as_float(_get(row, "building_source_count"))
    if n_src is not None and n_src <= 1:
        flags.append("nur_eine_gebaeudequelle")
    if _get(row, "terrain_ok") is False:
        flags.append("gelaende_fehlt")
    if _get(row, "shore_segments_ok") is False:
        flags.append("ufersegmente_fehlen")
    dem_res = _as_float(_get(row, "dem_resolution_m"))
    if dem_res is not None and dem_res > 5:
        flags.append("dgm_zu_grob_fuer_zeltflaeche")

    nodata = _as_float(_get(row, f"nodata_percent_{ref}m"))
    if nodata is not None and nodata > 5:
        flags.append("raster_luecken")

    valid = _as_float(_get(row, f"valid_pixels_{ref}m"))
    if valid is not None and valid < 500:
        flags.append("wenige_pixel")

    censored = [
        k.replace("distance_", "").replace("_censored", "")
        for k, v in row.items()
        if k.endswith("_censored") and bool(v)
    ]
    if censored:
        flags.append("distanz_abgeschnitten")

    area = _as_float(_get(row, "area_ha"))
    if area is not None and area < 2:
        flags.append("kleiner_see_raster_grob")  # 10-m-Raster, wenige Pixel im Ufergürtel

    if str(lc.get("year", 2021)) and int(lc.get("year", 2021)) <= 2021:
        flags.append("landcover_stand_2021")

    hard = {
        "landcover_fehlt",
        "raster_luecken",
        "osm_luecke_moeglich",
        "building_sources_disagree",
        "gelaende_fehlt",
    }
    soft = {
        "wenige_pixel",
        "kleiner_see_raster_grob",
        "landcover_underestimates_development",
        "nur_eine_gebaeudequelle",
        "ufersegmente_fehlen",
        "dgm_zu_grob_fuer_zeltflaeche",
    }
    if hard & set(flags):
        conf = "niedrig"
    elif soft & set(flags):
        conf = "mittel"
    else:
        conf = "hoch"
    return flags, conf


# --------------------------------------------------------------------------
# Tabellenweit
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Getrennte Teil-Scores
# --------------------------------------------------------------------------

SCORE_BLOCKS: tuple[str, ...] = (
    "wilderness",
    "visual_seclusion",
    "access",
    "privacy_exposure",
    "campsite_suitability",
    "safety",
)

# Standardgewichte fuer den Gesamtscore, falls die config keine nennt.
DEFAULT_OVERALL_WEIGHTS = {
    "wilderness": 2.0,
    "visual_seclusion": 2.0,
    "campsite_suitability": 1.5,
    "privacy_exposure": 1.0,
    "access": 1.0,
    "safety": 1.0,
}


def block_config(cfg: dict, block: str) -> dict:
    """Konfiguration eines Score-Blocks, mit Rueckfall auf das alte Schema.

    Ohne ``scoring.scores.wilderness`` benutzt der Wilderness-Block weiter
    ``scoring.components`` / ``scoring.penalties`` -- alte config.yaml-Dateien
    liefern damit exakt dieselben Zahlen wie vorher.
    """
    scfg = cfg.get("scoring", {}) or {}
    scores = scfg.get("scores", {}) or {}
    if block in scores and scores[block]:
        return scores[block] or {}
    if block == "wilderness":
        return {
            "components": scfg.get("components", {}) or {},
            "penalties": scfg.get("penalties", {}) or {},
        }
    return {}


def score_block(row: Mapping[str, Any], cfg: dict, block: str) -> dict[str, Any]:
    """Einen Teil-Score rechnen. Ohne Konfiguration: alles None (nicht 0)."""
    bcfg = block_config(cfg, block)
    if not (bcfg.get("components") or bcfg.get("penalties")):
        return {
            "score": None,
            "base": None,
            "penalties": None,
            "breakdown": [],
            "missing": [],
            "configured": False,
        }
    # Wie viel des Komponentengewichts ist ueberhaupt mit Werten belegt?
    # Ein Block, dessen Metriken zum grossen Teil fehlen, liefert None --
    # nicht eine kleine Zahl aus lauter missing-Defaults. "Nicht gerechnet"
    # ist etwas anderes als "schlecht", und ein Spot-Score auf Seenebene,
    # wo es noch gar keinen Spot gibt, waere schlicht erfunden.
    comps = bcfg.get("components", {}) or {}
    total_w = have_w = 0.0
    for spec in comps.values():
        w = float(spec.get("weight", 1.0))
        if w <= 0:
            continue
        total_w += w
        if _as_float(_get(row, spec.get("metric", ""))) is not None:
            have_w += w
    share = (have_w / total_w) if total_w else 0.0
    min_share = float(
        (cfg.get("scoring", {}) or {}).get("min_data_share", 0.5)
    )
    if total_w > 0 and share < min_share:
        return {
            "score": None,
            "base": None,
            "penalties": None,
            "breakdown": [],
            "missing": [
                spec.get("metric", n)
                for n, spec in comps.items()
                if _as_float(_get(row, spec.get("metric", ""))) is None
            ],
            "configured": True,
            "insufficient_data": True,
            "data_share": round(share, 3),
        }

    res = score_row(row, {"scoring": bcfg})
    return {
        "score": res["wilderness_score"],
        "base": res["score_base"],
        "penalties": res["score_penalties"],
        "breakdown": res["score_breakdown"],
        "missing": res["score_missing_metrics"],
        "configured": True,
        "insufficient_data": False,
        "data_share": round(share, 3),
    }


def data_confidence_score(row: Mapping[str, Any], cfg: dict) -> dict[str, Any]:
    """Vertrauen in die Datengrundlage, 0-100, getrennt von allen Sachscores.

    Absichtlich KEIN Faktor auf die anderen Scores: ein Platz mit Score 92
    und Confidence 61 ist nicht dasselbe wie ein Platz mit Score 61. Der
    Nutzer soll beide Zahlen sehen und selbst entscheiden.
    """
    from .filters import data_availability

    ccfg = (cfg.get("scoring", {}) or {}).get("confidence", {}) or {}
    start = float(ccfg.get("start", 100.0))
    flag_pen: dict[str, float] = ccfg.get("flag_penalties", {}) or {
        "landcover_fehlt": 40,
        "gelaende_fehlt": 25,
        "osm_luecke_moeglich": 25,
        "building_sources_disagree": 20,
        "raster_luecken": 15,
        "ufersegmente_fehlen": 15,
        "landcover_underestimates_development": 12,
        "nur_eine_gebaeudequelle": 10,
        "wenige_pixel": 10,
        "kleiner_see_raster_grob": 8,
        "dgm_zu_grob_fuer_zeltflaeche": 8,
        "landcover_stand_2021": 5,
        "distanz_abgeschnitten": 3,
    }
    per_missing_source = float(ccfg.get("missing_source_penalty", 12.0))

    flags = row.get("quality_flags")
    if not isinstance(flags, (list, tuple)):
        flags, _ = quality_flags(row, cfg)

    score = start
    reasons: list[str] = []
    for f in flags:
        p = float(flag_pen.get(f, 0.0))
        if p:
            score -= p
            reasons.append(f"{f} -{p:g}")

    # Nur Quellen bestrafen, die laut Konfiguration ueberhaupt gebraucht
    # werden. Eine abgeschaltete Stufe ist keine Datenluecke -- sonst haette
    # jeder See dauerhaft Abzug fuer eine Gelaendeanalyse, die gar nicht
    # angefordert wurde.
    expected = {
        "landcover": True,
        "buildings": True,
        "osm_features": True,
        "shore_segments": bool((cfg.get("shore", {}) or {}).get("enabled", True)),
        "travel": bool((cfg.get("travel", {}) or {}).get("enabled", True))
        or bool((cfg.get("access", {}) or {}).get("enabled", True)),
        "terrain": bool((cfg.get("camp", {}) or {}).get("enabled", False)),
    }
    avail = data_availability(row, cfg)
    for src, ok in avail.items():
        if ok or not expected.get(src, True):
            continue
        score -= per_missing_source
        reasons.append(f"Quelle {src} fehlt -{per_missing_source:g}")

    return {
        "data_confidence_score": round(clamp(score, 0.0, 100.0), 1),
        "confidence_reasons": reasons,
        "sources_available": {k: bool(v) for k, v in avail.items()},
    }


def profile_weights(cfg: dict, profile: str | None = None) -> dict[str, float]:
    """Gewichte fuer den Gesamtscore -- Profile aendern nur diese Gewichte."""
    ocfg = (cfg.get("scoring", {}) or {}).get("overall", {}) or {}
    weights = dict(ocfg.get("weights", {}) or DEFAULT_OVERALL_WEIGHTS)
    if profile:
        profiles = ocfg.get("profiles", {}) or {}
        if profile in profiles:
            weights.update({k: float(v) for k, v in (profiles[profile] or {}).items()})
        else:
            LOG.warning("Unbekanntes Profil '%s' - benutze Standardgewichte.", profile)
    return {k: float(v) for k, v in weights.items() if float(v) > 0}


def overall_spot_score(
    scores: Mapping[str, Any], cfg: dict, profile: str | None = None
) -> dict[str, Any]:
    """Gewichtetes Mittel der vorhandenen Teil-Scores. Ohne data_confidence.

    ``overall_coverage`` sagt, wie viel des Gesamtgewichts tatsaechlich mit
    Werten belegt war. Ein Gesamtscore aus zwei von sechs Bausteinen ist
    kein vollwertiger Gesamtscore, und das soll sichtbar bleiben.
    """
    weights = profile_weights(cfg, profile)
    acc = 0.0
    wsum = 0.0
    used: dict[str, float] = {}
    for block, w in weights.items():
        v = scores.get(f"{block}_score")
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        acc += w * float(v)
        wsum += w
        used[block] = w
    total_w = sum(weights.values()) or 1.0
    if wsum <= 0:
        return {"overall_spot_score": None, "overall_coverage": 0.0, "overall_profile": profile or "default"}
    return {
        "overall_spot_score": round(acc / wsum, 1),
        "overall_coverage": round(100.0 * wsum / total_w, 1),
        "overall_profile": profile or "default",
        "overall_weights_used": used,
    }


def score_all(row: Mapping[str, Any], cfg: dict, profile: str | None = None) -> dict[str, Any]:
    """Alle Teil-Scores, Confidence und Gesamtscore fuer eine Zeile."""
    out: dict[str, Any] = {}
    breakdowns: dict[str, list] = {}
    for block in SCORE_BLOCKS:
        res = score_block(row, cfg, block)
        out[f"{block}_score"] = res["score"]
        breakdowns[block] = res["breakdown"]
        if block == "wilderness":
            # Rueckwaertskompatible Einzelspalten
            out["score_base"] = res["base"]
            out["score_penalties"] = res["penalties"]
            out["score_breakdown"] = res["breakdown"]
            out["score_missing_metrics"] = res["missing"]
    out["score_breakdowns"] = breakdowns
    out.update(data_confidence_score(row, cfg))
    out.update(overall_spot_score(out, cfg, profile))
    return out


# --------------------------------------------------------------------------
# Tabellenweit
# --------------------------------------------------------------------------

_TABLE_COLUMNS = (
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
    "score_breakdowns",
    "score_missing_metrics",
    "confidence_reasons",
    "sources_available",
    "passes_filters",
    "filter_status",
    "filter_reasons",
    "filter_fail_reasons",
    "filter_unknown_reasons",
    "filter_checks",
    "quality_flags",
    "confidence",
)


def score_table(df: pd.DataFrame, cfg: dict, profile: str | None = None) -> pd.DataFrame:
    """Alle Scores, die harten Filter (PASS/FAIL/UNKNOWN) und Qualitaets-Flags.

    ``wilderness_score``, ``passes_filters``, ``filter_reasons``,
    ``quality_flags`` und ``confidence`` behalten ihre alten Namen und
    Bedeutungen; alles Neue kommt als zusaetzliche Spalten dazu.
    """
    from . import filters as filt

    if df.empty:
        for c in _TABLE_COLUMNS:
            df[c] = pd.Series(dtype="object")
        return df

    recs = df.to_dict("records")
    collected: dict[str, list] = {c: [] for c in _TABLE_COLUMNS}

    for row in recs:
        # Qualitaets-Flags zuerst: die Confidence baut darauf auf.
        fl, cf = quality_flags(row, cfg)
        row = dict(row)
        row["quality_flags"] = fl

        s = score_all(row, cfg, profile)
        outcome = filt.evaluate(row, cfg)
        cols = filt.outcome_columns(outcome)

        for c in _TABLE_COLUMNS:
            if c == "quality_flags":
                collected[c].append(fl)
            elif c == "confidence":
                collected[c].append(cf)
            elif c in cols:
                collected[c].append(cols[c])
            else:
                collected[c].append(s.get(c))

    out = df.copy()
    for c, vals in collected.items():
        out[c] = vals

    n_pass = int(out["passes_filters"].sum())
    n_fail = int((out["filter_status"] == filt.FAIL).sum())
    n_unknown = int((out["filter_status"] == filt.UNKNOWN).sum())
    LOG.info(
        "Scores berechnet. Filter: %d PASS, %d FAIL, %d UNKNOWN (davon %d als bestanden gewertet). "
        "Median Wildnis-Score %.1f.",
        int((out["filter_status"] == filt.PASS).sum()),
        n_fail,
        n_unknown,
        n_pass - int((out["filter_status"] == filt.PASS).sum()),
        float(pd.to_numeric(out["wilderness_score"], errors="coerce").median()),
    )
    sort_col = "overall_spot_score" if out["overall_spot_score"].notna().any() else "wilderness_score"
    return out.sort_values(sort_col, ascending=False, na_position="last").reset_index(drop=True)


def explain(row: Mapping[str, Any], top: int = 8) -> str:
    """Kurze Textbegruendung des Scores -- fuer Popup und CLI."""
    bd: Sequence[dict] = row.get("score_breakdown") or []
    if not bd:
        return "Keine Aufschluesselung verfuegbar."
    items = sorted(bd, key=lambda b: -abs(float(b.get("points", 0))))[:top]
    lines = []
    for b in items:
        val = b.get("value")
        vs = f"{val:.1f}" if isinstance(val, float) else str(val)
        lines.append(f"  {b.get('label', b['name']):<34} {vs:>10}  {float(b['points']):+6.1f}")
    return "\n".join(lines)
