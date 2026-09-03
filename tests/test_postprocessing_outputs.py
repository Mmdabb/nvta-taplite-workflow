from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from nvta_taplite_workflow.dtalite4cube.cube2gmns.funclib import district_id_map
from nvta_taplite_workflow.postprocessing.runner import (
    preprocess_and_summarize_scenario,
)


class PostprocessingOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nvta_postprocessing_"))
        self.period_dir = self.root / "am"
        self.period_dir.mkdir(parents=True)

        pd.DataFrame(
            {
                "link_id": [1, 2, 3],
                "from_node_id": [10, 20, 30],
                "to_node_id": [11, 21, 31],
                "link_type": [101, 101, 100],
                "free_speed": [60.0, 60.0, 30.0],
                "length": [1.0, 2.0, 0.5],
                "length_in_mile": [1.0, 2.0, 0.5],
                "TAZ": [1500, 3000, 1500],
                "district_id": [3, 10, 3],
                "FT": [1, 1, 0],
                "TOLLGRP": [0, 0, 0],
                "AMLIMIT": [0, 0, 0],
                "geometry": [
                    "LINESTRING (0 0, 0.5 1, 1 0)",
                    "LINESTRING (1 0, 1.5 1, 2 0)",
                    "LINESTRING (2 0, 3 0)",
                ],
                "STREETNAME": ["Main Street", "Regional Avenue", "Connector"],
            }
        ).to_csv(self.period_dir / "link.csv", index=False)

        pd.DataFrame(
            {
                "link_id": [1, 2, 3],
                "speed_mph": [30.0, 40.0, 20.0],
                "volume": [100.0, 200.0, 50.0],
            }
        ).to_csv(self.period_dir / "link_performance.csv", index=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_writes_jurisdiction_and_regional_outputs_with_street_names(self) -> None:
        jurisdiction = preprocess_and_summarize_scenario(
            scenario_dir=self.root,
            time_periods=["am"],
            period_range_list=["0600_0900"],
            time_duration_dict={"am": 3.0},
        )
        regional = pd.read_csv(
            self.root / "link_performance_combined_processed_regional.csv"
        )

        self.assertEqual(jurisdiction["link_id"].astype(int).tolist(), [1])
        self.assertEqual(regional["link_id"].astype(int).tolist(), [1, 2])
        self.assertEqual(jurisdiction.loc[0, "STREETNAME"], "Main Street")
        self.assertEqual(regional["STREETNAME"].tolist(), ["Main Street", "Regional Avenue"])
        self.assertEqual(
            regional.loc[regional["link_id"].astype(int) == 1, "geometry"].iloc[0],
            "LINESTRING (0 0, 0.5 1, 1 0)",
        )

        jurisdiction_stats = pd.read_csv(self.root / "statistics_data.csv")
        regional_stats = pd.read_csv(self.root / "statistics_data_regional.csv")
        self.assertFalse(jurisdiction_stats["time_period"].isna().any())
        self.assertFalse(regional_stats["time_period"].isna().any())
        self.assertNotIn("sum", jurisdiction_stats["person_mile"].astype(str).tolist())
        self.assertNotIn("sum", regional_stats["person_mile"].astype(str).tolist())
        jurisdiction_overall = jurisdiction_stats[
            jurisdiction_stats["time_period"] == "overall"
        ].iloc[0]
        regional_overall = regional_stats[
            regional_stats["time_period"] == "overall"
        ].iloc[0]
        self.assertGreater(regional_overall["person_mile"], jurisdiction_overall["person_mile"])

    def test_conversion_uses_packaged_jurisdiction_lookup_when_source_omits_it(self) -> None:
        conversion_dir = self.root / "conversion"
        source_dir = self.root / "source"
        conversion_dir.mkdir()
        source_dir.mkdir()
        pd.DataFrame(
            {
                "link_id": [1],
                "from_node_id": [1405],
                "to_node_id": [10001],
                "TAZ": [1405],
            }
        ).to_csv(conversion_dir / "link.csv", index=False)
        pd.DataFrame(
            {
                "node_id": [1405, 10001],
                "zone_id": [1405, ""],
                "x_coord": [0, 1],
                "y_coord": [0, 1],
            }
        ).to_csv(conversion_dir / "node.csv", index=False)

        district_id_map(
            conversion_dir,
            "am",
            link_filename="link.csv",
            jurisdiction_dir=source_dir,
        )

        converted = pd.read_csv(conversion_dir / "link.csv")
        self.assertEqual(converted.loc[0, "JUR_NAME"], "Arlington")
        self.assertEqual(int(converted.loc[0, "district_id"]), 2)


if __name__ == "__main__":
    unittest.main()
