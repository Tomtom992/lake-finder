"""
Tests für die Erweiterung zum Wild-Camp-/Remote-Lake-Spot-Finder.

Ausführen:
    python -m unittest discover -s tests -v

Die Tests in dieser Datei brauchen weder geopandas/shapely noch
Netzzugang: sie prüfen genau die Rechen- und Entscheidungslogik, die man
sonst erst im Feld bemerkt, wenn sie falsch ist.

Die Szenarien A-J am Ende sind die in der Anforderung genannten Fälle.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from src import camp, terrain, viewshed
from src.access import candidate_score, select_best_access, walk_estimate
from src.buildings.merge import dedup_records, source_summary
from src.filters import FAIL, PASS, UNKNOWN, data_availability, evaluate, evaluate_check
from src.scoring import (
    data_confidence_score,
    overall_spot_score,
    profile_weights,
    score_all,
    score_block,
)
from src.shoreline import (
    aggregate_segments,
    longest_run_length,
    weighted_mean,
    weighted_percentile,
)

# --------------------------------------------------------------------------
# Gemeinsame Testkonfiguration
# --------------------------------------------------------------------------

CFG = {
    "filters": {
        "fail_on_missing_critical_data": True,
        "checks": {
            "min_dist_building": {
                "label": "Abstand Gebäude", "metric": "distance_nearest_building_m",
                "min": 300, "requires": ["buildings"], "critical": True,
            },
            "max_b100": {
                "label": "Gebäude 100 m", "metric": "building_count_100m",
                "max": 0, "requires": ["buildings"], "critical": True,
            },
            "exclude_campsite": {
                "label": "Campingplatz", "flag": "has_campsite", "must_be": False,
                "requires": ["osm_features"], "critical": True,
            },
            "min_tree_500": {
                "label": "Wald 500 m", "metric": "tree_cover_500m",
                "min": 60, "requires": ["landcover"], "critical": True,
            },
            "max_drive": {
                "label": "Fahrzeit", "metric": "drive_time_min",
                "max": 120, "requires": ["travel"], "critical": False,
            },
        },
    },
    "scoring": {
        "scores": {
            "wilderness": {
                "components": {
                    "tree": {"metric": "tree_cover_500m", "weight": 2.0, "ramp": [30, 95]},
                    "dist": {"metric": "distance_nearest_building_m", "weight": 2.0,
                             "ramp": [100, 1500]},
                },
                "penalties": {},
            },
            "visual_seclusion": {
                "components": {
                    "p10": {"metric": "shore_segment_p10_score", "weight": 3.0, "ramp": [30, 90]},
                    "nat": {"metric": "natural_shore_percent", "weight": 2.0, "ramp": [60, 100]},
                    "worst": {"metric": "shore_segment_min_score", "weight": 1.0,
                              "ramp": [10, 80]},
                },
                "penalties": {
                    "dev": {"metric": "developed_shore_percent", "threshold": 0.0,
                            "points_per_unit": 1.5, "max_points": 35},
                },
            },
            "access": {
                "components": {
                    "drive": {"metric": "drive_time_min", "weight": 1.0, "ramp": [180, 30]},
                },
                "penalties": {},
            },
        },
        "confidence": {
            "start": 100, "missing_source_penalty": 10,
            "flag_penalties": {"landcover_fehlt": 40, "building_sources_disagree": 20},
        },
        "overall": {
            "weights": {"wilderness": 2.0, "visual_seclusion": 2.0, "access": 1.0},
            "profiles": {
                "max_seclusion": {"visual_seclusion": 4.0, "access": 0.2},
                "easy_access": {"access": 4.0, "visual_seclusion": 0.5},
            },
        },
    },
}

FULL_ROW = {
    "landcover_ok": True,
    "building_data_ok": True,
    "osm_features_ok": True,
    "travel_ok": True,
    "shore_segments_ok": True,
    "terrain_ok": True,
}


def row(**kw):
    r = dict(FULL_ROW)
    r.update(kw)
    return r


# ==========================================================================
# Milestone 1 -- Filter mit drei Zuständen
# ==========================================================================


class TestTriStateFilters(unittest.TestCase):
    def test_clean_lake_passes(self):
        out = evaluate(
            row(distance_nearest_building_m=1200, building_count_100m=0,
                has_campsite=False, tree_cover_500m=91, drive_time_min=95),
            CFG,
        )
        self.assertEqual(out.status, PASS)
        self.assertTrue(out.passed)

    def test_zermittensee_case_now_fails(self):
        """Der reale Fehlfall aus dem Stechlin-Lauf muss durchfallen."""
        out = evaluate(
            row(distance_nearest_building_m=60.9, building_count_100m=1,
                building_count_500m=2, has_campsite=True, has_residential=True,
                has_parking=True, tree_cover_500m=88, drive_time_min=95),
            CFG,
        )
        self.assertEqual(out.status, FAIL)
        self.assertFalse(out.passed)
        self.assertGreaterEqual(len(out.fail_reasons), 3)

    def test_missing_landcover_is_unknown_not_pass(self):
        r = row(distance_nearest_building_m=1200, building_count_100m=0,
                has_campsite=False, drive_time_min=95)
        r["landcover_ok"] = False
        r.pop("tree_cover_500m", None)
        out = evaluate(r, CFG)
        self.assertEqual(out.status, UNKNOWN)
        self.assertFalse(out.passed)          # fail_on_missing_critical_data = true
        self.assertTrue(any("landcover" in u for u in out.unknown_reasons))

    def test_unknown_can_be_tolerated_when_configured(self):
        cfg = {**CFG, "filters": {**CFG["filters"], "fail_on_missing_critical_data": False}}
        r = row(distance_nearest_building_m=1200, building_count_100m=0, has_campsite=False)
        r["landcover_ok"] = False
        out = evaluate(r, cfg)
        self.assertEqual(out.status, UNKNOWN)
        self.assertTrue(out.passed)

    def test_non_critical_unknown_does_not_block(self):
        r = row(distance_nearest_building_m=1200, building_count_100m=0,
                has_campsite=False, tree_cover_500m=91)
        r["travel_ok"] = False
        out = evaluate(r, CFG)
        self.assertEqual(out.status, PASS)

    def test_a_value_without_its_source_is_still_unknown(self):
        """Ein Wert in der Zeile ersetzt keine Datengrundlage."""
        r = row(tree_cover_500m=95, distance_nearest_building_m=1200,
                building_count_100m=0, has_campsite=False)
        r["landcover_ok"] = False
        out = evaluate(r, CFG)
        self.assertEqual(out.status, UNKNOWN)

    def test_censored_distance_does_not_violate_a_maximum(self):
        spec = {"metric": "distance_marina_m", "max": 500, "requires": []}
        chk = evaluate_check(
            "m", spec, {"distance_marina_m": 3000.0, "distance_marina_censored": True}, {}
        )
        self.assertEqual(chk.status, PASS)

    def test_availability_defaults_to_unavailable(self):
        avail = data_availability({}, CFG)
        self.assertFalse(any(avail.values()))

    def test_legacy_hard_filters_still_apply(self):
        legacy = {"scoring": {"hard_filters": {
            "min_tree_cover_500m": {"metric": "tree_cover_500m", "min": 80}}}}
        out = evaluate(row(tree_cover_500m=55), legacy)
        self.assertEqual(out.status, FAIL)


# ==========================================================================
# Milestone 1 -- getrennte Scores
# ==========================================================================


class TestScoreSeparation(unittest.TestCase):
    def test_blocks_are_independent(self):
        r = row(tree_cover_500m=95, distance_nearest_building_m=1500,
                shore_segment_p10_score=30, natural_shore_percent=60,
                developed_shore_percent=20, drive_time_min=170)
        wild = score_block(r, CFG, "wilderness")["score"]
        seclusion = score_block(r, CFG, "visual_seclusion")["score"]
        access = score_block(r, CFG, "access")["score"]
        self.assertGreater(wild, 90)
        self.assertLess(seclusion, 20)
        self.assertLess(access, 20)

    def test_unconfigured_block_is_none_not_zero(self):
        res = score_block(row(), CFG, "safety")
        self.assertIsNone(res["score"])
        self.assertFalse(res["configured"])

    def test_block_without_data_is_none_not_a_low_score(self):
        """Ein Spot-Block auf Seenebene darf keinen Zahlenwert erfinden."""
        cfg = {"scoring": {"min_data_share": 0.5, "scores": {"campsite": {
            "components": {
                "flat": {"metric": "flat_area_m2", "weight": 2.0, "ramp": [6, 45],
                         "missing": 0.2},
                "slope": {"metric": "mean_slope_deg", "weight": 2.0, "ramp": [8, 1],
                          "missing": 0.2},
            }}}}}
        res = score_block(row(), cfg, "campsite")
        self.assertIsNone(res["score"])
        self.assertTrue(res["insufficient_data"])
        self.assertEqual(res["data_share"], 0.0)

        full = score_block(row(flat_area_m2=30, mean_slope_deg=2.0), cfg, "campsite")
        self.assertIsNotNone(full["score"])
        self.assertGreater(full["score"], 60)
        self.assertEqual(full["data_share"], 1.0)

    def test_partial_data_still_scores_above_threshold(self):
        cfg = {"scoring": {"min_data_share": 0.5, "scores": {"campsite": {
            "components": {
                "flat": {"metric": "flat_area_m2", "weight": 3.0, "ramp": [6, 45]},
                "slope": {"metric": "mean_slope_deg", "weight": 1.0, "ramp": [8, 1]},
            }}}}}
        res = score_block(row(flat_area_m2=30), cfg, "campsite")
        self.assertIsNotNone(res["score"])       # 75 % des Gewichts belegt
        self.assertEqual(res["data_share"], 0.75)

    def test_confidence_is_not_folded_into_scores(self):
        good = row(tree_cover_500m=95, distance_nearest_building_m=1500)
        bad = dict(good)
        bad["landcover_ok"] = False
        bad["quality_flags"] = ["landcover_fehlt"]
        s_good = score_all(good, CFG)
        s_bad = score_all(bad, CFG)
        self.assertLess(s_bad["data_confidence_score"], s_good["data_confidence_score"])
        # Der Sachscore bleibt, was er ist -- er wird nicht heruntergerechnet.
        self.assertEqual(s_good["wilderness_score"], s_bad["wilderness_score"])

    def test_confidence_drops_with_flags_and_missing_sources(self):
        r = row(quality_flags=["landcover_fehlt", "building_sources_disagree"])
        res = data_confidence_score(r, CFG)
        self.assertLessEqual(res["data_confidence_score"], 40)
        self.assertTrue(res["confidence_reasons"])

    def test_overall_excludes_confidence(self):
        scores = {"wilderness_score": 90.0, "visual_seclusion_score": 90.0,
                  "access_score": 90.0, "data_confidence_score": 10.0}
        res = overall_spot_score(scores, CFG)
        self.assertAlmostEqual(res["overall_spot_score"], 90.0, places=5)
        self.assertEqual(res["overall_coverage"], 100.0)

    def test_overall_reports_partial_coverage(self):
        res = overall_spot_score({"wilderness_score": 80.0}, CFG)
        self.assertEqual(res["overall_spot_score"], 80.0)
        self.assertLess(res["overall_coverage"], 50.0)

    def test_profiles_only_shift_weights(self):
        scores = {"wilderness_score": 50.0, "visual_seclusion_score": 100.0,
                  "access_score": 0.0}
        sec = overall_spot_score(scores, CFG, "max_seclusion")["overall_spot_score"]
        acc = overall_spot_score(scores, CFG, "easy_access")["overall_spot_score"]
        self.assertGreater(sec, acc)
        self.assertIn("visual_seclusion", profile_weights(CFG, "max_seclusion"))

    def test_unknown_profile_falls_back(self):
        w = profile_weights(CFG, "gibt_es_nicht")
        self.assertEqual(w["wilderness"], 2.0)


# ==========================================================================
# Milestone 2 -- Ufersegmente
# ==========================================================================


class TestShoreAggregation(unittest.TestCase):
    def test_weighted_percentile(self):
        # 90 % der Länge hat 100, 10 % hat 0 -> P10 ist 0
        vals = [0.0] + [100.0] * 9
        lens = [10.0] * 10
        self.assertEqual(weighted_percentile(vals, lens, 10), 0.0)
        self.assertEqual(weighted_percentile(vals, lens, 50), 100.0)

    def test_weighted_mean_respects_length(self):
        self.assertAlmostEqual(weighted_mean([0.0, 100.0], [9.0, 1.0]), 10.0)

    def test_longest_run_wraps_around_the_ring(self):
        # Gestörter Abschnitt liegt über dem Startpunkt der Uferlinie
        mask = [True, True, False, False, False, True]
        lens = [10.0] * 6
        self.assertEqual(longest_run_length(mask, lens, circular=True), 30.0)
        self.assertEqual(longest_run_length(mask, lens, circular=False), 20.0)

    def test_all_developed_returns_total(self):
        self.assertEqual(longest_run_length([True] * 4, [5.0] * 4), 20.0)

    def test_five_percent_village_is_punished(self):
        """Szenario B: 95 % perfekter Wald, 5 % Häuser am Ufer."""
        perfect = aggregate_segments([95.0] * 100, [25.0] * 100)
        village = aggregate_segments([95.0] * 95 + [15.0] * 5, [25.0] * 100)
        # Bei genau 5 % gestoertem Ufer liegt das P10 noch im guten Bereich --
        # das ist rechnerisch richtig und genau der Grund, warum der Score
        # zusaetzlich Minimum, Anteil und laengsten Abschnitt braucht.
        self.assertGreaterEqual(perfect["shore_segment_p10_score"],
                                village["shore_segment_p10_score"])
        self.assertGreater(perfect["shore_segment_min_score"],
                           village["shore_segment_min_score"])
        self.assertEqual(perfect["developed_shore_percent"], 0.0)
        self.assertAlmostEqual(village["developed_shore_percent"], 5.0, places=1)
        self.assertEqual(village["longest_developed_section_m"], 125.0)
        # Der Mittelwert allein würde den Unterschied fast verschlucken:
        self.assertLess(perfect["shore_segment_mean_score"]
                        - village["shore_segment_mean_score"], 5.0)

    def test_p10_catches_a_larger_disturbed_share(self):
        perfect = aggregate_segments([95.0] * 100, [25.0] * 100)
        quarter = aggregate_segments([95.0] * 80 + [20.0] * 20, [25.0] * 100)
        self.assertGreater(perfect["shore_segment_p10_score"],
                           quarter["shore_segment_p10_score"])
        self.assertAlmostEqual(quarter["developed_shore_percent"], 20.0, places=1)

    def test_scores_feed_visual_seclusion(self):
        perfect = aggregate_segments([95.0] * 100, [25.0] * 100)
        village = aggregate_segments([95.0] * 95 + [15.0] * 5, [25.0] * 100)
        s_perfect = score_block(row(**perfect), CFG, "visual_seclusion")["score"]
        s_village = score_block(row(**village), CFG, "visual_seclusion")["score"]
        self.assertGreater(s_perfect - s_village, 15.0)

    def test_empty_shore_returns_none_not_zero(self):
        agg = aggregate_segments([], [])
        self.assertIsNone(agg["shore_segment_p10_score"])
        self.assertEqual(agg["shore_segment_count"], 0)

    def test_worst_segment_index(self):
        agg = aggregate_segments([90.0, 12.0, 80.0], [25.0] * 3)
        self.assertEqual(agg["worst_shore_segment_index"], 1)
        self.assertEqual(agg["shore_segment_min_score"], 12.0)


# ==========================================================================
# Milestone 3 -- Gebäude aus mehreren Quellen
# ==========================================================================


class TestBuildingMerge(unittest.TestCase):
    def test_same_building_from_two_sources_is_one(self):
        recs = [
            {"x": 100.0, "y": 100.0, "area_m2": 80.0, "source": "osm", "id": "o1"},
            {"x": 103.0, "y": 101.0, "area_m2": 92.0, "source": "overture", "id": "v1"},
        ]
        kept, _ = dedup_records(recs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(set(kept[0]["sources"]), {"osm", "overture"})

    def test_neighbouring_buildings_stay_separate(self):
        recs = [
            {"x": 0.0, "y": 0.0, "area_m2": 80.0, "source": "osm"},
            {"x": 40.0, "y": 0.0, "area_m2": 80.0, "source": "overture"},
        ]
        kept, _ = dedup_records(recs)
        self.assertEqual(len(kept), 2)

    def test_area_ratio_prevents_swallowing(self):
        """Eine große Halle darf die Hütte daneben nicht schlucken."""
        recs = [
            {"x": 0.0, "y": 0.0, "area_m2": 1200.0, "source": "overture"},
            {"x": 6.0, "y": 0.0, "area_m2": 15.0, "source": "osm"},
        ]
        kept, _ = dedup_records(recs, max_centroid_dist_m=12.0, max_area_ratio=4.0)
        self.assertEqual(len(kept), 2)

    def test_official_wins_priority(self):
        recs = [
            {"x": 0.0, "y": 0.0, "area_m2": 100.0, "source": "osm", "id": "o"},
            {"x": 2.0, "y": 0.0, "area_m2": 100.0, "source": "official", "id": "a"},
            {"x": 1.0, "y": 1.0, "area_m2": 100.0, "source": "overture", "id": "v"},
        ]
        kept, _ = dedup_records(recs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source"], "official")
        self.assertEqual(kept[0]["n_sources"], 3)

    def test_result_is_order_independent(self):
        recs = [
            {"x": 0.0, "y": 0.0, "area_m2": 100.0, "source": "osm"},
            {"x": 3.0, "y": 1.0, "area_m2": 110.0, "source": "overture"},
            {"x": 90.0, "y": 0.0, "area_m2": 60.0, "source": "overture"},
        ]
        a, _ = dedup_records(recs)
        b, _ = dedup_records(list(reversed(recs)))
        self.assertEqual(len(a), len(b))
        self.assertEqual(
            sorted(tuple(sorted(k["sources"])) for k in a),
            sorted(tuple(sorted(k["sources"])) for k in b),
        )

    def test_scenario_j_osm_blind(self):
        """Szenario J: OSM kennt 0 Gebäude, Overture 5."""
        recs = [
            {"x": float(50 * i), "y": 0.0, "area_m2": 70.0, "source": "overture"}
            for i in range(5)
        ]
        kept, _ = dedup_records(recs)
        summary = source_summary(kept, sources_queried=["osm", "overture"])
        self.assertEqual(summary["building_overture_count"], 5)
        self.assertEqual(summary["building_osm_count"], 0)
        self.assertTrue(summary["building_sources_disagree"])
        self.assertEqual(summary["building_source_count"], 1)

    def test_agreement_is_not_flagged(self):
        recs = []
        for i in range(4):
            recs.append({"x": 60.0 * i, "y": 0.0, "area_m2": 80.0, "source": "osm"})
            recs.append({"x": 60.0 * i + 2, "y": 1.0, "area_m2": 85.0, "source": "overture"})
        kept, _ = dedup_records(recs)
        summary = source_summary(kept, sources_queried=["osm", "overture"])
        self.assertEqual(summary["building_count_merged"], 4)
        self.assertFalse(summary["building_sources_disagree"])


# ==========================================================================
# Milestone 4 -- Zugangspunkte
# ==========================================================================


ACFG = {"access": {"max_walk_m": 2500.0}}


class TestAccessSelection(unittest.TestCase):
    def test_public_road_beats_private_shortcut(self):
        private_close = {"walk_m": 60.0, "road_class": "minor_road", "private": True}
        public_far = {"walk_m": 700.0, "road_class": "minor_road", "private": False}
        best, _ = select_best_access([private_close, public_far], ACFG)
        self.assertFalse(best["private"])

    def test_shorter_walk_wins_among_equals(self):
        best, _ = select_best_access(
            [{"walk_m": 900.0, "road_class": "minor_road", "private": False},
             {"walk_m": 250.0, "road_class": "minor_road", "private": False}],
            ACFG,
        )
        self.assertEqual(best["walk_m"], 250.0)

    def test_track_is_usable_but_ranked_lower(self):
        road = candidate_score({"walk_m": 400.0, "road_class": "minor_road", "private": False}, ACFG)
        track = candidate_score({"walk_m": 400.0, "road_class": "soft_way", "private": False}, ACFG)
        self.assertGreater(road, track)
        self.assertGreater(track, 0.0)

    def test_walk_estimate_applies_detour(self):
        self.assertAlmostEqual(walk_estimate(1000.0, {"access": {"walk_detour_factor": 1.35}}),
                               1350.0, places=1)

    def test_no_candidates_returns_none(self):
        best, scored = select_best_access([], ACFG)
        self.assertIsNone(best)
        self.assertEqual(scored, [])


# ==========================================================================
# Milestone 5/6 -- Gelände und Zeltfläche
# ==========================================================================


def plane(shape=(40, 40), slope_deg=0.0, px=1.0, axis="x"):
    r, c = np.mgrid[0 : shape[0], 0 : shape[1]]
    grad = math.tan(math.radians(slope_deg)) * px
    return (c * grad if axis == "x" else r * grad).astype("float64")


class TestTerrain(unittest.TestCase):
    def test_flat_plane_has_zero_slope(self):
        s, _ = terrain.slope_aspect(np.zeros((20, 20)), 1.0)
        self.assertLess(float(np.nanmax(s)), 1e-9)

    def test_known_slope_is_recovered(self):
        dem = plane((30, 30), slope_deg=5.0, px=1.0)
        s, _ = terrain.slope_aspect(dem, 1.0)
        self.assertAlmostEqual(float(np.median(s)), 5.0, places=4)

    def test_slope_scales_with_pixel_size(self):
        dem = plane((30, 30), slope_deg=5.0, px=1.0)
        s, _ = terrain.slope_aspect(dem, 2.0)   # dieselben Höhen, doppelte Zellgröße
        self.assertLess(float(np.median(s)), 5.0)

    def test_pit_is_detected_with_correct_depth(self):
        dem = np.zeros((21, 21))
        dem[10, 10] = -0.8
        depth = terrain.depression_depth(dem)
        self.assertAlmostEqual(float(depth[10, 10]), 0.8, places=6)
        self.assertAlmostEqual(float(depth[0, 0]), 0.0, places=6)

    def test_slope_has_no_depression(self):
        depth = terrain.depression_depth(plane((20, 20), 4.0))
        self.assertLess(float(depth.max()), 1e-6)

    def test_flow_accumulates_downhill(self):
        dem = plane((20, 20), slope_deg=3.0, axis="x")
        acc = terrain.flow_accumulation(dem, 1.0)
        # Wasser läuft zu kleinen x; dort muss mehr ankommen als oben
        self.assertGreater(float(acc[:, 0].mean()), float(acc[:, -1].mean()))

    def test_wetness_higher_in_hollow(self):
        dem = plane((25, 25), slope_deg=2.0, axis="y")
        dem[12, :] -= 0.5                      # Rinne quer zum Hang
        twi = terrain.wetness_index(dem, 1.0)
        self.assertGreater(float(twi[12, 12]), float(twi[5, 12]))

    def test_local_relief_measures_step(self):
        dem = np.zeros((20, 20))
        dem[10:, :] = 0.4
        rel = terrain.local_relief(dem, radius=2)
        self.assertAlmostEqual(float(rel[10, 5]), 0.4, places=6)
        self.assertAlmostEqual(float(rel[2, 5]), 0.0, places=6)

    def test_summary_carries_resolution(self):
        s = terrain.terrain_summary(plane((20, 20), 3.0), 1.0)
        self.assertEqual(s["dem_resolution_m"], 1.0)
        self.assertGreater(s["max_slope_deg"], 0.0)


class TestTentFootprint(unittest.TestCase):
    def test_rect_mask_area_matches_tent(self):
        m = camp.rect_mask((60, 60), (30, 30), 4.0, 6.0, 0.0, 0.5)
        area = m.sum() * 0.25
        self.assertAlmostEqual(area, 24.0, delta=3.0)

    def test_rotation_preserves_area(self):
        a = camp.rect_mask((80, 80), (40, 40), 4.0, 6.0, 0.0, 0.5).sum()
        b = camp.rect_mask((80, 80), (40, 40), 4.0, 6.0, 45.0, 0.5).sum()
        self.assertLess(abs(int(a) - int(b)) / max(int(a), 1), 0.25)

    def test_flat_ground_fits(self):
        dem = np.zeros((40, 40))
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), {"camp": {"tent": "4x6"}})
        self.assertTrue(fit["tent_fit"])
        self.assertLess(fit["max_slope_deg"], 0.01)

    def test_steep_ground_does_not_fit(self):
        dem = plane((40, 40), slope_deg=12.0)
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), {"camp": {"max_slope_deg": 5.0}})
        self.assertFalse(fit["tent_fit"])
        self.assertTrue(fit["tent_fit_failed"])

    def test_rotation_finds_the_flat_direction(self):
        """Schmales ebenes Band quer zum Hang: nur eine Ausrichtung passt."""
        dem = plane((60, 60), slope_deg=14.0, axis="y")
        dem[27:33, :] = dem[30, :].mean()       # 6 m breites ebenes Band
        cfg = {"camp": {"tent": "3x5", "max_slope_deg": 5.0, "rotation_step_deg": 15.0}}
        fit = camp.best_tent_fit(dem, 1.0, (30, 30), cfg)
        self.assertTrue(fit["tent_fit"])
        # Die Längsachse (5 m) passt nicht in 4 m Bandbreite, sie muss also
        # entlang des Bandes liegen -- Ausrichtung nahe 90 Grad.
        self.assertGreater(fit["best_tent_orientation_deg"], 60.0)
        self.assertLess(fit["best_tent_orientation_deg"], 120.0)

    def test_whole_footprint_must_be_flat(self):
        """Eine ebene Zelle neben einer Kante reicht nicht."""
        dem = np.zeros((40, 40))
        dem[:, 21:] = np.arange(19) * 0.6       # Steilkante direkt daneben
        cfg = {"camp": {"tent": "4x6", "max_slope_deg": 5.0, "max_relief_cm": 25.0}}
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), cfg)
        self.assertFalse(fit["tent_fit"])

    def test_flat_area_measures_connected_patch(self):
        dem = np.zeros((40, 40))
        dem[:, 20:] = plane((40, 20), slope_deg=20.0)
        slope, _ = terrain.slope_aspect(dem, 1.0)
        area = camp.flat_area_m2(slope, (10, 5), 5.0, 1.0)
        self.assertGreater(area, 200.0)
        self.assertLess(area, 40 * 40)

    def test_corridor_mask(self):
        d = np.array([[5.0, 30.0], [100.0, 400.0]])
        m = camp.corridor_mask(d, 20.0, 250.0)
        self.assertFalse(bool(m[0, 0]))
        self.assertTrue(bool(m[0, 1]))
        self.assertTrue(bool(m[1, 0]))
        self.assertFalse(bool(m[1, 1]))


class TestCampCandidates(unittest.TestCase):
    def _setup(self, dem):
        dist = np.zeros_like(dem)
        for c in range(dem.shape[1]):
            dist[:, c] = c * 1.0          # Wasser bei Spalte 0
        return dist

    def test_finds_candidates_on_flat_shore(self):
        dem = np.zeros((60, 60))
        dist = self._setup(dem)
        cands = camp.find_camp_candidates(
            dem, 1.0, dist, {"camp": {"corridor_min_m": 20, "corridor_max_m": 50,
                                      "max_candidates_per_lake": 3}}
        )
        self.assertTrue(cands)
        for c in cands:
            self.assertGreaterEqual(c["distance_to_water_m"], 20.0)
            self.assertLessEqual(c["distance_to_water_m"], 50.0)
            self.assertTrue(c["tent_fit"])

    def test_steep_shore_yields_nothing(self):
        dem = plane((60, 60), slope_deg=18.0, axis="y")
        dist = self._setup(dem)
        cands = camp.find_camp_candidates(
            dem, 1.0, dist, {"camp": {"corridor_min_m": 20, "corridor_max_m": 50}}
        )
        self.assertEqual(cands, [])

    def test_wet_hollow_is_excluded(self):
        dem = np.zeros((60, 60))
        dem[25:35, 25:35] = -0.6          # feuchte Senke mitten im Korridor
        dist = self._setup(dem)
        cands = camp.find_camp_candidates(
            dem, 1.0, dist,
            {"camp": {"corridor_min_m": 20, "corridor_max_m": 45,
                      "max_depression_depth_m": 0.25, "max_candidates_per_lake": 20}},
        )
        for c in cands:
            self.assertFalse(25 <= c["row"] < 35 and 25 <= c["col"] < 35)

    def test_allowed_area_is_respected(self):
        dem = np.zeros((60, 60))
        dist = self._setup(dem)
        allowed = np.zeros((60, 60), dtype=bool)
        allowed[0:20, :] = True
        cands = camp.find_camp_candidates(
            dem, 1.0, dist,
            {"camp": {"corridor_min_m": 20, "corridor_max_m": 50,
                      "max_candidates_per_lake": 10}},
            allowed_mask=allowed,
        )
        self.assertTrue(cands)
        for c in cands:
            self.assertLess(c["row"], 20)
            self.assertTrue(c["allowed_area_checked"])

    def test_candidates_keep_minimum_separation(self):
        dem = np.zeros((80, 80))
        dist = self._setup(dem)
        cands = camp.find_camp_candidates(
            dem, 1.0, dist,
            {"camp": {"corridor_min_m": 20, "corridor_max_m": 70,
                      "min_candidate_separation_m": 30, "max_candidates_per_lake": 6}},
        )
        for i, a in enumerate(cands):
            for b in cands[i + 1:]:
                self.assertGreaterEqual(math.hypot(a["row"] - b["row"], a["col"] - b["col"]), 30.0)


# ==========================================================================
# Milestone 7 -- Sichtbarkeit und Privacy
# ==========================================================================


class TestViewshed(unittest.TestCase):
    def test_flat_plane_is_fully_visible(self):
        vis = viewshed.viewshed(np.zeros((31, 31)), 1.0, (15, 15))
        self.assertGreater(vis.mean(), 0.9)

    def test_wall_blocks_the_view(self):
        dsm = np.zeros((31, 31))
        dsm[:, 20] = 12.0                  # Mauer/Waldkante
        vis = viewshed.viewshed(dsm, 1.0, (15, 5), observer_height_m=1.6)
        self.assertFalse(bool(vis[15, 28]))
        self.assertTrue(bool(vis[15, 10]))

    def test_radius_limits_visibility(self):
        vis = viewshed.viewshed(np.zeros((61, 61)), 1.0, (30, 30), max_radius_m=10.0)
        self.assertFalse(bool(vis[30, 55]))
        self.assertTrue(bool(vis[30, 35]))

    def test_visible_fraction(self):
        vis = np.zeros((5, 5), dtype=bool)
        vis[0, :] = True
        targets = np.zeros((5, 5), dtype=bool)
        targets[0, :2] = True
        targets[4, :2] = True
        self.assertAlmostEqual(viewshed.visible_fraction(vis, targets), 0.5)

    def test_visible_fraction_without_targets_is_none(self):
        self.assertIsNone(
            viewshed.visible_fraction(np.ones((3, 3), dtype=bool), np.zeros((3, 3), dtype=bool))
        )

    def test_terrain_screening_is_measured(self):
        dem = np.zeros((41, 41))
        dem[:, 20] = 15.0                  # Geländerippe zwischen Platz und Straße
        targets = np.zeros((41, 41), dtype=bool)
        targets[18:23, 35] = True          # "Straße" dahinter
        sc = viewshed.screening_metrics(dem, None, 1.0, (20, 5), targets)
        self.assertGreater(sc["terrain_screening_percent"], 50.0)
        self.assertIsNone(sc["vegetation_screening_percent"])   # ohne DOM: nicht gemessen

    def test_vegetation_screening_needs_a_surface_model(self):
        dem = np.zeros((41, 41))
        dsm = dem.copy()
        dsm[:, 20] = 18.0                  # Waldriegel, aber flaches Gelände
        targets = np.zeros((41, 41), dtype=bool)
        targets[18:23, 35] = True
        sc = viewshed.screening_metrics(dem, dsm, 1.0, (20, 5), targets)
        self.assertLess(sc["terrain_screening_percent"], 10.0)
        self.assertGreater(sc["vegetation_screening_percent"], 50.0)

    def test_privacy_metrics_shape(self):
        dem = np.zeros((31, 31))
        roads = np.zeros((31, 31), dtype=bool)
        roads[15, 25:28] = True
        out = viewshed.privacy_metrics(
            dem, None, 1.0, (15, 5), {"minor_road": roads}, {"viewshed": {"max_radius_m": 40}}
        )
        self.assertIn("exposure_from_roads", out)
        self.assertEqual(out["viewshed_model"], "nur DGM")
        self.assertGreater(out["exposure_from_roads"], 0.0)

    def test_path_and_road_flags_are_separate(self):
        flags = viewshed.proximity_flags({"soft_way": 10.0, "minor_road": 400.0})
        self.assertTrue(flags["path_at_spot"])
        self.assertFalse(flags["road_at_spot"])


# ==========================================================================
# Szenarien A-J aus der Anforderung
# ==========================================================================


class TestScenarios(unittest.TestCase):
    """Die geforderten synthetischen Fälle, jeweils End-zu-Ende der Logik."""

    def test_a_perfect_forest_lake(self):
        r = row(distance_nearest_building_m=2200, building_count_100m=0,
                has_campsite=False, tree_cover_500m=96, drive_time_min=100,
                shore_segment_p10_score=92, natural_shore_percent=100,
                developed_shore_percent=0.0)
        out = evaluate(r, CFG)
        self.assertEqual(out.status, PASS)
        self.assertGreater(score_block(r, CFG, "visual_seclusion")["score"], 85)

    def test_b_ninetyfive_percent_forest_five_percent_houses(self):
        agg = aggregate_segments([95.0] * 95 + [10.0] * 5, [25.0] * 100)
        r = row(distance_nearest_building_m=90, building_count_100m=1,
                has_campsite=False, tree_cover_500m=93, drive_time_min=100, **agg)
        out = evaluate(r, CFG)
        self.assertEqual(out.status, FAIL)      # Gebäude am Ufer kippt den See
        self.assertLess(score_block(r, CFG, "visual_seclusion")["score"], 80)

    def test_c_road_right_next_to_the_lake(self):
        agg = aggregate_segments([90.0] * 70 + [25.0] * 30, [25.0] * 100)
        self.assertGreater(agg["developed_shore_percent"], 25.0)
        r = row(shore_segment_p10_score=agg["shore_segment_p10_score"],
                natural_shore_percent=agg["natural_shore_percent"],
                developed_shore_percent=agg["developed_shore_percent"])
        self.assertLess(score_block(r, CFG, "visual_seclusion")["score"], 60)

    def test_d_forest_track_is_mild(self):
        """Ein Waldweg darf nicht wie eine Straße wirken."""
        flags = viewshed.proximity_flags({"soft_way": 12.0})
        self.assertTrue(flags["path_at_spot"])
        self.assertFalse(flags["road_at_spot"])
        # und er bleibt ein brauchbarer Zugang
        self.assertGreater(
            candidate_score({"walk_m": 300.0, "road_class": "soft_way", "private": False}, ACFG),
            30.0,
        )

    def test_e_flat_dry_campsite(self):
        dem = np.zeros((40, 40))
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), {"camp": {}})
        dry = terrain.dryness_from_terrain(dem, 1.0)
        self.assertTrue(fit["tent_fit"])
        # Eine perfekt ebene Flaeche ist hydrologisch mehrdeutig (sie kann
        # auch Staunaesse haben) -- entscheidend ist der Abstand zur Mulde
        # in Szenario F, nicht ein Absolutwert nahe 100.
        self.assertGreater(float(dry["dryness_score"][20, 20]), 70.0)
        self.assertLess(float(dry["wetness_risk"][20, 20]), 0.3)

    def test_f_flat_but_wet_hollow(self):
        dem = np.zeros((41, 41))
        yy, xx = np.mgrid[0:41, 0:41]
        dem -= 0.7 * np.exp(-((yy - 20) ** 2 + (xx - 20) ** 2) / 60.0)
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), {"camp": {"max_relief_cm": 25}})
        dry = terrain.dryness_from_terrain(dem, 1.0)
        # Flach genug in der Mitte der Mulde, aber nass
        flat_dry = terrain.dryness_from_terrain(np.zeros((41, 41)), 1.0)
        self.assertLess(
            float(dry["dryness_score"][20, 20]),
            float(flat_dry["dryness_score"][20, 20]) - 15.0,
        )
        self.assertGreater(float(dry["wetness_risk"][20, 20]), 0.3)

    def test_g_steep_ground(self):
        dem = plane((40, 40), slope_deg=16.0)
        fit = camp.best_tent_fit(dem, 1.0, (20, 20), {"camp": {"max_slope_deg": 5.0}})
        self.assertFalse(fit["tent_fit"])
        self.assertGreater(fit["max_slope_deg"], 10.0)

    def test_h_clearing_visible_from_road(self):
        dem = np.zeros((41, 41))
        road = np.zeros((41, 41), dtype=bool)
        road[18:23, 35] = True
        out = viewshed.privacy_metrics(
            dem, None, 1.0, (20, 5), {"minor_road": road},
            {"viewshed": {"max_radius_m": 60}},
        )
        self.assertGreater(out["exposure_from_roads"], 50.0)
        self.assertLess(out["terrain_screening_percent"], 20.0)

    def test_i_clearing_screened_by_terrain_and_trees(self):
        dem = np.zeros((41, 41))
        dem[:, 20] = 14.0
        dsm = dem.copy()
        dsm[:, 15] = 18.0
        road = np.zeros((41, 41), dtype=bool)
        road[18:23, 35] = True
        out = viewshed.privacy_metrics(
            dem, dsm, 1.0, (20, 5), {"minor_road": road},
            {"viewshed": {"max_radius_m": 60}},
        )
        self.assertLess(out["exposure_from_roads"], 20.0)
        self.assertGreater(out["terrain_screening_percent"], 50.0)

    def test_j_osm_blind_overture_sees_five(self):
        recs = [{"x": 40.0 * i, "y": 0.0, "area_m2": 70.0, "source": "overture"} for i in range(5)]
        kept, _ = dedup_records(recs)
        summary = source_summary(kept, sources_queried=["osm", "overture"])
        self.assertTrue(summary["building_sources_disagree"])
        summary.pop("building_sources_queried", None)
        summary.pop("building_sources", None)
        r = row(quality_flags=["building_sources_disagree"], **summary)
        conf = data_confidence_score(r, CFG)["data_confidence_score"]
        self.assertLess(conf, 85.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
