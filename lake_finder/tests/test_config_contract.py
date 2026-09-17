"""
Vertrag zwischen config.yaml und Code.

Der häufigste stille Fehler in einer konfigurierbaren Scoring-Pipeline ist
ein Metrikname, den die config nennt und den niemand berechnet. Er fällt
nicht auf: die Score-Engine behandelt ihn als "fehlend", zieht still den
``missing``-Wert ab und liefert plausible Zahlen auf falscher Grundlage.

Dieser Test führt deshalb eine explizite Liste ALLER Spalten, die die
Pipeline erzeugen kann, und prüft jede Metrik aus config.yaml dagegen.
Wer eine neue Metrik einbaut, trägt sie hier ein -- wer sich vertippt,
merkt es sofort.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

DISTANCES = [100, 250, 500, 1000]
OSM_CATS = [
    "building", "residential_area", "commercial_industrial", "campsite",
    "accommodation", "marina", "parking", "man_made", "major_road",
    "minor_road", "soft_way", "railway",
]
LC_CLASSES = [
    "tree_cover", "shrubland", "grassland", "cropland", "built_up", "bare",
    "snow_ice", "water", "wetland", "mangroves", "moss_lichen",
]


def produced_columns() -> set[str]:
    """Alle Spalten, die die Pipeline erzeugen kann."""
    cols: set[str] = {
        # lakes.py
        "name", "osm_type", "osm_id", "lake_uid", "area_ha", "perimeter_m",
        "shore_complexity", "latitude", "longitude", "centroid_lat", "centroid_lon",
        # osm_features.py
        "has_campsite", "has_marina", "has_accommodation", "has_residential",
        "has_parking", "has_railway", "flag_radius_m", "osm_features_ok",
        "distance_nearest_building_m", "distance_residential_m",
        # buildings/
        "building_osm_count", "building_overture_count", "building_official_count",
        "building_source_count", "building_sources_disagree", "building_count_merged",
        "building_data_ok", "building_sources", "building_sources_queried",
        # landcover.py
        "shore_tree_cover", "shore_natural_percent", "shore_built_up_percent",
        "natural_land_percent", "built_up_percent", "cropland_percent",
        "grassland_percent", "shrubland_percent", "landcover_ok",
        "landcover_ref_distance_m", "landcover_source",
        # shoreline.py
        "shore_segment_count", "shore_length_m", "shore_segment_mean_score",
        "shore_segment_median_score", "shore_segment_p10_score",
        "shore_segment_min_score", "developed_shore_fraction",
        "developed_shore_percent", "natural_shore_fraction", "natural_shore_percent",
        "longest_developed_section_m", "longest_natural_section_m",
        "worst_shore_segment_index", "worst_shore_segment", "shore_segments_ok",
        "shore_segment_spacing_m",
        # travel.py / access.py
        "air_distance_km", "drive_time_min", "drive_distance_km", "drive_time_source",
        "drive_access_lat", "drive_access_lon", "walking_distance_to_lake_m",
        "walking_distance_to_spot_m", "walking_distance_source", "travel_ok",
        "access_point_missing", "access_via_private_road", "access_road_class",
        "access_road_name", "access_candidate_count", "access_candidates",
        # camp.py / terrain.py / spots.py
        "flat_area_m2", "mean_slope_deg", "max_slope_deg", "relief_cm",
        "local_relief_cm", "roughness_cm", "depression_depth_m", "wet_depression",
        "dryness_score", "wetness_risk", "wetness_index", "flood_risk_proxy",
        "distance_to_water_m", "height_above_water_m", "close_to_water",
        "very_long_walkout", "tent_fit", "tent_fit_failed", "tent_size_m",
        "best_tent_orientation_deg", "footprint_cells", "terrain_ok",
        "dem_resolution_m", "dem_source", "tent_grade_dem", "allowed_area_checked",
        "safety_flags", "spot_id", "lake_name",
        # viewshed.py
        "terrain_screening_percent", "vegetation_screening_percent",
        "exposure_from_roads", "exposure_from_paths", "exposure_from_buildings",
        "visible_buildings_count", "visible_road_length_m", "visible_path_length_m",
        "visible_human_features", "lake_visibility_percent", "viewshed_radius_m",
        "viewshed_model", "path_at_spot", "road_at_spot", "parking_near_spot",
        # scoring.py
        "wilderness_score", "visual_seclusion_score", "access_score",
        "privacy_exposure_score", "campsite_suitability_score", "safety_score",
        "data_confidence_score", "overall_spot_score", "overall_coverage",
        "quality_flags", "confidence", "passes_filters", "filter_status",
    }
    for d in DISTANCES:
        for cat in OSM_CATS:
            cols.add(f"{cat}_count_{d}m")
        for cat in OSM_CATS + ["any_road"]:
            cols.add(f"distance_{cat}_m")
        for cls in LC_CLASSES:
            cols.add(f"{cls}_percent_{d}m")
        for src in ("osm", "overture", "official"):
            cols.add(f"building_{src}_count_{d}m")
        cols.update({
            f"tree_cover_{d}m", f"natural_land_percent_{d}m", f"n_pixels_{d}m",
            f"valid_pixels_{d}m", f"nodata_percent_{d}m", f"water_percent_{d}m",
        })
    for key in ("buildings", "major_road", "minor_road", "soft_way", "parking", "campsite"):
        cols.add(f"distance_{key}_m")
        cols.add(f"exposure_{key}_percent")
        cols.add(f"visible_{key}_cells")
    return cols


# Bewusst noch nicht berechnete Metriken. Sie stehen mit Gewicht 0 bzw.
# 0 Punkten in der config und werden aktiv, sobald ein Oberflächenmodell
# (DOM/nDOM) konfiguriert ist -- Phase 10 der Ausbaustufe.
PLANNED_NOT_YET_COMPUTED = {"ground_openness", "canopy_cover_percent",
                            "vegetation_height_estimate", "opening_area_m2",
                            "unscreened_open_ground"}


class TestConfigContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        cls.produced = produced_columns()

    def _entries(self):
        """(Pfad, Spec) für jede Score-Komponente, jeden Abzug, jeden Filter."""
        s = self.cfg.get("scoring", {}) or {}
        out = []
        for name, spec in (s.get("components", {}) or {}).items():
            out.append((f"scoring.components.{name}", spec))
        for name, spec in (s.get("penalties", {}) or {}).items():
            out.append((f"scoring.penalties.{name}", spec))
        for block, bcfg in (s.get("scores", {}) or {}).items():
            for name, spec in ((bcfg or {}).get("components", {}) or {}).items():
                out.append((f"scoring.scores.{block}.components.{name}", spec))
            for name, spec in ((bcfg or {}).get("penalties", {}) or {}).items():
                out.append((f"scoring.scores.{block}.penalties.{name}", spec))
        for name, spec in ((self.cfg.get("filters", {}) or {}).get("checks", {}) or {}).items():
            out.append((f"filters.checks.{name}", spec))
        return out

    def test_every_configured_metric_is_produced(self):
        unknown = []
        for path, spec in self._entries():
            if not isinstance(spec, dict):
                continue
            key = spec.get("metric") or spec.get("flag")
            if key is None:
                key = path.rsplit(".", 1)[-1]
            if key in self.produced or key in PLANNED_NOT_YET_COMPUTED:
                continue
            unknown.append(f"{path} -> '{key}'")
        self.assertEqual(
            unknown, [],
            "Diese Metriken stehen in config.yaml, werden aber nirgends berechnet:\n  "
            + "\n  ".join(unknown),
        )

    def test_planned_metrics_carry_no_weight(self):
        """Noch nicht berechnete Metriken dürfen den Score nicht verschieben."""
        for path, spec in self._entries():
            if not isinstance(spec, dict):
                continue
            key = spec.get("metric") or spec.get("flag")
            if key in PLANNED_NOT_YET_COMPUTED:
                weight = float(spec.get("weight", 0.0))
                points = float(spec.get("points", 0.0) or 0.0)
                ppu = float(spec.get("points_per_unit", 0.0) or 0.0)
                self.assertEqual(
                    (weight, points, ppu), (0.0, 0.0, 0.0),
                    f"{path} nutzt '{key}', das noch nicht berechnet wird, "
                    "hat aber ein Gewicht.",
                )

    def test_ramps_are_two_numbers(self):
        for path, spec in self._entries():
            if isinstance(spec, dict) and "ramp" in spec:
                r = spec["ramp"]
                self.assertEqual(len(r), 2, f"{path}: ramp braucht genau zwei Werte")
                self.assertNotEqual(r[0], r[1], f"{path}: ramp mit identischen Grenzen")

    def test_filter_checks_have_a_condition(self):
        for name, spec in ((self.cfg.get("filters", {}) or {}).get("checks", {}) or {}).items():
            has = {"min", "max", "flag"} & set(spec)
            self.assertTrue(has, f"filters.checks.{name} definiert keine Bedingung")

    def test_filter_sources_are_known(self):
        from src.filters import ALL_SOURCES

        for name, spec in ((self.cfg.get("filters", {}) or {}).get("checks", {}) or {}).items():
            for src in spec.get("requires", []):
                self.assertIn(src, ALL_SOURCES, f"filters.checks.{name}: Quelle '{src}' unbekannt")

    def test_overall_profiles_reference_known_blocks(self):
        from src.scoring import SCORE_BLOCKS

        ocfg = (self.cfg.get("scoring", {}) or {}).get("overall", {}) or {}
        known = set(SCORE_BLOCKS) | {"lake_view"}
        for name, weights in (ocfg.get("profiles", {}) or {}).items():
            for block in (weights or {}):
                self.assertIn(block, known, f"Profil {name}: unbekannter Block '{block}'")
        for block in (ocfg.get("weights", {}) or {}):
            self.assertIn(block, set(SCORE_BLOCKS), f"overall.weights: unbekannt '{block}'")

    def test_strict_defaults_reject_the_zermittensee(self):
        """Die ausgelieferten Standardwerte müssen den realen Fehlfall fangen."""
        from src.filters import FAIL, evaluate

        out = evaluate(
            {
                "landcover_ok": True, "building_data_ok": True, "osm_features_ok": True,
                "travel_ok": True, "shore_segments_ok": True,
                "distance_nearest_building_m": 60.9, "building_count_100m": 1,
                "building_count_250m": 1, "building_count_500m": 2,
                "has_campsite": True, "has_residential": True, "has_parking": True,
                "has_marina": False, "has_accommodation": False,
                "tree_cover_500m": 88.0, "shore_tree_cover": 91.0,
                "built_up_percent": 0.3, "cropland_percent": 4.0,
                "distance_major_road_m": 980.0, "drive_time_min": 95.0,
                "developed_shore_percent": 8.0, "shore_segment_p10_score": 52.0,
            },
            self.cfg,
        )
        self.assertEqual(out.status, FAIL)
        self.assertFalse(out.passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
