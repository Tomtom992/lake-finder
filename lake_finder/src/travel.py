"""
travel.py -- Erreichbarkeit: geschaetzte oder geroutete Fahrzeit ab Startpunkt.

Zwei Verfahren:

1. ``haversine`` (Default, immer verfuegbar, kein Netzwerk)
   Luftlinie * Umwegfaktor / Durchschnittsgeschwindigkeit.
   Der Umwegfaktor (Default 1.3) ist der uebliche Erfahrungswert fuer
   Strassennetze in Mitteleuropa; in Seenlandschaften mit Umfahrungen
   liegt er real eher bei 1.3-1.5. Die Schaetzung ist damit systematisch
   eher zu optimistisch -- das steht bewusst so in den Flags.

2. ``osrm`` (optional, genauer)
   Oeffentlicher OSRM-Demoserver (router.project-osrm.org), Table-Service,
   ein Request fuer viele Ziele. Der Demoserver ist nicht fuer Dauerlast
   gedacht: Requests werden gedrosselt, Fehler fuehren automatisch zum
   Rueckfall auf Verfahren 1 (Spalte ``drive_time_source``).

Die Fahrzeit ist als hartes Filterkriterium UND als Score-Komponente
nutzbar (siehe config.yaml).
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import pandas as pd

from .utils import JsonCache, cache_key, haversine_m

LOG = logging.getLogger("lake_finder.travel")

OSRM_DEFAULT = "https://router.project-osrm.org"


def straight_line_estimate(
    origin: tuple[float, float],
    dests: Sequence[tuple[float, float]],
    detour_factor: float = 1.3,
    avg_speed_kmh: float = 75.0,
) -> tuple[list[float], list[float]]:
    """(Fahrzeit [min], Strecke [km]) je Ziel -- reine Luftlinien-Schaetzung."""
    mins, kms = [], []
    for lon, lat in dests:
        air_km = haversine_m(origin[0], origin[1], lon, lat) / 1000.0
        road_km = air_km * detour_factor
        mins.append(round(60.0 * road_km / max(avg_speed_kmh, 1.0), 1))
        kms.append(round(road_km, 1))
    return mins, kms


def osrm_table(
    origin: tuple[float, float],
    dests: Sequence[tuple[float, float]],
    base_url: str = OSRM_DEFAULT,
    chunk: int = 80,
    min_interval_s: float = 1.2,
    timeout_s: int = 60,
    cache: JsonCache | None = None,
) -> tuple[list[float | None], list[float | None]]:
    """Echte Fahrzeiten via OSRM-Table-Service. None, wo OSRM nichts liefert."""
    import requests

    out_min: list[float | None] = []
    out_km: list[float | None] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "lake-finder/1.0 (open data, low volume)"})

    for i in range(0, len(dests), chunk):
        part = list(dests)[i : i + chunk]
        key = cache_key("osrm", origin, part)
        cached = cache.get(key) if cache else None
        if cached:
            out_min.extend(cached["min"])
            out_km.extend(cached["km"])
            continue

        coords = ";".join(f"{lon:.5f},{lat:.5f}" for lon, lat in [origin, *part])
        url = (
            f"{base_url.rstrip('/')}/table/v1/driving/{coords}"
            f"?sources=0&annotations=duration,distance"
        )
        try:
            r = session.get(url, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "Ok":
                raise RuntimeError(f"OSRM-Code {data.get('code')}")
            durations = data["durations"][0][1:]
            distances = (data.get("distances") or [[None] * (len(part) + 1)])[0][1:]
            mins = [round(d / 60.0, 1) if d is not None else None for d in durations]
            kms = [round(d / 1000.0, 1) if d is not None else None for d in distances]
            if cache:
                cache.put(key, {"min": mins, "km": kms})
            out_min.extend(mins)
            out_km.extend(kms)
        except Exception as exc:
            LOG.warning("OSRM-Abfrage fehlgeschlagen (%s) - Rueckfall auf Luftlinie.", exc)
            out_min.extend([None] * len(part))
            out_km.extend([None] * len(part))
        time.sleep(min_interval_s)

    return out_min, out_km


def add_travel_metrics(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Ergaenzt drive_time_min, drive_distance_km, air_distance_km, drive_time_source."""
    tcfg = cfg.get("travel", {}) or {}
    if not tcfg.get("enabled", True) or df.empty:
        return df
    origin = tcfg.get("origin")
    if not origin or len(origin) != 2:
        LOG.info("Kein travel.origin gesetzt - Fahrzeit wird uebersprungen.")
        return df

    origin = (float(origin[0]), float(origin[1]))
    dests = list(zip(df["longitude"], df["latitude"])) if "longitude" in df.columns else list(
        zip(df["centroid_lon"], df["centroid_lat"])
    )

    detour = float(tcfg.get("detour_factor", 1.3))
    speed = float(tcfg.get("avg_speed_kmh", 75.0))
    est_min, est_km = straight_line_estimate(origin, dests, detour, speed)
    air = [round(haversine_m(origin[0], origin[1], lon, lat) / 1000.0, 1) for lon, lat in dests]

    mode = str(tcfg.get("mode", "haversine")).lower()
    src = ["schaetzung"] * len(dests)
    final_min, final_km = list(est_min), list(est_km)

    if mode == "osrm":
        cache = JsonCache(tcfg.get("cache_dir", "data/cache/osrm"), enabled=True, ttl_days=90)
        r_min, r_km = osrm_table(
            origin,
            dests,
            base_url=str(tcfg.get("osrm_url", OSRM_DEFAULT)),
            chunk=int(tcfg.get("osrm_chunk", 80)),
            min_interval_s=float(tcfg.get("osrm_min_interval_s", 1.2)),
            cache=cache,
        )
        for i, v in enumerate(r_min):
            if v is not None:
                final_min[i] = v
                final_km[i] = r_km[i] if r_km[i] is not None else final_km[i]
                src[i] = "osrm"
        n_ok = sum(1 for s in src if s == "osrm")
        LOG.info("Fahrzeiten: %d/%d via OSRM geroutet, Rest geschaetzt.", n_ok, len(src))

    out = df.copy()
    out["air_distance_km"] = air
    out["drive_time_min"] = final_min
    out["drive_distance_km"] = final_km
    out["drive_time_source"] = src
    return out
