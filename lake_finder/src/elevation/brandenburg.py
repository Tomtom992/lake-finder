"""
elevation/brandenburg.py -- Höhenmodell Brandenburg.

Behörde : LGB (Landesvermessung und Geobasisinformation Brandenburg)
Portal  : https://geobroker.geobasis-bb.de/
Produkt : DGM1, 1 m Gitterweite, GeoTIFF-Kacheln
CRS     : EPSG:25833
Lizenz  : Datenlizenz Deutschland -- Namensnennung 2.0 (Open Data seit 2024)

Geobasisdaten seit 2024 Open Data (DL-DE/BY-2.0). DGM1 über den Geobroker; Bezug als Kachelpaket, kein stabiler Direktlink -- deshalb lokal ablegen.

Vorgehen: Kacheln der Zielregion einmal herunterladen, Verzeichnis in
config.yaml eintragen:

    elevation:
      enabled: true
      source: state
      state: brandenburg
      states:
        brandenburg:
          path: data/dem/brandenburg
          pattern: "*.tif"

Die Download-Links der Länder ändern sich regelmäßig; sie stehen deshalb
bewusst nicht im Code.
"""

from __future__ import annotations

from .common import LocalTileSource, WcsSource

SOURCE_INFO = {
    "state": "brandenburg",
    "label": "Brandenburg",
    "agency": "LGB (Landesvermessung und Geobasisinformation Brandenburg)",
    "portal": "https://geobroker.geobasis-bb.de/",
    "product": "DGM1",
    "resolution_m": 1.0,
    "crs": "EPSG:25833",
    "licence": "dl-de/by-2-0",
    "notes": "Geobasisdaten seit 2024 Open Data (DL-DE/BY-2.0). DGM1 über den Geobroker; Bezug als Kachelpaket, kein stabiler Direktlink -- deshalb lokal ablegen.",
}


def make_source(cfg: dict):
    """Baut die Höhenquelle für Brandenburg aus der config."""
    scfg = ((cfg.get("elevation", {}) or {}).get("states", {}) or {}).get("brandenburg", {}) or {}
    if scfg.get("wcs_url"):
        return WcsSource(
            url=str(scfg["wcs_url"]),
            coverage_id=str(scfg.get("coverage_id", "dgm1")),
            resolution_m=float(scfg.get("resolution_m", 1.0)),
            crs=str(scfg.get("crs", SOURCE_INFO["crs"])),
            name="brandenburg_wcs",
            extra=scfg.get("wcs_extra", {}) or {},
        )
    return LocalTileSource(
        path=str(scfg.get("path", "data/dem/brandenburg")),
        pattern=str(scfg.get("pattern", "*.tif")),
        resolution_m=float(scfg.get("resolution_m", 1.0)),
        name="brandenburg_dgm1",
        src_crs=scfg.get("crs", SOURCE_INFO["crs"]),
    )
