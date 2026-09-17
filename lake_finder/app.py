#!/usr/bin/env python3
"""
app.py -- mobile-first Weboberflaeche (Streamlit) fuer den Waldseen-Finder.

Zwei Betriebsarten (siehe src/deployment.py):

  precomputed   Die App liest ausschliesslich vorberechnete Buendel aus
                data/processed. Beim Oeffnen geht KEINE Anfrage nach
                draussen -- kein Overpass, kein WorldCover, kein OSRM.
                Regler und Profile rechnen trotzdem live, weil alle
                Metriken im Buendel stecken. Das ist der Cloud-Modus.

  live          Region waehlen, Daten holen, rechnen. Faellt eine Quelle
                aus, zeigt die App eine klare Meldung und bleibt auf den
                vorberechneten Daten stehen, statt abzustuerzen.

Start lokal:
    streamlit run app.py
    # im gleichen WLAN vom Handy:  streamlit run app.py --server.address 0.0.0.0

Cloud (Streamlit Community Cloud): in den App-Secrets
    LAKE_FINDER_MODE = "precomputed"
setzen und ein Buendel unter data/processed/ mit einchecken.
"""

from __future__ import annotations

import copy
import json
import sys
import traceback
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

st.set_page_config(
    page_title="Waldseen-Finder",
    page_icon="🌲",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
  #MainMenu, footer, header {visibility:hidden;}
  .block-container{padding:0.6rem 0.8rem 3rem; max-width:860px;}
  h1{font-size:1.45rem !important; margin-bottom:.2rem}
  h2{font-size:1.1rem !important}
  .stButton>button{width:100%; padding:.8rem 1rem; font-size:1rem; font-weight:650;
    border-radius:12px;}
  div[data-testid="stMetricValue"]{font-size:1.25rem}
  div[data-testid="stExpander"] details{border-radius:12px}
  .stSlider{padding-top:.2rem}
  .hint{font-size:.8rem; color:#7b8a80; line-height:1.45}
  .warnbox{background:#3b2d0a; border:1px solid #6b5313; color:#fde68a;
    border-radius:10px; padding:.6rem .8rem; font-size:.83rem; line-height:1.45}
  .okbox{background:#0f2418; border:1px solid #1e4430; color:#a7f3c8;
    border-radius:10px; padding:.6rem .8rem; font-size:.83rem; line-height:1.45}
  .modebar{font-size:.78rem; color:#7b8a80; border-top:1px solid #24352c;
    margin-top:1.4rem; padding-top:.5rem; line-height:1.5}
</style>
""",
    unsafe_allow_html=True,
)


# ==========================================================================
# Start: Konfiguration und Betriebsart -- beides absturzsicher
# ==========================================================================


@st.cache_data(show_spinner=False)
def load_config() -> tuple[dict, str]:
    """Konfiguration laden. Liefert (cfg, Fehlertext)."""
    import yaml

    path = ROOT / "config.yaml"
    if not path.exists():
        return {}, f"config.yaml nicht gefunden (erwartet unter {path})."
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}, ""
    except Exception as exc:
        return {}, f"config.yaml ist nicht lesbar: {exc}"


CFG, CFG_ERROR = load_config()

st.title("🌲 Waldseen-Finder")

if CFG_ERROR:
    st.error(CFG_ERROR)
    st.stop()

from src import VERSION  # noqa: E402
from src import deployment as deploy_mod  # noqa: E402
from src import map as map_mod  # noqa: E402
from src import scoring as score_mod  # noqa: E402
from src import store as store_mod  # noqa: E402
from src.utils import Bbox, setup_logging  # noqa: E402

setup_logging("INFO", None)
PRESETS = CFG.get("presets", {}) or {}
DATA_DIR = deploy_mod.data_dir_from(CFG)


@st.cache_data(show_spinner=False, ttl=300)
def available_bundles(directory: str) -> list[dict]:
    try:
        return store_mod.list_bundles(directory)
    except Exception as exc:  # pragma: no cover
        st.warning(f"Vorberechnete Daten nicht lesbar: {exc}")
        return []


BUNDLES = available_bundles(str(DATA_DIR))
DEPLOY = deploy_mod.resolve(CFG, bundle_count=len(BUNDLES))

st.caption(
    "Abgelegene, waldumstandene Seen aus OpenStreetMap + ESA WorldCover 10 m"
    + ("  ·  vorberechnet" if DEPLOY.is_precomputed else "  ·  Live-Analyse")
)


@st.cache_data(show_spinner=False, ttl=600)
def get_bundle(path: str):
    """Ein Buendel laden. Nur Dateisystem, kein Netzwerk."""
    b = store_mod.load_bundle(path)
    problems = store_mod.validate_bundle(b)
    return b, problems


# ==========================================================================
# Bewertung -- identisch in beiden Modi
# ==========================================================================


def scoring_controls(cfg: dict) -> dict:
    """Regler fuer harte Filter und Gewichte. Aendert nur die Bewertung."""
    c = copy.deepcopy(cfg)
    checks = c.setdefault("filters", {}).setdefault("checks", {})

    def setval(name: str, key: str, value):
        checks.setdefault(name, {}).setdefault("metric", name)
        checks[name][key] = value

    with st.expander("Harte Filter", expanded=False):
        c["filters"]["fail_on_missing_critical_data"] = st.checkbox(
            "Fehlende Daten gelten als NICHT bestanden", value=True,
            help="UNKNOWN ist kein PASS. Aus: Seen mit Datenlücken erscheinen mit, "
                 "aber als UNKNOWN markiert.",
        )
        setval("min_shore_tree_cover", "min", st.slider("Wald am Ufer mind. (%)", 0, 100, 70, 5))
        setval("min_tree_cover_500m", "min", st.slider("Wald im 500-m-Ring mind. (%)", 0, 100, 60, 5))
        setval("min_distance_nearest_building_m", "min",
               st.slider("Abstand nächstes Gebäude mind. (m)", 0, 2000, 300, 25))
        setval("max_buildings_100m", "max", st.slider("Gebäude im 100-m-Ring max.", 0, 10, 0, 1))
        setval("max_buildings_250m", "max", st.slider("Gebäude im 250-m-Ring max.", 0, 20, 0, 1))
        setval("max_buildings_500m", "max", st.slider("Gebäude im 500-m-Ring max.", 0, 30, 2, 1))
        setval("max_built_up_percent", "max", st.slider("Siedlungsanteil max. (%)", 0.0, 10.0, 1.0, 0.5))
        setval("max_cropland_percent", "max", st.slider("Ackeranteil max. (%)", 0, 60, 15, 5))
        setval("min_distance_major_road", "min",
               st.slider("Abstand Hauptstraße mind. (m)", 0, 3000, 300, 50))
        setval("max_developed_shore_percent", "max",
               st.slider("Bebauter Uferanteil max. (%)", 0, 60, 10, 1))
        setval("min_shore_p10", "min",
               st.slider("Schlechtestes Uferzehntel mind. (Score)", 0, 100, 45, 5))
        setval("max_drive_time", "max", st.slider("Fahrzeit max. (min)", 30, 300, 120, 10))
        checks["max_drive_time"]["enabled"] = st.checkbox("Fahrzeit-Filter aktiv", value=True)

        st.caption("Ausschlusskriterien (im Umkreis von %d m)"
                   % int((c.get("osm", {}) or {}).get("flag_radius_m", 1000)))
        for name, lab, flag in (
            ("exclude_residential", "Wohngebiet", "has_residential"),
            ("exclude_campsite", "Campingplatz", "has_campsite"),
            ("exclude_marina", "Marina / Steg", "has_marina"),
            ("exclude_accommodation", "Hotel / Hütte", "has_accommodation"),
            ("exclude_parking", "Parkplatz", "has_parking"),
        ):
            on = st.checkbox(f"{lab} ausschließen", value=(name != "exclude_parking"))
            checks.setdefault(name, {"flag": flag, "must_be": False, "label": lab,
                                     "requires": ["osm_features"]})
            checks[name]["enabled"] = on

    with st.expander("Gewichte im Wildnis-Score", expanded=False):
        comps = c.setdefault("scoring", {}).setdefault("components", {})
        for key, label, default in (
            ("shore_tree_cover", "Wald am Ufer", 3.0),
            ("distance_building", "Abstand Gebäude", 2.5),
            ("tree_cover_500m", "Wald 500 m", 2.0),
            ("low_built_up", "wenig Siedlung", 1.5),
            ("distance_major_road", "Abstand Hauptstraße", 1.5),
            ("low_cropland", "wenig Acker", 1.0),
        ):
            if key in comps:
                comps[key]["weight"] = st.slider(label, 0.0, 5.0, default, 0.5)
    return c


def profile_picker() -> str | None:
    profiles = list(((CFG.get("scoring", {}) or {}).get("overall", {}) or {})
                    .get("profiles", {}) or {"balanced": {}})
    names = {
        "balanced": "Ausgewogen", "max_seclusion": "Maximal abgeschieden",
        "easy_access": "Leicht erreichbar", "best_tent": "Beste Zeltfläche",
        "best_view": "Beste Aussicht", "max_privacy": "Maximale Privatsphäre",
    }
    return st.selectbox(
        "Profil (verschiebt nur die Gewichte)", profiles,
        format_func=lambda k: names.get(k, k),
    )


# ==========================================================================
# Datenbeschaffung je Modus
# ==========================================================================


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24 * 14, max_entries=8)
def analyse_live(bbox_list: list[float], fingerprint: str, _cfg: dict):
    """Live-Datenstufe. Nur im Modus 'live' erreichbar.

    Nutzt dieselbe Funktion wie die Kommandozeile, damit Web und CLI
    garantiert dieselben Zahlen liefern.
    """
    import logging

    from main import run_data

    df, ctx = run_data(
        _cfg, Bbox.from_list(bbox_list), "Web", logging.getLogger("lake_finder"),
        deployment=DEPLOY,
    )
    return df, ctx


def fingerprint(cfg: dict) -> str:
    """Nur die Teile, die den Datenabruf beeinflussen -- nicht das Scoring."""
    keep = {
        "lakes": cfg.get("lakes"), "buffers": cfg.get("buffers"),
        "osm": {k: v for k, v in (cfg.get("osm") or {}).items() if k != "cache_ttl_days"},
        "landcover": cfg.get("landcover"), "travel": cfg.get("travel"),
        "access": cfg.get("access"), "shore": cfg.get("shore"), "crs": cfg.get("crs"),
    }
    return json.dumps(keep, sort_keys=True, default=str)


def current_dataset(score_cfg: dict, profile: str | None):
    """Liefert (FeatureCollection, DataFrame, Herkunftstext) fuer die Anzeige.

    Beide Modi enden hier zusammen: die Karte und die Liste sehen nur noch
    Daten, egal woher sie kamen.
    """
    src = st.session_state.get("dataset_source")
    if src == "live" and st.session_state.get("live_gdf") is not None:
        gdf = st.session_state["live_gdf"]
        scored = score_mod.score_table(gdf, score_cfg, profile)
        import geopandas as gpd

        scored = gpd.GeoDataFrame(scored, geometry="geometry", crs=gdf.crs)
        data = map_mod.build_app_geojson(scored, score_cfg, full_props=True)
        return data, scored, st.session_state.get("live_label", "Live-Analyse")

    bundle = st.session_state.get("bundle")
    if bundle is None:
        return None, None, ""
    data, scored = store_mod.rescore_bundle(bundle, score_cfg, profile)
    data = store_mod.sort_features(data)
    return data, scored, bundle.label


# ==========================================================================
# Oberflaeche
# ==========================================================================

if DEPLOY.is_precomputed:
    tabs = st.tabs(["Karte", "Liste", "Datensatz", "Info"])
    tab_map, tab_list, tab_data, tab_info = tabs
    tab_search = None
else:
    tabs = st.tabs(["Suchen", "Karte", "Liste", "Info"])
    tab_search, tab_map, tab_list, tab_info = tabs
    tab_data = None

# ---- Buendel auswaehlen (beide Modi, falls vorhanden) --------------------
if BUNDLES and st.session_state.get("bundle") is None:
    try:
        b, problems = get_bundle(BUNDLES[0]["_path"])
        if problems:
            st.warning("Vorberechneter Datensatz unvollständig: " + "; ".join(problems))
        else:
            st.session_state["bundle"] = b
            st.session_state.setdefault("dataset_source", "bundle")
    except Exception as exc:
        st.warning(f"Vorberechneter Datensatz konnte nicht geladen werden: {exc}")

if DEPLOY.is_precomputed and not BUNDLES:
    st.error(
        "Modus 'precomputed', aber unter "
        f"`{DATA_DIR}` liegt kein vorberechneter Datensatz.\n\n"
        "Einen erzeugen (auf einem Rechner mit Internet und vollem Geo-Stack):\n\n"
        "```\npython main.py --mode live --preset stechlin --export-precomputed\n```\n"
        "und den Ordner `data/processed/<name>/` mit ins Repository legen."
    )
    st.stop()

# ---------------------------- Suchen (nur live) ---------------------------
if tab_search is not None:
    with tab_search:
        st.radio(
            "Region festlegen",
            ["Voreinstellung", "Umkreis um Punkt", "Bounding Box"],
            key="region_mode", horizontal=True,
        )
        mode = st.session_state.get("region_mode", "Voreinstellung")
        if mode == "Voreinstellung":
            st.selectbox("Gebiet", list(PRESETS.keys()),
                         format_func=lambda k: PRESETS[k].get("label", k), key="preset")
            p = PRESETS[st.session_state.get("preset", next(iter(PRESETS)))]
            bbox = (Bbox.from_list(p["bbox"]) if "bbox" in p
                    else Bbox.from_center(p["center"][0], p["center"][1],
                                          float(p.get("size_km", 30))))
            label = p.get("label", "")
        elif mode == "Umkreis um Punkt":
            c1, c2 = st.columns(2)
            lon = c1.number_input("Länge (O)", value=13.30, format="%.4f")
            lat = c2.number_input("Breite (N)", value=53.33, format="%.4f")
            km = st.slider("Kantenlänge (km)", 10, 80, 30, 5)
            bbox = Bbox.from_center(lon, lat, km)
            label = f"Umkreis {km:.0f} km um {lat:.3f}, {lon:.3f}"
        else:
            c1, c2 = st.columns(2)
            w = c1.number_input("West", value=13.05, format="%.4f")
            e = c2.number_input("Ost", value=13.55, format="%.4f")
            c3, c4 = st.columns(2)
            s = c3.number_input("Süd", value=53.20, format="%.4f")
            n = c4.number_input("Nord", value=53.47, format="%.4f")
            bbox = Bbox.from_list([w, s, e, n])
            label = "Eigene Bounding Box"

        st.markdown(
            f'<div class="hint">{label} · rund {bbox.width_km:.0f} × {bbox.height_km:.0f} km '
            f"({bbox.area_km2:.0f} km²)</div>", unsafe_allow_html=True,
        )
        if bbox.area_km2 > 6000:
            st.markdown(
                '<div class="warnbox">Große Region: der erste Lauf kann mehrere Minuten '
                "dauern und belastet die öffentlichen Overpass-Server.</div>",
                unsafe_allow_html=True,
            )

        run_cfg = copy.deepcopy(CFG)
        with st.expander("Seen-Filter (was überhaupt als See zählt)"):
            c1, c2 = st.columns(2)
            run_cfg["lakes"]["min_area_ha"] = c1.number_input("Mindestfläche (ha)", 0.1, 500.0, 1.0, 0.5)
            run_cfg["lakes"]["max_area_ha"] = c2.number_input("Höchstfläche (ha)", 5.0, 20000.0, 5000.0, 50.0)
            run_cfg["lakes"]["include_reservoirs"] = st.checkbox("Stauseen einbeziehen", value=False)
        with st.expander("Erreichbarkeit"):
            run_cfg["travel"]["enabled"] = st.checkbox("Fahrzeit berechnen", value=True)
            c1, c2 = st.columns(2)
            run_cfg["travel"]["origin"] = [
                c1.number_input("Start Länge", value=13.4050, format="%.4f"),
                c2.number_input("Start Breite", value=52.5200, format="%.4f"),
            ]
            run_cfg["travel"]["mode"] = (
                "osrm" if st.radio("Verfahren", ["Luftlinie × Umwegfaktor",
                                                 "OSRM-Routing (genauer, langsamer)"]).startswith("OSRM")
                else "haversine"
            )

        if st.button("🔍 Seen suchen", type="primary"):
            with st.status("Analyse läuft …", expanded=True) as status:
                try:
                    st.write("OpenStreetMap: Seen, Infrastruktur, Gebäude …")
                    st.write("ESA WorldCover: Waldanteile je Pufferring …")
                    st.write("Ufersegmente, Zugangspunkte, Bewertung …")
                    gdf, ctx = analyse_live(bbox.as_list(), fingerprint(run_cfg), run_cfg)
                    st.session_state["live_gdf"] = gdf
                    st.session_state["live_label"] = label
                    st.session_state["run_cfg"] = run_cfg
                    st.session_state["dataset_source"] = "live"
                    st.session_state["live_error"] = None
                    status.update(label=f"Fertig – {len(gdf)} Seen analysiert.",
                                  state="complete", expanded=False)
                except Exception as exc:
                    st.session_state["live_error"] = f"{type(exc).__name__}: {exc}"
                    st.session_state["live_traceback"] = traceback.format_exc()
                    status.update(label="Live-Analyse fehlgeschlagen", state="error")

        # Fehler NACH dem status-Block anzeigen, damit die App stehen bleibt
        if st.session_state.get("live_error"):
            st.error(
                "Die Live-Analyse ist fehlgeschlagen:\n\n"
                f"`{st.session_state['live_error']}`"
            )
            st.markdown(
                '<div class="hint">Häufigste Ursachen: Overpass ist überlastet '
                "(HTTP 429/504), keine Internetverbindung, oder der Geo-Stack ist "
                "nicht vollständig installiert. Bereits geholte Kacheln sind "
                "gecacht – ein erneuter Versuch in einigen Minuten ist meist "
                "erfolgreich.</div>",
                unsafe_allow_html=True,
            )
            with st.expander("Technische Details"):
                st.code(st.session_state.get("live_traceback") or st.session_state["live_error"])
            if st.session_state.get("bundle") is not None:
                st.markdown(
                    '<div class="okbox">Die vorberechneten Daten sind weiterhin '
                    "nutzbar – Reiter „Karte“ zeigt sie unverändert an.</div>",
                    unsafe_allow_html=True,
                )
                if st.button("Auf vorberechnete Daten zurückfallen"):
                    st.session_state["dataset_source"] = "bundle"
                    st.session_state["live_error"] = None

        if st.session_state.get("dataset_source") == "live":
            st.success(f"{len(st.session_state['live_gdf'])} Seen im Speicher – Reiter „Karte“.")

# ---------------------------- Karte ---------------------------------------
with tab_map:
    if st.session_state.get("bundle") is None and st.session_state.get("live_gdf") is None:
        st.info("Noch keine Daten. Im Reiter „Suchen“ eine Region analysieren.")
    else:
        profile = profile_picker()
        base_cfg = st.session_state.get("run_cfg") or CFG
        score_cfg = scoring_controls(base_cfg)
        try:
            data, scored, origin = current_dataset(score_cfg, profile)
        except Exception as exc:
            st.error(f"Bewertung fehlgeschlagen: {exc}")
            data, scored, origin = None, None, ""

        if data is None or scored is None or len(scored) == 0:
            st.warning("Keine Daten zum Anzeigen.")
        else:
            st.session_state["scored"] = scored
            st.session_state["score_cfg"] = score_cfg
            st.session_state["last_data"] = data
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Seen", len(scored))
            c2.metric("PASS", int((scored["filter_status"] == "PASS").sum()))
            c3.metric("UNKNOWN", int((scored["filter_status"] == "UNKNOWN").sum()))
            best = scored["wilderness_score"].max()
            c4.metric("bester Score", f"{best:.0f}" if best == best else "–")

            b = st.session_state.get("bundle")
            banner = ""
            if st.session_state.get("dataset_source") != "live" and b is not None and b.is_demo:
                banner = ("<b>Demo-Datensatz.</b> Diese Seen und Messwerte sind erfunden. "
                          "Für echte Daten einen eigenen Lauf exportieren.")
            meta = map_mod.app_meta(
                data, score_cfg, banner=banner,
                spots=(b.spots if b is not None else None),
            )
            try:
                out = ROOT / "output" / "app.html"
                map_mod.build_app_from_data(data, meta, out, title=f"Waldseen – {origin}")
                components.html(out.read_text(encoding="utf-8"), height=680, scrolling=False)
                st.download_button("📱 Karte als HTML-Datei speichern",
                                   data=out.read_bytes(),
                                   file_name="waldseen_karte.html", mime="text/html")
            except Exception as exc:
                st.error(f"Karte konnte nicht erzeugt werden: {exc}")
            st.markdown(
                '<div class="hint">Karte: Filter-Button oben links, Ergebnisliste unten '
                "hochziehen, Kartenlayer mit dem Quadrat-Symbol wechseln.</div>",
                unsafe_allow_html=True,
            )

# ---------------------------- Liste ---------------------------------------
with tab_list:
    scored = st.session_state.get("scored")
    if scored is None or len(scored) == 0:
        st.info("Noch keine Ergebnisse.")
    else:
        only_pass = st.toggle("Nur Seen, die alle harten Filter bestehen", value=True)
        view = scored[scored["passes_filters"]] if only_pass else scored
        cols = [c for c in (
            "name", "wilderness_score", "visual_seclusion_score", "overall_spot_score",
            "data_confidence_score", "area_ha", "shore_tree_cover", "developed_shore_percent",
            "building_count_500m", "distance_nearest_building_m", "drive_time_min",
            "filter_status",
        ) if c in view.columns]
        st.dataframe(
            view[cols].rename(columns={
                "name": "Name", "wilderness_score": "Wildnis",
                "visual_seclusion_score": "Sicht", "overall_spot_score": "Gesamt",
                "data_confidence_score": "Konfidenz", "area_ha": "ha",
                "shore_tree_cover": "Ufer %", "developed_shore_percent": "beb. Ufer %",
                "building_count_500m": "Geb. 500 m",
                "distance_nearest_building_m": "dGeb m", "drive_time_min": "Fahrt min",
                "filter_status": "Filter",
            }),
            use_container_width=True, hide_index=True,
        )
        csv_cols = [c for c in map_mod.CSV_COLUMNS if c in view.columns]
        csv = view[csv_cols].copy()
        for c in csv.columns:
            if csv[c].map(lambda v: isinstance(v, (list, tuple))).any():
                csv[c] = csv[c].apply(
                    lambda v: "; ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v)
        st.download_button("⬇️ results.csv", csv.to_csv(index=False).encode("utf-8"),
                           "results.csv", "text/csv")
        data = st.session_state.get("last_data")
        if data is not None:
            st.download_button(
                "⬇️ results.geojson",
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "results.geojson", "application/geo+json",
            )

# ---------------------------- Datensatz (precomputed) ---------------------
if tab_data is not None:
    with tab_data:
        st.subheader("Vorberechnete Datensätze")
        if not BUNDLES:
            st.info("Keine vorhanden.")
        else:
            labels = {m["_path"]: f"{m.get('label', m['slug'])} · {m.get('created_utc','')[:10]}"
                      for m in BUNDLES}
            chosen = st.selectbox("Datensatz", list(labels), format_func=lambda p: labels[p])
            if st.button("Diesen Datensatz laden"):
                try:
                    b, problems = get_bundle(chosen)
                    if problems:
                        st.error("Datensatz unvollständig: " + "; ".join(problems))
                    else:
                        st.session_state["bundle"] = b
                        st.session_state["dataset_source"] = "bundle"
                        st.success(f"Geladen: {b.describe()}")
                except Exception as exc:
                    st.error(f"Laden fehlgeschlagen: {exc}")
            b = st.session_state.get("bundle")
            if b is not None:
                st.markdown(f'<div class="hint">{b.describe()}</div>', unsafe_allow_html=True)
                with st.expander("Manifest"):
                    st.json({k: v for k, v in b.manifest.items() if not k.startswith("_")})
        st.markdown(
            '<div class="hint">Ein neuer Datensatz entsteht auf einem Rechner mit '
            "Internet:<br><code>python main.py --mode live --preset stechlin "
            "--export-precomputed</code><br>Danach den Ordner unter "
            "<code>data/processed/</code> ins Repository legen.</div>",
            unsafe_allow_html=True,
        )

# ---------------------------- Info ----------------------------------------
with tab_info:
    lcy = (CFG.get("landcover", {}) or {}).get("year", 2021)
    st.markdown(
        f"""
**Betriebsart.** {DEPLOY.describe()}
Ermittelt über: {DEPLOY.source}.

**Was der Score misst.** Getrennte Teil-Scores für Wildnis, Sichtschutz am Ufer,
Erreichbarkeit, Privatsphäre, Zeltflächen-Eignung und Sicherheit – plus ein
Datenvertrauen, das **nicht** in den Gesamtscore eingerechnet wird. Ein Platz mit
Score 92 und Vertrauen 61 ist nicht dasselbe wie ein Platz mit Score 61.

**Harte Filter kennen drei Zustände.** PASS, FAIL und UNKNOWN. UNKNOWN heißt:
für diesen See fehlt eine Datengrundlage. Das ist kein Bestehen.

**Distanzen immer ab Ufer.** Gepuffert wird das Seepolygon, nicht der Mittelpunkt,
und gerechnet wird in einem metrischen CRS, nie in Grad.

**Datenquellen.** OpenStreetMap über Overpass, optional Overture Maps Buildings
und amtliche Hausumringe, ESA WorldCover 10 m (Stand **{lcy}**).

**Grenzen, die man kennen muss.**
- Ein hoher Score heißt: *in diesen Daten* ist nichts Menschliches verzeichnet.
  OSM ist außerhalb von Siedlungen lückenhaft – vor der Fahrt im Satellitenbild
  gegenprüfen.
- WorldCover ist von {lcy}. Windwurf, Borkenkäfer und Neubau danach fehlen.
- Die Fußwegschätzung ist Luftlinie × Umwegfaktor, kein Routing auf dem Wegenetz.
- Naturschutzgebiete, Kernzonen und Betretungsverbote sind **nicht** ausgewertet.
  Gerade die unzugänglichsten Seen liegen oft in Schutzzonen.
"""
    )

st.markdown(
    f'<div class="modebar">lake_finder {VERSION} · Modus <b>{DEPLOY.mode}</b> '
    f"({DEPLOY.source}) · Datenverzeichnis <code>{DATA_DIR}</code> · "
    f"{len(BUNDLES)} vorberechnete/r Datensatz/Datensätze</div>",
    unsafe_allow_html=True,
)
