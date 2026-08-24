from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from nvta_taplite_workflow.dtalite4cube.qvdf_smoother import SmootherConfig, smooth_scenario
from nvta_taplite_workflow.dtalite4cube.qvdf_smoother.batch import (
    MAX_WORKERS,
    _stage_parent,
    choose_worker_count,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]


def _speed_columns(start_hour: int, end_hour: int) -> list[str]:
    return [
        f"spd_mph_{minute // 60:02d}:{minute % 60:02d}"
        for minute in range(start_hour * 60, end_hour * 60, 5)
    ]


class QvdfSmootherIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = WORKFLOW_ROOT / ".test-artifacts" / str(uuid4())
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def _write_period(self, name: str, start_hour: int, end_hour: int) -> None:
        period = self.root / name
        period.mkdir()
        with (period / "settings.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "demand_period_starting_hours",
                    "demand_period_ending_hours",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "demand_period_starting_hours": start_hour,
                    "demand_period_ending_hours": end_hour,
                }
            )

        link = {
            "link_id": "101",
            "lanes": "2",
            "capacity": "1800",
            "vdf_plf": "1",
            "vdf_free_speed_mph": "60",
            "vdf_length_mi": "1",
            "cutoff_speed": "45",
            "vdf_alpha": "0.15",
            "vdf_beta": "4",
            "vdf_cp": "0.28125",
            "vdf_cd": "1",
            "vdf_n": "1",
            "vdf_s": "4",
            "vdf_type": "0",
            "qvdf_profile_mode": "0",
            "t0_hour": "",
            "t2_hour": "",
            "t3_hour": "",
            "qvdf_start_speed_mph": "60" if name == "am" else "20",
            "qvdf_end_speed_mph": "20" if name == "am" else "60",
        }
        with (period / "link.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(link))
            writer.writeheader()
            writer.writerow(link)

        speed_columns = _speed_columns(start_hour, end_hour)
        performance = {
            "link_id": "101",
            "volume": "1000",
            "speed_mph": "45",
            "note": "preserve-me",
            **{column: "99" for column in speed_columns},
        }
        with (period / "link_performance.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(performance))
            writer.writeheader()
            writer.writerow(performance)

    def test_smoother_writes_period_profiles_before_aggregation(self):
        self._write_period("am", 6, 7)
        self._write_period("md", 7, 8)

        report = smooth_scenario(
            SmootherConfig(
                scenario_dir=self.root,
                periods=["am", "md"],
                workers="2",
                backup=False,
            )
        )

        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["staging"]["unique_links"], 1)
        self.assertEqual(report["compute"]["workers"]["selected"], 2)
        self.assertIn(report["compute"]["workers"]["used"], {1, 2})
        self.assertTrue((self.root / "qvdf_batch_report.json").is_file())
        saved_report = json.loads(
            (self.root / "qvdf_batch_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved_report["status"], "complete")

        for period_name in ("am", "md"):
            with (self.root / period_name / "link_performance.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["volume"], "1000")
            self.assertEqual(row["speed_mph"], "45")
            self.assertEqual(row["note"], "preserve-me")
            smoothed = [
                float(value)
                for name, value in row.items()
                if name.startswith("spd_mph_")
            ]
            self.assertNotEqual(smoothed, [99.0] * len(smoothed))
            self.assertLessEqual(
                max(
                    abs(smoothed[index + 1] - smoothed[index])
                    for index in range(len(smoothed) - 1)
                ),
                8.0 + 1e-8,
            )

    def test_worker_count_is_bounded_by_workflow_ceiling(self):
        workers, _ = choose_worker_count("999")
        self.assertLessEqual(workers, MAX_WORKERS)

    def test_unc_output_uses_local_temporary_storage(self):
        previous = os.environ.pop("NVTA_QVDF_TEMP_DIR", None)
        try:
            parent, policy = _stage_parent(
                Path(r"\\server\share\NVTA\assignment")
            )
        finally:
            if previous is not None:
                os.environ["NVTA_QVDF_TEMP_DIR"] = previous

        self.assertEqual(parent, Path(tempfile.gettempdir()).resolve())
        self.assertEqual(policy, "local system temp for UNC output")

    def test_explicit_smoother_temp_directory_must_be_absolute(self):
        previous = os.environ.get("NVTA_QVDF_TEMP_DIR")
        os.environ["NVTA_QVDF_TEMP_DIR"] = "relative-temp"
        try:
            with self.assertRaisesRegex(Exception, "must be an absolute path"):
                _stage_parent(self.root)
        finally:
            if previous is None:
                os.environ.pop("NVTA_QVDF_TEMP_DIR", None)
            else:
                os.environ["NVTA_QVDF_TEMP_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
