from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from nvta_taplite_workflow.dtalite4cube.qvdf_overrides import PARAMETER_COLUMNS, apply_overrides


class QvdfOverrideTests(unittest.TestCase):
    def _write_inputs(
        self,
        root: Path,
        *,
        include_second_pair: bool = True,
    ) -> tuple[Path, Path]:
        assignment_root = root / "assignment"
        period_root = assignment_root / "am"
        period_root.mkdir(parents=True)

        rows = [
            {
                "link_id": 1,
                "from_node_id": 10,
                "to_node_id": 20,
                "vdf_type": 2,
                "qvdf_profile_mode": 2,
                **{column: -1.0 for column in PARAMETER_COLUMNS.values()},
            },
            {
                "link_id": 2,
                "from_node_id": 20,
                "to_node_id": 30,
                "vdf_type": 2,
                "qvdf_profile_mode": 2,
                **{column: -2.0 for column in PARAMETER_COLUMNS.values()},
            },
        ]
        pd.DataFrame(rows).to_csv(period_root / "link.csv", index=False)

        dictionary: dict[tuple[int, int], dict[str, float]] = {}
        pairs = ((10, 20), (20, 30)) if include_second_pair else ((10, 20),)
        for pair_index, pair in enumerate(pairs, start=1):
            values: dict[str, float] = {}
            for period_sequence in (1, 2, 3):
                for parameter_index, parameter in enumerate(PARAMETER_COLUMNS, start=1):
                    values[f"QVDF_{parameter}{period_sequence}"] = float(
                        period_sequence * 100 + pair_index * 10 + parameter_index
                    )
            dictionary[pair] = values

        dictionary_path = root / "qvdf_node_pair_overrides.npy"
        np.save(dictionary_path, dictionary, allow_pickle=True)
        return assignment_root, dictionary_path

    def test_override_replaces_all_eight_fields_and_skips_nt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_root, dictionary_path = self._write_inputs(root)
            original = pd.read_csv(assignment_root / "am" / "link.csv")
            backup_root = assignment_root / "qvdf_override_audit" / "pre-overlay-network"

            manifest = apply_overrides(
                assignment_root,
                dictionary_path,
                backup_root,
                periods=["am", "nt"],
            )

            actual = pd.read_csv(assignment_root / "am" / "link.csv")
            for parameter_index, target in enumerate(PARAMETER_COLUMNS.values(), start=1):
                self.assertEqual(
                    actual[target].tolist(),
                    [110 + parameter_index, 120 + parameter_index],
                )
            self.assertEqual(actual["vdf_type"].tolist(), [2, 2])
            self.assertEqual(actual["qvdf_profile_mode"].tolist(), [2, 2])

            backup = pd.read_csv(backup_root / "am" / "link.csv")
            pd.testing.assert_frame_equal(backup, original)
            self.assertEqual(manifest["status"], "PASS")
            self.assertEqual(manifest["selected_periods"], ["AM"])
            self.assertEqual(manifest["skipped_periods"], ["NT"])
            self.assertEqual(manifest["periods"][0]["changed_parameter_cells"], 16)

            manifest_path = (
                assignment_root
                / "qvdf_override_audit"
                / "qvdf_override_manifest.json"
            )
            saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved_manifest["skipped_period_policy"],
                "retain normal network-converter QVDF mapping",
            )

    def test_override_rejects_incomplete_directed_pair_coverage(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_root, dictionary_path = self._write_inputs(
                root,
                include_second_pair=False,
            )

            with self.assertRaisesRegex(ValueError, "1 node pairs missing"):
                apply_overrides(
                    assignment_root,
                    dictionary_path,
                    assignment_root / "qvdf_override_audit" / "pre-overlay-network",
                    periods=["am"],
                )
