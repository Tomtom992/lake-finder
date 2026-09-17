"""
elevation/sachsen.py -- Höhenmodell Sachsen.

Behörde : GeoSN (Staatsbetrieb Geobasisinformation und Vermessung Sachsen)
Portal  : https://www.geodaten.sachsen.de/
Produkt : DGM1, 1 m Gitterweite, GeoTIFF-Kacheln
CRS     : EPSG:25833
Lizenz  : Datenlizenz Deutschland -- Namensnennung 2.0 (Open Data seit 2024)

DGM1 als Open Data über den GeoSN-Downloadbereich, Kachelabgabe.

Vorgehen: Kacheln der Zielregion einmal herunterladen, Verzeichnis in
config.yaml eintragen:

    elevation:
      enabled: true
      source: state
      state: sachsen
      states:
        sachsen:
          path: data/dem/sachsen
          pattern: "*.tif"

Die Download-Links der Länder ändern sich regelmäßig; sie stehen deshalb
bewusst nicht im Code.
"""

from __future__ import annotations

from .common import LocalTileSource, WcsSource

SOURCE_INFO = {
    "state": "sachsen",
    "label": "Sachsen",
    "agency": "GeoSN (Staatsbetrieb Geobasisinformation und Vermessung Sachsen)",
    "portal": "https://www.geodaten.sachsen.de/",
    "product": "DGM1",
    "resolution_m": 1.0,
    "crs": "EPSG:25833",
    "licence": "dl-de/by-2-0",
    "notes": "DGM1 als Open Data über den GeoSN-Downloadbereich, Kachelabgabe.",
}


def make_source(cfg: dict):
    """Baut die Höhenquelle für Sachsen aus der config."""
    scfg = ((cfg.get("elevation", {}) or {}).get("states", {}) or {}).get("sachsen", {}) or {}
    if scfg.get("wcs_url"):
        return WcsSource(
            url=str(scfg["wcs_url"]),
            coverage_id=str(scfg.get("coverage_id", "dgm1")),
            resolution_m=float(scfg.get("resolution_m", 1.0)),
            crs=str(scfg.get("crs", SOURCE_INFO["crs"])),
            name="sachsen_wcs",
            extra=scfg.get("wcs_extra", {}) or {},
        )
    return LocalTileSource(
        path=str(scfg.get("path", "data/dem/sachsen")),
        pattern=str(scfg.get("pattern", "*.tif")),
        resolution_m=float(scfg.get("resolution_m", 1.0)),
        name="sachsen_dgm1",
        src_crs=scfg.get("crs", SOURCE_INFO["crs"]),
    )
