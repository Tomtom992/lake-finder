"""
terrain.py -- Geländeanalyse auf einem Höhenraster.

Alles hier arbeitet auf einem 2D-numpy-Array plus Pixelgröße in Metern.
Keine GDAL-, geopandas- oder shapely-Abhängigkeit -- damit ist genau die
Rechenlogik offline testbar, die man am leichtesten falsch macht
(Randbehandlung, Vorzeichen der Neigung, Senkenfüllung, Flussakkumulation).

Was berechnet wird und warum

  slope / aspect       Horn (1981), das Standardverfahren aus GDAL und
                       ArcGIS. 3x3-Fenster, robuster gegen Rauschen als
                       einfache Differenzenquotienten.
  roughness            Standardabweichung der Höhe im Fenster. Unterscheidet
                       "eben" von "eben im Mittel, aber voller Wurzeln".
  local_relief         max - min im Fenster. Das ist die Zahl, die zählt,
                       wenn man auf einer Isomatte liegt.
  curvature            Laplace-Operator. Negativ = Mulde, positiv = Kuppe.
  fill_depressions     Priority-Flood (Barnes et al. 2014). Liefert die
                       Tiefe abflussloser Senken -- das sind die Stellen,
                       an denen nach Regen das Wasser steht.
  flow_accumulation    D8 auf dem gefüllten Raster. Viele zufließende Zellen
                       = Tiefenlinie = feucht.
  wetness_index        ln(a / tan(beta)), der klassische TWI-Proxy.

Genauigkeitsgrenze: Ein DGM1 hat 1 m Gitterweite und laut BKG eine
Höhengenauigkeit von etwa ±0,3 m. Ein daraus abgeleitetes Mikrorelief in
Zentimetern ist eine Tendenz, keine Vermessung. Ein 30-m-DEM taugt für
Hangneigung im Landschaftsmaßstab und NICHT für die Frage, ob ein Zelt
eben steht -- ``dem_resolution_m`` wird deshalb überall mitgeführt.
"""

from __future__ import annotations

import heapq
import logging
import math
from typing import Any

import numpy as np

LOG = logging.getLogger("lake_finder.terrain")

NODATA = np.nan


# ==========================================================================
# Grundlagen
# ==========================================================================


def _pad(dem: np.ndarray) -> np.ndarray:
    return np.pad(dem.astype("float64"), 1, mode="edge")


def slope_aspect(dem: np.ndarray, px: float) -> tuple[np.ndarray, np.ndarray]:
    """Hangneigung [Grad] und Exposition [Grad, 0=Nord, im Uhrzeigersinn].

    Verfahren nach Horn: gewichteter 3x3-Gradient. ``px`` ist die
    Pixelgröße in Metern (quadratische Zellen vorausgesetzt).
    """
    if dem.ndim != 2 or min(dem.shape) < 2:
        raise ValueError("DEM muss ein 2D-Raster mit mindestens 2x2 Zellen sein.")
    z = _pad(dem)
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, _e, f = z[1:-1, :-2], z[1:-1, 1:-1], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]

    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * px)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * px)

    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    aspect = np.degrees(np.arctan2(dzdy, -dzdx))
    aspect = (90.0 - aspect) % 360.0
    aspect[np.hypot(dzdx, dzdy) < 1e-12] = np.nan  # ebene Fläche hat keine Exposition
    return slope, aspect


def _window_stack(dem: np.ndarray, radius: int) -> np.ndarray:
    """Alle Verschiebungen eines (2r+1)-Fensters als 3D-Stapel."""
    z = np.pad(dem.astype("float64"), radius, mode="edge")
    n = 2 * radius + 1
    h, w = dem.shape
    out = np.empty((n * n, h, w), dtype="float64")
    k = 0
    for dy in range(n):
        for dx in range(n):
            out[k] = z[dy : dy + h, dx : dx + w]
            k += 1
    return out


def roughness(dem: np.ndarray, radius: int = 1) -> np.ndarray:
    """Standardabweichung der Höhe im Fenster [m]."""
    return _window_stack(dem, radius).std(axis=0)


def local_relief(dem: np.ndarray, radius: int = 2) -> np.ndarray:
    """Höhenunterschied max-min im Fenster [m]."""
    st = _window_stack(dem, radius)
    return st.max(axis=0) - st.min(axis=0)


def curvature(dem: np.ndarray, px: float) -> np.ndarray:
    """Laplace-Krümmung [1/m]. Negativ = Mulde, positiv = Kuppe."""
    z = _pad(dem)
    lap = (
        z[:-2, 1:-1] + z[2:, 1:-1] + z[1:-1, :-2] + z[1:-1, 2:] - 4.0 * z[1:-1, 1:-1]
    ) / (px * px)
    return lap


# ==========================================================================
# Hydrologie
# ==========================================================================


def fill_depressions(dem: np.ndarray) -> np.ndarray:
    """Senken auffüllen (Priority-Flood). Liefert das gefüllte Raster.

    Vom Rand her wird nach innen geflutet; jede Zelle bekommt mindestens
    die Höhe des niedrigsten Weges, über den sie den Rand erreicht. Die
    Differenz zum Original ist die Tiefe der abflusslosen Senke.
    """
    z = dem.astype("float64")
    h, w = z.shape
    filled = np.full_like(z, np.inf)
    visited = np.zeros((h, w), dtype=bool)
    heap: list[tuple[float, int, int]] = []

    for r in range(h):
        for c in (0, w - 1):
            heapq.heappush(heap, (float(z[r, c]), r, c))
            visited[r, c] = True
            filled[r, c] = z[r, c]
    for c in range(w):
        for r in (0, h - 1):
            if not visited[r, c]:
                heapq.heappush(heap, (float(z[r, c]), r, c))
                visited[r, c] = True
                filled[r, c] = z[r, c]

    while heap:
        lvl, r, c = heapq.heappop(heap)
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < h and 0 <= cc < w) or visited[rr, cc]:
                continue
            visited[rr, cc] = True
            new_lvl = max(float(z[rr, cc]), lvl)
            filled[rr, cc] = new_lvl
            heapq.heappush(heap, (new_lvl, rr, cc))
    return filled


def depression_depth(dem: np.ndarray) -> np.ndarray:
    """Tiefe der abflusslosen Senke je Zelle [m]. 0 = kein Wasserrückhalt."""
    return np.maximum(fill_depressions(dem) - dem.astype("float64"), 0.0)


_D8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def flow_accumulation(dem: np.ndarray, px: float = 1.0) -> np.ndarray:
    """D8-Flussakkumulation auf dem gefüllten Raster [Anzahl Zellen].

    Jede Zelle gibt ihren gesamten Zufluss an den steilsten tieferen
    Nachbarn ab. Grob, aber für "liegt dieser Platz in einer Tiefenlinie"
    völlig ausreichend -- und ohne externe Abhängigkeit.
    """
    z = fill_depressions(dem)
    h, w = z.shape
    acc = np.ones((h, w), dtype="float64")
    order = np.argsort(z, axis=None)[::-1]  # hoch -> tief

    for flat in order:
        r, c = divmod(int(flat), w)
        best = None
        best_drop = 0.0
        for dr, dc in _D8:
            rr, cc = r + dr, c + dc
            if not (0 <= rr < h and 0 <= cc < w):
                continue
            dist = px * (math.sqrt(2.0) if dr and dc else 1.0)
            drop = (z[r, c] - z[rr, cc]) / dist
            if drop > best_drop:
                best_drop = drop
                best = (rr, cc)
        if best is not None:
            acc[best] += acc[r, c]
    return acc


def wetness_index(dem: np.ndarray, px: float = 1.0) -> np.ndarray:
    """Topographic Wetness Index als Proxy: ln(a / tan(beta))."""
    acc = flow_accumulation(dem, px)
    slope, _ = slope_aspect(dem, px)
    tan_b = np.tan(np.radians(np.maximum(slope, 0.05)))  # 0.05 Grad Untergrenze
    a = (acc * px * px) / px  # spezifisches Einzugsgebiet je Konturlänge
    return np.log(np.maximum(a, 1e-6) / tan_b)


# ==========================================================================
# Abgeleitete Bewertungen
# ==========================================================================


def dryness_from_terrain(
    dem: np.ndarray,
    px: float,
    height_above_water_m: float | None = None,
    cfg: dict | None = None,
) -> dict[str, np.ndarray]:
    """Rasterweise Trockenheits- und Nässe-Proxys.

    Ausdrücklich KEINE hydrologische Aussage: hier wird nur Geometrie
    ausgewertet. Ein gefüllter Graben, ein verdichteter Boden oder ein
    hoher Grundwasserstand sind im Höhenraster nicht sichtbar.
    """
    cfg = cfg or {}
    tcfg = (cfg.get("terrain", {}) or {}).get("dryness", {}) or {}
    depth_full = float(tcfg.get("depression_full_penalty_m", 0.35))
    twi_lo = float(tcfg.get("twi_dry", 4.0))
    twi_hi = float(tcfg.get("twi_wet", 9.0))

    depth = depression_depth(dem)
    twi = wetness_index(dem, px)

    depth_term = np.clip(depth / max(depth_full, 1e-6), 0.0, 1.0)
    twi_term = np.clip((twi - twi_lo) / max(twi_hi - twi_lo, 1e-6), 0.0, 1.0)

    wet = np.clip(0.6 * depth_term + 0.4 * twi_term, 0.0, 1.0)
    dry_score = 100.0 * (1.0 - wet)

    if height_above_water_m is not None:
        # Nahe am Wasserspiegel ist alles feuchter, unabhängig vom Relief.
        h_term = float(np.clip(1.0 - (height_above_water_m / 3.0), 0.0, 1.0))
        dry_score = dry_score * (1.0 - 0.35 * h_term)

    return {
        "depression_depth_m": depth,
        "wetness_index": twi,
        "wetness_risk": wet,
        "dryness_score": np.clip(dry_score, 0.0, 100.0),
    }


def terrain_summary(dem: np.ndarray, px: float, cfg: dict | None = None) -> dict[str, Any]:
    """Kennzahlen eines Höhenausschnitts -- für Logs und schnelle Prüfungen."""
    slope, _ = slope_aspect(dem, px)
    rel = local_relief(dem, radius=max(1, int(round(1.0 / max(px, 0.25)))))
    return {
        "dem_resolution_m": px,
        "mean_slope_deg": float(np.nanmean(slope)),
        "max_slope_deg": float(np.nanmax(slope)),
        "mean_relief_m": float(np.nanmean(rel)),
        "roughness_m": float(np.nanmean(roughness(dem))),
        "max_depression_depth_m": float(np.nanmax(depression_depth(dem))),
    }
