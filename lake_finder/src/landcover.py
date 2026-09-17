"""
landcover.py -- Land-Cover-Statistik aus ESA WorldCover 10 m.

Datenquelle (Stand 2026 geprueft):
    Bucket : s3://esa-worldcover  (Region eu-central-1, anonym lesbar)
    HTTPS  : https://esa-worldcover.s3.eu-central-1.amazonaws.com
    Pfad   : v200/2021/map/ESA_WorldCover_10m_2021_v200_<TILE>_Map.tif
    TILE   : 3x3-Grad-Kachel, SW-Ecke, z.B. N51E012 / S48E036
    Format : Cloud Optimized GeoTIFF, EPSG:4326, 10 m, uint8, nodata 0

Es existieren zwei Versionen: v100 (Jahr 2020) und v200 (Jahr 2021).
Eine neuere WorldCover-Version gibt es Stand 2026 nicht; das Alter der
Daten ist eine reale Schwaeche und wird als Qualitaets-Flag mitgefuehrt.

Berechnungsprinzip:

  Fuer jeden See werden RINGE ausgewertet, nicht Scheiben:
      ring(d) = buffer(d) \\ Seepolygon
  Der 100-m-Ring ist damit exakt der geforderte "Ufergürtel".
  Die eigene Wasserflaeche faellt so automatisch heraus; Wasser
  benachbarter Seen wird zusaetzlich aus dem Nenner genommen
  (konfigurierbar), damit ein See nicht dafuer bestraft wird, dass
  neben ihm noch ein See liegt.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from .utils import Bbox, retry

if False:  # nur fuer Typpruefer; geopandas wird erst in den Funktionen geladen,
    import geopandas as gpd  # damit die Kachel-/Legendenlogik ohne Geo-Stack testbar bleibt

LOG = logging.getLogger("lake_finder.landcover")

# ESA WorldCover Legende (v100/v200 identisch)
WC_CLASSES: dict[int, str] = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare",
    70: "snow_ice",
    80: "water",
    90: "wetland",
    95: "mangroves",
    100: "moss_lichen",
}
NODATA = 0

# Was zaehlt als "natuerliche Flaeche"?
NATURAL_CLASSES = (10, 20, 30, 60, 90, 95, 100)


def worldcover_tile(lon: float, lat: float) -> str:
    """Kachelname der 3x3-Grad-Kachel, in der (lon, lat) liegt."""
    tlat = int(math.floor(lat / 3.0) * 3)
    tlon = int(math.floor(lon / 3.0) * 3)
    ns = "N" if tlat >= 0 else "S"
    ew = "E" if tlon >= 0 else "W"
    return f"{ns}{abs(tlat):02d}{ew}{abs(tlon):03d}"


def tiles_for_bbox(bbox: Bbox) -> list[str]:
    """Alle WorldCover-Kacheln, die eine Bounding Box beruehren."""
    out: list[str] = []
    lat = math.floor(bbox.south / 3.0) * 3
    while lat < bbox.north:
        lon = math.floor(bbox.west / 3.0) * 3
        while lon < bbox.east:
            out.append(worldcover_tile(lon + 0.001, lat + 0.001))
            lon += 3
        lat += 3
    return sorted(set(out))


def tile_url(tile: str, cfg: dict) -> str:
    lc = cfg.get("landcover", {})
    base = str(lc.get("base_url", "https://esa-worldcover.s3.eu-central-1.amazonaws.com")).rstrip("/")
    version = str(lc.get("version", "v200"))
    year = str(lc.get("year", 2021))
    return f"{base}/{version}/{year}/map/ESA_WorldCover_10m_{year}_{version}_{tile}_Map.tif"


def configure_gdal() -> None:
    """GDAL fuer effizientes Lesen entfernter COGs einstellen."""
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
    os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
    os.environ.setdefault("GDAL_HTTP_VERSION", "2")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("VSI_CACHE_SIZE", "104857600")  # 100 MB
    os.environ.setdefault("GDAL_CACHEMAX", "512")


# --------------------------------------------------------------------------
# Rasterzugriff
# --------------------------------------------------------------------------


@dataclass
class LandCoverSource:
    """Haelt die noetigen WorldCover-Kacheln und liefert Fenster-Arrays."""

    cfg: dict
    bbox: Bbox
    mode: str = "vsicurl"  # vsicurl | download
    _paths: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        configure_gdal()
        lc = self.cfg.get("landcover", {})
        self.mode = str(lc.get("mode", "vsicurl")).lower()
        tiles = tiles_for_bbox(self.bbox)
        LOG.info("WorldCover-Kacheln fuer die Region: %s", ", ".join(tiles))
        self._paths = [self._prepare(t) for t in tiles]

    @retry(times=3, delay=3.0)
    def _download(self, tile: str) -> str:
        import requests

        lc = self.cfg.get("landcover", {})
        cache_dir = Path(lc.get("cache_dir", "data/cache/worldcover"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        url = tile_url(tile, self.cfg)
        dest = cache_dir / url.rsplit("/", 1)[-1]
        if dest.exists() and dest.stat().st_size > 1_000_000:
            LOG.info("  Kachel %s bereits im Cache.", tile)
            return str(dest)
        LOG.info("  Lade Kachel %s (das kann einige Minuten dauern) ...", tile)
        tmp = dest.with_suffix(".part")
        with requests.get(url, stream=True, timeout=(30, 600)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        tmp.replace(dest)
        return str(dest)

    def _prepare(self, tile: str) -> str:
        if self.mode == "download":
            return self._download(tile)
        return "/vsicurl/" + tile_url(tile, self.cfg)

    # -- Lesen ------------------------------------------------------------
    def read(self, bounds: tuple[float, float, float, float]) -> tuple[np.ndarray, Any]:
        """Liest ein Fenster (west, south, east, north in EPSG:4326)."""
        import rasterio
        from rasterio.merge import merge
        from rasterio.windows import from_bounds

        west, south, east, north = bounds
        datasets = []
        try:
            for p in self._paths:
                try:
                    ds = rasterio.open(p)
                except Exception as exc:
                    LOG.warning("Kachel %s nicht lesbar: %s", p, exc)
                    continue
                b = ds.bounds
                if b.left < east and b.right > west and b.bottom < north and b.top > south:
                    datasets.append(ds)
                else:
                    ds.close()

            if not datasets:
                raise RuntimeError(
                    "Keine WorldCover-Kachel deckt den Bereich ab "
                    f"({west:.3f},{south:.3f},{east:.3f},{north:.3f})."
                )

            if len(datasets) == 1:
                ds = datasets[0]
                win = from_bounds(west, south, east, north, ds.transform)
                arr = ds.read(1, window=win, boundless=True, fill_value=NODATA)
                transform = ds.window_transform(win)
                return arr, transform

            arr, transform = merge(datasets, bounds=(west, south, east, north))
            return arr[0], transform
        finally:
            for ds in datasets:
                try:
                    ds.close()
                except Exception:  # pragma: no cover
                    pass


class RegionSampler:
    """Haelt das Land-Cover-Raster der Region im Speicher und beantwortet
    Punktabfragen im metrischen CRS.

    Wird von der Ufersegmentanalyse benutzt: fuer jeden der oft mehreren
    hundert Segmentpunkte je See ein eigener HTTP-Fensterzugriff waere
    absurd teuer. Ein Regions-Read, danach alles im Arbeitsspeicher.
    """

    def __init__(self, arr, transform, metric_crs: str, exclude_water: bool = True):
        from pyproj import Transformer

        self.arr = arr
        self.transform = transform
        self.metric_crs = metric_crs
        self.exclude_water = exclude_water
        self._to_wgs = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True)
        self.px_deg = abs(transform.a) if transform is not None else 1.0 / 12000.0

    def sample(self, x: float, y: float, radius_m: float) -> dict[str, float]:
        """Klassenanteile in einem Kreis um einen Punkt (metrisches CRS)."""
        if self.arr is None:
            return {}
        lon, lat = self._to_wgs.transform(x, y)
        # Meter -> Grad: Breite konstant, Laenge mit cos(lat) gestaucht
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))

        inv = ~self.transform
        c0, r0 = inv * (lon - dlon, lat + dlat)
        c1, r1 = inv * (lon + dlon, lat - dlat)
        c0, r0 = int(math.floor(c0)), int(math.floor(r0))
        c1, r1 = int(math.ceil(c1)), int(math.ceil(r1))
        r0 = max(r0, 0)
        c0 = max(c0, 0)
        r1 = min(r1, self.arr.shape[0])
        c1 = min(c1, self.arr.shape[1])
        if r1 <= r0 or c1 <= c0:
            return {}

        sub = self.arr[r0:r1, c0:c1]
        rows = np.arange(r0, r1)[:, None]
        cols = np.arange(c0, c1)[None, :]
        # Pixelmittelpunkte zurueck nach Grad
        lons = self.transform.c + (cols + 0.5) * self.transform.a
        lats = self.transform.f + (rows + 0.5) * self.transform.e
        dx = (lons - lon) * 111_320.0 * math.cos(math.radians(lat))
        dy = (lats - lat) * 111_320.0
        mask = (dx**2 + dy**2) <= radius_m**2
        vals = sub[mask]
        return _class_fractions(vals, exclude_water=self.exclude_water)


def build_region_sampler(cfg: dict, bbox: Bbox, metric_crs: str, pad_m: float = 1200.0):
    """Liest das Land-Cover-Raster der Region einmal und liefert einen Sampler."""
    lc = cfg.get("landcover", {}) or {}
    max_px = int(lc.get("region_read_max_px", 8000))
    big = bbox.buffered(pad_m)
    px_x = (big.east - big.west) * 12000.0
    px_y = (big.north - big.south) * 12000.0
    if px_x > max_px or px_y > max_px:
        LOG.warning(
            "Region zu gross fuer einen Sampler-Read (~%dx%d Pixel > %d). "
            "Ufersegment-Land-Cover wird uebersprungen; region_read_max_px erhoehen "
            "oder Region verkleinern.",
            int(px_x), int(px_y), max_px,
        )
        return None
    try:
        src = LandCoverSource(cfg=cfg, bbox=bbox)
        arr, tr = src.read((big.west, big.south, big.east, big.north))
    except Exception as exc:
        LOG.warning("Land-Cover-Regions-Read fehlgeschlagen: %s", exc)
        return None
    LOG.info("Land-Cover-Sampler bereit (%d x %d Pixel).", arr.shape[0], arr.shape[1])
    return RegionSampler(
        arr, tr, metric_crs, bool(lc.get("exclude_water_from_denominator", True))
    )


# --------------------------------------------------------------------------
# Zonale Statistik
# --------------------------------------------------------------------------


def _class_fractions(
    values: np.ndarray,
    exclude_water: bool,
) -> dict[str, float]:
    """Klassenanteile in Prozent. Nodata immer, Wasser optional aus dem Nenner."""
    if values.size == 0:
        return {f"{name}_percent": float("nan") for name in WC_CLASSES.values()} | {
            "n_pixels": 0,
            "nodata_percent": 100.0,
            "natural_land_percent": float("nan"),
        }

    total = values.size
    nodata_n = int((values == NODATA).sum())
    counts = {code: int((values == code).sum()) for code in WC_CLASSES}

    denom = total - nodata_n
    if exclude_water:
        denom -= counts[80]
    denom = max(denom, 0)

    out: dict[str, float] = {"n_pixels": int(total), "nodata_percent": round(100.0 * nodata_n / total, 2)}
    for code, name in WC_CLASSES.items():
        if code == 80 and exclude_water:
            # Wasseranteil weiterhin ausweisen, aber bezogen auf alle gueltigen Pixel
            valid = max(total - nodata_n, 1)
            out["water_percent"] = round(100.0 * counts[80] / valid, 2)
            continue
        out[f"{name}_percent"] = round(100.0 * counts[code] / denom, 2) if denom else float("nan")
    if exclude_water and "water_percent" not in out:
        out["water_percent"] = 0.0
    nat = sum(counts[c] for c in NATURAL_CLASSES)
    out["natural_land_percent"] = round(100.0 * nat / denom, 2) if denom else float("nan")
    out["valid_pixels"] = int(denom)
    return out


def compute_landcover_metrics(
    lakes: "gpd.GeoDataFrame",
    cfg: dict,
    bbox: Bbox,
    sampler: "RegionSampler | None" = None,
) -> pd.DataFrame:
    """Land-Cover-Anteile je See und Pufferring."""
    import geopandas as gpd
    import rasterio
    from rasterio.features import geometry_mask

    lc = cfg.get("landcover", {})
    distances = [int(d) for d in cfg.get("buffers", {}).get("distances_m", [100, 250, 500, 1000])]
    shore_m = int(cfg.get("buffers", {}).get("shore_ring_m", 100))
    exclude_water = bool(lc.get("exclude_water_from_denominator", True))
    all_d = sorted(set(distances + [shore_m]))

    if lakes.empty:
        return pd.DataFrame(columns=["lake_uid"])

    src = (
        LandCoverSource(cfg=cfg, bbox=bbox)
        if (sampler is None or getattr(sampler, "arr", None) is None)
        else None
    )

    # Region am Stueck lesen, wenn sie in den Speicher passt (deutlich
    # schneller als ein HTTP-Fenster je See). Wurde bereits ein Sampler
    # gebaut, wird dessen Array wiederverwendet -- kein zweiter Read.
    max_px = int(lc.get("region_read_max_px", 8000))
    big = bbox.buffered(max(all_d) + 200)
    px_x = (big.east - big.west) / (1.0 / 12000.0)  # 10 m ~ 1/12000 Grad
    px_y = (big.north - big.south) / (1.0 / 12000.0)
    region_mode = px_x <= max_px and px_y <= max_px

    region_arr = region_tr = None
    if sampler is not None and sampler.arr is not None:
        region_arr, region_tr = sampler.arr, sampler.transform
        region_mode = True
        LOG.info("Land-Cover-Raster aus dem Sampler uebernommen (kein zweiter Read).")
    elif region_mode:
        LOG.info(
            "Lese WorldCover fuer die gesamte Region am Stueck (~%dx%d Pixel).",
            int(px_x),
            int(px_y),
        )
        try:
            region_arr, region_tr = src.read((big.west, big.south, big.east, big.north))
        except Exception as exc:
            LOG.warning("Regions-Read fehlgeschlagen (%s) - falle auf Fenster je See zurueck.", exc)
            region_mode = False
    else:
        LOG.info("Region zu gross fuer einen Read (~%dx%d Pixel) - Fenster je See.", int(px_x), int(px_y))

    lakes_wgs = lakes.to_crs("EPSG:4326")
    rows: list[dict[str, Any]] = []
    n = len(lakes)

    for i, (idx, lake) in enumerate(lakes.iterrows(), 1):
        if i % 25 == 0 or i == n:
            LOG.info("  Land Cover: %d/%d Seen", i, n)

        geom_m = lake.geometry
        rec: dict[str, Any] = {"lake_uid": lake["lake_uid"]}

        # Ringe im metrischen CRS bauen, dann nach WGS84 projizieren
        ring_geoms_m = {}
        for d in all_d:
            try:
                ring = geom_m.buffer(d).difference(geom_m)
            except Exception:  # pragma: no cover
                ring = geom_m.buffer(d)
            ring_geoms_m[d] = ring
        rings = gpd.GeoSeries(list(ring_geoms_m.values()), crs=lakes.crs).to_crs("EPSG:4326")
        ring_by_d = dict(zip(all_d, rings.values))

        outer = ring_by_d[max(all_d)]
        w, s, e, nn = outer.bounds
        pad = 0.0005
        bounds = (w - pad, s - pad, e + pad, nn + pad)

        try:
            if region_mode:
                from rasterio.windows import from_bounds as _fb
                from rasterio.transform import array_bounds

                win = _fb(*bounds, region_tr)
                r0 = max(int(math.floor(win.row_off)), 0)
                c0 = max(int(math.floor(win.col_off)), 0)
                r1 = min(int(math.ceil(win.row_off + win.height)), region_arr.shape[0])
                c1 = min(int(math.ceil(win.col_off + win.width)), region_arr.shape[1])
                if r1 <= r0 or c1 <= c0:
                    raise ValueError("Fenster ausserhalb des Regions-Rasters")
                arr = region_arr[r0:r1, c0:c1]
                tr = rasterio.windows.transform(
                    rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0), region_tr
                )
            else:
                arr, tr = src.read(bounds)
        except Exception as exc:
            LOG.warning("Land Cover fuer %s nicht lesbar: %s", lake["lake_uid"], exc)
            rec["landcover_ok"] = False
            rows.append(rec)
            continue

        rec["landcover_ok"] = True
        for d in all_d:
            ring = ring_by_d[d]
            if ring.is_empty:
                continue
            try:
                mask = geometry_mask(
                    [ring], out_shape=arr.shape, transform=tr, invert=True, all_touched=False
                )
            except Exception as exc:  # pragma: no cover
                LOG.debug("Maskierung fehlgeschlagen (%s, %d m): %s", lake["lake_uid"], d, exc)
                continue
            vals = arr[mask]
            fr = _class_fractions(vals, exclude_water=exclude_water)
            suffix = f"{d}m"
            for k, v in fr.items():
                rec[f"{k}_{suffix}"] = v

        # Geforderte Kurznamen
        for d in distances:
            if f"tree_cover_percent_{d}m" in rec:
                rec[f"tree_cover_{d}m"] = rec[f"tree_cover_percent_{d}m"]
        if f"tree_cover_percent_{shore_m}m" in rec:
            rec["shore_tree_cover"] = rec[f"tree_cover_percent_{shore_m}m"]
            rec["shore_natural_percent"] = rec.get(f"natural_land_percent_{shore_m}m")
            rec["shore_built_up_percent"] = rec.get(f"built_up_percent_{shore_m}m")

        ref = max(distances)
        rec["built_up_percent"] = rec.get(f"built_up_percent_{ref}m")
        rec["cropland_percent"] = rec.get(f"cropland_percent_{ref}m")
        rec["grassland_percent"] = rec.get(f"grassland_percent_{ref}m")
        rec["shrubland_percent"] = rec.get(f"shrubland_percent_{ref}m")
        rec["natural_land_percent"] = rec.get(f"natural_land_percent_{ref}m")
        rec["landcover_ref_distance_m"] = ref
        rows.append(rec)

    df = pd.DataFrame(rows)
    df["landcover_source"] = f"ESA WorldCover {lc.get('year', 2021)} {lc.get('version', 'v200')}"
    return df
