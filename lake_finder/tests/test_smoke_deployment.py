"""
Tests für Betriebsart, Ergebnisbündel und Deployment (Release v2-rc1).

Der eigentliche Rauchtest ist ``tools/smoke_test.py`` -- er ist für Menschen
und für CI gedacht und gibt einen lesbaren Bericht aus. Hier steht, was
davon als harte Zusicherung gilt:

  * die Betriebsart löst sich in der richtigen Reihenfolge auf
  * im precomputed-Modus ist der Live-Pfad wirklich gesperrt
  * ein Bündel lässt sich schreiben, lesen, prüfen und neu bewerten
  * aus einem Bündel entsteht eine Karte -- ohne Geo-Stack, ohne Netzwerk

Diese Tests laufen bewusst ohne geopandas/shapely/streamlit, weil genau
das die Zusicherung für den Cloud-Betrieb ist.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from src import BUNDLE_FORMAT_VERSION, VERSION
from src import deployment as deploy_mod
from src import map as map_mod
from src import store as store_mod

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def minimal_feature(uid: str = "w1", **props) -> dict:
    base = {
        "lake_uid": uid, "osm_type": "way", "osm_id": int(uid[1:]),
        "name": f"Testsee {uid}", "latitude": 53.3, "longitude": 13.3,
        "area_ha": 12.0, "wilderness_score": 88.0,
        "landcover_ok": True, "building_data_ok": True, "osm_features_ok": True,
        "travel_ok": True, "shore_segments_ok": True,
        "tree_cover_500m": 92.0, "tree_cover_1000m": 88.0, "shore_tree_cover": 95.0,
        "natural_land_percent": 96.0, "built_up_percent": 0.0, "cropland_percent": 2.0,
        "building_count_100m": 0, "building_count_250m": 0, "building_count_500m": 0,
        "building_count_1000m": 0, "distance_nearest_building_m": 1400.0,
        "distance_major_road_m": 1500.0, "distance_residential_m": 2600.0,
        "has_campsite": False, "has_marina": False, "has_accommodation": False,
        "has_residential": False, "has_parking": False,
        "shore_segment_p10_score": 88.0, "shore_segment_median_score": 93.0,
        "shore_segment_min_score": 80.0, "natural_shore_percent": 99.0,
        "developed_shore_percent": 0.0, "longest_developed_section_m": 0.0,
        "drive_time_min": 95.0, "landcover_ref_distance_m": 1000,
    }
    base.update(props)
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon",
                     "coordinates": [[[13.29, 53.29], [13.31, 53.29],
                                      [13.31, 53.31], [13.29, 53.31], [13.29, 53.29]]]},
        "properties": base,
    }


def write_test_bundle(directory: Path, slug: str = "testregion", n: int = 3,
                      is_demo: bool = False) -> Path:
    target = directory / slug
    target.mkdir(parents=True, exist_ok=True)
    feats = [minimal_feature(f"w{i}") for i in range(1, n + 1)]
    # ein See, der die harten Filter reißt -- sonst testet man nichts
    feats.append(minimal_feature("w99", distance_nearest_building_m=40.0,
                                 building_count_100m=2, has_campsite=True,
                                 wilderness_score=31.0))
    (target / store_mod.APP_DATA).write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    (target / store_mod.MANIFEST).write_text(json.dumps({
        "format_version": BUNDLE_FORMAT_VERSION, "lake_finder_version": VERSION,
        "created_utc": "2026-09-17T12:00:00+00:00", "slug": slug,
        "label": "Testregion", "region_bbox": [13.0, 53.2, 13.5, 53.4],
        "metric_crs": "EPSG:32633", "is_demo": is_demo,
        "counts": {"lakes": len(feats), "passes": n, "spots": 0},
        "files": {"app_data": store_mod.APP_DATA},
    }), encoding="utf-8")
    return target


# ==========================================================================
# Betriebsart
# ==========================================================================


class TestDeploymentMode(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop(deploy_mod.ENV_MODE, None)
        os.environ.pop(deploy_mod.ENV_DATA_DIR, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_auto_picks_precomputed_when_bundles_exist(self):
        d = deploy_mod.resolve({"deployment": {"mode": "auto"}}, bundle_count=2)
        self.assertEqual(d.mode, deploy_mod.PRECOMPUTED)
        self.assertTrue(d.is_precomputed)

    def test_auto_falls_back_to_live_without_bundles(self):
        d = deploy_mod.resolve({"deployment": {"mode": "auto"}}, bundle_count=0)
        self.assertEqual(d.mode, deploy_mod.LIVE)

    def test_precedence_parameter_beats_env_and_config(self):
        os.environ[deploy_mod.ENV_MODE] = "precomputed"
        cfg = {"deployment": {"mode": "precomputed"}}
        self.assertEqual(deploy_mod.resolve(cfg, requested="live").mode, deploy_mod.LIVE)

    def test_precedence_env_beats_config(self):
        os.environ[deploy_mod.ENV_MODE] = "live"
        cfg = {"deployment": {"mode": "precomputed"}}
        d = deploy_mod.resolve(cfg, bundle_count=5)
        self.assertEqual(d.mode, deploy_mod.LIVE)
        self.assertIn(deploy_mod.ENV_MODE, d.source)

    def test_unknown_mode_is_ignored_not_fatal(self):
        d = deploy_mod.resolve({"deployment": {"mode": "voll-automatisch"}}, bundle_count=1)
        self.assertIn(d.mode, deploy_mod.MODES)

    def test_env_data_dir_is_honoured(self):
        os.environ[deploy_mod.ENV_DATA_DIR] = str(Path("tmp") / "irgendwo")
        self.assertEqual(
            deploy_mod.data_dir_from({}),
            Path("tmp") / "irgendwo",
        )

    def test_guard_blocks_live_access_in_precomputed(self):
        d = deploy_mod.resolve({}, requested="precomputed")
        with self.assertRaises(deploy_mod.LiveModeDisabled):
            deploy_mod.guard_live(d, "Overpass")

    def test_guard_allows_live_access_in_live_mode(self):
        d = deploy_mod.resolve({}, requested="live")
        deploy_mod.guard_live(d, "Overpass")   # darf nicht werfen

    def test_describe_mentions_no_external_calls(self):
        d = deploy_mod.resolve({}, requested="precomputed")
        self.assertIn("keine externen Dienste", d.describe())


# ==========================================================================
# Ergebnisbündel
# ==========================================================================


class TestBundleStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write_test_bundle(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bundle_is_found_and_listed(self):
        found = store_mod.list_bundles(self.dir)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["slug"], "testregion")

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(store_mod.list_bundles(self.dir / "gibtsnicht"), [])

    def test_bundle_loads_and_validates(self):
        b = store_mod.load_bundle(self.dir / "testregion")
        self.assertEqual(store_mod.validate_bundle(b), [])
        self.assertEqual(b.n_lakes, 4)
        self.assertFalse(b.is_demo)
        self.assertIn("Testregion", b.describe())

    def test_broken_manifest_does_not_raise(self):
        (self.dir / "kaputt").mkdir()
        (self.dir / "kaputt" / store_mod.MANIFEST).write_text("{kein json", encoding="utf-8")
        self.assertEqual(len(store_mod.list_bundles(self.dir)), 1)

    def test_validation_reports_missing_properties(self):
        target = self.dir / "unvollstaendig"
        target.mkdir()
        (target / store_mod.APP_DATA).write_text(json.dumps(
            {"type": "FeatureCollection",
             "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
                           "properties": {"name": "x"}}]}), encoding="utf-8")
        (target / store_mod.MANIFEST).write_text(json.dumps(
            {"format_version": BUNDLE_FORMAT_VERSION, "slug": "unvollstaendig"}), encoding="utf-8")
        b = store_mod.load_bundle(target)
        problems = store_mod.validate_bundle(b)
        self.assertTrue(any("Pflicht-Properties" in p for p in problems))

    def test_future_format_version_is_rejected_clearly(self):
        target = self.dir / "zukunft"
        target.mkdir()
        (target / store_mod.APP_DATA).write_text(json.dumps(
            {"type": "FeatureCollection", "features": [minimal_feature()]}), encoding="utf-8")
        (target / store_mod.MANIFEST).write_text(json.dumps(
            {"format_version": BUNDLE_FORMAT_VERSION + 5, "slug": "zukunft"}), encoding="utf-8")
        problems = store_mod.validate_bundle(store_mod.load_bundle(target))
        self.assertTrue(any("neuer als dieser Code" in p for p in problems))

    def test_slugify(self):
        self.assertEqual(store_mod.slugify("Müritz-NP Ost / Feldberger"), "mueritz-np-ost-feldberger")
        self.assertEqual(store_mod.slugify(""), "region")


# ==========================================================================
# Neubewertung ohne Netzwerk und ohne Geo-Stack
# ==========================================================================


class TestRescoreWithoutNetwork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write_test_bundle(self.dir)
        self.bundle = store_mod.load_bundle(self.dir / "testregion")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rescore_produces_all_score_columns(self):
        data, scored = store_mod.rescore_bundle(self.bundle, CFG)
        for col in ("wilderness_score", "passes_filters", "filter_status",
                    "data_confidence_score", "overall_spot_score"):
            self.assertIn(col, scored.columns)
        self.assertEqual(len(scored), 4)

    def test_strict_defaults_reject_the_bad_lake(self):
        _, scored = store_mod.rescore_bundle(self.bundle, CFG)
        bad = scored[scored.lake_uid == "w99"].iloc[0]
        self.assertFalse(bool(bad["passes_filters"]))
        self.assertEqual(bad["filter_status"], "FAIL")
        good = scored[scored.lake_uid == "w1"].iloc[0]
        self.assertTrue(bool(good["passes_filters"]))

    def test_loosening_a_threshold_changes_the_result(self):
        import copy

        lax = copy.deepcopy(CFG)
        checks = lax["filters"]["checks"]
        checks["min_distance_nearest_building_m"]["min"] = 10
        checks["max_buildings_100m"]["max"] = 5
        checks["max_buildings_250m"]["max"] = 5
        checks["exclude_campsite"]["enabled"] = False
        _, strict = store_mod.rescore_bundle(self.bundle, CFG)
        _, loose = store_mod.rescore_bundle(self.bundle, lax)
        self.assertLess(int(strict.passes_filters.sum()), int(loose.passes_filters.sum()))

    def test_profile_changes_only_the_overall_score(self):
        _, a = store_mod.rescore_bundle(self.bundle, CFG, profile="max_seclusion")
        _, b = store_mod.rescore_bundle(self.bundle, CFG, profile="easy_access")
        self.assertEqual(sorted(a.wilderness_score.tolist()),
                         sorted(b.wilderness_score.tolist()))

    def test_scores_are_written_back_into_the_features(self):
        data, _ = store_mod.rescore_bundle(self.bundle, CFG)
        props = data["features"][0]["properties"]
        self.assertIn("filter_status", props)
        self.assertIsInstance(props["passes_filters"], bool)
        # muss wieder serialisierbar sein -- sonst scheitert der Download
        json.dumps(data)

    def test_sorting_puts_the_best_first(self):
        data, _ = store_mod.rescore_bundle(self.bundle, CFG)
        ordered = store_mod.sort_features(data)
        scores = [f["properties"].get("overall_spot_score") or
                  f["properties"].get("wilderness_score")
                  for f in ordered["features"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_bundle_does_not_crash(self):
        target = self.dir / "leer"
        target.mkdir()
        (target / store_mod.APP_DATA).write_text(
            json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        (target / store_mod.MANIFEST).write_text(
            json.dumps({"format_version": BUNDLE_FORMAT_VERSION, "slug": "leer"}), encoding="utf-8")
        b = store_mod.load_bundle(target)
        data, scored = store_mod.rescore_bundle(b, CFG)
        self.assertEqual(len(scored), 0)


# ==========================================================================
# Karte aus vorberechneten Daten
# ==========================================================================


class TestMapFromBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write_test_bundle(self.dir)
        self.bundle = store_mod.load_bundle(self.dir / "testregion")

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_is_built_without_geo_stack(self):
        data, _ = store_mod.rescore_bundle(self.bundle, CFG)
        meta = map_mod.app_meta(data, CFG)
        out = map_mod.build_app_from_data(data, meta, self.dir / "app.html", "Test")
        html = out.read_text(encoding="utf-8")
        self.assertGreater(len(html), 10000)
        for placeholder in ("__DATA__", "__META__", "__TITLE__"):
            self.assertNotIn(placeholder, html)
        self.assertIn("leaflet", html.lower())
        self.assertIn("Testsee w1", html)

    def test_meta_centre_comes_from_the_data(self):
        meta = map_mod.app_meta(self.bundle.data, CFG)
        self.assertAlmostEqual(meta["center"][0], 53.3, places=3)
        self.assertAlmostEqual(meta["center"][1], 13.3, places=3)

    def test_meta_survives_empty_data(self):
        meta = map_mod.app_meta({"type": "FeatureCollection", "features": []}, CFG)
        self.assertEqual(len(meta["center"]), 2)

    def test_demo_bundle_is_marked(self):
        write_test_bundle(self.dir, slug="demo", is_demo=True)
        b = store_mod.load_bundle(self.dir / "demo")
        self.assertTrue(b.is_demo)


# ==========================================================================
# Der Rauchtest selbst
# ==========================================================================


class TestSmokeScript(unittest.TestCase):
    def test_smoke_test_runs_and_passes(self):
        """tools/smoke_test.py muss in dieser Installation sauber durchlaufen."""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "smoke_test.py"), "--json"],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"Rauchtest fehlgeschlagen:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}",
        )
        payload = proc.stdout[proc.stdout.index("{"):]
        result = json.loads(payload)
        self.assertEqual(result["failed"], 0)
        names = [r["name"] for r in result["rows"]]
        for required in ("config.yaml vollständig", "Karte erzeugbar"):
            self.assertTrue(any(required in n for n in names), f"{required} nicht geprüft")

    def test_cli_version_works_without_geo_stack(self):
        """main.py --version darf nie an einem fehlenden Import scheitern."""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "--version"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn(VERSION, proc.stdout + proc.stderr)

    def test_cli_refuses_to_compute_in_precomputed_mode(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "--mode", "precomputed",
             "--preset", "stechlin"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("precomputed", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ==========================================================================
# Die Streamlit-App tatsächlich ausführen (mit Stub statt echtem Streamlit)
# ==========================================================================


class TestStreamlitApp(unittest.TestCase):
    """Führt app.py komplett aus und prüft, was die Seite aufbaut.

    Das ersetzt keinen echten ``streamlit run``, fängt aber genau die
    Fehlerklasse, die man sonst erst im Browser sieht: Tippfehler,
    falsche Signaturen, None an der falschen Stelle.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import streamlit_stub

        self.mod = streamlit_stub
        self.stub = streamlit_stub.install()
        self._env = dict(os.environ)
        self._cwd = os.getcwd()
        os.chdir(ROOT)

        # Harte Netzwerksperre für die gesamte Testklasse. Ohne sie würde ein
        # Klick auf "Seen suchen" auf einem Rechner MIT Geo-Stack tatsächlich
        # Overpass abfragen -- ein Unit-Test darf das unter keinen Umständen.
        # Der Socket-Riegel ist die Zusicherung; das gefälschte main-Modul
        # weiter unten ist der kontrollierte Fehlerfall.
        self._real_socket = socket.socket
        self._real_create_connection = socket.create_connection

        msg = ("Unit-Test hat versucht, eine Netzwerkverbindung zu öffnen. "
               "Live-Aufrufe müssen gemockt werden.")

        # Nicht die Socket-KLASSE ersetzen, sondern nur das Verbinden sperren:
        # so schlägt genau der Verbindungsversuch fehl, mit klarer Meldung,
        # statt dass urllib an einem unpassenden Objekt mit einem kryptischen
        # TypeError scheitert.
        class _NoNetworkSocket(self._real_socket):
            def connect(self, *a, **k):
                raise AssertionError(msg)

            def connect_ex(self, *a, **k):
                raise AssertionError(msg)

        def _blocked(*a, **k):
            raise AssertionError(msg)

        socket.socket = _NoNetworkSocket
        socket.create_connection = _blocked

    def tearDown(self):
        socket.socket = self._real_socket
        socket.create_connection = self._real_create_connection
        self.mod.uninstall()
        os.environ.clear()
        os.environ.update(self._env)
        os.chdir(self._cwd)
        for mod in ("app", "main"):
            sys.modules.pop(mod, None)

    def _mock_live_analysis(self, message: str = "simulated live analysis failure"):
        """Ersetzt die Live-Datenstufe durch einen sofortigen Fehler.

        ``app.analyse_live`` importiert ``run_data`` erst beim Aufruf
        (``from main import run_data``). Ein vorab in ``sys.modules``
        hinterlegtes Ersatzmodul wird dabei benutzt -- deshalb braucht es
        weder eine Änderung an app.py noch an main.py.
        """
        fake = types.ModuleType("main")

        def run_data(*a, **k):
            raise RuntimeError(message)

        fake.run_data = run_data
        sys.modules["main"] = fake
        return fake

    def _run_app(self, mode: str, data_dir: str | None = None, clicks: dict | None = None):
        import runpy

        os.environ[deploy_mod.ENV_MODE] = mode
        if data_dir is not None:
            os.environ[deploy_mod.ENV_DATA_DIR] = data_dir
        else:
            os.environ.pop(deploy_mod.ENV_DATA_DIR, None)
        self.stub.button_returns = clicks or {}
        stopped = False
        try:
            runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
        except self.mod.StopExecution:
            stopped = True
        return stopped

    def test_precomputed_mode_renders_without_errors(self):
        stopped = self._run_app("precomputed")
        self.assertFalse(stopped, "App hat abgebrochen")
        self.assertEqual(self.stub.errors, [], f"Fehlerboxen: {self.stub.errors}")
        self.assertTrue(self.stub.html_payloads, "keine Karte eingebettet")
        self.assertGreater(len(self.stub.html_payloads[0]), 20000)

    def test_precomputed_mode_computes_metrics(self):
        self._run_app("precomputed")
        metrics = {a[0]: a[1] for name, a, _ in self.stub.calls if name == "metric"}
        self.assertIn("Seen", metrics)
        self.assertGreater(int(metrics["Seen"]), 0)
        self.assertIn("PASS", metrics)

    def test_precomputed_mode_without_data_explains_itself(self):
        with tempfile.TemporaryDirectory() as empty:
            stopped = self._run_app("precomputed", data_dir=empty)
        self.assertTrue(stopped, "App hätte anhalten müssen")
        self.assertTrue(self.stub.errors)
        joined = " ".join(self.stub.errors)
        self.assertIn("export-precomputed", joined)

    def test_live_mode_shows_search_tab(self):
        self._run_app("live")
        tabs = [a[0] for name, a, _ in self.stub.calls if name == "tabs"]
        self.assertTrue(tabs)
        self.assertIn("Suchen", tabs[0])

    def test_live_analysis_failure_does_not_crash_the_app(self):
        """Scheitert die Live-Analyse, zeigt die App eine Meldung statt zu sterben.

        Der Fehler wird simuliert (gefälschtes ``main.run_data``), es geht
        garantiert nichts ins Netz -- der Socket-Riegel aus setUp würde das
        sonst als Testfehler melden.
        """
        self._mock_live_analysis()
        stopped = self._run_app("live", clicks={"🔍 Seen suchen": True})
        self.assertFalse(stopped, "App hat abgebrochen")
        self.assertTrue(self.stub.errors, "keine Fehlermeldung angezeigt")
        self.assertTrue(
            any("Live-Analyse ist fehlgeschlagen" in e for e in self.stub.errors),
            f"unerwartete Meldung: {self.stub.errors}",
        )
        # Die Ursache muss durchgereicht werden, nicht verschluckt
        self.assertTrue(
            any("simulated live analysis failure" in e for e in self.stub.errors),
            "die konkrete Ursache fehlt in der Meldung",
        )

    def test_live_mode_still_shows_precomputed_data_after_failure(self):
        """Nach einem Live-Fehler bleiben die vorberechneten Daten sichtbar."""
        self._mock_live_analysis()
        self._run_app("live", clicks={"🔍 Seen suchen": True})
        self.assertTrue(self.stub.html_payloads, "Karte fehlt trotz vorhandener Bündel")
        self.assertGreater(len(self.stub.html_payloads[0]), 20000)
        metrics = {a[0]: a[1] for name, a, _ in self.stub.calls if name == "metric"}
        self.assertIn("Seen", metrics)
        self.assertGreater(int(metrics["Seen"]), 0,
                           "vorberechnete Seen werden nach dem Fehler nicht mehr gezählt")

    def test_version_is_shown(self):
        self._run_app("precomputed")
        self.assertTrue(any(VERSION in t for t in self.stub.texts))
