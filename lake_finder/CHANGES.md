# Ausbaustufe: vom Waldsee-Finder zum Wild-Camp-/Remote-Lake-Spot-Finder

> **Release Candidate v2.0.0-rc1 (Stabilisierung).** Betriebsart
> live/precomputed, vorberechnete Ergebnisbündel, Absturzsicherheit und
> Deployment-Rauchtest sind in **[RELEASE.md](RELEASE.md)** beschrieben —
> dort stehen auch die exakten PowerShell-Befehle für Installation, Tests,
> Stechlin-Lauf, Streamlit und Cloud-Deployment. An DGM, Terrain, Camp-Spots
> und Viewshed wurde dafür nichts geändert.

Diese Datei beschreibt, was gegenüber der ersten Version geändert wurde,
was rückwärtskompatibel bleibt und welche Befehle du lokal ausführen sollst.

**Nichts wurde neu geschrieben.** Alle bestehenden Module sind erhalten;
neue Funktionalität liegt in neuen Dateien oder in additiven Erweiterungen.
Die 46 Tests der ersten Version laufen unverändert durch.

---

## Phase 0 — Bestandsaufnahme

**Datenmodell (unverändert im Kern).** Die Pipeline arbeitet auf einem
GeoDataFrame mit einer Zeile je See, Geometrie im metrischen CRS
(UTM-Zone des Regionsmittelpunkts, bei großen Regionen EPSG:3035).
Schlüssel ist `lake_uid` (`w<osm_id>` bzw. `r<osm_id>`). Jede Stufe
liefert einen DataFrame mit `lake_uid` und ihren Spalten, der per
Left-Join angehängt wird. Diese Struktur ist geblieben; alle neuen Stufen
fügen sich als weitere Joins ein.

**Bestehende Spalten:** keine wurde entfernt oder umbenannt.
`wilderness_score`, `passes_filters`, `filter_reasons`, `quality_flags`,
`confidence`, alle `tree_cover_*`, `building_count_*`, `distance_*` und
`has_*` bedeuten weiterhin dasselbe.

**Rückwärtskompatibilität im Code:**

| Alt | Status |
|---|---|
| `scoring.score_row(row, cfg)` | unverändert, gleiche Rückgabe |
| `scoring.check_hard_filters(row, cfg)` | unverändert (zweiwertig), nur noch für Altkonfigurationen |
| `scoring.quality_flags(row, cfg)` | erweitert um neue Flags, gleiche Signatur |
| `scoring.score_table(df, cfg)` | dritter Parameter `profile` ist optional |
| `scoring.components` / `scoring.penalties` in config.yaml | bleibt die Definition des Wildnis-Scores |
| `scoring.hard_filters` in config.yaml | wird weiterhin gelesen; in der mitgelieferten config nach `filters:` umgezogen |
| `main.run()` | liefert jetzt `(seen, spots, kontext)` statt nur `seen` |

Die einzige bewusste Verhaltensänderung: `passes_filters` ist strenger
geworden. Das war der Auftrag.

---

## Milestone 1 — Filter mit drei Zuständen, getrennte Scores

### Der behobene Fehler

Der Kleine Zermittensee bestand die Filter mit einem Gebäude 60,9 m vom
Ufer, Campingplatz, Wohngebiet und Parkplatz in Reichweite. Zwei Ursachen:

1. Die Filterliste prüfte **weder Gebäudeabstand noch die `has_*`-Flags** —
   nur Waldanteil, Gebäudezahl im 500-m-Ring, Siedlungs- und Ackeranteil,
   Straßenabstand und Fahrzeit. `max_buildings_500m: 3` ließ zwei Gebäude
   durch, und ein Campingplatz war nirgends ein Ausschlusskriterium.
2. Ein fehlender Wert galt als bestanden (`if val is None: continue`).

### Neu: `src/filters.py`

Jeder Check liefert **PASS, FAIL oder UNKNOWN**. UNKNOWN ist kein PASS.

```yaml
filters:
  fail_on_missing_critical_data: true    # Standard für die hochwertige Suche
  checks:
    min_distance_nearest_building_m:
      metric: distance_nearest_building_m
      min: 300
      requires: [buildings]              # Datengrundlage, an die der Check gebunden ist
      critical: true
```

`requires` ist der entscheidende Teil: Ist die Datengrundlage für diesen
See nicht verfügbar (`landcover_ok = false`, keine Gebäudequelle, keine
Ufersegmente), wird der Check UNKNOWN — **auch dann, wenn zufällig ein
Wert in der Zeile steht**. Ein Wert ohne Quelle ist keine Aussage.

Der Gesamtstatus: ein FAIL genügt für FAIL; sonst UNKNOWN, wenn ein
kritischer Check unbekannt ist; sonst PASS.
`passes_filters = (PASS) oder (UNKNOWN und fail_on_missing = false)`.

**Neue Spalten:** `filter_status`, `filter_fail_reasons`,
`filter_unknown_reasons`, `filter_n_fail`, `filter_n_unknown`,
`filter_checks` (vollständiges Protokoll je Check).

### Neue harte Filter (Standardwerte)

| Check | Standard |
|---|---|
| `min_distance_nearest_building_m` | ≥ 300 m |
| `max_buildings_100m` | 0 |
| `max_buildings_250m` | 0 |
| `max_buildings_500m` | 2 |
| `exclude_residential` / `exclude_campsite` / `exclude_marina` / `exclude_accommodation` | keines in Reichweite (1000 m) |
| `exclude_parking` | vorhanden, standardmäßig **aus** — ein Wanderparkplatz ist auch der Zugang |
| `max_developed_shore_percent` | ≤ 10 % |
| `min_shore_p10` | ≥ 45 |
| `min_tree_cover_500m` / `min_shore_tree_cover` | 60 % / 70 % |
| `max_drive_time` | ≤ 120 min, `critical: false` |

Mit diesen Werten fällt der Kleine Zermittensee mit fünf Begründungen
durch. Ein Test sichert das ab (`test_strict_defaults_reject_the_zermittensee`).

### Getrennte Scores

`scoring.scores.<block>` definiert je Block Komponenten und Abzüge, in
derselben Syntax wie bisher:

```
wilderness_score            visual_seclusion_score      access_score
privacy_exposure_score      campsite_suitability_score  safety_score
data_confidence_score       overall_spot_score (+ overall_coverage)
```

Regeln, die im Code durchgesetzt sind:

* Ein Block ohne Konfiguration liefert **None, nicht 0** — „nicht
  gerechnet" ist etwas anderes als „schlecht".
* `data_confidence_score` geht **nicht** in `overall_spot_score` ein.
  Score 92 bei Confidence 61 ist nicht dasselbe wie Score 61.
* `overall_coverage` sagt, wie viel des Gesamtgewichts wirklich mit
  Werten belegt war.
* Profile (`balanced`, `max_seclusion`, `easy_access`, `best_tent`,
  `best_view`, `max_privacy`) verschieben **nur Gewichte**; es wird nichts
  neu berechnet.

---

## Milestone 2 — Segmentierte Uferanalyse (`src/shoreline.py`)

Die äußere Uferlinie wird in Segmente von 25 m zerlegt (konfigurierbar).
Je Segment: Land Cover im 100-m-Umkreis, Distanzen zu acht
Infrastrukturkategorien, Objektzahl im 150-m-Umkreis. Daraus ein
0–100-Wert je Segment über dieselbe Score-Engine.

Aggregation je See:

```
shore_segment_mean_score / median / p10 / min
developed_shore_fraction, developed_shore_percent
natural_shore_fraction,  natural_shore_percent
longest_developed_section_m, longest_natural_section_m
worst_shore_segment  (Index, Score, Koordinate, Ursache)
```

Zwei Details, die man leicht falsch macht und die getestet sind:

* **Längengewichtete Perzentile.** Segmente sind nicht exakt gleich lang
  (Ringschluss, mehrere Ringe). Ungewichtet würden kurze Stücke
  überbewertet.
* **Ringschluss beim längsten Abschnitt.** Ein gestörter Uferabschnitt,
  der über den willkürlichen Startpunkt der Uferlinie hinweggeht, wird als
  **ein** Abschnitt gezählt, nicht als zwei.

**Zur Wirkung von P10 — ehrlich gerechnet:** Bei genau 5 % gestörtem Ufer
liegt das P10 noch im guten Bereich; das ist arithmetisch richtig und
genau der Grund, warum `visual_seclusion` zusätzlich `shore_segment_min_score`
(Gewicht 1,0), `natural_shore_percent` (2,0) und einen Abzug auf
`developed_shore_percent` (1,5 Punkte je Prozent) verwendet. Ab etwa 10 %
gestörtem Ufer greift das P10 selbst. Im Test liegt der Unterschied
zwischen „100 % natürlich" und „95 % Wald + 5 % Dorf" bei über
15 Score-Punkten.

---

## Milestone 3 — Zweite Gebäudequelle (`src/buildings/`)

```
src/buildings/
    osm.py        Gebäude aus der bereits geholten Feature-Ebene (keine neue Abfrage)
    overture.py   Overture Maps Buildings über DuckDB + S3-Parquet
    official.py   amtliche Hausumringe aus lokalen Dateien (höchste Priorität)
    merge.py      Deduplizierung, Quellenzähler, Widerspruchserkennung
```

Priorität `official > overture > osm`. Die Deduplizierung arbeitet auf
Schwerpunktabstand (Standard 12 m) und Flächenverhältnis (max. 4:1, damit
eine Halle nicht die Hütte daneben schluckt) und ist **reihenfolge-
unabhängig** — auch das ist getestet.

Neue Spalten: `building_osm_count`, `building_overture_count`,
`building_official_count`, `building_source_count`,
`building_sources_disagree`, `building_count_merged`, `building_data_ok`
sowie `building_<quelle>_count_<d>m` je Pufferdistanz.

**Widerspruch senkt Confidence, nicht den Sachscore.**
`building_sources_disagree` wird gesetzt, wenn eine **abgefragte** Quelle
(fast) nichts sieht und eine andere deutlich mehr — der Fall „OSM 0,
Overture 5". Eine gar nicht konfigurierte Quelle mit 0 Gebäuden ist kein
Widerspruch, sondern eine Lücke; dafür gibt es `building_source_count`
und das Flag `nur_eine_gebaeudequelle`.

Overture ist standardmäßig **aus** (`buildings.overture.enabled: false`),
weil es `duckdb` voraussetzt. Einschalten:

```bash
pip install duckdb
# config.yaml: buildings.overture.enabled: true
```

Release-String steht in der config (`2026-08-19.0`); ein neuer
Monatsrelease ist ein Einzeiler, keine Codeänderung.

---

## Milestone 4 — Echte Zugangspunkte (`src/access.py`)

Nicht mehr zum See-Mittelpunkt routen — der liegt im Wasser.

1. Straßen im Umkreis von 2500 m aus der vorhandenen Feature-Ebene.
2. Je Straße der ufernächste Punkt, zusätzlich Abtastung alle 250 m.
3. Bewertung: Fußweg (Gewicht 3), Straßenklasse (1,5), Abzug 45 für
   private/gesperrte Zufahrt (`access`, `motor_vehicle`, `vehicle`),
   Abzug 8 für reine Waldwege.
4. Die besten drei Kandidaten je See werden geroutet, der beste bleibt.

Neue Spalten: `drive_access_lat`, `drive_access_lon`,
`walking_distance_to_lake_m`, `walking_distance_source`,
`access_road_class`, `access_road_name`, `access_via_private_road`,
`access_point_missing`, `access_candidates`, `access_score`.

**Ehrlichkeitsgrenze:** Der Fußweg ist Luftlinie × 1,35, **kein Routing
auf dem Wegenetz** — der öffentliche OSRM-Demoserver bietet nur das
Auto-Profil. `walking_distance_source` sagt das in jeder Zeile. Mit einem
lokalen OSRM/Valhalla mit Fußprofil (`access.foot_router_url`) wird daraus
echtes Routing.

Dazu neu erfasste OSM-Tags: `access`, `motor_vehicle`, `vehicle`,
`surface`, `tracktype`.

---

## Milestone 5–7 — Gelände, Zeltfläche, Nässe, Sichtbarkeit, Privacy

### `src/terrain.py`

Reines numpy auf einem Höhenraster: `slope_aspect` (Horn 1981),
`roughness`, `local_relief`, `curvature`, `fill_depressions`
(Priority-Flood nach Barnes et al.), `depression_depth`,
`flow_accumulation` (D8), `wetness_index` (TWI-Proxy),
`dryness_from_terrain`.

### `src/camp.py`

Der entscheidende Punkt: **eine Zeltfläche ist eine Fläche.** Geprüft wird
ein rotierbares Rechteck (Standard 4 × 6 m, auch 3 × 4, 3 × 5, 5 × 7 oder
frei), in 15°-Schritten gedreht. `tent_fit` ist nur dann `true`, wenn der
**komplette Grundriss** die Schwellen einhält — Maximum der Neigung und
Relief über die ganze Fläche, nicht der Mittelwert.

Ausgabe je Kandidat: `tent_fit`, `best_tent_orientation_deg`,
`flat_area_m2`, `mean_slope_deg`, `max_slope_deg`, `relief_cm`,
`roughness_cm`, `depression_depth_m`, `distance_to_water_m`,
`height_above_water_m`, `dryness_score`, `wetness_risk`.

Suchkorridor 20–250 m vom Wasser, konfigurierbar. **Erlaubte
Gebietskulisse:** `camp.allowed_area_geojson`. Ohne diese Datei trägt
jeder Kandidat `allowed_area_checked: false` — es wird **keine Annahme
über Betretungsrechte** getroffen.

### `src/viewshed.py`

R2-Sichtbarkeitsanalyse (Franklin & Ray). Ein Detail, das die erste
Implementierung falsch hatte und das ein Test jetzt festhält: der Winkel,
der die **Sicht auf ein Ziel** entscheidet (Zieloberkante), ist ein
anderer als der, der **dahinter verdeckt** (Geländeoberfläche). Setzt man
beide gleich, ist auf ebener Fläche mit Zielhöhe > 0 alles ab der zweiten
Zelle scheinbar verdeckt.

Abschirmung wird nach Ursache getrennt, über drei Läufe (ebene Fläche /
DGM / DOM): `terrain_screening_percent` und
`vegetation_screening_percent`. Ohne DOM bleibt letzteres **None**, nicht
0 — „nicht gemessen" ist nicht „keine Deckung".

### Privacy-Exposure

Beschreibt ausschließlich, **wie einsehbar** ein legal genutzter Platz von
öffentlich zugänglichen Bereichen ist: `exposure_from_roads`,
`exposure_from_paths`, `exposure_from_buildings`,
`terrain_screening_percent`, `vegetation_screening_percent`,
`visible_buildings_count`, `visible_road_length_m`,
`visible_path_length_m`, `lake_visibility_percent`.

Waldwege sind bewusst getrennt modelliert: Gewicht 0,8 gegenüber 2,0 für
Straßen, flachere Rampe, und in der Zugangsbewertung sind sie positiv.
Ein Weg ist gut für die Anfahrt und nur leicht negativ für die Ruhe.

Der Score modelliert **keine Entdeckungswahrscheinlichkeit** — nur
Sichtlinien.

### `src/elevation/`

```
common.py                 DemSource-Schnittstelle, LocalTileSource, WcsSource,
                          CopernicusDsmSource, exakte Distanztransformation
brandenburg.py            Portal, Lizenz, CRS, Bezugsweg je Land
mecklenburg_vorpommern.py
sachsen.py
sachsen_anhalt.py         (UTM-Zone 32, nicht 33)
thueringen.py             (UTM-Zone 32)
states.py                 Auswahl
```

**Warum kein automatischer DGM1-Download:** Die Geobasisdaten sind seit
Juni 2024 Open Data (DL-DE/BY-2.0), der Bezug ist es nicht einheitlich.
Der bundesweite BKG-Downloaddienst für DGM1 muss pro Nutzer freigeschaltet
werden und liefert über Meta4-Dateien mit persönlicher UUID; die
Länderportale haben je eigene Formate, Kachelschemata und
Registrierungswege. Ein hart verdrahteter Downloader wäre in drei Monaten
kaputt. Also: einmal herunterladen, Pfad in die config.

Als Rückfall ohne DGM1 gibt es Copernicus DEM GLO-30 (weltweit, anonym auf
AWS). **Das ist ein 30-m-Oberflächenmodell** — es taugt für Hangneigung im
Landschaftsmaßstab und ist für eine Zeltfläche um zwei Größenordnungen zu
grob. Die Pipeline führt `dem_resolution_m` überall mit und setzt das Flag
`dgm_zu_grob_fuer_zeltflaeche`, sobald > 2 m.

---

## Neue Ausgaben

* `output/results.csv` — um alle neuen Spalten erweitert, keine entfernt
* `output/results.geojson` — verschachtelte Werte als JSON-String, damit
  jedes GIS die Datei lesen kann
* `output/spots.csv` — Spot-Kandidaten (nur mit `--spots`)
* `output/app.html` — zeigt jetzt Teil-Scores als Chipreihe, Datenvertrauen,
  Uferkennzahlen und, wenn vorhanden, die Spot-Kandidaten je See (Ebene 2)

---

## Tests

```
tests/test_core.py             46 Tests   (unverändert, alle grün)
tests/test_milestones.py       79 Tests   (neu)
tests/test_config_contract.py   7 Tests   (neu)
                              ---------
                              132 Tests
```

`test_config_contract.py` ist der Test, der sich am ehesten auszahlt: er
führt eine explizite Liste aller Spalten, die die Pipeline erzeugen kann,
und prüft **jede** Metrik aus `config.yaml` dagegen. Ein Tippfehler in der
config fällt sonst nicht auf — die Score-Engine behandelt ihn als
„fehlend", zieht still den `missing`-Wert ab und liefert plausible Zahlen
auf falscher Grundlage. Der Test hat beim Schreiben genau einen solchen
Fall gefunden (`unscreened_open_ground` mit 12 Punkten Gewicht auf einer
Metrik, die ohne DOM nie berechnet wird).

Die Szenarien A–J aus der Anforderung sind als Tests umgesetzt
(`TestScenarios`).

Sieben Tests werden übersprungen, wenn geopandas/shapely fehlen — das
betrifft nur die Geometrie-Tests aus der ersten Version.

---

## Was lokal zu tun ist

### 1. Tests

```bash
cd lake_finder
python -m unittest discover -s tests -v
# erwartet: Ran 132 tests ... OK
```

### 2. Stechlin neu rechnen und mit dem alten Lauf vergleichen

```bash
# alten Lauf sichern
cp output/results.csv output/results_OLD.csv

# neu rechnen (Overpass-Cache ist noch gültig, das geht schnell)
python main.py --preset stechlin
```

Erwartung, die du prüfen solltest:

* deutlich weniger als 54 Treffer — mit den strengen Standardwerten
  wahrscheinlich eine einstellige Zahl
* `filter_status` zeigt PASS / FAIL / UNKNOWN statt nur wahr/falsch
* **Kleiner Zermittensee: `passes_filters = False`, `filter_status = FAIL`**,
  `filter_reasons` nennt Gebäudeabstand, Gebäude im 100-/250-m-Ring,
  Wohngebiet und Campingplatz
* Gerlinsee, Großer Stiegsee, Wotzensee, Breutzensee: prüfen, ob sie
  weiterhin bestehen; falls einer auf UNKNOWN steht, sagt
  `filter_unknown_reasons`, welche Datengrundlage fehlt

Vergleich der beiden Läufe:

```bash
python - <<'EOF'
import pandas as pd
old = pd.read_csv("output/results_OLD.csv")
new = pd.read_csv("output/results.csv")
m = old.merge(new, on="osm_id", suffixes=("_old", "_new"))
print("Seen gesamt:", len(old), "->", len(new))
print("bestanden :", int(old.passes_filters.sum()), "->", int(new.passes_filters.sum()))
print(new.filter_status.value_counts(), "\n")
cols = ["name_new", "wilderness_score_old", "wilderness_score_new",
        "passes_filters_old", "passes_filters_new", "filter_status",
        "developed_shore_percent", "shore_segment_p10_score"]
for n in ["Zermitten", "Gerlin", "Stiegsee", "Wotzen", "Breutzen"]:
    sub = m[m.name_new.fillna("").str.contains(n, case=False)]
    if len(sub):
        print(sub[[c for c in cols if c in sub.columns]].to_string(index=False), "\n")
EOF
```

### 3. Overture als zweite Gebäudequelle (empfohlen)

```bash
pip install duckdb
# config.yaml: buildings.overture.enabled: true
python main.py --preset stechlin
```

Danach in `results.csv` prüfen: `building_osm_count` vs.
`building_overture_count` und wie oft `building_sources_disagree` steht.
Das ist die Antwort auf die Frage, wie blind OSM in dieser Region ist.

### 4. Erst danach: DGM und Spot-Analyse

```bash
# DGM1-Kacheln der Testregion vom LGB-Geobroker holen, entpacken nach
#   data/dem/brandenburg/
# config.yaml: elevation.enabled: true, camp.enabled: true
python main.py --preset stechlin --spots --profile best_tent
```

Ohne DGM1, nur zum Ausprobieren der Mechanik (30 m, **nicht** für
Zeltflächen aussagekräftig):

```yaml
elevation: {enabled: true, source: copernicus}
```

### 5. Weboberfläche

```bash
streamlit run app.py --server.address 0.0.0.0
```

Profilauswahl und die neuen Filterregler sitzen im Reiter „Karte".

---

## Was bewusst noch NICHT drin ist

| Phase | Stand |
|---|---|
| 10 — Vegetation, Lichtungen, `canopy_cover`, `ground_openness` | braucht DOM/nDOM; Konfigurationsplätze vorhanden, Gewicht 0 |
| 11 — Viewshed mit echtem DOM | Algorithmus fertig und getestet, es fehlt die DOM-Quelle |
| 17 — Vorberechnung für ganz Ostdeutschland, GeoParquet/PostGIS | PBF-Backend und Caching stehen, die Datenbankstufe nicht |
| Fußweg-Routing auf dem Wegenetz | Schätzung mit Umwegfaktor, Anschluss für lokalen Router vorbereitet |

Für die Datenbankstufe (Phase 17) die Empfehlung vorab: **GeoParquet für
die Seenstufe, SQLite/Spatialite für die Auslieferung ans Handy.**
GeoParquet, weil die Seenstufe eine spaltenorientierte Analyse über
Millionen Zeilen ist (Filter auf wenige Spalten, Prädikat-Pushdown, gute
Kompression) und DuckDB sie ohne Server liest — dieselbe Technik, die hier
schon für Overture benutzt wird. SQLite/Spatialite für die Auslieferung,
weil eine Datei ohne Server auf dem Telefon liegen kann. PostGIS lohnt erst,
wenn mehrere Nutzer gleichzeitig schreiben — das ist hier nicht der Fall.
