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
            "network_free_speed": [60 * KPH_PER_MPH], "speed_at_capacity_mph": [45],
            "network_capacity": [1800], "network_lanes": [3],
        })
        params = s3_parameters(data, np.array([[45.0]]))
        self.assertAlmostEqual(params.loc[0, "free_speed"], 60)
        self.assertAlmostEqual(params.loc[0, "s3_m"], 2 * np.log(2) / np.log(60 / 45))
        self.assertEqual(params.loc[0, "capacity"], 5400)
        self.assertAlmostEqual(inverse_s3_link_flow(np.array([[45.0]]), params)[0, 0], 5400)

    def test_m_derives_critical_speed_and_link_capacity_is_not_multiplied(self):
        params = s3_parameters(pd.DataFrame({
            "network_vdf_free_speed_mph": [60], "s3_m": [4],
            "link_capacity": [5000], "network_lanes": [3],
        }), np.array([[40.0]]))
        self.assertAlmostEqual(params.loc[0, "critical_speed_mph"], 60 / np.sqrt(2))
        self.assertEqual(params.loc[0, "capacity"], 5000)

    def test_default_critical_speed_matches_kernel_ratio(self):
        params = s3_parameters(pd.DataFrame({"network_vdf_free_speed_mph": [17]}), np.array([[15.0]]))
        self.assertEqual(params.loc[0, "free_speed"], 17)
        self.assertAlmostEqual(params.loc[0, "critical_speed_mph"], 12.75)
        self.assertAlmostEqual(params.loc[0, "s3_m"], 4.818841679306419)
        self.assertEqual(params.loc[0, "critical_speed_source"], "assumed_0.75_vf")
        self.assertEqual(params.loc[0, "capacity"], 1800)

    def test_cbi_inverse_equation_and_clipping(self):
        speeds = np.array([[10, 45, 59.4, 90, 0, np.nan, -1]])
        params = s3_parameters(pd.DataFrame({"network_vdf_free_speed_mph": [60], "link_capacity": [4000]}), speeds)
        actual = inverse_s3_link_flow(speeds, params)
        clipped = np.minimum(speeds[0, :5], 59.4)
        m = params.loc[0, "s3_m"]
        density = (4000 / 45) * ((60 / np.maximum(clipped, 1)) ** (m / 2) - 1) ** (1 / m)
        np.testing.assert_allclose(actual[0, :5], np.minimum(clipped * density, 4000))
        np.testing.assert_array_equal(actual[0, 5:], [0, 0])


class SpeedVolumeExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nvta_s3_")
        self.root = Path(self.temp.name)
        self.periods = ["am", "md"]
        self.ranges = ["0630_0730", "0730_0845"]
        self.links = pd.DataFrame({
            "link_id": [1, 2], "from_node_id": [10, 20], "to_node_id": [20, 10],
            "link_type": [101, 101], "lanes": [3, 2], "capacity": [1800, 1800],
            "free_speed": [60 * KPH_PER_MPH] * 2, "vdf_free_speed_mph": [60] * 2,
            "TAZ": [1500, 3000], "FT": [1, 1], "length": [1609.344] * 2,
            "length_in_mile": [1.0] * 2, "STREETNAME": ["Forward", "Reverse"],
        })
        frames = []
        for period, start, end, volumes in [
            ("am", 390, 450, [120, 0]), ("md", 450, 525, [240, 70]),
        ]:
            directory = self.root / period
            directory.mkdir()
            self.links.to_csv(directory / "link.csv", index=False)
            frame = pd.DataFrame({
                "link_id": [9001, 9002], "from_node_id": [10, 20], "to_node_id": [20, 10],
                "volume": volumes, "speed_mph": [30, 40], "time_period": period,
            })
            speeds = {f"spd_mph_{t // 60:02d}:{t % 60:02d}": [20 if t < (start + end)/2 else 40, 30]
                      for t in range(start, end, 5)}
            frame = pd.concat([frame, pd.DataFrame(speeds)], axis=1)
            frame.to_csv(directory / "link_performance.csv", index=False)
            frames.append(frame)
        self.combined = pd.concat(frames, ignore_index=True)

    def tearDown(self):
        self.temp.cleanup()

    def export(self, data=None, periods=None, ranges="default"):
        return write_speed_volume(
            self.combined if data is None else data, self.root,
            self.periods if periods is None else periods,
            self.ranges if ranges == "default" else ranges,
            output_filename="link_speed_volume.csv",
        )

    def test_hourly_bins_start_at_first_period_and_end_at_last_with_partial_hour(self):
        output, audit = self.export()
        self.assertEqual(output.link_id.tolist(), [1, 2])
        self.assertEqual(output.columns[:7].tolist(), BASE_COLUMNS)
        self.assertEqual(output.columns[7:].tolist(), [
            "spd_mph_06:30_07:30", "spd_mph_07:30_08:30", "spd_mph_08:30_08:45",
            "approx_volume_06:30_07:30", "approx_volume_07:30_08:30", "approx_volume_08:30_08:45",
        ])
        self.assertEqual(output.loc[0, "capacity"], 5400)
        saved = pd.read_csv(self.root / "link_speed_volume.csv")
        self.assertAlmostEqual(saved.loc[0, "approx_volume_06:30_07:30"], 120)
        self.assertAlmostEqual(saved.loc[0, ["approx_volume_07:30_08:30", "approx_volume_08:30_08:45"]].sum(), 240)
        self.assertAlmostEqual(saved.loc[0, "spd_mph_06:30_07:30"], 2/(1/20 + 1/40))
        self.assertLess(audit.hourly_conservation_error.abs().max(), 1e-10)
        self.assertEqual(list(self.root.glob("*_audit.csv")), [])

    def test_volume_approximation_precedes_hourly_aggregation(self):
        output, audit = self.export()
        am = audit[(audit.time_period == "am") & (audit.from_node_id == "10")].iloc[0]
        params = pd.DataFrame([am])
        raw = inverse_s3_link_flow(np.array([[20]*6 + [40]*6]), params) / 12
        self.assertAlmostEqual(raw.sum() * am.scale_factor, output.loc[0, "approx_volume_06:30_07:30"])
        md = audit[(audit.time_period == "md") & (audit.from_node_id == "10")].iloc[0]
        raw = inverse_s3_link_flow(np.array([[20]*8 + [40]*7]), pd.DataFrame([md])) / 12
        self.assertAlmostEqual(raw[0, :12].sum()*md.scale_factor, output.loc[0, "approx_volume_07:30_08:30"])

    def test_hour_bin_crossing_period_boundary_combines_scaled_contributions(self):
        ranges = ["0630_0730", "0730_0845"]
        # Shift bin origin to 06:00 while leaving the second boundary at 07:30.
        extra = self.combined[self.combined.time_period == "am"].copy()
        for minute in range(360, 390, 5):
            extra[f"spd_mph_{minute//60:02d}:{minute%60:02d}"] = 20
        data = pd.concat([extra, self.combined[self.combined.time_period == "md"]], ignore_index=True)
        output, audit = self.export(data, ranges=["0600_0730", "0730_0845"])
        self.assertAlmostEqual(output.loc[0].filter(like="approx_volume_").sum(), 360)
        self.assertLess(audit.hourly_conservation_error.abs().max(), 1e-10)
        # One common harmonic calculation for all 12 samples in 07:00-08:00.
        self.assertAlmostEqual(output.loc[0, "spd_mph_07:00_08:00"], 60 / (30/40 + 30/20))

    def test_settings_supply_actual_range_and_truncate_export_at_kernel_end(self):
        pd.DataFrame({"demand_period_starting_hours": [6.5], "demand_period_ending_hours": [7.5]}).to_csv(self.root/"am"/"settings.csv", index=False)
        pd.DataFrame({"demand_period_starting_hours": [7.5], "demand_period_ending_hours": [8]}).to_csv(self.root/"md"/"settings.csv", index=False)
        output, audit = self.export(ranges=None)
        self.assertEqual(output.filter(like="spd_mph_").columns.tolist(), ["spd_mph_06:30_07:30", "spd_mph_07:30_08:00"])
        self.assertTrue((audit.period_range_source == "settings.csv").all())
        self.assertAlmostEqual(output.loc[0, "approx_volume_07:30_08:00"], 240)

    def test_24_hour_endpoint_and_cross_midnight_order(self):
        data = self.combined[self.combined.time_period == "am"].copy()
        data = data.drop(columns=[c for c in data if c.startswith("spd_")])
        for minute in range(23*60, 24*60, 5):
            data[f"spd_mph_{minute//60:02d}:{minute%60:02d}"] = 30
        output, _ = self.export(data, ["am"], ["2300_2400"])
        self.assertIn("spd_mph_23:00_24:00", output)
        for minute in range(0, 60, 5):
            data[f"spd_mph_00:{minute:02d}"] = 40
        output, _ = self.export(data, ["am"], ["2300_0100"])
        self.assertEqual(output.filter(like="spd_mph_").columns.tolist(), ["spd_mph_23:00_24:00", "spd_mph_24:00_25:00"])
        self.assertAlmostEqual(output.loc[0].filter(like="approx_volume_").sum(), 120)

    def test_zero_speed_harmonic_and_invalid_speed_are_explicit(self):
        data = self.combined.copy()
        data.loc[data.time_period == "am", "spd_mph_06:30"] = 0
        data.loc[data.time_period == "md", "spd_mph_07:30"] = np.nan
        output, _ = self.export(data)
        self.assertEqual(output.loc[0, "spd_mph_06:30_07:30"], 0)
        self.assertTrue(pd.isna(output.loc[0, "spd_mph_07:30_08:30"]))

    def test_zero_flow_fallback_and_zero_assignment(self):
        data = self.combined.copy()
        data.loc[data.time_period == "am", [c for c in data if c.startswith("spd_")]] = 0
        output, audit = self.export(data)
        self.assertEqual(output.loc[0, "approx_volume_06:30_07:30"], 120)
        self.assertEqual(output.loc[1, "approx_volume_06:30_07:30"], 0)
        self.assertEqual((audit.status == "uniform_duration_fallback").sum(), 1)

    def test_period_lanes_and_capacity_still_used(self):
        links = self.links.copy(); links["lanes"] = [1, 2]
        links.to_csv(self.root/"md"/"link.csv", index=False)
        output, audit = self.export()
        self.assertEqual(output.loc[0, "capacity"], 5400)
        self.assertEqual(audit[(audit.time_period == "md") & (audit.from_node_id == "10")].iloc[0].capacity, 1800)

    def test_missing_range_overlap_and_duplicate_pairs_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "Missing period range"):
            self.export(ranges=None)
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.export(ranges=["0630_0730", "0700_0845"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.export(pd.concat([self.combined, self.combined.iloc[[0]]]))

    def test_runner_creates_exactly_two_scope_exports(self):
        preprocess_and_summarize_scenario(scenario_dir=self.root, time_periods=self.periods,
            period_range_list=self.ranges, time_duration_dict={"am": 1, "md": 1.25})
        jurisdiction = pd.read_csv(self.root/"link_speed_volume.csv")
        regional = pd.read_csv(self.root/"link_speed_volume_regional.csv")
        self.assertEqual(len(jurisdiction), 1)
        self.assertEqual(len(regional), 2)
        self.assertEqual(len(list(self.root.glob("link_speed_volume*.csv"))), 2)

    def test_scenario_runner_uses_settings_when_ranges_are_omitted(self):
        for period, start, end in [("am", 6.5, 7.5), ("md", 7.5, 8.75)]:
            pd.DataFrame({"demand_period_starting_hours": [start], "demand_period_ending_hours": [end]}).to_csv(self.root/period/"settings.csv", index=False)
        preprocess_and_summarize_scenario(scenario_dir=self.root, time_periods=self.periods,
            period_range_list=None, time_duration_dict={})
        output = pd.read_csv(self.root/"link_speed_volume_regional.csv")
        self.assertEqual(output.filter(like="spd_mph_").columns[-1], "spd_mph_08:30_08:45")
        self.assertAlmostEqual(output.loc[0].filter(like="approx_volume_").sum(), 360)


if __name__ == "__main__":
    unittest.main()
