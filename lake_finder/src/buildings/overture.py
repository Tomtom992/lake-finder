"""
buildings/overture.py -- Overture Maps Buildings als zweite Gebaeudequelle.

Warum diese Quelle
------------------
Overture buendelt u. a. maschinell aus Luft- und Satellitenbildern
extrahierte Gebaeudeumringe (Microsoft, Google) mit OSM. Genau in den
Gegenden, um die es hier geht -- Waldrand, Einzelgehoeft, Bootshaus am
See -- ist OSM lueckenhaft, waehrend die Bildextraktion das Dach trotzdem
sieht. Umgekehrt erzeugt die Bildextraktion Fehlalarme (Silos, Container,
Schattenwuerfe). Deshalb wird Overture NICHT als Wahrheit behandelt,
sondern als zweite Meinung: Widerspruch senkt die Confidence.

Zugriff (Stand der Doku, September 2026)
----------------------------------------
Parquet-Release auf S3, anonym lesbar, ueber DuckDB mit den Erweiterungen
``httpfs`` und ``spatial``:

    s3://overturemaps-us-west-2/release/<RELEASE>/theme=buildings/type=building/*

Release wird in config.yaml gesetzt (``buildings.overture.release``), damit
ein neuer Monatsrelease ohne Codeaenderung uebernommen werden kann. Die
Spalten ``bbox.xmin/ymin/xmax/ymax`` erlauben ein Praedikat-Pushdown, das
nur die benoetigten Row-Groups liest -- ohne das laedt man versehentlich
ein Weltdatenset.

Ergebnisse werden als GeoJSON je Bounding Box gecacht; ein zweiter Lauf
derselben Region fasst S3 nicht mehr an.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..utils import Bbox, cache_key

LOG = logging.getLogger("lake_finder.buildings.overture")

DEFAULT_RELEASE = "2026-08-19.0"
DEFAULT_S3 = "s3://overturemaps-us-west-2/release/{release}/theme=buildings/type=building/*"


class OvertureUnavailable(RuntimeError):
    pass


def _cache_path(cfg: dict, bbox: Bbox, release: str) -> Path:
    bcfg = (cfg.get("buildings", {}) or {}).get("overture", {}) or {}
    d = Path(bcfg.get("cache_dir", "data/cache/overture"))
    d.mkdir(parents=True, exist_ok=True)
    return d / f"buildings_{release}_{cache_key(bbox.as_list())}.geojson"


def _query_sql(path: str, bbox: Bbox, limit: int | None, wkb_expr: str) -> str:
    lim = f"\nLIMIT {int(limit)}" if limit else ""
    return f"""
SELECT
  id,
  height,
  num_floors,
  class,
  {wkb_expr} AS wkb
FROM read_parquet('{path}', filename=true, hive_partitioning=1)
WHERE bbox.xmin <= {bbox.east:.6f}
  AND bbox.xmax >= {bbox.west:.6f}
  AND bbox.ymin <= {bbox.north:.6f}
  AND bbox.ymax >= {bbox.south:.6f}{lim}
"""


def load(bbox: Bbox, cfg: dict, metric_crs: str):
    """Laedt Overture-Gebaeude fuer die Box und liefert das gemeinsame Schema."""
    import geopandas as gpd

    bcfg = (cfg.get("buildings", {}) or {}).get("overture", {}) or {}
    if not bcfg.get("enabled", False):
        LOG.info("Overture ist deaktiviert (buildings.overture.enabled = false).")
        return None

    release = str(bcfg.get("release", DEFAULT_RELEASE))
    cache = _cache_path(cfg, bbox, release)
    if cache.exists() and cache.stat().st_size > 2:
        LOG.info("Overture aus dem Cache: %s", cache.name)
        g = gpd.read_file(cache)
        return _finalise(g, metric_crs)

    try:
        import duckdb
    except ImportError as exc:
        raise OvertureUnavailable(
            "duckdb ist nicht installiert. 'pip install duckdb' oder "
            "buildings.overture.enabled auf false setzen."
        ) from exc

    from shapely import wkb as shapely_wkb

    path = str(bcfg.get("s3_path", DEFAULT_S3)).format(release=release)
    limit = bcfg.get("limit")
    con = duckdb.connect()
    for ext in ("httpfs", "spatial"):
        try:
            con.execute(f"INSTALL {ext}; LOAD {ext};")
        except Exception as exc:
            LOG.warning("DuckDB-Erweiterung %s nicht ladbar: %s", ext, exc)
    con.execute("SET s3_region='us-west-2';")

    rows = None
    last_exc: Exception | None = None
    # Je nach DuckDB-/spatial-Version ist die geometry-Spalte bereits WKB
    # oder ein GEOMETRY-Typ. Beide Varianten werden probiert, statt eine
    # Version zu raten.
    for expr in ("ST_AsWKB(geometry)", "geometry"):
        try:
            rows = con.execute(_query_sql(path, bbox, limit, expr)).fetchall()
            LOG.debug("Overture-Query erfolgreich mit '%s'.", expr)
            break
        except Exception as exc:  # pragma: no cover - versionsabhaengig
            last_exc = exc
            LOG.debug("Overture-Query mit '%s' fehlgeschlagen: %s", expr, exc)
    if rows is None:
        raise OvertureUnavailable(f"Overture-Abfrage fehlgeschlagen: {last_exc}")

    geoms, ids, heights = [], [], []
    for rid, height, _floors, _cls, blob in rows:
        if blob is None:
            continue
        try:
            geoms.append(shapely_wkb.loads(bytes(blob)))
        except Exception:  # pragma: no cover
            continue
        ids.append(f"overture:{rid}")
        heights.append(height)

    g = gpd.GeoDataFrame(
        {"source_id": ids, "source": "overture", "height": heights},
        geometry=geoms,
        crs="EPSG:4326",
    )
    LOG.info("Overture: %d Gebaeude aus Release %s.", len(g), release)
    try:
        g.to_file(cache, driver="GeoJSON")
    except Exception as exc:  # pragma: no cover
        LOG.warning("Overture-Cache nicht schreibbar: %s", exc)
    return _finalise(g, metric_crs)


def _finalise(g, metric_crs: str):
    import geopandas as gpd

    if g is None or len(g) == 0:
        return gpd.GeoDataFrame(
            {"source_id": [], "source": [], "height": []}, geometry=[], crs=metric_crs
        )
    if "source" not in g.columns:
        g["source"] = "overture"
    if g.crs is None:
        g = g.set_crs("EPSG:4326")
    g = g.to_crs(metric_crs)
    return g[g.geometry.notna() & ~g.geometry.is_empty].copy()
