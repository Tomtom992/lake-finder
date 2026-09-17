"""
elevation/mecklenburg_vorpommern.py -- Höhenmodell Mecklenburg-Vorpommern.

Behörde : LAiV-MV
Portal  : https://www.geoportal-mv.de/
Produkt : DGM1, 1 m Gitterweite, GeoTIFF-Kacheln
CRS     : EPSG:25833
Lizenz  : Datenlizenz Deutschland -- Namensnennung 2.0 (Open Data seit 2024)

Es existiert ein WCS für das Digitale Geländemodell (WCS_MV_DGM), auffindbar über den GDI-DE-Katalog. Endpunkt in config.yaml eintragen, dann source: wcs.

Vorgehen: Kacheln der Zielregion einmal herunterladen, Verzeichnis in
config.yaml eintragen:

    elevation:
      enabled: true
      source: state
      state: mecklenburg_vorpommern
      states:
        mecklenburg_vorpommern:
          path: data/dem/mecklenburg_vorpommern
          pattern: "*.tif"

Die Download-Links der Länder ändern sich regelmäßig; sie stehen deshalb
bewusst nicht im Code.
"""

from __future__ import annotations

from .common import LocalTileSource, WcsSource

SOURCE_INFO = {
    "state": "mecklenburg_vorpommern",
    "label": "Mecklenburg-Vorpommern",
    "agency": "LAiV-MV",
    "portal": "https://www.geoportal-mv.de/",
    "product": "DGM1",
    "resolution_m": 1.0,
    "crs": "EPSG:25833",
    "licence": "dl-de/by-2-0",
    "notes": "Es existiert ein WCS für das Digitale Geländemodell (WCS_MV_DGM), auffindbar über den GDI-DE-Katalog. Endpunkt in config.yaml eintragen, dann source: wcs.",
}


def make_source(cfg: dict):
    """Baut die Höhenquelle für Mecklenburg-Vorpommern aus der config."""
    scfg = ((cfg.get("elevation", {}) or {}).get("states", {}) or {}).get("mecklenburg_vorpommern", {}) or {}
    if scfg.get("wcs_url"):
        return WcsSource(
            url=str(scfg["wcs_url"]),
            coverage_id=str(scfg.get("coverage_id", "dgm1")),
            resolution_m=float(scfg.get("resolution_m", 1.0)),
            crs=str(scfg.get("crs", SOURCE_INFO["crs"])),
            name="mecklenburg_vorpommern_wcs",
            extra=scfg.get("wcs_extra", {}) or {},
        )
    return LocalTileSource(
        path=str(scfg.get("path", "data/dem/mecklenburg_vorpommern")),
        pattern=str(scfg.get("pattern", "*.tif")),
        resolution_m=float(scfg.get("resolution_m", 1.0)),
        name="mecklenburg_vorpommern_dgm1",
        src_crs=scfg.get("crs", SOURCE_INFO["crs"]),
    )
