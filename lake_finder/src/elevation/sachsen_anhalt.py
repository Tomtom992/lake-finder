"""
elevation/sachsen_anhalt.py -- Höhenmodell Sachsen-Anhalt.

Behörde : LVermGeo Sachsen-Anhalt
Portal  : https://www.lvermgeo.sachsen-anhalt.de/de/gdp-open-data.html
Produkt : DGM1, 1 m Gitterweite, GeoTIFF-Kacheln
CRS     : EPSG:25832
Lizenz  : Datenlizenz Deutschland -- Namensnennung 2.0 (Open Data seit 2024)

Open-Data-Portal mit eigener WMS/WFS/WCS-Übersicht. Achtung: UTM-Zone 32, nicht 33 wie in Brandenburg und Sachsen.

Vorgehen: Kacheln der Zielregion einmal herunterladen, Verzeichnis in
config.yaml eintragen:

    elevation:
      enabled: true
      source: state
      state: sachsen_anhalt
      states:
        sachsen_anhalt:
          path: data/dem/sachsen_anhalt
          pattern: "*.tif"

Die Download-Links der Länder ändern sich regelmäßig; sie stehen deshalb
bewusst nicht im Code.
"""

from __future__ import annotations

from .common import LocalTileSource, WcsSource

SOURCE_INFO = {
    "state": "sachsen_anhalt",
    "label": "Sachsen-Anhalt",
    "agency": "LVermGeo Sachsen-Anhalt",
    "portal": "https://www.lvermgeo.sachsen-anhalt.de/de/gdp-open-data.html",
    "product": "DGM1",
    "resolution_m": 1.0,
    "crs": "EPSG:25832",
    "licence": "dl-de/by-2-0",
    "notes": "Open-Data-Portal mit eigener WMS/WFS/WCS-Übersicht. Achtung: UTM-Zone 32, nicht 33 wie in Brandenburg und Sachsen.",
}


def make_source(cfg: dict):
    """Baut die Höhenquelle für Sachsen-Anhalt aus der config."""
    scfg = ((cfg.get("elevation", {}) or {}).get("states", {}) or {}).get("sachsen_anhalt", {}) or {}
    if scfg.get("wcs_url"):
        return WcsSource(
            url=str(scfg["wcs_url"]),
            coverage_id=str(scfg.get("coverage_id", "dgm1")),
            resolution_m=float(scfg.get("resolution_m", 1.0)),
            crs=str(scfg.get("crs", SOURCE_INFO["crs"])),
            name="sachsen_anhalt_wcs",
            extra=scfg.get("wcs_extra", {}) or {},
        )
    return LocalTileSource(
        path=str(scfg.get("path", "data/dem/sachsen_anhalt")),
        pattern=str(scfg.get("pattern", "*.tif")),
        resolution_m=float(scfg.get("resolution_m", 1.0)),
        name="sachsen_anhalt_dgm1",
        src_crs=scfg.get("crs", SOURCE_INFO["crs"]),
    )
