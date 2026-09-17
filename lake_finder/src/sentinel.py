"""
sentinel.py -- optionale zweite Verifikationsstufe mit Sentinel-2.

Zweck: Ein hoher Wilderness-Score heisst zunaechst nur, dass in OSM und im
Land-Cover-Raster nichts Menschliches verzeichnet ist. Ein echtes Bild ist
die guenstigste Gegenprobe. Diese Stufe laedt fuer die besten Treffer einen
wolkenarmen Sommer-Ausschnitt als True-Color-PNG.

Datenquelle: Earth Search v1 (STAC) von Element 84 auf den offenen
Sentinel-2-L2A-COGs in AWS us-west-2. Kein Login noetig.
    https://earth-search.aws.element84.com/v1   (Collection: sentinel-2-l2a)

Alternativen, falls der Endpunkt ausfaellt:
    * Copernicus Data Space Ecosystem STAC (Registrierung fuer Downloads)
    * Microsoft Planetary Computer (STAC offen, Assets brauchen Signatur)
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

LOG = logging.getLogger("lake_finder.sentinel")


def _stretch(band: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0) -> np.ndarray:
    b = band.astype("float32")
    valid = b[np.isfinite(b) & (b > 0)]
    if valid.size == 0:
        return np.zeros_like(b, dtype="uint8")
    lo, hi = np.percentile(valid, [lo_pct, hi_pct])
    if hi <= lo:
        hi = lo + 1
    return np.clip((b - lo) / (hi - lo) * 255.0, 0, 255).astype("uint8")


def make_chips(scored, cfg: dict) -> list[Path]:
    """True-Color-Ausschnitte fuer die besten Treffer schreiben."""
    from pystac_client import Client
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    scfg = cfg.get("sentinel", {}) or {}
    out_dir = Path(scfg.get("out_dir", "output/chips"))
    out_dir.mkdir(parents=True, exist_ok=True)
    top_n = int(scfg.get("top_n", 20))
    size_m = float(scfg.get("chip_size_m", 2000))
    max_cloud = float(scfg.get("max_cloud_percent", 10))
    months = set(int(m) for m in scfg.get("months", [5, 6, 7, 8, 9]))
    years_back = int(scfg.get("years_back", 2))

    client = Client.open(str(scfg.get("stac_url", "https://earth-search.aws.element84.com/v1")))
    today = date.today()
    start = date(today.year - years_back, 1, 1)

    written: list[Path] = []
    sub = scored.head(top_n)
    for _, r in sub.iterrows():
        lat, lon = float(r["latitude"]), float(r["longitude"])
        half_deg = size_m / 2 / 111_320.0
        bbox4326 = [lon - half_deg / np.cos(np.radians(lat)), lat - half_deg,
                    lon + half_deg / np.cos(np.radians(lat)), lat + half_deg]
        try:
            search = client.search(
                collections=[str(scfg.get("collection", "sentinel-2-l2a"))],
                bbox=bbox4326,
                datetime=f"{start.isoformat()}/{today.isoformat()}",
                query={"eo:cloud_cover": {"lt": max_cloud}},
                limit=50,
            )
            items = [
                it for it in search.items()
                if it.datetime and it.datetime.month in months
            ]
            if not items:
                LOG.info("Kein wolkenarmes Sommerbild fuer %s.", r.get("lake_uid"))
                continue
            item = min(items, key=lambda it: it.properties.get("eo:cloud_cover", 100))

            bands = []
            for key in ("red", "green", "blue"):
                href = item.assets[key].href
                with rasterio.open(href) as ds:
                    b = transform_bounds("EPSG:4326", ds.crs, *bbox4326, densify_pts=21)
                    win = from_bounds(*b, ds.transform)
                    arr = ds.read(1, window=win, boundless=True, fill_value=0)
                bands.append(_stretch(arr))
            rgb = np.dstack(bands)

            name = str(r.get("name") or r.get("lake_uid")).replace("/", "_")[:50]
            path = out_dir / f"{r['lake_uid']}_{name}_{item.datetime:%Y%m%d}.png"
            _write_png(path, rgb)
            written.append(path)
            LOG.info(
                "Chip geschrieben: %s (Wolken %.1f %%)",
                path.name,
                item.properties.get("eo:cloud_cover", float("nan")),
            )
        except Exception as exc:
            LOG.warning("Sentinel-Chip fuer %s fehlgeschlagen: %s", r.get("lake_uid"), exc)
    return written


def _write_png(path: Path, rgb: np.ndarray) -> None:
    """PNG schreiben -- bevorzugt Pillow, sonst rasterio."""
    try:
        from PIL import Image

        Image.fromarray(rgb, mode="RGB").save(path)
        return
    except ImportError:
        pass
    import rasterio

    h, w, _ = rgb.shape
    with rasterio.open(
        path, "w", driver="PNG", height=h, width=w, count=3, dtype="uint8"
    ) as ds:
        for i in range(3):
            ds.write(rgb[:, :, i], i + 1)
