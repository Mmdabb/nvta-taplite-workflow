from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKFLOW_ROOT))

from nvta_taplite_workflow.dtalite4cube.runner import AssignmentConfig
from nvta_taplite_workflow.dtalite4cube.unit_contract import (
    KPH_PER_MPH,
    METERS_PER_MILE,
    TAPLITE_LINK_UNIT_CONTRACT,
    miles_to_taplite_length,
    mph_to_taplite_free_speed,
    validate_taplite_converter_units,
    validate_taplite_link_values,
)


class TapliteUnitContractTests(unittest.TestCase):
    def test_cube_source_values_map_to_fixed_taplite_columns(self):
        self.assertAlmostEqual(miles_to_taplite_length(1.0), METERS_PER_MILE)
        self.assertAlmostEqual(mph_to_taplite_free_speed(60.0), 60.0 * KPH_PER_MPH)
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["length"], "meter")
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["free_speed"], "kph")
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["vdf_length_mi"], "mile")
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["vdf_free_speed_mph"], "mph")
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["qvdf_start_speed_mph"], "mph")
        self.assertEqual(TAPLITE_LINK_UNIT_CONTRACT["qvdf_end_speed_mph"], "mph")

    def test_legacy_unit_flag_cannot_change_link_schema(self):
        metric = AssignmentConfig(network_path=WORKFLOW_ROOT, unit_system="metric")
        imperial = AssignmentConfig(network_path=WORKFLOW_ROOT, unit_system="imperial")
        for config in (metric, imperial):
            self.assertEqual(config.length_unit, "meter")
            self.assertEqual(config.speed_unit, "kph")
            self.assertEqual(config.metric_system, 1)

    def test_non_schema_converter_units_are_rejected(self):
        validate_taplite_converter_units("meter", "kph")
        for length_unit, speed_unit in (
            ("mile", "mph"),
            ("km", "kph"),
            ("meter", "mph"),
        ):
            with self.subTest(length_unit=length_unit, speed_unit=speed_unit):
                with self.assertRaises(ValueError):
                    validate_taplite_converter_units(length_unit, speed_unit)

    def test_converted_link_dual_columns_are_numerically_equivalent(self):
        validate_taplite_link_values(
            length_meters=METERS_PER_MILE,
            free_speed_kph=60.0 * KPH_PER_MPH,
            vdf_length_mi=1.0,
            vdf_free_speed_mph=60.0,
            vdf_fftt_minutes=1.0,
            link_id=10,
        )
        with self.assertRaises(ValueError):
            validate_taplite_link_values(
                length_meters=1.0,
                free_speed_kph=60.0,
                vdf_length_mi=1.0,
                vdf_free_speed_mph=60.0,
                vdf_fftt_minutes=1.0,
                link_id=10,
            )

    def test_packaged_lookup_metadata_declares_units(self):
        resources = WORKFLOW_ROOT / "src" / "nvta_taplite_workflow" / "dtalite4cube" / "resources"
        plf = json.loads(
            (resources / "observed_link_plf_lookup" / "metadata.json").read_text()
        )
        speed = json.loads(
            (resources / "observed_link_speed_boundary_lookup" / "metadata.json").read_text()
        )
        observed_t2 = json.loads(
            (resources / "observed_link_t2_lookup" / "metadata.json").read_text()
        )
        boundaries = json.loads(
            (resources / "congestion_t_node_pair_lookup" / "metadata.json").read_text()
        )
        self.assertEqual(plf["unit"], "dimensionless")
        self.assertEqual(speed["speed_unit"], "mph")
        self.assertIn("NaN triplet", observed_t2["no_episode_rule"])
        observed_t2_fields = np.load(
            resources / "observed_link_t2_lookup" / "observed_link_t2.npy",
            mmap_mode="r",
            allow_pickle=False,
        ).dtype.names
        expected_observed_t2_fields = {
            f"observed_t{boundary}_hour_{period}"
            for period in ("am", "md", "pm")
            for boundary in (0, 2, 3)
        }
        self.assertTrue(expected_observed_t2_fields.issubset(observed_t2_fields))
        self.assertEqual(boundaries["time_unit"], "decimal hour")


if __name__ == "__main__":
    unittest.main()
