from __future__ import annotations

import csv
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from nvta_taplite_workflow.dtalite4cube.runner import (
    AssignmentConfig,
    NETWORK_MAPPING_SCHEMA_FIELDS,
    NETWORK_QVDF_VALUE_FIELDS,
    run_network_conversion,
    validate_network_mapping_outputs,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]


class NetworkConversionMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = WORKFLOW_ROOT / ".test-artifacts" / str(uuid.uuid4())
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def write_mapped_link(self, *, omit: str | None = None) -> Path:
        output = self.root / "am" / "link.csv"
        output.parent.mkdir(parents=True)
        fields = [
            "link_id",
            *NETWORK_MAPPING_SCHEMA_FIELDS,
            "district_id",
        ]
        if omit is not None:
            fields.remove(omit)
        row = {field: "" for field in fields}
        row["link_id"] = "101"
        for field in NETWORK_QVDF_VALUE_FIELDS:
            if field in row:
                row[field] = "1.0"
        row["qvdf_profile_mode"] = "2"
        row["t0_hour"] = "6.25"
        row["qvdf_start_speed_mph"] = "45.0"
        row["district_id"] = "3"
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        return output

    def test_public_network_conversion_runs_and_verifies_mapping(self) -> None:
        output = self.write_mapped_link()
        fake_result = {
            "elapsed_sec": 0.01,
            "worker_plan": {"workers": 1},
            "outputs": [{"period": "am", "output": str(output), "links": 1}],
        }
        config = AssignmentConfig(
            network_path=self.root / "source",
            output_dir=self.root,
            network_conversion=True,
            vdf_type="qvdf",
            time_periods=["am"],
            period_times=["0600_0900"],
        )

        with patch(
            "nvta_taplite_workflow.dtalite4cube.cube2gmns.get_gmns_from_cube",
            return_value=fake_result,
        ) as converter:
            result = run_network_conversion(config)

        converter.assert_called_once()
        kwargs = converter.call_args.kwargs
        self.assertEqual(kwargs["vdf_type"], "qvdf")
        self.assertTrue(kwargs["district_id_assignment"])
        self.assertTrue(kwargs["period_folder_output"])
        self.assertTrue(result["mapping_validation"]["validated"])
        self.assertEqual(result["mapping_validation"]["qvdf_mapped_links"], 1)
        summary = result["mapping_validation"]["periods"][0]
        self.assertEqual(summary["congestion_boundary_links"], 1)
        self.assertEqual(summary["observed_speed_anchor_links"], 1)
        self.assertTrue(summary["district_mapping_present"])

    def test_mapping_validation_rejects_missing_qvdf_column(self) -> None:
        output = self.write_mapped_link(omit="vdf_qdf")
        with self.assertRaisesRegex(RuntimeError, "missing: vdf_qdf"):
            validate_network_mapping_outputs(
                [{"period": "am", "output": str(output), "links": 1}]
            )


if __name__ == "__main__":
    unittest.main()
