"""
viewshed.py -- Sichtbarkeitsanalyse und daraus abgeleitete Privacy-Metriken.

Verfahren: R2 (Franklin & Ray). Von der Beobachterzelle wird zu jeder
Randzelle des Ausschnitts ein Strahl gezogen; entlang des Strahls wird der
bisher größte Vertikalwinkel mitgeführt. Eine Zelle ist sichtbar, wenn ihr
Winkel diesen Wert übersteigt. Das ist das Standardverfahren, weil es
linear in der Zellzahl ist -- exakte Sichtbarkeit für jedes Zellpaar wäre
quadratisch und für die hier nötigen Radien sinnlos teuer.

Was hier NICHT behauptet wird
-----------------------------
Sichtbarkeit im Raster ist eine geometrische Aussage über Oberflächen, die
im Modell stehen. Ein Zaun, ein Schilfgürtel, ein Holzstapel oder eine
Kurve der Straße stehen nicht darin. Ein "nicht sichtbar" heißt: nach
Geländemodell und, falls vorhanden, Oberflächenmodell besteht keine freie
Sichtlinie. Nicht mehr.

Privacy-Exposure
----------------
Der Score beschreibt ausschließlich, wie einsehbar ein Platz von
öffentlich zugänglichen Bereichen ist. Er ist als Komfortmaß für einen
LEGAL genutzten Platz gedacht -- nicht als Hilfsmittel, um Kontrollen
auszuweichen. Entsprechend wird nichts über Entdeckungswahrscheinlichkeit
modelliert, sondern nur über Sichtlinien.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Sequence

import numpy as np

LOG = logging.getLogger("lake_finder.viewshed")


# ==========================================================================
# Kernalgorithmus
# ==========================================================================


def _perimeter_cells(h: int, w: int) -> list[tuple[int, int]]:
    cells = []
    for c in range(w):
        cells.append((0, c))
        cells.append((h - 1, c))
    for r in range(1, h - 1):
        cells.append((r, 0))
        cells.append((r, w - 1))
    return cells


def viewshed(
    surface: np.ndarray,
    px: float,
    observer_rc: tuple[int, int],
    observer_height_m: float = 1.6,
    target_height_m: float = 0.0,
    max_radius_m: float | None = None,
) -> np.ndarray:
    """Boolesches Sichtbarkeitsraster von einem Beobachterpunkt aus.

    ``surface`` ist das Modell, das die Sicht blockiert: das reine
    Geländemodell (DGM) für Geländeabschirmung, das Oberflächenmodell
    (DOM) wenn Vegetation und Gebäude mitblocken sollen.
    """
    h, w = surface.shape
    r0, c0 = int(observer_rc[0]), int(observer_rc[1])
    if not (0 <= r0 < h and 0 <= c0 < w):
        raise ValueError("Beobachterposition liegt außerhalb des Rasters.")

    vis = np.zeros((h, w), dtype=bool)
    vis[r0, c0] = True
    z0 = float(surface[r0, c0]) + observer_height_m
    rmax = None if max_radius_m is None else max_radius_m / px

    for pr, pc in _perimeter_cells(h, w):
        dr, dc = pr - r0, pc - c0
        steps = max(abs(dr), abs(dc))
        if steps == 0:
            continue
        max_angle = -math.inf
        for s in range(1, steps + 1):
            r = r0 + dr * s / steps
            c = c0 + dc * s / steps
            ri, ci = int(round(r)), int(round(c))
            if not (0 <= ri < h and 0 <= ci < w):
                break
            dist_px = math.hypot(ri - r0, ci - c0)
            if dist_px == 0:
                continue
            if rmax is not None and dist_px > rmax:
                break
            dist_m = dist_px * px
            z = float(surface[ri, ci])
            # Zwei verschiedene Winkel, und das ist der springende Punkt:
            #   angle_test  -- die OBERKANTE des Ziels (Gebaeude, Fahrzeug,
            #                  Person) entscheidet, ob man es sieht;
            #   angle_block -- die Gelaende-/Oberflaechenhoehe selbst
            #                  entscheidet, was dahinter verdeckt wird.
            # Wuerde man beide gleichsetzen, waere auf ebener Flaeche mit
            # Zielhoehe > 0 alles ab der zweiten Zelle "verdeckt".
            angle_test = (z + target_height_m - z0) / dist_m
            angle_block = (z - z0) / dist_m
            if angle_test > max_angle:
                vis[ri, ci] = True
            if angle_block > max_angle:
                max_angle = angle_block
    return vis


def visible_fraction(vis: np.ndarray, targets: np.ndarray) -> float | None:
    """Anteil der Zielzellen, die sichtbar sind (0-1)."""
    n = int(targets.sum())
    if n == 0:
        return None
    return float((vis & targets).sum()) / n


# ==========================================================================
# Abschirmung und Exposure
# ==========================================================================


def screening_metrics(
    dem: np.ndarray,
    dsm: np.ndarray | None,
    px: float,
    spot_rc: tuple[int, int],
    targets: np.ndarray,
    observer_height_m: float = 1.6,
    target_height_m: float = 2.0,
    max_radius_m: float | None = None,
) -> dict[str, Any]:
    """Wie viel der menschlichen Infrastruktur verdeckt Gelände, wie viel Vegetation?

    Drei Sichtbarkeitsläufe:
      * ``flat``    -- ebene Fläche: was wäre ohne jede Abschirmung sichtbar?
      * ``terrain`` -- auf dem DGM: Geländeabschirmung
      * ``surface`` -- auf dem DOM: zusätzlich Vegetation und Gebäude

    Daraus ergibt sich, welcher Anteil der Sicht durch Relief und welcher
    durch Bewuchs verschwindet. Ohne DOM bleibt der Vegetationsanteil
    ``None`` -- nicht 0, denn "nicht gemessen" ist nicht "keine Deckung".
    """
    n_targets = int(targets.sum())
    if n_targets == 0:
        return {
            "targets": 0,
            "visible_flat": None,
            "visible_terrain": None,
            "visible_surface": None,
            "terrain_screening_percent": None,
            "vegetation_screening_percent": None,
        }

    flat = np.zeros_like(dem)
    vis_flat = viewshed(flat, px, spot_rc, observer_height_m, target_height_m, max_radius_m)
    vis_terr = viewshed(dem, px, spot_rc, observer_height_m, target_height_m, max_radius_m)
    n_flat = int((vis_flat & targets).sum())
    n_terr = int((vis_terr & targets).sum())

    out: dict[str, Any] = {
        "targets": n_targets,
        "visible_flat": n_flat,
        "visible_terrain": n_terr,
        "terrain_screening_percent": (
            round(100.0 * (n_flat - n_terr) / n_flat, 1) if n_flat else 0.0
        ),
    }

    if dsm is not None:
        vis_surf = viewshed(dsm, px, spot_rc, observer_height_m, target_height_m, max_radius_m)
        n_surf = int((vis_surf & targets).sum())
        out["visible_surface"] = n_surf
        out["vegetation_screening_percent"] = (
            round(100.0 * (n_terr - n_surf) / n_terr, 1) if n_terr else 0.0
        )
        out["visible_final"] = n_surf
    else:
        out["visible_surface"] = None
        out["vegetation_screening_percent"] = None
        out["visible_final"] = n_terr
    return out


def exposure_percent(visible: int | None, total: int | None) -> float | None:
    """Einsehbarkeit in Prozent der vorhandenen Objekte."""
    if not total:
        return None if total is None else 0.0
    if visible is None:
        return None
    return round(100.0 * visible / total, 1)


def privacy_metrics(
    dem: np.ndarray,
    dsm: np.ndarray | None,
    px: float,
    spot_rc: tuple[int, int],
    feature_rasters: Mapping[str, np.ndarray],
    cfg: dict | None = None,
    water_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    """Sichtbarkeits- und Exposure-Kennzahlen für einen Platz.

    ``feature_rasters`` erwartet boolesche Raster derselben Form, z. B.
    ``{"buildings": ..., "major_road": ..., "minor_road": ..., "soft_way": ...,
       "parking": ..., "campsite": ...}``.
    """
    cfg = cfg or {}
    vcfg = cfg.get("viewshed", {}) or {}
    radius = float(vcfg.get("max_radius_m", 800.0))
    obs_h = float(vcfg.get("observer_height_m", 1.6))

    out: dict[str, Any] = {"viewshed_radius_m": radius, "dem_resolution_m": px}

    heights = {
        "buildings": float(vcfg.get("building_height_m", 5.0)),
        "major_road": 0.5,
        "minor_road": 0.5,
        "soft_way": 0.5,
        "parking": 1.5,
        "campsite": 2.0,
    }

    vis_terr = viewshed(dem, px, spot_rc, obs_h, 1.0, radius)
    vis_final = (
        viewshed(dsm, px, spot_rc, obs_h, 1.0, radius) if dsm is not None else vis_terr
    )

    cell_m = px
    for key, raster in feature_rasters.items():
        if raster is None:
            continue
        total = int(raster.sum())
        seen = int((vis_final & raster).sum())
        out[f"visible_{key}_cells"] = seen
        out[f"{key}_cells_in_radius"] = total
        if key in ("major_road", "minor_road", "soft_way"):
            out[f"visible_{key}_length_m"] = round(seen * cell_m, 1)
        out[f"exposure_{key}_percent"] = exposure_percent(seen, total)

    out["visible_buildings_count"] = out.get("visible_buildings_cells")
    out["visible_road_length_m"] = round(
        sum(
            out.get(f"visible_{k}_length_m", 0.0) or 0.0
            for k in ("major_road", "minor_road")
        ),
        1,
    )
    out["visible_path_length_m"] = out.get("visible_soft_way_length_m", 0.0)

    # Aggregierte Exposure-Werte (höher = einsehbarer)
    out["exposure_from_roads"] = _combine(
        [out.get("exposure_major_road_percent"), out.get("exposure_minor_road_percent")]
    )
    out["exposure_from_paths"] = out.get("exposure_soft_way_percent")
    out["exposure_from_buildings"] = out.get("exposure_buildings_percent")

    # Abschirmung, getrennt nach Ursache
    human = None
    for key in ("buildings", "major_road", "minor_road", "parking", "campsite"):
        r = feature_rasters.get(key)
        if r is None:
            continue
        human = r.copy() if human is None else (human | r)
    if human is not None and human.any():
        sc = screening_metrics(
            dem, dsm, px, spot_rc, human, obs_h,
            float(vcfg.get("building_height_m", 5.0)), radius,
        )
        out["terrain_screening_percent"] = sc["terrain_screening_percent"]
        out["vegetation_screening_percent"] = sc["vegetation_screening_percent"]
        out["visible_human_features"] = sc["visible_final"]
    else:
        out["terrain_screening_percent"] = None
        out["vegetation_screening_percent"] = None
        out["visible_human_features"] = 0

    if water_mask is not None and water_mask.any():
        out["lake_visibility_percent"] = round(
            100.0 * float((vis_final & water_mask).sum()) / float(water_mask.sum()), 1
        )

    out["viewshed_model"] = "DGM+DOM" if dsm is not None else "nur DGM"
    return out


def _combine(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return None if not vals else round(max(vals), 1)


def proximity_flags(distances: Mapping[str, float | None], cfg: dict | None = None) -> dict[str, bool]:
    """Nähe-Flags für den Privacy-Score.

    Ein Waldweg wird bewusst nicht wie eine Straße behandelt: er ist für
    den Zugang nützlich und für die Abgeschiedenheit nur leicht negativ.
    """
    cfg = cfg or {}
    pcfg = (cfg.get("privacy", {}) or {})
    return {
        "path_at_spot": _closer(distances.get("soft_way"), float(pcfg.get("path_at_spot_m", 25.0))),
        "road_at_spot": _closer(
            min_or_none(distances.get("major_road"), distances.get("minor_road")),
            float(pcfg.get("road_at_spot_m", 50.0)),
        ),
        "parking_near_spot": _closer(distances.get("parking"), float(pcfg.get("parking_near_m", 150.0))),
    }


def min_or_none(*values: float | None) -> float | None:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _closer(value: float | None, threshold: float) -> bool:
    return value is not None and float(value) < threshold
