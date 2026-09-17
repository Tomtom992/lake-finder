"""
camp.py -- vom guten See zur konkreten Zeltfläche.

Pipeline (Phase 7 der Anforderung):

    guter See -> gute Ufersegmente -> Suchkorridor -> Gelände -> Kandidaten

Der entscheidende Unterschied zu "flacher Punkt gefunden": eine Zeltfläche
ist eine FLÄCHE. Ein 4x6-m-Zelt steht nicht, weil der Mittelpunkt eben ist,
sondern nur wenn der komplette Grundriss eben ist. Deshalb wird ein
rotierbares Rechteck geprüft, nicht eine einzelne Zelle.

Alles in diesem Modul arbeitet auf numpy-Rastern im metrischen CRS und ist
ohne GIS-Stack testbar.

Erlaubte Gebietskulisse
-----------------------
``allowed_mask`` ist ein boolesches Raster. Ist es gesetzt, liegen
Kandidaten ausschließlich darin. Es gibt bewusst KEINE eingebaute Annahme
darüber, wo man zelten darf -- die Kulisse kommt aus einer vom Nutzer
gepflegten ``allowed_area.geojson``. Ohne diese Datei erzeugt das Modul
Kandidatenflächen, die geeignet, aber nicht notwendigerweise erlaubt sind;
das steht so auch in jedem Ausgabedatensatz (``allowed_area_checked``).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Iterable, Sequence

import numpy as np

from .terrain import depression_depth, local_relief, roughness, slope_aspect

LOG = logging.getLogger("lake_finder.camp")

# Verbreitete Zeltgrundrisse (Breite x Länge in Metern)
TENT_PRESETS = {
    "3x4": (3.0, 4.0),
    "3x5": (3.0, 5.0),
    "4x6": (4.0, 6.0),
    "5x7": (5.0, 7.0),
}
DEFAULT_TENT = "4x6"


# ==========================================================================
# Zeltgrundriss
# ==========================================================================


def rect_mask(
    shape: tuple[int, int],
    center_rc: tuple[int, int],
    width_m: float,
    length_m: float,
    angle_deg: float,
    px: float,
) -> np.ndarray:
    """Boolesche Maske eines gedrehten Rechtecks um ``center_rc``.

    ``angle_deg`` ist die Drehung der Längsachse gegen die Rasterzeilen.
    Getestet wird über die Rücktransformation der Zellmittelpunkte in das
    Rechteckkoordinatensystem -- exakt, ohne Rasterisierungsartefakte an
    den Ecken.
    """
    h, w = shape
    r0, c0 = center_rc
    half_diag = 0.5 * math.hypot(width_m, length_m) / px + 2
    rmin = max(0, int(math.floor(r0 - half_diag)))
    rmax = min(h, int(math.ceil(r0 + half_diag)) + 1)
    cmin = max(0, int(math.floor(c0 - half_diag)))
    cmax = min(w, int(math.ceil(c0 + half_diag)) + 1)

    mask = np.zeros(shape, dtype=bool)
    if rmax <= rmin or cmax <= cmin:
        return mask

    rr = (np.arange(rmin, rmax)[:, None] - r0) * px
    cc = (np.arange(cmin, cmax)[None, :] - c0) * px
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    u = cc * ca + rr * sa          # entlang der Breite
    v = -cc * sa + rr * ca         # entlang der Länge
    # Halboffene Intervalle: sonst zaehlen beide Randzellen mit und die
    # Grundflaeche faellt um eine Zellbreite je Achse zu gross aus.
    inside = (
        (u >= -width_m / 2.0) & (u < width_m / 2.0)
        & (v >= -length_m / 2.0) & (v < length_m / 2.0)
    )
    mask[rmin:rmax, cmin:cmax] = inside
    return mask


def evaluate_footprint(
    dem: np.ndarray,
    slope: np.ndarray,
    mask: np.ndarray,
) -> dict[str, float]:
    """Kennzahlen des Zeltgrundrisses: Neigung und Mikrorelief."""
    if not mask.any():
        return {"cells": 0, "mean_slope_deg": float("nan"), "max_slope_deg": float("nan"),
                "relief_cm": float("nan"), "tilt_deg": float("nan")}
    z = dem[mask]
    s = slope[mask]
    return {
        "cells": int(mask.sum()),
        "mean_slope_deg": float(np.nanmean(s)),
        "max_slope_deg": float(np.nanmax(s)),
        "relief_cm": float((np.nanmax(z) - np.nanmin(z)) * 100.0),
        "tilt_deg": float(np.nanmean(s)),
    }


def best_tent_fit(
    dem: np.ndarray,
    px: float,
    center_rc: tuple[int, int],
    cfg: dict | None = None,
    slope: np.ndarray | None = None,
) -> dict[str, Any]:
    """Probiert mehrere Ausrichtungen und liefert die beste.

    ``tent_fit`` ist nur dann True, wenn der KOMPLETTE Grundriss die
    Schwellen einhält -- nicht der Mittelwert, sondern das Maximum der
    Neigung und das Relief über die gesamte Fläche.
    """
    cfg = cfg or {}
    ccfg = (cfg.get("camp", {}) or {})
    tent_key = str(ccfg.get("tent", DEFAULT_TENT))
    size = ccfg.get("tent_size_m")
    width, length = (
        (float(size[0]), float(size[1])) if size else TENT_PRESETS.get(tent_key, TENT_PRESETS[DEFAULT_TENT])
    )
    angle_step = float(ccfg.get("rotation_step_deg", 15.0))
    max_slope = float(ccfg.get("max_slope_deg", 5.0))
    max_relief_cm = float(ccfg.get("max_relief_cm", 25.0))
    min_cover = float(ccfg.get("min_footprint_cell_cover", 0.6))

    if slope is None:
        slope, _ = slope_aspect(dem, px)

    expected_cells = (width * length) / (px * px)
    best: dict[str, Any] | None = None
    for angle in np.arange(0.0, 180.0, angle_step):
        m = rect_mask(dem.shape, center_rc, width, length, float(angle), px)
        ev = evaluate_footprint(dem, slope, m)
        if ev["cells"] < max(1, min_cover * expected_cells):
            continue  # Grundriss ragt über den Rasterausschnitt hinaus
        ev["angle_deg"] = float(angle)
        ev["fits"] = bool(
            ev["max_slope_deg"] <= max_slope and ev["relief_cm"] <= max_relief_cm
        )
        key = (not ev["fits"], ev["max_slope_deg"], ev["relief_cm"])
        if best is None or key < (not best["fits"], best["max_slope_deg"], best["relief_cm"]):
            best = ev
    if best is None:
        return {
            "tent_fit": False,
            "tent_fit_failed": True,
            "best_tent_orientation_deg": None,
            "mean_slope_deg": None,
            "max_slope_deg": None,
            "relief_cm": None,
            "tent_size_m": [width, length],
            "reason": "Grundriss passt nicht in den Rasterausschnitt",
        }
    return {
        "tent_fit": bool(best["fits"]),
        "tent_fit_failed": not bool(best["fits"]),
        "best_tent_orientation_deg": round(best["angle_deg"], 1),
        "mean_slope_deg": round(best["mean_slope_deg"], 2),
        "max_slope_deg": round(best["max_slope_deg"], 2),
        "relief_cm": round(best["relief_cm"], 1),
        "tent_size_m": [width, length],
        "footprint_cells": best["cells"],
    }


# ==========================================================================
# Ebene Flächen
# ==========================================================================


def flat_patch(slope: np.ndarray, seed_rc: tuple[int, int], max_slope_deg: float) -> np.ndarray:
    """Zusammenhängende Fläche mit Neigung <= Schwelle um einen Startpunkt.

    Vier-Nachbarschaft: eine diagonale Verbindung über eine Rinne hinweg
    wäre für eine Liegefläche keine Verbindung.
    """
    h, w = slope.shape
    r0, c0 = seed_rc
    out = np.zeros((h, w), dtype=bool)
    if not (0 <= r0 < h and 0 <= c0 < w):
        return out
    ok = np.isfinite(slope) & (slope <= max_slope_deg)
    if not ok[r0, c0]:
        return out
    q = deque([(r0, c0)])
    out[r0, c0] = True
    while q:
        r, c = q.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < h and 0 <= cc < w and ok[rr, cc] and not out[rr, cc]:
                out[rr, cc] = True
                q.append((rr, cc))
    return out


def flat_area_m2(slope: np.ndarray, seed_rc: tuple[int, int], max_slope_deg: float, px: float) -> float:
    return float(flat_patch(slope, seed_rc, max_slope_deg).sum()) * px * px


# ==========================================================================
# Kandidatensuche
# ==========================================================================


def corridor_mask(dist_to_water: np.ndarray, min_m: float, max_m: float) -> np.ndarray:
    """Suchkorridor: Zellen mit Wasserabstand in [min_m, max_m]."""
    return (dist_to_water >= min_m) & (dist_to_water <= max_m)


def find_camp_candidates(
    dem: np.ndarray,
    px: float,
    dist_to_water: np.ndarray,
    cfg: dict | None = None,
    allowed_mask: np.ndarray | None = None,
    water_level_m: float | None = None,
    extra_masks: dict[str, np.ndarray] | None = None,
) -> list[dict[str, Any]]:
    """Sucht im Korridor Kandidatenflächen und bewertet sie geometrisch.

    Rückgabe je Kandidat: Rasterposition, Zeltpassung, ebene Fläche,
    Relief, Nässe-Proxys und Wasserabstand. Die gesellschaftlichen Fragen
    (Sichtbarkeit, Privacy, Erlaubnis) kommen in viewshed.py bzw. aus der
    erlaubten Gebietskulisse dazu.
    """
    cfg = cfg or {}
    ccfg = cfg.get("camp", {}) or {}
    cmin = float(ccfg.get("corridor_min_m", 20.0))
    cmax = float(ccfg.get("corridor_max_m", 250.0))
    max_slope = float(ccfg.get("max_slope_deg", 5.0))
    min_flat = float(ccfg.get("min_flat_area_m2", 12.0))
    max_cand = int(ccfg.get("max_candidates_per_lake", 12))
    min_sep_m = float(ccfg.get("min_candidate_separation_m", 40.0))
    max_depr = float(ccfg.get("max_depression_depth_m", 0.25))

    slope, _aspect = slope_aspect(dem, px)
    rel = local_relief(dem, radius=max(1, int(round(1.0 / max(px, 0.25)))))
    rough = roughness(dem)
    depr = depression_depth(dem)

    mask = corridor_mask(dist_to_water, cmin, cmax)
    mask &= np.isfinite(slope) & (slope <= max_slope)
    mask &= depr <= max_depr
    if allowed_mask is not None:
        mask &= allowed_mask
    if extra_masks:
        for m in extra_masks.values():
            mask &= m

    if not mask.any():
        return []

    # Güte je Zelle: flach, wenig Relief, wenig Nässe, nicht zu nah am Wasser
    quality = (
        -1.0 * np.nan_to_num(slope, nan=99.0)
        - 2.0 * np.nan_to_num(rel, nan=9.0)
        - 3.0 * depr
        - 0.002 * np.abs(dist_to_water - (cmin + 30.0))
    )
    quality[~mask] = -np.inf

    order = np.argsort(quality, axis=None)[::-1]
    h, w = dem.shape
    sep_px = max(1.0, min_sep_m / px)
    chosen: list[tuple[int, int]] = []
    out: list[dict[str, Any]] = []

    for flat in order:
        if len(out) >= max_cand:
            break
        r, c = divmod(int(flat), w)
        if not np.isfinite(quality[r, c]):
            break
        if any(math.hypot(r - rr, c - cc) < sep_px for rr, cc in chosen):
            continue

        fit = best_tent_fit(dem, px, (r, c), cfg, slope=slope)
        area = flat_area_m2(slope, (r, c), max_slope, px)
        if area < min_flat:
            continue

        rec: dict[str, Any] = {
            "row": int(r),
            "col": int(c),
            "distance_to_water_m": round(float(dist_to_water[r, c]), 1),
            "flat_area_m2": round(area, 1),
            "local_relief_cm": round(float(rel[r, c]) * 100.0, 1),
            "roughness_cm": round(float(rough[r, c]) * 100.0, 1),
            "depression_depth_m": round(float(depr[r, c]), 3),
            "wet_depression": bool(depr[r, c] > max_depr * 0.6),
            "dem_resolution_m": px,
            "allowed_area_checked": allowed_mask is not None,
        }
        rec.update(fit)
        if water_level_m is not None:
            rec["height_above_water_m"] = round(float(dem[r, c] - water_level_m), 2)
            rec["close_to_water"] = bool(rec["distance_to_water_m"] < cmin + 5)
        chosen.append((r, c))
        out.append(rec)

    LOG.info("Kandidatenflächen gefunden: %d (Korridor %.0f-%.0f m)", len(out), cmin, cmax)
    return out
