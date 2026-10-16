from __future__ import annotations

import gc
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from nvta_taplite_workflow.dtalite4cube.automatic_future_transfer import (
    ACTIVE_TRANSFER_NAME,
    TRANSFER_ROOT_NAME,
    ensure_automatic_future_transfer,
)
from nvta_taplite_workflow.dtalite4cube.runner import (
    AssignmentConfig,
    prepare_automatic_future_transfer,
)


class AutomaticFutureTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / ".test-artifacts" / str(uuid4())
        self.root.mkdir(parents=True)
        self.network_dir = self.root / "future-network"
        self.network_dir.mkdir()
        self.network_path = self.network_dir / "DTALiteNetworkInput_TRUESHP.gpkg"
        self.base_path = self.root / "base.gpkg"
        self.base_path.write_bytes(b"base-reference")
        self.calibration_path = self.root / "base.npy"
        self.calibration_path.write_bytes(b"calibration-reference")
        self.corridor_dir = self.root / "corridor-reference"
        self.corridor_dir.mkdir()
        (self.corridor_dir / "corridor_qvdf_dictionary.csv").write_text(
            "corridor,period\n", encoding="utf-8"
        )
        (self.corridor_dir / "corridor_node_pair_lookup.csv").write_text(
            "from_node_id,to_node_id\n", encoding="utf-8"
        )
        self._write_network(lanes=2)

    def tearDown(self) -> None:
        gc.collect()
        for attempt in range(6):
            try:
                shutil.rmtree(self.root)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def _write_network(self, *, lanes: int) -> None:
        frame = gpd.GeoDataFrame(
            [
                {
                    "A": 1,
                    "B": 2,
                    "ATYPE": 1,
                    "FTYPE": 2,
                    "STREETNAME": "MAIN ST",
                    "LANES": lanes,
                    "geometry": LineString([(0, 0), (1000, 0)]),
                }
            ],
            geometry="geometry",
            crs="EPSG:2248",
        )
        frame.to_file(self.network_path, layer="network", driver="GPKG")

    @staticmethod
    def _fake_transfer(**kwargs):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        existed = (output_dir / "future_qvdf_node_pair_overrides.npy").is_file()
        np.save(output_dir / "future_qvdf_node_pair_overrides.npy", {}, allow_pickle=True)
        np.save(output_dir / "future_speed_t_overrides.npy", {}, allow_pickle=True)
        return {
            "expanded_dictionary_action": "reused" if existed else "created",
            "expanded_boundary_dictionary_action": "reused" if existed else "created",
        }

    def _ensure(self, network_path: Path | None = None) -> dict[str, object]:
        with (
            patch(
                "nvta_taplite_workflow.dtalite4cube.automatic_future_transfer._reference_signature",
                return_value={"reference": "test"},
            ),
            patch(
                "nvta_taplite_workflow.dtalite4cube.automatic_future_transfer.run_transfer",
                side_effect=self._fake_transfer,
            ),
        ):
            return ensure_automatic_future_transfer(
                network_path or self.network_path,
                self.calibration_path,
                base_network_path=self.base_path,
                corridor_reference_dir=self.corridor_dir,
                write_spatial=False,
            )

    def test_same_network_reuses_set_but_any_source_edit_creates_new_set(self) -> None:
        first = self._ensure()
        second = self._ensure()
        self.assertEqual(first["transfer_set"], second["transfer_set"])
        self.assertEqual(second["qvdf_cache_action"], "reused")

        self._write_network(lanes=3)
        changed = self._ensure()
        self.assertNotEqual(first["transfer_set"], changed["transfer_set"])

        transfer_root = self.network_dir / TRANSFER_ROOT_NAME
        self.assertTrue((transfer_root / ACTIVE_TRANSFER_NAME).is_file())
        self.assertEqual(len(list((transfer_root / "sets").iterdir())), 2)

    def test_same_network_in_another_folder_creates_folder_local_set(self) -> None:
        first = self._ensure()

        second_network_dir = self.root / "moved-future-network"
        second_network_dir.mkdir()
        second_network_path = second_network_dir / self.network_path.name
        shutil.copy2(self.network_path, second_network_path)

        moved = self._ensure(second_network_path)

        self.assertEqual(
            first["source_network_fingerprint"],
            moved["source_network_fingerprint"],
        )
        self.assertNotEqual(
            Path(str(first["transfer_set"])).name,
            Path(str(moved["transfer_set"])).name,
        )
        self.assertEqual(moved["qvdf_cache_action"], "created")
        self.assertEqual(
            Path(str(moved["source_network_folder"])),
            second_network_dir.resolve(),
        )
        self.assertTrue(
            Path(str(moved["qvdf_dictionary"])).is_relative_to(
                second_network_dir / TRANSFER_ROOT_NAME
            )
        )
        self.assertTrue(
            (second_network_dir / TRANSFER_ROOT_NAME / ACTIVE_TRANSFER_NAME).is_file()
        )

    def test_pipeline_binding_replaces_manual_dictionary_paths(self) -> None:
        generated_qvdf = self.root / "generated-qvdf.npy"
        generated_speed_t = self.root / "generated-speed-t.npy"
        generated_qvdf.write_bytes(b"qvdf")
        generated_speed_t.write_bytes(b"speed")
        config = AssignmentConfig(
            network_path=self.network_dir,
            network_conversion=True,
            qvdf_override_dictionary=self.root / "old-qvdf.npy",
            speed_t_override_dictionary=self.root / "old-speed.npy",
        )
        result = {
            "qvdf_dictionary": str(generated_qvdf),
            "speed_t_dictionary": str(generated_speed_t),
        }

        with patch(
            "nvta_taplite_workflow.dtalite4cube.automatic_future_transfer.ensure_automatic_future_transfer",
            return_value=result,
        ):
            prepared = prepare_automatic_future_transfer(config)

        self.assertIs(prepared, result)
        self.assertEqual(config.qvdf_override_dictionary, generated_qvdf.resolve())
        self.assertEqual(config.speed_t_override_dictionary, generated_speed_t.resolve())


if __name__ == "__main__":
    unittest.main()
