"""
deployment.py -- Betriebsart: live oder precomputed.

Warum das eine eigene Sache ist
-------------------------------
Die Analyse ist teuer und haengt an fremden Diensten (Overpass, S3). Auf
einem gehosteten Server ist beides falsch: der erste Besucher wartet
Minuten, und wenn Overpass gerade ueberlastet ist, sieht er eine
Fehlerseite. Fuer die Cloud gehoert deshalb ein Modus her, der
ausschliesslich aus einem vorberechneten Ergebnisbuendel liest und beim
Oeffnen **keine einzige** Anfrage nach draussen stellt.

Die drei Modi
-------------
``live``         wie bisher: Region waehlen, Daten holen, rechnen.
``precomputed``  nur vorberechnete Buendel. Der Analyse-Pfad ist gesperrt;
                 ein versehentlicher Aufruf wirft ``LiveModeDisabled``
                 statt still doch ins Netz zu gehen.
``auto``         precomputed, wenn ein Buendel vorliegt, sonst live.
                 Sinnvoller Standard fuer den Rechner zu Hause.

Reihenfolge der Quellen (erste gewinnt)
---------------------------------------
1. ausdruecklicher Parameter (CLI ``--mode``)
2. Umgebungsvariable ``LAKE_FINDER_MODE``  (so setzt man es in der Cloud)
3. ``deployment.mode`` in der config.yaml
4. Standard ``auto``

Dieses Modul ist bewusst frei von schweren Abhaengigkeiten -- es wird
importiert, bevor irgendetwas anderes geladen wird.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("lake_finder.deployment")

LIVE = "live"
PRECOMPUTED = "precomputed"
AUTO = "auto"
MODES = (LIVE, PRECOMPUTED, AUTO)

ENV_MODE = "LAKE_FINDER_MODE"
ENV_DATA_DIR = "LAKE_FINDER_DATA_DIR"

DEFAULT_DATA_DIR = "data/processed"


class LiveModeDisabled(RuntimeError):
    """Im precomputed-Modus wurde ein Live-Datenabruf versucht."""


@dataclass(frozen=True)
class Deployment:
    """Aufgeloeste Betriebsart samt Begruendung -- die Begruendung ist
    wichtig, weil sie in der Oberflaeche angezeigt wird."""

    mode: str
    data_dir: Path
    source: str
    requested: str
    bundles_available: int = 0

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE

    @property
    def is_precomputed(self) -> bool:
        return self.mode == PRECOMPUTED

    def describe(self) -> str:
        if self.is_precomputed:
            return (
                f"Vorberechnete Daten aus {self.data_dir} "
                f"({self.bundles_available} Datensatz/Datensätze). "
                "Beim Öffnen werden keine externen Dienste abgefragt."
            )
        return "Live-Analyse: Overpass und ESA WorldCover werden bei Bedarf abgefragt."


def _normalise(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v in MODES:
        return v
    LOG.warning("Unbekannter Deployment-Modus '%s' -- ignoriert. Erlaubt: %s",
                value, ", ".join(MODES))
    return None


def data_dir_from(cfg: dict | None = None, override: str | None = None) -> Path:
    """Verzeichnis mit den vorberechneten Buendeln."""
    if override:
        return Path(override)
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env)
    dcfg = ((cfg or {}).get("deployment", {}) or {})
    return Path(str(dcfg.get("data_dir", DEFAULT_DATA_DIR)))


def resolve(
    cfg: dict | None = None,
    requested: str | None = None,
    data_dir: str | Path | None = None,
    bundle_count: int | None = None,
) -> Deployment:
    """Ermittelt die Betriebsart.

    ``bundle_count`` kann uebergeben werden, wenn der Aufrufer die Buendel
    schon gezaehlt hat; sonst wird das Verzeichnis selbst geprueft (nur
    Dateisystem, kein Netzwerk).
    """
    cfg = cfg or {}
    dcfg = cfg.get("deployment", {}) or {}
    directory = data_dir_from(cfg, str(data_dir) if data_dir else None)

    if bundle_count is None:
        bundle_count = _count_bundles(directory)

    mode = _normalise(requested)
    source = "Parameter"
    if mode is None:
        mode = _normalise(os.environ.get(ENV_MODE))
        source = f"Umgebungsvariable {ENV_MODE}"
    if mode is None:
        mode = _normalise(dcfg.get("mode"))
        source = "config.yaml (deployment.mode)"
    if mode is None:
        mode = AUTO
        source = "Standard"

    requested_mode = mode
    if mode == AUTO:
        mode = PRECOMPUTED if bundle_count > 0 else LIVE
        source += f" -> auto: {mode}"

    return Deployment(
        mode=mode,
        data_dir=directory,
        source=source,
        requested=requested_mode,
        bundles_available=bundle_count,
    )


def _count_bundles(directory: Path) -> int:
    """Zaehlt vorberechnete Buendel, ohne etwas zu laden."""
    try:
        if not directory.exists():
            return 0
        n = len(list(directory.glob("*/manifest.json")))
        if (directory / "manifest.json").exists():
            n += 1
        return n
    except OSError:  # pragma: no cover - Rechteprobleme o.ae.
        return 0


def guard_live(deployment: Deployment, what: str = "Datenabruf") -> None:
    """Wirft, wenn im precomputed-Modus doch ein Live-Zugriff versucht wird.

    Absichtlich eine harte Ausnahme statt einer stillen Erlaubnis: der
    ganze Sinn des Modus ist, dass beim Oeffnen der Seite garantiert
    nichts nach draussen geht.
    """
    if deployment.is_precomputed:
        raise LiveModeDisabled(
            f"{what} ist im Modus 'precomputed' gesperrt. "
            f"Zum Neuberechnen: LAKE_FINDER_MODE=live setzen oder "
            f"'python main.py --mode live ...' auf dem eigenen Rechner ausführen."
        )
