"""
utils.py -- Infrastruktur fuer das lake_finder-Projekt.

Enthaelt:
  * Logging-Setup
  * Bbox-Datentyp (inkl. Kachelung fuer Overpass)
  * Wahl eines metrischen CRS (Distanzen NIE in EPSG:4326)
  * Disk-Cache (gzip-JSON)
  * OverpassClient mit Endpoint-Rotation, Retry/Backoff, Rate-Limit, Cache

Bewusst ohne harte Abhaengigkeit auf geopandas/shapely, damit die
Hilfsfunktionen auch in Umgebungen ohne vollstaendigen Geo-Stack
importierbar und testbar bleiben.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

LOG = logging.getLogger("lake_finder")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def setup_logging(level: str = "INFO", logfile: str | os.PathLike | None = None) -> logging.Logger:
    """Konfiguriert das Paket-Logging einmalig."""
    logger = logging.getLogger("lake_finder")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", datefmt="%H:%M:%S"
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------
# Geometrie-Hilfen ohne shapely
# --------------------------------------------------------------------------

EARTH_R = 6371008.8  # mittlerer Erdradius [m]


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Grosskreisdistanz in Metern (nur fuer grobe Abschaetzungen)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def meters_to_deg(meters: float, lat: float) -> tuple[float, float]:
    """Naeherung: Meter -> (dLon, dLat) in Grad auf Breite ``lat``.

    NUR fuer das Aufweiten von Bounding-Boxen benutzen, niemals fuer
    Distanz- oder Flaechenberechnungen.
    """
    dlat = meters / 111_320.0
    dlon = meters / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return dlon, dlat


@dataclass(frozen=True)
class Bbox:
    """Bounding Box in EPSG:4326, Reihenfolge (west, south, east, north)."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError(f"Ungueltige Laengengrade: {self.west} .. {self.east}")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError(f"Ungueltige Breitengrade: {self.south} .. {self.north}")

    # -- Konstruktoren ----------------------------------------------------
    @classmethod
    def from_list(cls, v: Sequence[float]) -> "Bbox":
        if len(v) != 4:
            raise ValueError("bbox braucht genau 4 Werte [west, south, east, north]")
        return cls(float(v[0]), float(v[1]), float(v[2]), float(v[3]))

    @classmethod
    def from_center(cls, lon: float, lat: float, size_km: float) -> "Bbox":
        """Quadratische Box mit Kantenlaenge ``size_km`` um einen Mittelpunkt."""
        half = size_km * 1000.0 / 2.0
        dlon, dlat = meters_to_deg(half, lat)
        return cls(lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    # -- Eigenschaften ----------------------------------------------------
    @property
    def center(self) -> tuple[float, float]:
        return ((self.west + self.east) / 2.0, (self.south + self.north) / 2.0)

    @property
    def width_km(self) -> float:
        return haversine_m(self.west, self.center[1], self.east, self.center[1]) / 1000.0

    @property
    def height_km(self) -> float:
        return haversine_m(self.center[0], self.south, self.center[0], self.north) / 1000.0

    @property
    def area_km2(self) -> float:
        return self.width_km * self.height_km

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]

    def overpass(self) -> str:
        """Overpass erwartet (south, west, north, east)."""
        return f"{self.south:.7f},{self.west:.7f},{self.north:.7f},{self.east:.7f}"

    def buffered(self, meters: float) -> "Bbox":
        """Box um ``meters`` aufweiten (nur Vorfilter, keine exakte Geometrie)."""
        dlon, dlat = meters_to_deg(meters, self.center[1])
        return Bbox(
            max(self.west - dlon, -180.0),
            max(self.south - dlat, -90.0),
            min(self.east + dlon, 180.0),
            min(self.north + dlat, 90.0),
        )

    def tiles(self, max_deg: float = 0.25) -> list["Bbox"]:
        """Zerlegt die Box in Kacheln <= ``max_deg`` Kantenlaenge.

        Overpass-Abfragen skalieren schlecht mit der Flaeche; die Kachelung
        haelt einzelne Anfragen klein genug fuer das Server-Timeout und
        erlaubt granulares Caching.
        """
        nx = max(1, math.ceil((self.east - self.west) / max_deg))
        ny = max(1, math.ceil((self.north - self.south) / max_deg))
        dx = (self.east - self.west) / nx
        dy = (self.north - self.south) / ny
        out: list[Bbox] = []
        for i in range(nx):
            for j in range(ny):
                out.append(
                    Bbox(
                        self.west + i * dx,
                        self.south + j * dy,
                        self.west + (i + 1) * dx,
                        self.south + (j + 1) * dy,
                    )
                )
        return out

    def __str__(self) -> str:  # pragma: no cover - reine Anzeige
        return (
            f"[{self.west:.4f}, {self.south:.4f}, {self.east:.4f}, {self.north:.4f}]"
            f" (~{self.width_km:.0f}x{self.height_km:.0f} km)"
        )


# --------------------------------------------------------------------------
# CRS-Wahl
# --------------------------------------------------------------------------


def utm_epsg(lon: float, lat: float) -> str:
    """EPSG-Code der UTM-Zone fuer einen Punkt."""
    zone = int(math.floor((lon + 180.0) / 6.0) % 60) + 1
    return f"EPSG:{32600 + zone}" if lat >= 0 else f"EPSG:{32700 + zone}"


def pick_metric_crs(bbox: Bbox, override: str | None = None) -> str:
    """Waehlt ein metrisches CRS fuer Buffer/Distanzen/Flaechen.

    * ``override`` (z.B. ``EPSG:3035``) hat immer Vorrang.
    * Kleine Regionen (< 4 Grad Breite): UTM-Zone des Mittelpunkts.
      Massstabsfehler in Zonenmitte ~0.04 %, am Zonenrand ~0.1 % --
      fuer Distanzen im 100-m-Bereich voellig ausreichend.
    * Groessere Regionen in Europa: ETRS89 / LAEA Europe (EPSG:3035),
      flaechentreu und zonenuebergreifend stabil.
    * Sonst: World Equidistant Cylindrical waere verzerrt -> wir nehmen
      weiterhin UTM und warnen.
    """
    if override and str(override).lower() not in ("auto", "", "none"):
        return str(override)
    lon, lat = bbox.center
    span = max(bbox.east - bbox.west, bbox.north - bbox.south)
    if span <= 4.0:
        return utm_epsg(lon, lat)
    if -25 <= lon <= 45 and 34 <= lat <= 72:
        return "EPSG:3035"
    LOG.warning(
        "Region spannt %.1f Grad ausserhalb Europas - UTM-Zone des Mittelpunkts "
        "kann am Rand ungenau werden. Bitte crs.metric explizit setzen.",
        span,
    )
    return utm_epsg(lon, lat)


# --------------------------------------------------------------------------
# Disk-Cache
# --------------------------------------------------------------------------


def cache_key(*parts: Any) -> str:
    raw = "|".join(json.dumps(p, sort_keys=True, default=str) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


class JsonCache:
    """Simpler gzip-JSON-Cache auf der Platte."""

    def __init__(self, directory: str | os.PathLike, enabled: bool = True, ttl_days: float | None = 30.0):
        self.dir = Path(directory)
        self.enabled = enabled
        self.ttl_s = None if ttl_days is None else float(ttl_days) * 86400.0
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json.gz"

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        p = self.path(key)
        if not p.exists():
            return None
        if self.ttl_s is not None and (time.time() - p.stat().st_mtime) > self.ttl_s:
            LOG.debug("Cache-Eintrag abgelaufen: %s", p.name)
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # defekter Cache darf nie den Lauf killen
            LOG.warning("Cache-Eintrag %s unlesbar (%s) - wird ignoriert.", p.name, exc)
            return None

    def put(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        p = self.path(key)
        tmp = p.with_suffix(".tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(value, fh)
            tmp.replace(p)
        except Exception as exc:  # pragma: no cover
            LOG.warning("Cache-Schreibfehler %s: %s", p.name, exc)


# --------------------------------------------------------------------------
# HTTP / Overpass
# --------------------------------------------------------------------------


class OverpassError(RuntimeError):
    pass


class OverpassClient:
    """Robuster Overpass-Client.

    Eigenschaften:
      * mehrere Endpunkte, Rotation bei 429/504/5xx
      * exponentielles Backoff mit Jitter, respektiert ``Retry-After``
      * Mindestabstand zwischen zwei Requests (Rate-Limit-Hygiene)
      * aussagekraeftiger User-Agent (Overpass antwortet sonst teils mit 406)
      * transparenter Disk-Cache pro (Endpoint-unabhaengiger) Query
    """

    def __init__(
        self,
        endpoints: Sequence[str],
        timeout_s: int = 300,
        max_retries: int = 4,
        min_interval_s: float = 1.5,
        user_agent: str = "lake-finder/1.0 (open-data lake wilderness screening)",
        cache: JsonCache | None = None,
    ):
        import requests  # lokal importiert: utils bleibt ohne requests importierbar

        if not endpoints:
            raise ValueError("Mindestens ein Overpass-Endpoint noetig.")
        self.endpoints = list(endpoints)
        self.timeout_s = int(timeout_s)
        self.max_retries = int(max_retries)
        self.min_interval_s = float(min_interval_s)
        self.cache = cache
        self._last_call = 0.0
        self._ep_idx = 0
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            }
        )

    # -- intern -----------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _next_endpoint(self) -> str:
        ep = self.endpoints[self._ep_idx % len(self.endpoints)]
        self._ep_idx += 1
        return ep

    # -- API --------------------------------------------------------------
    def query(self, ql: str, cache_tag: str = "") -> dict:
        """Fuehrt eine Overpass-QL-Abfrage aus und liefert das JSON-Dict."""
        key = cache_key("overpass", ql, cache_tag)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                LOG.debug("Overpass-Cache-Treffer (%s)", key)
                return hit

        import requests

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            ep = self._next_endpoint()
            self._throttle()
            t0 = time.time()
            try:
                LOG.debug("Overpass POST %s (Versuch %d/%d)", ep, attempt, self.max_retries)
                resp = self._session.post(
                    ep, data={"data": ql}, timeout=(30, self.timeout_s + 30)
                )
                self._last_call = time.time()
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise OverpassError(
                            f"Antwort von {ep} ist kein JSON (evtl. HTML-Fehlerseite): {exc}"
                        ) from exc
                    LOG.debug(
                        "Overpass OK in %.1fs, %d Elemente",
                        time.time() - t0,
                        len(data.get("elements", [])),
                    )
                    if self.cache is not None:
                        self.cache.put(key, data)
                    return data

                if resp.status_code in (429, 504, 502, 503, 500):
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else min(60.0, 2.0 ** attempt + random.uniform(0, 2))
                    )
                    LOG.warning(
                        "Overpass %s -> HTTP %d, warte %.0fs und wechsle Endpoint.",
                        ep,
                        resp.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    last_exc = OverpassError(f"HTTP {resp.status_code} von {ep}")
                    continue

                if resp.status_code == 400:
                    # Syntaxfehler in der Query -> Wiederholen sinnlos.
                    raise OverpassError(
                        f"Overpass lehnt die Query ab (HTTP 400). Auszug der Antwort:\n"
                        f"{resp.text[:800]}"
                    )
                if resp.status_code == 406:
                    LOG.warning(
                        "HTTP 406 von %s - Endpoint mag den Request-Header nicht. "
                        "Wechsle Endpoint.",
                        ep,
                    )
                    last_exc = OverpassError(f"HTTP 406 von {ep}")
                    continue

                raise OverpassError(f"Unerwarteter Status {resp.status_code} von {ep}")

            except (requests.Timeout, requests.ConnectionError) as exc:
                self._last_call = time.time()
                delay = min(60.0, 2.0 ** attempt + random.uniform(0, 2))
                LOG.warning("Netzwerkfehler bei %s (%s). Neuer Versuch in %.0fs.", ep, exc, delay)
                time.sleep(delay)
                last_exc = exc

        raise OverpassError(
            f"Overpass-Abfrage nach {self.max_retries} Versuchen fehlgeschlagen. "
            f"Letzter Fehler: {last_exc}"
        )


# --------------------------------------------------------------------------
# Kleinkram
# --------------------------------------------------------------------------


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return default if not b else a / b


def pct(part: float, whole: float) -> float:
    """Prozentwert, gerundet auf 2 Nachkommastellen, 0 bei leerem Nenner."""
    return round(100.0 * safe_div(part, whole, 0.0), 2)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ramp(value: float | None, lo: float, hi: float, missing: float = 0.0) -> float:
    """Lineare Rampe auf [0, 1].

    ``lo < hi``  -> je groesser ``value``, desto besser.
    ``lo > hi``  -> je kleiner ``value``, desto besser (inverse Rampe).
    ``value is None`` -> ``missing`` (konservativer Default).
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return missing
    if lo == hi:
        return 1.0 if value >= hi else 0.0
    if lo < hi:
        return clamp((value - lo) / (hi - lo))
    return clamp((lo - value) / (lo - hi))


def chunked(seq: Iterable[Any], n: int) -> Iterator[list[Any]]:
    buf: list[Any] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def retry(times: int = 3, delay: float = 2.0, exceptions: tuple = (Exception,)) -> Callable:
    """Kleiner Retry-Dekorator fuer nicht-Overpass-Netzwerkaufrufe."""

    def deco(fn: Callable) -> Callable:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last: Exception | None = None
            for i in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last = exc
                    if i == times:
                        break
                    wait = delay * (2 ** (i - 1)) + random.uniform(0, 1)
                    LOG.warning(
                        "%s fehlgeschlagen (%s). Versuch %d/%d in %.1fs.",
                        fn.__name__,
                        exc,
                        i + 1,
                        times,
                        wait,
                    )
                    time.sleep(wait)
            raise last  # type: ignore[misc]

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return deco
