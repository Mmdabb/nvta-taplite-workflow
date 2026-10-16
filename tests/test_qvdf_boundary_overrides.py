from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from nvta_taplite_workflow.dtalite4cube.qvdf_boundary_overrides import (
    PERIODS,
    TARGET_FIELDS,
    apply_boundary_overrides,
    validate_boundary_overrides,
)


def _values(seed: float) -> dict[str, float]:
    return {
        f"{field}_{period}": seed + period_index * 10 + field_index
        for period_index, period in enumerate(PERIODS)
        for field_index, field in enumerate(TARGET_FIELDS)
    }


class QvdfBoundaryOverrideTests(unittest.TestCase):
    def _write_inputs(self, root: Path, *, complete: bool = True) -> tuple[Path, Path]:
        assignment_root = root / "assignment"
        rows = [
            {
                "from_node_id": 10,
                "to_node_id": 20,
                "allowed_use": "sov;hov2",
                **{field: -1.0 for field in TARGET_FIELDS},
            },
            {
                "from_node_id": 20,
                "to_node_id": 30,
                "allowed_use": "closed",
                **{field: -2.0 for field in TARGET_FIELDS},
            },
            {
                "from_node_id": 30,
                "to_node_id": 40,
                "allowed_use": "sov;hov2",
                **{field: -3.0 for field in TARGET_FIELDS},
            },
        ]
        for period in ("am", "nt"):
            period_root = assignment_root / period
            period_root.mkdir(parents=True)
            pd.DataFrame(rows).to_csv(period_root / "link.csv", index=False)

        dictionary = {
            (10, 20): _values(100.0),
            (20, 30): _values(200.0),
        }
        if complete:
            dictionary[(30, 40)] = {
                f"{field}_{period}": float("nan")
                for period in PERIODS
                for field in TARGET_FIELDS
            }
        dictionary_path = root / "future_speed_t_overrides.npy"
        np.save(dictionary_path, dictionary, allow_pickle=True)
        return assignment_root, dictionary_path

    def test_applies_lineage_blanks_fallback_and_respects_closed_t_fields(self):
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
            root = Path(temporary_directory)
            assignment_root, dictionary_path = self._write_inputs(root)
            nt_before = pd.read_csv(assignment_root / "nt" / "link.csv")
            manifest = apply_boundary_overrides(
                assignment_root,
                dictionary_path,
                assignment_root / "speed_t_override_audit" / "pre-overlay-network",
                periods=("am", "nt"),
            )

            actual = pd.read_csv(assignment_root / "am" / "link.csv")
            self.assertEqual(actual.loc[0, "qvdf_start_speed_mph"], 100.0)
            self.assertEqual(actual.loc[0, "qvdf_end_speed_mph"], 101.0)
            self.assertEqual(actual.loc[0, "t0_hour"], 102.0)
            self.assertEqual(actual.loc[0, "t2_hour"], 103.0)
            self.assertEqual(actual.loc[0, "t3_hour"], 104.0)
            self.assertEqual(actual.loc[1, "qvdf_start_speed_mph"], 200.0)
            self.assertEqual(actual.loc[1, "qvdf_end_speed_mph"], 201.0)
            self.assertTrue(actual.loc[1, ["t0_hour", "t2_hour", "t3_hour"]].isna().all())
            self.assertTrue(actual.loc[2, list(TARGET_FIELDS)].isna().all())
            pd.testing.assert_frame_equal(
                pd.read_csv(assignment_root / "nt" / "link.csv"), nt_before
            )
            self.assertEqual(manifest["status"], "PASS")
            self.assertEqual(manifest["selected_periods"], ["AM"])
            self.assertEqual(manifest["skipped_periods"], ["NT"])
            self.assertEqual(
                validate_boundary_overrides(
                    assignment_root,
                    dictionary_path,
                    periods=("am", "nt"),
                )["status"],
                "PASS",
            )

    def test_rejects_incomplete_future_pair_coverage(self):
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
            root = Path(temporary_directory)
            assignment_root, dictionary_path = self._write_inputs(
                root, complete=False
            )
            with self.assertRaisesRegex(ValueError, "1 node pairs missing"):
                apply_boundary_overrides(
                    assignment_root,
                    dictionary_path,
                    assignment_root / "speed_t_override_audit" / "pre-overlay-network",
                    periods=("am",),
                )


if __name__ == "__main__":
    unittest.main()
