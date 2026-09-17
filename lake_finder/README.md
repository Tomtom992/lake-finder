# lake_finder — naturbelassene, abgelegene Seen aus offenen Geodaten

Findet innerhalb einer frei definierbaren Region Seen, deren Umgebung möglichst
wenig menschliche Bebauung besitzt und überwiegend aus Wald besteht. Ausgabe:
`results.csv`, `results.geojson`, eine Folium-Karte und eine **mobile-first
Weboberfläche**, die sich vom Handy aus bedienen lässt.

Kein Google-Maps-Scraping. Ausschließlich offene, automatisiert nutzbare Quellen:
OpenStreetMap (Overpass bzw. lokale PBF-Extrakte), Overture Maps Buildings,
ESA WorldCover 10 m, amtliche DGM1-Höhenmodelle und optional Sentinel-2 über
einen offenen STAC-Katalog.

> **Release v2.0.0-rc1:** Installation, Tests, Betriebsarten (live /
> precomputed) und Cloud-Deployment inklusive der PowerShell-Befehle stehen
> in **[RELEASE.md](RELEASE.md)**. Schnellprüfung einer Installation:
> `python tools/smoke_test.py`.
>
> **Ausbaustufe zum Wild-Camp-/Remote-Lake-Spot-Finder:** Was sich gegenüber
> der ersten Version geändert hat — dreiwertige Filter (PASS/FAIL/UNKNOWN),
> sieben getrennte Teil-Scores, segmentierte Uferanalyse, zweite
> Gebäudequelle, echte Zugangspunkte, Gelände- und Zeltflächenanalyse —
> steht vollständig in **[CHANGES.md](CHANGES.md)**, inklusive der Befehle
> für den lokalen Vergleichslauf.

---

## 1. Architektur

```
                 ┌─────────────────┐   ┌──────────────────────┐
  Region (BBox)  │ Overpass / PBF  │   │ ESA WorldCover 10 m  │
        │        │  OpenStreetMap  │   │  COG auf AWS S3      │
        ▼        └────────┬────────┘   └──────────┬───────────┘
 ┌───────────────┐        │                       │
 │  src/lakes.py │◄───────┘  Seepolygone          │
 │  Ways + Multi-│           (Ways + Relationen,  │
 │  polygone     │            Inseln als Löcher)  │
 └───────┬───────┘                                │
         │  GeoDataFrame, metrisches CRS          │
         ▼                                        ▼
 ┌────────────────────┐              ┌────────────────────────┐
 │ src/osm_features.py│              │  src/landcover.py      │
 │ Infrastruktur,     │              │  Ringe statt Scheiben:  │
 │ STRtree-Index,     │              │  ring(d)=buffer(d)\See │
 │ Zählung + Distanz  │              │  Klassenanteile je Ring │
 │ AB UFERLINIE       │              │  (Ufergürtel = 100 m)   │
 └─────────┬──────────┘              └───────────┬────────────┘
           └──────────────┬─────────────────────┘
                          ▼
              ┌──────────────────────┐      ┌──────────────────┐
              │  src/scoring.py      │◄─────┤  config.yaml     │
              │  gewichteter Score,  │      │  Gewichte,       │
              │  harte Filter,       │      │  Rampen, Filter  │
              │  Qualitäts-Flags     │      └──────────────────┘
              └──────────┬───────────┘
                         ▼
        ┌────────────────────────────────────────┐
        │  src/map.py                            │
        │  results.csv · results.geojson         │
        │  map.html (Folium) · app.html (mobil)  │
        └────────────────┬───────────────────────┘
                         ▼
             main.py (CLI)     app.py (Streamlit-Web-UI)
```

### Die fünf Entwurfsentscheidungen, auf die es ankommt

**1. Distanzen immer ab Uferlinie, nie ab Mittelpunkt.**
Gepuffert wird das Seepolygon selbst. Bei einem 500 ha großen, langgestreckten
See liegen Mittelpunkt und Ufer mehrere Kilometer auseinander — eine Messung ab
Mittelpunkt würde genau die Seen bevorzugen, die groß genug sind, um ihre eigene
Bebauung zu verstecken.

**2. Alles Metrische in einem metrischen CRS.**
`pick_metric_crs()` wählt die UTM-Zone des Regionsmittelpunkts (bei großen
europäischen Regionen ETRS89/LAEA, EPSG:3035). In EPSG:4326 wird nie gepuffert,
gemessen oder Fläche gerechnet. Ein Grad Länge ist auf 53° N rund 67 km, ein Grad
Breite 111 km — ein „Buffer" in Grad wäre eine Ellipse mit 40 % Achsenverhältnis.

**3. Ringe statt Scheiben beim Land Cover.**
`ring(d) = buffer(d) \ Seepolygon`. Der 100-m-Ring ist damit exakt der geforderte
Ufergürtel, und die eigene Wasserfläche fällt automatisch aus dem Nenner. Wasser
benachbarter Seen wird zusätzlich aus dem Nenner genommen (`exclude_water_from_
denominator`), sonst würde ein See dafür bestraft, dass neben ihm noch ein See liegt.

**4. Eine gebündelte Abfrage statt vieler.**
Für alle Seen zusammen wird *eine* Overpass-Abfrage pro Kachel bzw. pro Batch von
25 Seen gestellt, die sämtliche Kategorien in einem Block holt. Anschließend
arbeitet ein shapely-`STRtree` (R-Baum) die Zählungen und Nächste-Nachbar-Suchen
lokal ab. Ergebnis für eine 30 × 30 km-Region mit ~100 Seen: rund 5–8
HTTP-Requests statt mehrerer tausend.

**5. Der Score ist getrennt von den harten Filtern.**
Ein See, der durchfällt, wird markiert (`passes_filters=false`,
`filter_reasons`), nicht gelöscht. So sieht man, *warum* ein plausibler Kandidat
fehlt — das ist beim Kalibrieren der Schwellen die halbe Miete.

---

## 2. Datenquellen (Stand geprüft: September 2026)

### OpenStreetMap über Overpass

Vier Endpunkte werden rotierend genutzt; bei HTTP 429/504/5xx wird der nächste
probiert, mit exponentiellem Backoff und Respekt vor `Retry-After`.

| Endpunkt | Rolle |
|---|---|
| `overpass-api.de/api/interpreter` | Hauptinstanz |
| `overpass.kumi.systems/api/interpreter` | leistungsfähiger Mirror |
| `lz4.overpass-api.de/api/interpreter` | für große Abfragen |
| `z.overpass-api.de/api/interpreter` | schnell bei kurzen Abfragen |

Ein aussagekräftiger `User-Agent` ist Pflicht: die Hauptinstanz beantwortet
Requests mit generischem oder fehlendem User-Agent teilweise mit **HTTP 406**.
Der Client setzt ihn (`osm.user_agent` in der config) zusammen mit `Accept` und
`Accept-Encoding`.

Abgefragte Tags: `natural=water` (+ optional `landuse=reservoir`) für Seen;
`building=*`, `landuse=residential|commercial|industrial|retail|farmyard|quarry|…`,
`tourism=camp_site|caravan_site|hotel|chalet|…`, `leisure=marina|slipway`,
`harbour=*`, `mooring=*`, `amenity=parking`, `man_made=*` (nur technische Werte),
`power=plant|substation|line|tower`, `railway=*` (nur aktive), `highway=*`.

### ESA WorldCover 10 m

```
Bucket : s3://esa-worldcover        (eu-central-1, anonym lesbar)
HTTPS  : https://esa-worldcover.s3.eu-central-1.amazonaws.com
Pfad   : v200/2021/map/ESA_WorldCover_10m_2021_v200_<TILE>_Map.tif
TILE   : 3×3-Grad-Kachel nach SW-Ecke, z. B. N51E012
Format : Cloud Optimized GeoTIFF, EPSG:4326, 10 m, uint8, nodata 0
```

Legende: 10 Baumbedeckung · 20 Strauch · 30 Gras · 40 Acker · 50 Siedlung ·
60 vegetationsarm · 70 Schnee/Eis · 80 Wasser · 90 Feuchtgebiet · 95 Mangrove ·
100 Moos/Flechte.

**Dokumentierte Entscheidung:** Es existieren genau zwei Versionen — v100 (2020)
und v200 (2021). Eine neuere WorldCover-Version gibt es Stand 2026 nicht. Wir
nehmen v200. Geprüfte Alternativen und warum sie hier nicht gewählt wurden:

| Alternative | Auflösung / Stand | Warum nicht Primärquelle |
|---|---|---|
| Esri/Impact Observatory Annual LULC | 10 m, jährlich bis aktuell | Assets über Planetary Computer brauchen eine (kostenlose) Signatur; zusätzliche Abhängigkeit |
| Google Dynamic World | 10 m, nahezu tagesaktuell | nur über Earth Engine mit Konto und Auth |
| CORINE Land Cover | 100 m, 2018 | zu grob für einen 100-m-Ufergürtel (ein Pixel = der halbe Ring) |
| Copernicus HRL Tree Cover Density | 10 m, 2018 | gute Ergänzung, deckt aber nur Baumdichte ab, keine Siedlung/Acker |

WorldCover wird als Cloud Optimized GeoTIFF **fensterweise über HTTP** gelesen
(`/vsicurl/`), es wird also nicht die ganze 3°-Kachel geladen. Passt die Region
in `landcover.region_read_max_px` (Default 8000 × 8000 Pixel ≈ 80 × 80 km), wird
sie einmal am Stück gelesen und alle Seen werden im Arbeitsspeicher ausgewertet —
das spart hunderte Range-Requests. Mit `mode: download` lassen sich die Kacheln
stattdessen vollständig zwischenspeichern.

### Sentinel-2 (optional, zweite Verifikationsstufe)

Earth Search v1 (STAC) von Element 84 auf den offenen L2A-COGs in AWS:
`https://earth-search.aws.element84.com/v1`, Collection `sentinel-2-l2a`, kein
Login. `main.py --sentinel` schreibt für die Top-Treffer einen wolkenarmen
True-Color-Ausschnitt nach `output/chips/`.

### OSRM (optional, Fahrzeit)

Öffentlicher Demoserver `router.project-osrm.org`, Table-Service (ein Request für
viele Ziele). Nicht für Dauerlast gedacht — bei Fehlern fällt der Code
automatisch auf die Luftlinien-Schätzung zurück, sichtbar in
`drive_time_source`.

---

## 3. Code

```
lake_finder/
├── main.py                  CLI-Pipeline
├── app.py                   mobile-first Streamlit-Oberfläche
├── config.yaml              alle Schwellen, Gewichte, Filter
├── requirements.txt
├── README.md
├── .streamlit/config.toml
├── CHANGES.md               Ausbaustufe: was neu ist, Migration, lokale Befehle
├── src/
│   ├── utils.py             Bbox, CRS-Wahl, Cache, Overpass-Client, Rampen
│   ├── lakes.py             Seen holen, Multipolygone zusammensetzen
│   ├── osm_features.py      Infrastruktur, Kategorien, STRtree, Metriken
│   ├── landcover.py         WorldCover-Kacheln, Ringe, Zonalstatistik, Sampler
│   ├── shoreline.py         Ufersegmente, P10, gestörte Abschnitte
│   ├── filters.py           harte Filter mit PASS / FAIL / UNKNOWN
│   ├── scoring.py           sieben getrennte Scores, Confidence, Profile
│   ├── access.py            Zugangspunkte aus dem Straßennetz, Routing
│   ├── travel.py            Fahrzeit (Luftlinie oder OSRM)
│   ├── terrain.py           Neigung, Rauigkeit, Senken, Abfluss, Nässe
│   ├── camp.py              Suchkorridor, Zeltgrundriss mit Rotationen
│   ├── viewshed.py          Sichtbarkeit, Abschirmung, Privacy-Exposure
│   ├── spots.py             Orchestrierung der Spot-Analyse
│   ├── buildings/           osm · overture · official · merge
│   ├── elevation/           DGM-Quellen je Bundesland + gemeinsame Schnittstelle
│   ├── map.py               CSV/GeoJSON, Folium-Karte, Web-Oberfläche
│   ├── sentinel.py          optionale Sentinel-2-Chips
│   ├── osm_pbf.py           Alternative: lokale PBF-Verarbeitung
│   └── templates/viewer.html   die mobile Oberfläche (Leaflet)
├── tools/make_demo_data.py  synthetischer Datensatz zum Ausprobieren der UI
├── tests/test_core.py            46 Tests (Grundlagen, erste Version)
├── tests/test_milestones.py      79 Tests (Filter, Ufer, Gebäude, Gelände, Szenarien A–J)
├── tests/test_config_contract.py  7 Tests (jede config-Metrik wird auch berechnet)
├── data/                    Caches (Overpass-JSON, WorldCover, OSRM)
├── web/                     Demo-Ausgabe
└── output/                  results.csv, results.geojson, map.html, app.html
```

### Der Score im Detail

```
base      = 100 · Σ(wᵢ · rampᵢ) / Σwᵢ
penalties = Σ Abzüge
score     = clamp(base − penalties, 0, 100)
```

Jede Komponente ist eine lineare Rampe zwischen zwei Schwellen aus der
`config.yaml`. `ramp: [40, 95]` heißt „0 Punkte bei 40 %, voll bei 95 %";
`ramp: [5, 0]` dreht die Richtung um (weniger ist besser). Kein Kriterium kann
den Score allein nach oben ziehen — der Test
`test_single_criterion_cannot_max_the_score` prüft genau das.

Voreingestellte Gewichte: Ufergürtel-Wald 3.0 · Abstand Gebäude 2.5 ·
Wald 500 m 2.0 · wenig Siedlung 1.5 · Abstand Hauptstraße 1.5 ·
Wald 1000 m / naturnahe Fläche / wenig Acker / Abstand Wohngebiet je 1.0.
Abzüge u. a.: 3 Punkte je Gebäude im 500-m-Ring (max. 25), 6 Punkte je Gebäude im
100-m-Ring (max. 30), −20 Campingplatz, −18 Marina, −20 Wohngebiet, −8 Bahnlinie.

**Erreichbarkeit geht bewusst nicht in den Wildnis-Score ein** (`weight: 0.0`) —
sie ist ein Filterkriterium, keine Eigenschaft des Sees. Wer sie einrechnen will,
setzt das Gewicht hoch.

Harte Filter (Voreinstellung): Wald 500 m ≥ 60 %, Ufer-Wald ≥ 70 %,
Gebäude im 500-m-Ring ≤ 3, Siedlungsanteil ≤ 1 %, Ackeranteil ≤ 15 %,
Abstand Hauptstraße ≥ 300 m, Fahrzeit ≤ 120 min.

---

## 4. Installation

```bash
git clone <dein-repo> && cd lake_finder
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.11 oder 3.12. Alle Geo-Pakete liefern Wheels mit gebündeltem GEOS, PROJ
und GDAL — eine systemweite GDAL-Installation ist **nicht** nötig. Auf Apple
Silicon und Windows funktioniert derselbe Befehl.

Tests:

```bash
python -m unittest discover -s tests -v
```

---

## 5. Beispielaufrufe

### Kommandozeile

```bash
# Testlauf: 30 × 30 km, Müritz-NP Ost / Feldberger Seenlandschaft
python main.py

# andere Voreinstellung (alle ≤ 2 h ab Berlin)
python main.py --preset stechlin
python main.py --preset schorfheide
python main.py --preset dahme_heideseen

# freie Bounding Box [West Süd Ost Nord]
python main.py --bbox 13.05 53.20 13.55 53.47

# Mittelpunkt + Kantenlänge, mit echtem Routing statt Luftlinie
python main.py --center 13.03 53.15 --size-km 40 --travel-mode osrm

# großer Lauf mit Sentinel-2-Sichtkontrolle der Top-20
python main.py --preset brandenburg_gross --sentinel
```

Ausgabe nach `output/`: `results.csv`, `results.geojson`, `buffers.geojson`,
`map.html` (Folium), `app.html` (mobile Oberfläche), `lake_finder.log`.

### Weboberfläche lokal

```bash
streamlit run app.py --server.address 0.0.0.0
```

Streamlit gibt eine **Network-URL** aus (`http://192.168.x.x:8501`). Die im
Handy-Browser öffnen, solange Handy und Rechner im selben WLAN sind.

### Weboberfläche als dauerhafte URL

**Streamlit Community Cloud** (kostenlos, für dieses Projekt der einfachste Weg):

1. Repository zu GitHub pushen (öffentlich oder privat).
2. `share.streamlit.io` → *New app* → Repo, Branch, `app.py` wählen.
3. Python 3.11 einstellen; `requirements.txt` wird automatisch installiert.
4. Nach ein paar Minuten steht eine `https://<name>.streamlit.app`-URL, die auf
   jedem Handy funktioniert. Als Lesezeichen zum Homescreen hinzufügen.

Zu beachten beim Hosting:

* Das Dateisystem ist **flüchtig**. Die Caches unter `data/` überleben einen
  Neustart nicht — der erste Lauf nach dem Aufwachen dauert wieder voll.
  `@st.cache_data` hält Ergebnisse im laufenden Prozess vor (TTL 14 Tage).
* Community Cloud schläft nach Inaktivität ein; der erste Aufruf braucht dann
  30–60 s.
* Overpass sieht bei gehosteten Apps viele Anfragen von einer IP. `min_interval_s`
  nicht unter 1.5 setzen und den `user_agent` auf einen eigenen Kontakt ändern.
* Für harten Dauerbetrieb: Ergebnisse einmal per CLI für eine große Region
  rechnen, `results.geojson` mit ins Repo legen und die App nur noch filtern
  lassen — dann ist sie sofort schnell und belastet keine fremden Server.

Alternativen: Hugging Face Spaces (Streamlit-Template), Railway/Render/Fly.io,
oder ein kleiner VPS mit `streamlit run` hinter einem Reverse Proxy.

### Karte ohne Server

`output/app.html` ist eine einzelne, eigenständige Datei mit eingebetteten Daten.
Per AirDrop/Cloud aufs Handy, im Browser öffnen, fertig — kein Server nötig. Nur
die Kartenkacheln kommen aus dem Netz.

Die Oberfläche ausprobieren, ohne etwas zu rechnen:

```bash
python tools/make_demo_data.py     # schreibt web/demo.html (synthetische Daten)
```

---

## 6. Mögliche Schwächen

Diese Liste ist nicht Höflichkeit, sondern Bedienungsanleitung. Wer sie ignoriert,
fährt 100 km zu einem See mit Bootssteg.

**OSM-Vollständigkeit ist die größte Fehlerquelle.**
Außerhalb von Siedlungen ist OSM lückenhaft: einzelne Forsthütten, Jagdkanzeln,
Stege, Bootshäuser, Zufahrten und Wochenendgrundstücke fehlen häufig. Ein Score
von 95 heißt „in diesen Daten ist nichts verzeichnet", nicht „hier ist nichts".
Gegenmittel im Code: das Flag `osm_luecke_moeglich` schlägt an, wenn WorldCover
Siedlungsfläche sieht (≥ 0,5 %), OSM aber kein einziges Gebäude kennt; die
`confidence`-Stufe sinkt dann auf „niedrig". Trotzdem gilt: **immer im
Satellitenbild gegenprüfen**, dafür sind die Links im Popup da.

**Der Land-Cover-Stand ist 2021.**
Windwurf, Borkenkäferbefall, Kahlschlag und Neubau der letzten Jahre fehlen. In
Brandenburg und Mecklenburg ist das relevant — nach den Dürrejahren sind ganze
Kiefernbestände weg. Sentinel-2 (`--sentinel`) ist die Gegenprobe.

**Kleine Seen sind statistisch wackelig.**
Bei 2 ha Fläche (Radius ~80 m) enthält der 100-m-Ufergürtel rund 5000
10-m-Pixel — noch brauchbar. Bei 0,5 ha sind es ~2000, und Mischpixel an der
Uferlinie (halb Wasser, halb Ufervegetation) verzerren spürbar. Flag:
`kleiner_see_raster_grob`.

**Distanzen sind zensiert.**
Infrastruktur wird nur im Umkreis `osm.feature_radius_m` (Default 3000 m) geholt.
„3000 m" in der Ausgabe heißt „mindestens 3000 m", nicht „genau 3000 m". Die
Spalten `distance_*_censored` machen das explizit, im Score ist der Effekt
unkritisch, weil die Rampen vorher sättigen.

**WorldCover-Klasse „Baumbedeckung" ist nicht „Wald".**
Eine Pappelplantage, eine Kiefernmonokultur im Erntealter und ein Buchen-Urwald
haben dieselbe Klasse 10. Der Score misst Geschlossenheit der Baumdecke, nicht
Naturnähe des Bestandes. Wer Letzteres will, braucht Waldstrukturdaten
(z. B. Copernicus Forest Type, nationale Forstinventuren).

**Uferpolygon ≠ Wasserlinie.**
OSM-Seeumrisse stammen oft aus Luftbildern verschiedener Jahre und Pegelstände.
Bei flachen, verlandenden Seen weicht die Uferlinie um Dutzende Meter ab, was
direkt in den Ufergürtel durchschlägt.

**Die Fahrzeit-Schätzung ist optimistisch.**
Luftlinie × 1,3 / 75 km/h ignoriert, dass die letzten 10 km oft Sandpiste sind.
Für belastbare Werte `--travel-mode osrm`.

**Was gar nicht geprüft wird: ob man dort sein darf.**
Naturschutzgebiete, Nationalpark-Kernzonen, Uferbetretungsverbote, Privatbesitz
und Badeverbote sind in der Auswertung nicht enthalten. Das ist kein Randproblem:
Die am besten bewerteten Seen liegen fast per Konstruktion in Schutzzonen — die
Serrahner Buchenwälder sind UNESCO-Welterbe mit Wegegebot. Ein hoher Score ist
ein Hinweis auf Naturnähe, keine Einladung.

**Gegenkräfte im Score selbst.** Zwei Effekte ziehen in entgegengesetzte
Richtungen und sollten beim Kalibrieren im Blick bleiben:

* Große Seen haben mehr Uferlänge und damit mehr Gelegenheit für ein einzelnes
  Gebäude — sie werden systematisch schlechter bewertet als kleine, obwohl sie
  landschaftlich beeindruckender sein können. Wer das nicht will, normiert
  `building_count_*` auf die Uferlänge (`perimeter_m` steht in der Ausgabe).
* Umgekehrt profitieren sehr kleine Seen von der Rasterauflösung: wenige,
  homogene Waldpixel im Ufergürtel ergeben leicht 98 %. Deshalb der
  `min_area_ha`-Filter und das Kleinseen-Flag.

**Kippbedingung für die harten Filter.** Bei `min_shore_tree_cover: 70` und
`max_buildings_500m: 3` bestehen in einer typischen brandenburgischen Region nur
wenige Prozent der Seen. Liefert ein Lauf null Treffer, ist fast immer nicht die
Region schuld, sondern die Kombination der Schwellen — in der Web-Oberfläche
lassen sie sich live verschieben, ohne die Daten neu zu holen.

---

## 7. Verbesserungsmöglichkeiten

**Naheliegend**

* `building_count` auf die Uferlänge normieren (Gebäude pro km Ufer) statt als
  Absolutzahl zu zählen.
* Sichtbarkeit statt Distanz: mit einem DGM (z. B. Copernicus DEM 30 m) prüfen,
  ob ein Gebäude vom Ufer aus überhaupt **sichtbar** ist. Ein Haus 300 m entfernt
  hinter einem Hügel stört weniger als eines 800 m entfernt in Sichtachse.
* Lärm statt Straßendistanz: Verkehrsstärke aus OSM-Tags (`maxspeed`, `lanes`,
  `ref`) grob in eine Emissionsklasse übersetzen.
* Schutzgebiete als Layer: `boundary=protected_area`, `leisure=nature_reserve`
  aus OSM holen und als eigene Spalte ausgeben — nicht als Abzug, sondern als
  rechtlicher Hinweis.
* Anfahrt bis zum Ufer: Distanz vom nächsten öffentlichen Parkplatz zum Ufer
  entlang des Wegenetzes (OSRM-Foot-Profil) — das unterscheidet „abgelegen" von
  „unerreichbar".

**Aufwändiger, aber lohnend**

* Zweite Land-Cover-Quelle als Kreuzvalidierung (Esri/IO Annual LULC, jährlich
  aktuell) und ein Konsistenz-Flag, wenn beide Quellen widersprechen.
* Sentinel-2-Zeitreihe statt Einzelbild: NDVI-Trend über 3 Jahre im Ufergürtel
  erkennt Kahlschlag und Absterben automatisch.
* Sentinel-1-Kohärenz oder Sentinel-2-Change-Detection zur Erkennung neuer
  Bebauung nach 2021.
* Eigenes Overpass-Instanz oder tägliche PBF-Diffs, wenn die Region groß wird
  (`osm.backend: pbf` ist dafür schon vorbereitet).
* Uferlinie aus dem Raster statt aus OSM: WorldCover-Wasserklasse zu Polygonen
  vektorisieren und mit OSM abgleichen — löst das Pegelstands-Problem teilweise.
* Ergebnisse versionieren und Läufe vergleichen („was hat sich seit letztem Jahr
  verändert?").

---

## Lizenzen und Namensnennung

* OpenStreetMap-Daten: © OpenStreetMap-Mitwirkende, ODbL 1.0. Bei
  Weiterverbreitung von Ableitungen ist die Namensnennung Pflicht.
* ESA WorldCover: © ESA WorldCover project / Contains modified Copernicus
  Sentinel data, CC BY 4.0.
* Sentinel-2: Contains modified Copernicus Sentinel data.
* Kartenkacheln: OpenStreetMap-Standardstil bzw. Esri World Imagery /
  OpenTopoMap — jeweils deren Nutzungsbedingungen beachten, für Dauerlast eigene
  Kachelquelle verwenden.
* Google-Maps-Links in den Popups sind normale externe Nutzerlinks, kein
  automatisierter Abruf.
