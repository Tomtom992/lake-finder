"""
Tests fuer die Kernlogik.

Ausfuehren:
    python -m unittest discover -s tests -v
    # oder, falls pytest vorhanden:
    pytest tests -v

Die Tests fuer Geometrie-Module (lakes, osm_features) werden automatisch
uebersprungen, wenn geopandas/shapely nicht installiert sind -- so laeuft
die Kernlogik auch in einer minimalen Umgebung durch.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src.landcover import WC_CLASSES, _class_fractions, tiles_for_bbox, worldcover_tile
from src.scoring import check_hard_filters, quality_flags, score_row
from src.utils import Bbox, clamp, haversine_m, pick_metric_crs, ramp, utm_epsg


# --------------------------------------------------------------------------
class TestBbox(unittest.TestCase):
    def test_from_center_size(self):
        b = Bbox.from_center(13.30, 53.33, 30.0)
        self.assertAlmostEqual(b.width_km, 30.0, delta=0.6)
        self.assertAlmostEqual(b.height_km, 30.0, delta=0.6)

    def test_overpass_order_is_south_west_north_east(self):
        b = Bbox(13.0, 53.0, 13.5, 53.4)
        self.assertEqual(b.overpass(), "53.0000000,13.0000000,53.4000000,13.5000000")

    def test_tiles_cover_and_partition(self):
        b = Bbox(13.0, 53.0, 13.6, 53.4)
        tiles = b.tiles(0.15)
        self.assertEqual(len(tiles), 4 * 3)
        self.assertAlmostEqual(min(t.west for t in tiles), b.west)
        self.assertAlmostEqual(max(t.east for t in tiles), b.east)
        area = sum((t.east - t.west) * (t.north - t.south) for t in tiles)
        self.assertAlmostEqual(area, (b.east - b.west) * (b.north - b.south), places=9)

    def test_buffered_grows(self):
        b = Bbox(13.0, 53.0, 13.5, 53.4).buffered(3000)
        self.assertLess(b.west, 13.0)
        self.assertGreater(b.north, 53.4)

    def test_rejects_inverted(self):
        with self.assertRaises(ValueError):
            Bbox(13.5, 53.0, 13.0, 53.4)

    def test_haversine_known_distance(self):
        # Berlin Mitte -> Feldberg, grob 105 km Luftlinie
        d = haversine_m(13.405, 52.52, 13.43, 53.33) / 1000.0
        self.assertTrue(88 < d < 95, f"unerwartet: {d:.1f} km")


class TestCRS(unittest.TestCase):
    def test_utm_zone_berlin(self):
        self.assertEqual(utm_epsg(13.405, 52.52), "EPSG:32633")

    def test_utm_zone_southern_hemisphere(self):
        self.assertEqual(utm_epsg(-60.0, -20.0), "EPSG:32721")

    def test_pick_small_region_is_utm(self):
        self.assertEqual(pick_metric_crs(Bbox.from_center(13.3, 53.3, 30)), "EPSG:32633")

    def test_pick_large_european_region_is_laea(self):
        self.assertEqual(pick_metric_crs(Bbox(5.0, 45.0, 15.0, 55.0)), "EPSG:3035")

    def test_override_wins(self):
        self.assertEqual(pick_metric_crs(Bbox.from_center(13.3, 53.3, 30), "EPSG:25833"), "EPSG:25833")


class TestRamp(unittest.TestCase):
    def test_rising(self):
        self.assertEqual(ramp(40, 40, 95), 0.0)
        self.assertEqual(ramp(95, 40, 95), 1.0)
        self.assertAlmostEqual(ramp(67.5, 40, 95), 0.5, places=6)
        self.assertEqual(ramp(200, 40, 95), 1.0)

    def test_inverse(self):
        self.assertEqual(ramp(5, 5, 0), 0.0)
        self.assertEqual(ramp(0, 5, 0), 1.0)
        self.assertAlmostEqual(ramp(2.5, 5, 0), 0.5, places=6)

    def test_missing_is_conservative(self):
        self.assertEqual(ramp(None, 0, 100, missing=0.2), 0.2)
        self.assertEqual(ramp(float("nan"), 0, 100, missing=0.0), 0.0)

    def test_clamp(self):
        self.assertEqual(clamp(-1), 0.0)
        self.assertEqual(clamp(2), 1.0)


class TestWorldCoverTiles(unittest.TestCase):
    def test_tile_name_germany(self):
        self.assertEqual(worldcover_tile(13.3, 53.3), "N51E012")

    def test_tile_name_south_west(self):
        self.assertEqual(worldcover_tile(-0.5, -0.5), "S03W003")

    def test_tile_name_on_boundary(self):
        self.assertEqual(worldcover_tile(12.0, 51.0), "N51E012")

    def test_bbox_spanning_two_tiles(self):
        tiles = tiles_for_bbox(Bbox(11.5, 53.0, 12.5, 53.5))
        self.assertEqual(tiles, ["N51E009", "N51E012"])

    def test_single_tile_bbox(self):
        self.assertEqual(tiles_for_bbox(Bbox(13.0, 53.0, 13.5, 53.4)), ["N51E012"])


class TestClassFractions(unittest.TestCase):
    def test_percentages_exclude_water_and_nodata(self):
        # 40 Baum, 30 Wasser, 20 Acker, 10 nodata
        arr = np.array([10] * 40 + [80] * 30 + [40] * 20 + [0] * 10, dtype="uint8")
        fr = _class_fractions(arr, exclude_water=True)
        # Nenner = 100 - 10 nodata - 30 Wasser = 60
        self.assertAlmostEqual(fr["tree_cover_percent"], 66.67, places=1)
        self.assertAlmostEqual(fr["cropland_percent"], 33.33, places=1)
        self.assertAlmostEqual(fr["nodata_percent"], 10.0, places=1)
        # Wasseranteil bezogen auf alle gueltigen Pixel (90)
        self.assertAlmostEqual(fr["water_percent"], 33.33, places=1)
        self.assertEqual(fr["valid_pixels"], 60)

    def test_percentages_including_water(self):
        arr = np.array([10] * 50 + [80] * 50, dtype="uint8")
        fr = _class_fractions(arr, exclude_water=False)
        self.assertAlmostEqual(fr["tree_cover_percent"], 50.0, places=1)

    def test_empty_ring_is_nan_not_zero(self):
        fr = _class_fractions(np.array([], dtype="uint8"), exclude_water=True)
        self.assertTrue(math.isnan(fr["tree_cover_percent"]))
        self.assertEqual(fr["n_pixels"], 0)

    def test_legend_is_complete(self):
        self.assertEqual(
            sorted(WC_CLASSES), [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
        )


# --------------------------------------------------------------------------
CFG = {
    "landcover": {"year": 2021},
    "scoring": {
        "components": {
            "shore": {"metric": "shore_tree_cover", "weight": 3.0, "ramp": [40, 95], "missing": 0.2},
            "d_build": {
                "metric": "distance_nearest_building_m",
                "weight": 2.0,
                "ramp": [100, 1500],
                "missing": 0.2,
            },
            "low_built": {"metric": "built_up_percent", "weight": 1.0, "ramp": [5, 0], "missing": 0.2},
        },
        "penalties": {
            "buildings": {"metric": "building_count_500m", "points_per_unit": 3.0, "max_points": 25},
            "camp": {"flag": "has_campsite", "points": 20},
        },
        "hard_filters": {
            "shore": {"metric": "shore_tree_cover", "min": 70},
            "b500": {"metric": "building_count_500m", "max": 3},
        },
    },
}

WILD = {
    "shore_tree_cover": 96.0,
    "distance_nearest_building_m": 1800.0,
    "built_up_percent": 0.0,
    "building_count_500m": 0,
    "building_count_1000m": 0,
    "has_campsite": False,
    "landcover_ref_distance_m": 1000,
}
TAME = {
    "shore_tree_cover": 45.0,
    "distance_nearest_building_m": 80.0,
    "built_up_percent": 4.0,
    "building_count_500m": 12,
    "building_count_1000m": 40,
    "has_campsite": True,
    "landcover_ref_distance_m": 1000,
}


class TestScoring(unittest.TestCase):
    def test_wild_beats_tame_by_a_lot(self):
        w = score_row(WILD, CFG)["wilderness_score"]
        t = score_row(TAME, CFG)["wilderness_score"]
        self.assertGreater(w, 90)
        self.assertLess(t, 20)

    def test_score_is_bounded(self):
        for row in (WILD, TAME, {}, {"building_count_500m": 9999}):
            s = score_row(row, CFG)["wilderness_score"]
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 100.0)

    def test_single_criterion_cannot_max_the_score(self):
        """Perfektes Ufer allein darf nicht reichen -- der Score ist gewichtet."""
        row = dict(WILD, distance_nearest_building_m=100.0, built_up_percent=5.0)
        s = score_row(row, CFG)["wilderness_score"]
        self.assertLess(s, 60, "Ein einzelnes Kriterium zieht den Score zu stark nach oben")

    def test_missing_values_are_penalised_not_rewarded(self):
        partial = {"shore_tree_cover": 96.0}  # Rest fehlt
        s_partial = score_row(partial, CFG)["wilderness_score"]
        s_full = score_row(WILD, CFG)["wilderness_score"]
        self.assertLess(s_partial, s_full)
        self.assertIn("distance_nearest_building_m", score_row(partial, CFG)["score_missing_metrics"])

    def test_breakdown_points_sum_to_score(self):
        r = score_row(WILD, CFG)
        total = sum(b["points"] for b in r["score_breakdown"])
        self.assertAlmostEqual(total, r["wilderness_score"], delta=0.35)

    def test_penalties_are_capped(self):
        row = dict(TAME, building_count_500m=1000)
        r = score_row(row, CFG)
        pen = [b for b in r["score_breakdown"] if b["name"] == "buildings"][0]
        self.assertEqual(abs(pen["points"]), 25.0)

    def test_monotonic_in_shore_tree_cover(self):
        prev = -1.0
        for v in range(40, 100, 5):
            s = score_row(dict(WILD, shore_tree_cover=float(v)), CFG)["wilderness_score"]
            self.assertGreaterEqual(s, prev)
            prev = s


class TestHardFilters(unittest.TestCase):
    def test_wild_passes(self):
        ok, reasons = check_hard_filters(WILD, CFG)
        self.assertTrue(ok, reasons)

    def test_tame_fails_with_reasons(self):
        ok, reasons = check_hard_filters(TAME, CFG)
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 2)

    def test_missing_value_does_not_fail_by_default(self):
        ok, _ = check_hard_filters({"building_count_500m": 0}, CFG)
        self.assertTrue(ok)

    def test_missing_value_fails_when_configured(self):
        cfg = {"scoring": {"hard_filters": {
            "shore": {"metric": "shore_tree_cover", "min": 70, "fail_on_missing": True}}}}
        ok, reasons = check_hard_filters({}, cfg)
        self.assertFalse(ok)
        self.assertIn("fehlt", reasons[0])


class TestQualityFlags(unittest.TestCase):
    def test_osm_gap_detected(self):
        row = {"built_up_percent": 2.0, "building_count_1000m": 0, "landcover_ref_distance_m": 1000}
        flags, conf = quality_flags(row, CFG)
        self.assertIn("osm_luecke_moeglich", flags)
        self.assertEqual(conf, "niedrig")

    def test_clean_row_is_high_confidence(self):
        row = {
            "built_up_percent": 0.0,
            "building_count_1000m": 0,
            "landcover_ref_distance_m": 1000,
            "nodata_percent_1000m": 0.0,
            "valid_pixels_1000m": 30000,
            "area_ha": 12.0,
            "landcover_ok": True,
        }
        flags, conf = quality_flags(row, CFG)
        self.assertEqual(conf, "hoch")
        self.assertIn("landcover_stand_2021", flags)  # Hinweis, keine Abwertung

    def test_censored_distance_flagged(self):
        row = dict(WILD, distance_building_censored=True)
        flags, _ = quality_flags(row, CFG)
        self.assertIn("distanz_abgeschnitten", flags)

    def test_small_lake_flagged(self):
        flags, conf = quality_flags(dict(WILD, area_ha=0.8), CFG)
        self.assertIn("kleiner_see_raster_grob", flags)
        self.assertEqual(conf, "mittel")


# --------------------------------------------------------------------------
# Nur mit vollstaendigem Geo-Stack
# --------------------------------------------------------------------------
try:
    import geopandas  # noqa: F401
    import shapely  # noqa: F401

    GEO = True
except ImportError:  # pragma: no cover
    GEO = False


@unittest.skipUnless(GEO, "geopandas/shapely nicht installiert")
class TestLakeTagFilter(unittest.TestCase):
    def test_accepts_and_rejects(self):
        from src.lakes import is_lake

        cfg = {
            "accept_water_values": ["lake", "pond"],
            "exclude_water_values": ["river", "canal"],
            "include_untagged_natural_water": True,
            "include_reservoirs": False,
        }
        self.assertTrue(is_lake({"natural": "water", "water": "lake"}, cfg)[0])
        self.assertTrue(is_lake({"natural": "water"}, cfg)[0])
        self.assertFalse(is_lake({"natural": "water", "water": "river"}, cfg)[0])
        self.assertFalse(is_lake({"natural": "water", "water": "reservoir"}, cfg)[0])
        self.assertFalse(is_lake({"natural": "water", "intermittent": "yes"}, cfg)[0])
        cfg2 = dict(cfg, include_reservoirs=True)
        self.assertTrue(is_lake({"natural": "water", "water": "reservoir"}, cfg2)[0])


@unittest.skipUnless(GEO, "geopandas/shapely nicht installiert")
class TestCategorize(unittest.TestCase):
    def test_roads_are_split_by_importance(self):
        from src.osm_features import categorize

        self.assertEqual(categorize({"highway": "primary"}), {"major_road"})
        self.assertEqual(categorize({"highway": "track"}), {"soft_way"})
        self.assertEqual(categorize({"highway": "residential"}), {"minor_road"})

    def test_building_no_is_not_a_building(self):
        from src.osm_features import categorize

        self.assertNotIn("building", categorize({"building": "no"}))
        self.assertIn("building", categorize({"building": "yes"}))

    def test_benign_man_made_ignored(self):
        from src.osm_features import categorize

        self.assertEqual(categorize({"man_made": "survey_point"}), set())
        self.assertIn("man_made", categorize({"man_made": "pier"}))


@unittest.skipUnless(GEO, "geopandas/shapely nicht installiert")
class TestRingStitching(unittest.TestCase):
    def test_two_segments_form_one_ring(self):
        from src.lakes import _stitch

        a = [(0, 0), (1, 0), (1, 1)]
        b = [(1, 1), (0, 1), (0, 0)]
        rings = _stitch([a, b])
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])

    def test_open_fragment_is_dropped(self):
        from src.lakes import _stitch

        self.assertEqual(_stitch([[(0, 0), (1, 0)]]), [])

    def test_hole_is_kept(self):
        from src.lakes import _polygon_from_rings

        outer = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        inner = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        poly = _polygon_from_rings([outer], [inner])
        self.assertAlmostEqual(poly.area, 100 - 4, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
