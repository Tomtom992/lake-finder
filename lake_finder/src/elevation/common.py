"""
elevation/common.py -- Höhenmodelle: gemeinsame Schnittstelle.

Eine Quelle liefert für eine Bounding Box im metrischen CRS ein
Höhenraster plus Auflösung. Welche Quelle das ist, entscheidet die
config -- der Rest der Pipeline sieht nur ``DemSource.read``.

Verfügbare Implementierungen

  LocalTileSource   Verzeichnis mit GeoTIFF-Kacheln, die man einmal vom
                    Landesportal geladen hat. Der Normalfall für DGM1.
  WcsSource         Generischer WCS-2.0.1-GetCoverage-Client für Dienste,
                    die ohne Anmeldung ausliefern.
  CopernicusDsmSource  Weltweites 30-m-Oberflächenmodell auf AWS, anonym.
                    Nur als Rückfall für die Grobbewertung.

Warum kein automatischer DGM1-Download
--------------------------------------
Die deutschen Geobasisdaten sind seit Juni 2024 Open Data (Datenlizenz
Deutschland -- Namensnennung 2.0), aber der Bezug ist es nicht einheitlich:
Der bundesweite BKG-Downloaddienst für DGM1 muss pro Nutzer freigeschaltet
werden und liefert über Meta4-Dateien mit einer persönlichen UUID; die
Länderportale haben je eigene Formate, Kachelschemata und Registrierungs-
wege. Ein hart verdrahteter Downloader wäre in drei Monaten kaputt.
Deshalb: einmal herunterladen, Pfad in die config, fertig.

Auflösung und Ehrlichkeit
-------------------------
``resolution_m`` wird immer mitgeführt und landet in jedem Ergebnis. Ein
DGM1 hat 1 m Gitterweite bei etwa ±0,3 m Höhengenauigkeit -- daraus lässt
sich Mikrorelief in Zentimetern als TENDENZ ableiten, nicht als Messung.
Aus einem 30-m-Modell lässt sich über eine Zeltfläche gar nichts sagen;
die Pipeline setzt dann das Flag ``dgm_zu_grob_fuer_zeltflaeche``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

LOG = logging.getLogger("lake_finder.elevation")

# Ab dieser Gitterweite ist eine Zeltflächenaussage nicht mehr seriös.
TENT_GRADE_MAX_RES_M = 2.0


@dataclass
class DemWindow:
    """Höhenausschnitt im metrischen CRS."""

    array: np.ndarray
    transform: Any           # affine Transformation (rasterio.Affine)
    crs: str
    resolution_m: float
    source: str

    @property
    def tent_grade(self) -> bool:
        return self.resolution_m <= TENT_GRADE_MAX_RES_M

    def rowcol(self, x: float, y: float) -> tuple[int, int]:
        inv = ~self.transform
        c, r = inv * (x, y)
        return int(math.floor(r)), int(math.floor(c))

    def xy(self, row: int, col: int) -> tuple[float, float]:
        x, y = self.transform * (col + 0.5, row + 0.5)
        return float(x), float(y)


class DemSource:
    """Basisklasse. ``read`` liefert einen DemWindow oder None."""

    name = "abstract"
    resolution_m = 1.0

    def read(self, bounds_metric: Sequence[float], metric_crs: str) -> DemWindow | None:
        raise NotImplementedError


# --------------------------------------------------------------------------


class LocalTileSource(DemSource):
    """GeoTIFF-Kacheln aus einem lokalen Verzeichnis (DGM1-Normalfall)."""

    def __init__(self, path: str, pattern: str = "*.tif", resolution_m: float = 1.0,
                 name: str = "local", src_crs: str | None = None):
        self.path = Path(path)
        self.pattern = pattern
        self.resolution_m = float(resolution_m)
        self.name = name
        self.src_crs = src_crs

    def read(self, bounds_metric: Sequence[float], metric_crs: str) -> DemWindow | None:
        import rasterio
        from rasterio.merge import merge
        from rasterio.warp import transform_bounds

        files = sorted(self.path.glob(self.pattern)) if self.path.exists() else []
        if not files:
            LOG.warning("Keine Höhenkacheln unter %s/%s", self.path, self.pattern)
            return None

        datasets = []
        try:
            for f in files:
                try:
                    ds = rasterio.open(f)
                except Exception:  # pragma: no cover
                    continue
                try:
                    b = transform_bounds(metric_crs, ds.crs, *bounds_metric, densify_pts=21)
                except Exception:  # pragma: no cover
                    ds.close()
                    continue
                if (
                    ds.bounds.left < b[2] and ds.bounds.right > b[0]
                    and ds.bounds.bottom < b[3] and ds.bounds.top > b[1]
                ):
                    datasets.append((ds, b))
                else:
                    ds.close()
            if not datasets:
                LOG.info("Keine Höhenkachel deckt den Ausschnitt ab.")
                return None
            arr, tr = merge([d for d, _ in datasets], bounds=datasets[0][1])
            ds0 = datasets[0][0]
            res = abs(tr.a)
            return DemWindow(arr[0].astype("float64"), tr, str(ds0.crs), res, self.name)
        finally:
            for ds, _ in datasets:
                try:
                    ds.close()
                except Exception:  # pragma: no cover
                    pass


# --------------------------------------------------------------------------


class WcsSource(DemSource):
    """Generischer WCS-2.0.1-Client (GetCoverage als GeoTIFF)."""

    def __init__(self, url: str, coverage_id: str, resolution_m: float = 1.0,
                 crs: str = "EPSG:25833", version: str = "2.0.1",
                 fmt: str = "image/tiff", name: str = "wcs", extra: dict | None = None):
        self.url = url.rstrip("?")
        self.coverage_id = coverage_id
        self.resolution_m = float(resolution_m)
        self.crs = crs
        self.version = version
        self.fmt = fmt
        self.name = name
        self.extra = extra or {}

    def read(self, bounds_metric: Sequence[float], metric_crs: str) -> DemWindow | None:
        import rasterio
        import requests
        from rasterio.io import MemoryFile
        from rasterio.warp import transform_bounds

        w, s, e, n = transform_bounds(metric_crs, self.crs, *bounds_metric, densify_pts=21)
        axis = self.extra.get("axis_labels", ("E", "N"))
        params = {
            "SERVICE": "WCS",
            "VERSION": self.version,
            "REQUEST": "GetCoverage",
            "COVERAGEID": self.coverage_id,
            "FORMAT": self.fmt,
            "SUBSET": [f"{axis[0]}({w},{e})", f"{axis[1]}({s},{n})"],
        }
        params.update({k: v for k, v in self.extra.items() if k != "axis_labels"})
        try:
            r = requests.get(self.url, params=params, timeout=(30, 180))
            r.raise_for_status()
            with MemoryFile(r.content) as mem, mem.open() as ds:
                arr = ds.read(1).astype("float64")
                return DemWindow(arr, ds.transform, str(ds.crs), abs(ds.transform.a), self.name)
        except Exception as exc:
            LOG.warning("WCS %s fehlgeschlagen: %s", self.name, exc)
            return None


# --------------------------------------------------------------------------


class CopernicusDsmSource(DemSource):
    """Copernicus DEM GLO-30, weltweit, anonym auf AWS.

    Achtung: das ist ein OBERFLÄCHENmodell (DSM) mit 30 m Gitterweite --
    Baumkronen und Gebäude stecken darin, und für eine Zeltfläche ist es
    um zwei Größenordnungen zu grob. Es taugt für Hangneigung im
    Landschaftsmaßstab und als Rückfall, wenn kein DGM1 vorliegt.
    """

    name = "copernicus_glo30"
    resolution_m = 30.0
    BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

    @staticmethod
    def tile_name(lon: float, lat: float) -> str:
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        return (
            f"Copernicus_DSM_COG_10_{ns}{abs(int(math.floor(lat))):02d}_00_"
            f"{ew}{abs(int(math.floor(lon))):03d}_00_DEM"
        )

    def read(self, bounds_metric: Sequence[float], metric_crs: str) -> DemWindow | None:
        import rasterio
        from rasterio.merge import merge
        from rasterio.warp import transform_bounds

        w, s, e, n = transform_bounds(metric_crs, "EPSG:4326", *bounds_metric, densify_pts=21)
        tiles = set()
        for lon in (w, e):
            for lat in (s, n):
                tiles.add(self.tile_name(lon, lat))

        datasets = []
        try:
            for t in sorted(tiles):
                url = f"/vsicurl/{self.BASE}/{t}/{t}.tif"
                try:
                    datasets.append(rasterio.open(url))
                except Exception as exc:
                    LOG.debug("Copernicus-Kachel %s nicht lesbar: %s", t, exc)
            if not datasets:
                return None
            arr, tr = merge(datasets, bounds=(w, s, e, n))
            return DemWindow(arr[0].astype("float64"), tr, "EPSG:4326", 30.0, self.name)
        finally:
            for ds in datasets:
                try:
                    ds.close()
                except Exception:  # pragma: no cover
                    pass


# --------------------------------------------------------------------------


def get_source(cfg: dict) -> DemSource | None:
    """Baut die in der config gewählte Höhenquelle."""
    ecfg = (cfg.get("elevation", {}) or {})
    if not ecfg.get("enabled", False):
        return None
    kind = str(ecfg.get("source", "local")).lower()

    if kind in ("local", "tiles", "dgm1"):
        return LocalTileSource(
            path=str(ecfg.get("path", "data/dem")),
            pattern=str(ecfg.get("pattern", "*.tif")),
            resolution_m=float(ecfg.get("resolution_m", 1.0)),
            name=str(ecfg.get("name", "dgm1_lokal")),
            src_crs=ecfg.get("crs"),
        )
    if kind == "wcs":
        return WcsSource(
            url=str(ecfg["wcs_url"]),
            coverage_id=str(ecfg["coverage_id"]),
            resolution_m=float(ecfg.get("resolution_m", 1.0)),
            crs=str(ecfg.get("crs", "EPSG:25833")),
            version=str(ecfg.get("wcs_version", "2.0.1")),
            name=str(ecfg.get("name", "wcs")),
            extra=ecfg.get("wcs_extra", {}) or {},
        )
    if kind in ("copernicus", "glo30", "fallback"):
        return CopernicusDsmSource()
    if kind == "state":
        from . import states

        return states.make_source(str(ecfg.get("state", "")), cfg)
    LOG.warning("Unbekannte Höhenquelle '%s'.", kind)
    return None


def _edt_1d(f: np.ndarray) -> np.ndarray:
    """Exakte 1D-Distanztransformation der unteren Einhüllenden (Felzenszwalb
    & Huttenlocher 2012). Lineare Laufzeit, reines numpy."""
    n = f.shape[0]
    d = np.empty(n, dtype="float64")
    v = np.zeros(n, dtype="int64")
    z = np.empty(n + 1, dtype="float64")
    k = 0
    v[0] = 0
    z[0] = -np.inf
    z[1] = np.inf
    for q in range(1, n):
        while True:
            p = v[k]
            s = ((f[q] + q * q) - (f[p] + p * p)) / (2.0 * q - 2.0 * p)
            if s <= z[k] and k > 0:
                k -= 1
                continue
            break
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = np.inf
    k = 0
    for q in range(n):
        while z[k + 1] < q:
            k += 1
        p = v[k]
        d[q] = (q - p) ** 2 + f[p]
    return d


def euclidean_distance_m(mask: np.ndarray, px: float) -> np.ndarray:
    """Abstand jeder Zelle zur nächsten True-Zelle in ``mask`` [m].

    Exakte euklidische Distanztransformation, separabel über beide Achsen.
    Wird für den Camp-Suchkorridor gebraucht (Abstand zum Wasser) und ist
    ohne scipy implementiert, damit die Abhängigkeitsliste schlank bleibt.
    """
    big = 1e12
    f = np.where(mask, 0.0, big).astype("float64")
    tmp = np.empty_like(f)
    for c in range(f.shape[1]):
        tmp[:, c] = _edt_1d(f[:, c])
    out = np.empty_like(f)
    for r in range(f.shape[0]):
        out[r, :] = _edt_1d(tmp[r, :])
    return np.sqrt(np.maximum(out, 0.0)) * px


def distance_raster(shape: tuple[int, int], transform: Any, geom, metric_crs: str) -> np.ndarray:
    """Abstand jeder Zelle zu einer Geometrie [m] -- für den Camp-Korridor."""
    from rasterio.features import rasterize

    mask = rasterize(
        [(geom, 1)], out_shape=shape, transform=transform, fill=0, dtype="uint8"
    ).astype(bool)
    px = abs(transform.a)
    if not mask.any():
        return np.full(shape, np.inf)
    return euclidean_distance_m(mask, px)
