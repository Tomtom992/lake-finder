# lake_finder v2.0.0-rc1.1 — Release Candidate

**rc1.1 gegenüber rc1:** nur Test-Isolation, keine Änderung am Produktivcode.
Die beiden Tests `test_live_analysis_failure_does_not_crash_the_app` und
`test_live_mode_still_shows_precomputed_data_after_failure` haben den
Live-Pfad ungemockt aufgerufen. In einer Umgebung ohne Geo-Stack scheiterte
der zufällig vorher am Import — mit installiertem Geo-Stack hätten sie
Overpass tatsächlich abgefragt. Jetzt wird `main.run_data` für genau diese
beiden Tests durch ein Modul ersetzt, das sofort
`RuntimeError("simulated live analysis failure")` wirft, und die gesamte
Testklasse läuft hinter einem Socket-Riegel: jeder Verbindungsversuch bricht
den Test mit klarer Meldung ab, statt still ins Netz zu gehen.

Stabilisierungsstand vor der Multi-Source-Elevation-Engine. **Keine neuen
Analysefeatures**, keine Änderungen an DGM, Terrain, Camp-Spots oder Viewshed.
Was dazugekommen ist: eine definierte Betriebsart, vorberechnete
Ergebnisdaten, Absturzsicherheit und ein Rauchtest, der den Zustand in
Sekunden prüfbar macht.

---

## Abnahmekriterien — Stand

| Kriterium | Stand |
|---|---|
| Bestehende Architektur nicht neu geschrieben | erfüllt — nur `app.py` umgebaut (Modusumschaltung), sonst additiv |
| Keine Regressionen | **171 Tests, alle grün** (134 vorher + 37 neu) |
| Kein Netzzugriff in Unit-Tests | erfüllt — Live-Pfad gemockt, Socket-Riegel in der App-Testklasse |
| `python main.py --preset stechlin` | Code unverändert lauffähig; **lokal zu verifizieren** (siehe unten) |
| `results.csv` / `results.geojson` / `map.html` / `app.html` | Erzeugung unverändert; `app.html` zusätzlich ohne Geo-Stack erzeugbar |
| `streamlit run app.py` | App wird in Tests vollständig ausgeführt (Stub); echter Start **lokal zu verifizieren** |
| `DEPLOYMENT_MODE` live / precomputed | erfüllt, inkl. harter Sperre im precomputed-Modus |
| Cloud standardmäßig precomputed möglich | erfüllt — `LAKE_FINDER_MODE=precomputed`, Bündel unter `data/processed/` |
| Live-Ausfall stürzt die App nicht ab | erfüllt und getestet (`test_live_analysis_failure_does_not_crash_the_app`) |
| Smoke-Test | `tools/smoke_test.py`, 14 Prüfungen, Exitcode-tauglich |

**Ehrlich dazu:** Die Sandbox, in der dieser Stand entstanden ist, hat keinen
Netzzugang und keinen Geo-Stack. Alles, was ohne geopandas/Netz prüfbar ist,
ist geprüft und automatisiert. Der echte Overpass-Lauf und der echte
`streamlit run` sind die zwei Dinge, die **du** einmal bestätigen musst —
dafür sind die Befehle unten.

---

## Was neu ist

### 1. Betriebsart (`src/deployment.py`)

```
live         Region wählen, Daten holen, rechnen (wie bisher)
precomputed  nur vorberechnete Bündel; der Live-Pfad ist HART gesperrt
auto         precomputed, wenn ein Bündel vorliegt, sonst live
```

Reihenfolge, erste gewinnt:
`--mode` → `LAKE_FINDER_MODE` → `config.yaml: deployment.mode` → `auto`.

Die Sperre ist keine Konvention, sondern eine Ausnahme: `guard_live()` wirft
`LiveModeDisabled`, wenn im precomputed-Modus doch jemand die Datenstufe
aufruft. Der ganze Sinn des Modus ist, dass beim Öffnen der Seite
garantiert nichts nach draußen geht — das darf nicht von Disziplin abhängen.

Die **Kommandozeile** benutzt weiter standardmäßig `live`, weil sie das
Werkzeug ist, das rechnet. `python main.py --mode precomputed` bricht mit
Exitcode 2 und einer Erklärung ab, statt still doch Daten zu holen.

### 2. Vorberechnete Ergebnisbündel (`src/store.py`)

```
data/processed/<name>/
    manifest.json      Region, Zeitstempel, Zählungen, Versionen, Quellen
    app_data.json      kartenfertige FeatureCollection mit ALLEN Metriken
    results.geojson    vollständige Ergebnisse für QGIS
    results.csv        Tabelle
    spots.json         Campingflächen, falls gerechnet
```

Der Kniff steckt in `app_data.json`: Geometrien vereinfacht, Pufferringe
gerechnet, **alle** Metriken als Properties. Damit lassen sich im
precomputed-Modus Karte, Filter und Gewichte neu rechnen — mit `json` und
`pandas`, ohne geopandas, ohne eine einzige Netzwerkanfrage. Die Regler in
der App bleiben also live, nur die Rohdaten sind eingefroren.

Das ist nicht behauptet, sondern geprüft: ein Test blockiert `geopandas`,
`shapely`, `rasterio`, `pyproj` und `folium` aktiv im Importsystem und lässt
den precomputed-Pfad trotzdem durchlaufen.

Warum kein schlichtes `data/processed/results.geojson`, wie du vorgeschlagen
hattest: GeoJSON-Treiber können nur flache Attributwerte. Die
Score-Aufschlüsselung, die Filter-Begründungen und die Pufferringe müssten
als JSON-Strings hineingequetscht und beim Lesen wieder geparst werden. Das
Bündel hat stattdessen ein Manifest (welche Region, wann gerechnet, mit
welcher Version) und eine Datei, die die Karte direkt versteht.
`results.geojson` liegt trotzdem mit im Bündel — für QGIS.

### 3. Absturzsicherheit

- `main.py` importiert den Geo-Stack erst in den Funktionen. `--version`,
  `--help` und die Modus-Sperre funktionieren auch ohne geopandas; eine
  fehlende Abhängigkeit endet in einer verständlichen Meldung mit dem
  Installationsbefehl statt in einem nackten `ImportError`.
- Die App fängt Fehler beim Laden der Config, beim Lesen der Bündel, bei der
  Live-Analyse, beim Bewerten und beim Kartenbau **einzeln** ab. Schlägt die
  Live-Analyse fehl, erscheint die Ursache als Text, die technischen Details
  hinter einem Aufklapper — und die vorberechneten Daten bleiben sichtbar.
- Kaputte Manifeste werden übersprungen, nicht geworfen. Ein Bündel in einem
  neueren Format wird als solches benannt statt halb gelesen.

### 4. Smoke-Test (`tools/smoke_test.py`)

14 Prüfungen, keine davon mit Netzwerkzugriff: Python-Version, Pakete,
Modulimporte, `config.yaml` (inkl. der Prüfung, ob die config Metriken
referenziert, die niemand berechnet), `app.py`-Syntax und -Importe,
Betriebsart, Live-Sperre, Bündel, Neubewertung, Kartenbau, Ausgaben des
letzten Laufs.

Exitcode 0 = bestanden. `--strict` wertet auch übersprungene Prüfungen
(fehlender Geo-Stack) als Fehler — das ist die Einstellung für CI auf einem
Rechner, auf dem alles installiert sein soll.

---

## Windows PowerShell — die Befehle

Alle Blöcke sind so gemeint, wie sie dastehen: Zeile für Zeile in eine
PowerShell einfügen. `<...>` ist das Einzige, was du ersetzen musst.

### 1. Frische Installation

```powershell
# Projektordner wählen und hineinwechseln
cd $HOME\Projekte
# Variante A: aus Git
git clone <REPO-URL> lake_finder
# Variante B: ZIP entpackt -> nur hineinwechseln
cd lake_finder

# Python prüfen (3.11 oder 3.12; py -0p listet alle Installationen)
py -0p
py -3.12 --version

# Virtuelle Umgebung anlegen und aktivieren
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1

# Abhängigkeiten
python -m pip install --upgrade pip
pip install -r requirements.txt

# Optional: zweite Gebäudequelle (Overture)
pip install duckdb
```

Steht danach `(.venv)` am Anfang der Eingabezeile, ist die Umgebung aktiv.
Ohne `Set-ExecutionPolicy` verweigert PowerShell das Aktivierungsskript —
die Zeile gilt nur für dieses Fenster, sie ändert nichts dauerhaft.

### 2. Tests

```powershell
# Alle Tests (erwartet: Ran 171 tests ... OK)
python -m unittest discover -s tests -v
"Exitcode: $LASTEXITCODE"

# Kurzfassung ohne Einzelausgabe
python -m unittest discover -s tests

# Deployment-Rauchtest (erwartet: alle 14 Prüfungen bestanden)
python tools\smoke_test.py
"Exitcode: $LASTEXITCODE"

# Streng: fehlender Geo-Stack zählt als Fehler
python tools\smoke_test.py --strict
```

Ein Einzeiler, der beides prüft und klar Stellung bezieht:

```powershell
python -m unittest discover -s tests; $t=$LASTEXITCODE; python tools\smoke_test.py; $s=$LASTEXITCODE
if ($t -eq 0 -and $s -eq 0) { Write-Host "v2-rc1: GRUEN" -ForegroundColor Green } else { Write-Host "v2-rc1: ROT (tests=$t smoke=$s)" -ForegroundColor Red }
```

### 3. Echter Stechlin-Lauf

```powershell
# Alten Lauf sichern, damit man vergleichen kann
if (Test-Path output\results.csv) { Copy-Item output\results.csv output\results_OLD.csv -Force }

# Lauf (Overpass-Cache greift, falls schon vorhanden)
python main.py --mode live --preset stechlin
"Exitcode: $LASTEXITCODE"

# Erzeugte Dateien ansehen
Get-ChildItem output\ | Select-Object Name, @{n='kB';e={[math]::Round($_.Length/1KB,1)}}, LastWriteTime

# Die vier geforderten Artefakte einzeln bestätigen
@('results.csv','results.geojson','map.html','app.html') | ForEach-Object {
  $p = "output\$_"
  if (Test-Path $p) { Write-Host ("OK   {0,-18} {1,8:N1} kB" -f $_, ((Get-Item $p).Length/1KB)) -ForegroundColor Green }
  else              { Write-Host ("FEHLT {0}" -f $_) -ForegroundColor Red }
}

# Karten im Browser öffnen
Start-Process output\app.html
Start-Process output\map.html
```

Gleicher Lauf, aber zusätzlich ein vorberechnetes Bündel für die Cloud:

```powershell
python main.py --mode live --preset stechlin --export-precomputed
Get-ChildItem data\processed -Recurse | Select-Object FullName, Length
```

Vergleich alt/neu (nach dem Umbau der Filter in v2):

```powershell
python tools\compare_runs.py

# andere Dateien oder andere Seen
python tools\compare_runs.py --old output\results_OLD.csv --new output\results.csv
python tools\compare_runs.py --lakes Zermitten Gerlin
```

Das Skript zeigt, wie viele Seen vorher und nachher bestanden haben, welche
Seen ihren Status verloren oder gewonnen haben (mit Begründung), und die
Einzelwerte der genannten Seen. **Erwartung für den Kleinen Zermittensee:**
`passes_filters = False`, `filter_status = FAIL`, mit Gründen zu
Gebäudeabstand, Gebäuden im 100-/250-m-Ring, Wohngebiet und Campingplatz.

### 4. Lokaler Streamlit-Test

```powershell
# a) Live-Modus (Region neu rechnen möglich)
$env:LAKE_FINDER_MODE = "live"
streamlit run app.py

# b) Precomputed-Modus — genau das, was später in der Cloud läuft
$env:LAKE_FINDER_MODE = "precomputed"
streamlit run app.py

# c) Vom Handy im gleichen WLAN erreichbar
$env:LAKE_FINDER_MODE = "precomputed"
streamlit run app.py --server.address 0.0.0.0
# die eigene IP für den Browser des Handys:
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } | Select-Object IPAddress, InterfaceAlias
# -> http://<IP>:8501 im Handy-Browser

# Beenden: Strg+C im Fenster. Variable wieder entfernen:
Remove-Item Env:\LAKE_FINDER_MODE
```

Worauf du im precomputed-Modus achten solltest: es gibt **keinen** Reiter
„Suchen", unten steht `Modus precomputed`, und im Browser-Netzwerktab
erscheinen beim Öffnen keine Anfragen an `overpass-api.de` oder
`esa-worldcover.s3...`. Die Kartenkacheln kommen weiterhin aus dem Netz —
das ist der Kartenhintergrund, nicht die Analyse.

### 5. Cloud-Deployment (Streamlit Community Cloud)

Der precomputed-Modus braucht **kein** geopandas. Deshalb ein eigener,
schlanker Abhängigkeitssatz: vier Pakete statt des ganzen Geo-Stacks —
Kaltstart in Sekunden statt Minuten, und der fehleranfälligste Teil einer
Cloud-Installation fällt komplett weg.

```powershell
# 0) Sicherstellen, dass ein Bündel existiert und mitgeliefert wird
python main.py --mode live --preset stechlin --export-precomputed
python tools\smoke_test.py          # muss grün sein

# 1) Deploy-Branch mit schlankem requirements.txt
git checkout -b deploy
Copy-Item requirements-cloud.txt requirements.txt -Force

# 2) Bündel MUSS mit ins Repo (data/processed ist nicht ignoriert)
git add -f data\processed
git add -A
git commit -m "v2.0.0-rc1: precomputed deployment (Stechlin-Bundle)"
git push -u origin deploy
```

Dann im Browser auf **share.streamlit.io**:

1. *New app* → Repository wählen → **Branch: `deploy`** → **Main file: `app.py`**
2. *Advanced settings* → Python-Version 3.11 oder 3.12
3. *Advanced settings* → *Secrets*, dort genau diese Zeile eintragen:

```toml
LAKE_FINDER_MODE = "precomputed"
```

4. *Deploy*. Nach dem Start unten auf der Seite prüfen:
   `Modus precomputed (Umgebungsvariable LAKE_FINDER_MODE)`.

Neue Daten später einspielen — auf dem eigenen Rechner rechnen, dann pushen:

```powershell
git checkout main
python main.py --mode live --preset stechlin --export-precomputed
git checkout deploy
git checkout main -- data\processed
git add -f data\processed
git commit -m "Daten aktualisiert: Stechlin"
git push
```

Die Cloud-App startet nach dem Push automatisch neu.

---

## Fehlerbilder und was sie bedeuten

| Meldung | Ursache | Abhilfe |
|---|---|---|
| `Der Geo-Stack fehlt oder ist unvollständig` (Exit 3) | venv nicht aktiv oder Installation unvollständig | `.\.venv\Scripts\Activate.ps1`, dann `pip install -r requirements.txt` |
| `Modus 'precomputed': die Kommandozeile … darf das nicht` (Exit 2) | `LAKE_FINDER_MODE` steht auf precomputed | `python main.py --mode live …` oder `Remove-Item Env:\LAKE_FINDER_MODE` |
| App: `Modus 'precomputed', aber … kein vorberechneter Datensatz` | Bündel fehlt im Repo | `--export-precomputed` laufen lassen, `git add -f data\processed` |
| App: `Die Live-Analyse ist fehlgeschlagen` | Overpass überlastet (429/504) oder offline | Minuten warten; vorberechnete Daten bleiben nutzbar |
| Rauchtest: `config referenziert nur berechnete Metriken` als Warnung | Tippfehler in `config.yaml` | genannten Metriknamen korrigieren |
| `Activate.ps1 kann nicht geladen werden` | PowerShell-Ausführungsrichtlinie | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force` |

---

## Was bewusst NICHT angefasst wurde

DGM, Terrain, Camp-Spot-Generator, Zeltflächen-Footprint, Viewshed, Privacy —
alles unverändert, wie gewünscht. `src/elevation/`, `src/terrain.py`,
`src/camp.py`, `src/viewshed.py` und `src/spots.py` haben in diesem Release
keine inhaltliche Änderung erfahren; ihre Tests laufen unverändert mit.

Die Multi-Source-Elevation-Engine setzt auf diesem Stand auf.
