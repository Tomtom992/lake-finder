"""
lake_finder -- naturbelassene, abgelegene Seen und Campingflaechen aus
offenen Geodaten.

Dieses Paket enthaelt bewusst keine schweren Importe auf Modulebene:
``import src`` funktioniert auch ohne geopandas, shapely oder rasterio.
Der precomputed-Modus und der Smoke-Test haengen davon ab.
"""

from __future__ import annotations

VERSION = "2.0.0-rc1.1"

#: Version des Ergebnisformats (Manifest, app_data.json). Aendert sich nur,
#: wenn alte Buendel nicht mehr gelesen werden koennen.
BUNDLE_FORMAT_VERSION = 1

__all__ = ["VERSION", "BUNDLE_FORMAT_VERSION"]
