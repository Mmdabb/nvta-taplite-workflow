from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from nvta_taplite_workflow.dtalite4cube.unit_contract import KPH_PER_MPH
from nvta_taplite_workflow.postprocessing.pipeline.speed_volume import (
    BASE_COLUMNS, inverse_s3_link_flow, s3_parameters, write_speed_volume,
)
from nvta_taplite_workflow.postprocessing.runner import preprocess_and_summarize_scenario


class S3Tests(unittest.TestCase):
    def test_critical_speed_recovers_whole_link_capacity_and_m(self):
        data = pd.DataFrame({
            "network_free_speed": [60 * KPH_PER_MPH],
            "speed_at_capacity_mph": [42],
            "network_capacity": [1800], "network_lanes": [3],
        })
        params = s3_parameters(data, np.array([[42.0]]))
        self.assertAlmostEqual(params.loc[0, "free_speed"], 60)
        self.assertAlmostEqual(params.loc[0, "s3_m"], 2 * np.log(2) / np.log(60 / 42))
        self.assertEqual(params.loc[0, "capacity"], 5400)
        self.assertAlmostEqual(inverse_s3_link_flow(np.array([[42.0]]), params)[0, 0], 5400)

    def test_m_derives_critical_speed_and_supplied_link_capacity_is_not_multiplied(self):
        data = pd.DataFrame({
            "network_vdf_free_speed_mph": [60], "s3_m": [4],
            "link_capacity": [5000], "network_lanes": [3],
        })
        params = s3_parameters(data, np.array([[40.0]]))
        self.assertAlmostEqual(params.loc[0, "critical_speed_mph"], 60 / np.sqrt(2))
        self.assertEqual(params.loc[0, "capacity"], 5000)

    def test_known_low_free_speed_is_preserved_and_only_missing_parameters_are_assumed(self):
        params = s3_parameters(pd.DataFrame({"network_vdf_free_speed_mph": [17]}), np.array([[15.0]]))
        self.assertEqual(params.loc[0, "free_speed"], 17)
        self.assertAlmostEqual(params.loc[0, "critical_speed_mph"], 11.9)
        self.assertEqual(params.loc[0, "capacity"], 1800)
        self.assertEqual(params.loc[0, "lanes_source"], "assumed_1_lane")

    def test_cbi_inverse_equation_and_clipping(self):
        params = s3_parameters(pd.DataFrame({
            "network_vdf_free_speed_mph": [60], "link_capacity": [4000],
        }), np.array([[10, 42, 59.4, 90, 0, np.nan, -1]]))
        speeds = np.array([[10, 42, 59.4, 90, 0, np.nan, -1]])
        actual = inverse_s3_link_flow(speeds, params)
        clipped = np.minimum(speeds[0, :5], 59.4)
        m = params.loc[0, "s3_m"]
        density = (4000 / 42) * ((60 / np.maximum(clipped, 1)) ** (m / 2) - 1) ** (1 / m)
        np.testing.assert_allclose(actual[0, :5], np.minimum(clipped * density, 4000))
        np.testing.assert_array_equal(actual[0, 5:], [0, 0])


class SpeedVolumeExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nvta_s3_")
        self.root = Path(self.temp.name)
        self.periods = ["am", "md", "nt"]
        self.ranges = ["0600_0610", "0610_0620", "2350_0010"]
        self.links = pd.DataFrame({
            "link_id": [1, 2], "from_node_id": [10, 20], "to_node_id": [20, 10],
            "link_type": [101, 101], "lanes": [3, 2], "capacity": [1800, 1800],
            "free_speed": [60 * KPH_PER_MPH] * 2, "vdf_free_speed_mph": [60] * 2,
            "TAZ": [1500, 3000], "FT": [1, 1], "length": [1609.344] * 2,
            "length_in_mile": [1.0] * 2, "STREETNAME": ["Forward", "Reverse"],
        })
        frames = []
        for period, stamps, volumes in [
            ("am", ["06:00", "06:05"], [120, 0]),
            ("md", ["06:10", "06:15"], [240, 70]),
            ("nt", ["23:50", "00:00"], [360, 80]),
        ]:
            directory = self.root / period
            directory.mkdir()
            self.links.to_csv(directory / "link.csv", index=False)
            frame = pd.DataFrame({
                "link_id": [9001, 9002], "from_node_id": [10, 20], "to_node_id": [20, 10],
                "volume": volumes, "speed_mph": [30, 40], "time_period": period,
                "spd_mph_" + stamps[0]: [20, 30], "spd_mph_" + stamps[1]: [42, 40],
            })
            frame.to_csv(directory / "link_performance.csv", index=False)
            frames.append(frame)
        self.combined = pd.concat(frames, ignore_index=True)

    def tearDown(self):
        self.temp.cleanup()

    def export(self, data=None, periods=None, ranges=None):
        return write_speed_volume(
            self.combined if data is None else data, self.root,
            self.periods if periods is None else periods, self.ranges if ranges is None else ranges,
            output_filename="link_speed_volume.csv",
        )

    def test_unique_rows_order_and_independent_period_conservation_after_csv_roundtrip(self):
        output, audit = self.export()
        self.assertEqual(len(output), 2)
        self.assertEqual(output.link_id.tolist(), [1, 2])  # Network IDs, not internal IDs.
        self.assertEqual(output.columns[:7].tolist(), BASE_COLUMNS)
        self.assertEqual(output.columns[7:13].tolist(), sorted(c for c in output if c.startswith("spd_mph_")))
        self.assertEqual(output.loc[0, "capacity"], 5400)
        saved = pd.read_csv(self.root / "link_speed_volume.csv")
        for cols, target in [
            (["06:00", "06:05"], 120), (["06:10", "06:15"], 240), (["23:50", "00:00"], 360),
        ]:
            self.assertAlmostEqual(saved.loc[0, ["approx_volume_" + t for t in cols]].sum(), target)
        self.assertLess(audit.conservation_error.abs().max(), 1e-10)
        self.assertTrue((saved.filter(like="approx_volume_") >= 0).all().all())
        self.assertEqual(audit.loc[audit.time_period == "nt", "interval_minutes_min"].iloc[0], 10)

    def test_each_interval_uses_the_same_period_multiplier(self):
        output, audit = self.export()
        am = audit[(audit.time_period == "am") & (audit.from_node_id == "10")].iloc[0]
        params = pd.DataFrame([am])
        raw = inverse_s3_link_flow(np.array([[20, 42]]), params) / 12
        np.testing.assert_allclose(output.loc[0, ["approx_volume_06:00", "approx_volume_06:05"]].to_numpy(dtype=float), raw[0] * am.scale_factor)

    def test_zero_and_invalid_speed_fallback_conserves_positive_assignment(self):
        data = self.combined.copy()
        data.loc[data.time_period == "am", ["spd_mph_06:00", "spd_mph_06:05"]] = 0
        data.loc[(data.time_period == "md") & (data.from_node_id == 10), ["spd_mph_06:10", "spd_mph_06:15"]] = np.nan
        output, audit = self.export(data)
        self.assertEqual(output.loc[0, "approx_volume_06:00"], 60)
        self.assertEqual(output.loc[0, "approx_volume_06:10"], 120)
        self.assertEqual((audit.status == "uniform_duration_fallback").sum(), 2)
        self.assertEqual(output.loc[1, "approx_volume_06:00"], 0)

    def test_period_lanes_and_capacity_are_preserved_in_audit(self):
        links = self.links.copy()
        links["lanes"] = [1, 2]
        links.to_csv(self.root / "md" / "link.csv", index=False)
        output, audit = self.export()
        self.assertEqual(output.loc[0, "capacity"], 5400)
        md = audit[(audit.time_period == "md") & (audit.from_node_id == "10")].iloc[0]
        self.assertEqual(md.capacity, 1800)
        self.assertEqual(md.lanes, 1)

    def test_missing_overnight_columns_use_period_average_on_complete_grid(self):
        data = self.combined.drop(columns=["spd_mph_00:00"]).copy()
        # Add 23:55 so the available NT profile defines a five-minute grid.
        data["spd_mph_23:55"] = 25.0
        output, audit = self.export(data)
        self.assertEqual(output.loc[0, "spd_mph_00:00"], 30)
        self.assertEqual(output.loc[0, "spd_mph_00:05"], 30)
        nt = audit[audit.time_period == "nt"]
        self.assertTrue((nt.imputed_speed_intervals == 2).all())
        self.assertTrue((nt.imputation_source == "speed_mph").all())
        self.assertAlmostEqual(output.loc[0, ["approx_volume_23:50", "approx_volume_23:55", "approx_volume_00:00", "approx_volume_00:05"]].sum(), 360)

    def test_legacy_kmph_speeds_convert_to_mph(self):
        data = self.combined[self.combined.time_period == "am"].copy()
        data["spd_kmph_06:00"] = data.pop("spd_mph_06:00") * KPH_PER_MPH
        data["spd_kmph_06:05"] = data.pop("spd_mph_06:05") * KPH_PER_MPH
        output, _ = self.export(data, ["am"], ["0600_0610"])
        self.assertAlmostEqual(output.loc[0, "spd_mph_06:00"], 20)

    def test_link_absent_from_a_period_keeps_blank_cells(self):
        data = self.combined.drop(index=0)
        output, _ = self.export(data)
        row = output[output.from_node_id == "10"].iloc[0]
        self.assertTrue(pd.isna(row["approx_volume_06:00"]))
        self.assertAlmostEqual(row["approx_volume_06:10"] + row["approx_volume_06:15"], 240)

    def test_overlap_duplicate_pairs_and_unmatched_pairs_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.export(ranges=["0600_0610", "0605_0620", "2350_0010"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.export(pd.concat([self.combined, self.combined.iloc[[0]]]))
        data = self.combined.copy()
        data.loc[0, "from_node_id"] = 999
        with self.assertRaisesRegex(ValueError, "Unmatched"):
            self.export(data)

    def test_runner_creates_both_scope_exports_automatically(self):
        preprocess_and_summarize_scenario(
            scenario_dir=self.root, time_periods=self.periods,
            period_range_list=self.ranges, time_duration_dict={"am": 1/6, "md": 1/6, "nt": 1/3},
        )
        jurisdiction = pd.read_csv(self.root / "link_speed_volume.csv")
        regional = pd.read_csv(self.root / "link_speed_volume_regional.csv")
        self.assertEqual(len(jurisdiction), 1)
        self.assertEqual(len(regional), 2)
        self.assertAlmostEqual(jurisdiction.loc[0, "approx_volume_00:00"] + jurisdiction.loc[0, "approx_volume_23:50"], 360)


if __name__ == "__main__":
    unittest.main()
