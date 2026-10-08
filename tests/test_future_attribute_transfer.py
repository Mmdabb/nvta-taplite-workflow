from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from nvta_taplite_workflow.dtalite4cube.future_attribute_transfer import (
    QVDF_FIELDS,
    build_transfer_crosswalk,
    run_transfer,
)


CRS = "EPSG:2248"


def _network(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def _values(seed: float) -> dict[str, float]:
    return {field: seed + index / 100 for index, field in enumerate(QVDF_FIELDS)}


class FutureAttributeTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _network(
            [
                {
                    "A": 1,
                    "B": 2,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(0, 0), (1000, 0)]),
                },
                {
                    "A": 10,
                    "B": 20,
                    "ATYPE": 2,
                    "FTYPE": 1,
                    "STREETNAME": "Broadway",
                    "geometry": LineString([(0, 1000), (1000, 1000)]),
                },
            ]
        )
        self.dictionary = {(1, 2): _values(1.0), (10, 20): _values(2.0)}

    def test_simple_uses_exact_pair_and_falls_back_for_new_links(self) -> None:
        future = _network(
            [
                {
                    "A": 1,
                    "B": 2,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(0, 0), (1000, 0)]),
                },
                {
                    "A": 2,
                    "B": 99,
                    "ATYPE": 3,
                    "FTYPE": 5,
                    "STREETNAME": "New Ramp",
                    "geometry": LineString([(1000, 0), (1200, 200)]),
                },
            ]
        )
        result = build_transfer_crosswalk(
            self.base, future, self.dictionary, method="simple"
        )
        self.assertEqual(result["match_method"].tolist(), ["EXACT_AB", "LINK_TYPE_FALLBACK"])
        self.assertEqual(result["confidence_class"].tolist(), ["B", "E"])

    def test_framework_maps_split_path_but_not_attached_ramp(self) -> None:
        future = _network(
            [
                {
                    "A": 1,
                    "B": 3,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(0, 0), (500, 0)]),
                },
                {
                    "A": 3,
                    "B": 2,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(500, 0), (1000, 0)]),
                },
                {
                    "A": 3,
                    "B": 99,
                    "ATYPE": 3,
                    "FTYPE": 5,
                    "STREETNAME": "New Ramp",
                    "geometry": LineString([(500, 0), (650, 250)]),
                },
                {
                    "A": 10,
                    "B": 20,
                    "ATYPE": 2,
                    "FTYPE": 1,
                    "STREETNAME": "Broadway",
                    "geometry": LineString([(0, 1000), (1000, 1000)]),
                },
            ]
        )
        result = build_transfer_crosswalk(
            self.base, future, self.dictionary, method="framework"
        )
        self.assertEqual(
            result["match_method"].tolist(),
            ["REPLACEMENT_PATH", "REPLACEMENT_PATH", "LINK_TYPE_FALLBACK", "EXACT_AB"],
        )
        self.assertEqual(result.loc[0, ["base_a", "base_b"]].tolist(), [1, 2])
        self.assertEqual(result.loc[1, ["base_a", "base_b"]].tolist(), [1, 2])

    def test_framework_recovers_changed_node_ids_by_directed_geometry(self) -> None:
        future = _network(
            [
                {
                    "A": 101,
                    "B": 102,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(2, 5), (1002, 5)]),
                },
                {
                    "A": 10,
                    "B": 20,
                    "ATYPE": 2,
                    "FTYPE": 1,
                    "STREETNAME": "Broadway",
                    "geometry": LineString([(0, 1000), (1000, 1000)]),
                },
            ]
        )
        result = build_transfer_crosswalk(
            self.base, future, self.dictionary, method="framework"
        )
        self.assertEqual(result.loc[0, "match_method"], "GEOMETRY")
        self.assertEqual(result.loc[0, ["base_a", "base_b"]].tolist(), [1, 2])

    def test_run_writes_complete_dictionary_and_missing_pair_log(self) -> None:
        future = _network(
            [
                {
                    "A": 1,
                    "B": 2,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "DISTANCE": 0.2,
                    "LINKID": 1,
                    "geometry": LineString([(0, 0), (1000, 0)]),
                },
                {
                    "A": 2,
                    "B": 99,
                    "ATYPE": 3,
                    "FTYPE": 5,
                    "STREETNAME": "New Ramp",
                    "DISTANCE": 0.1,
                    "LINKID": 2,
                    "geometry": LineString([(1000, 0), (1200, 200)]),
                },
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base_path = root / "base.gpkg"
            future_path = root / "future.gpkg"
            dictionary_path = root / "base.npy"
            self.base.to_file(base_path, driver="GPKG")
            future.to_file(future_path, driver="GPKG")
            np.save(dictionary_path, self.dictionary, allow_pickle=True)
            summary = run_transfer(
                base_network_path=base_path,
                future_network_path=future_path,
                calibration_dictionary_path=dictionary_path,
                output_dir=root / "result",
                method="simple",
                write_spatial=False,
            )
            transferred = np.load(
                root / "result" / "future_qvdf_node_pair_overrides.npy",
                allow_pickle=True,
            ).item()
            self.assertEqual(set(transferred), {(1, 2), (2, 99)})
            self.assertEqual(summary["missing_2025_pairs"], 1)
            self.assertEqual(summary["match_method_counts"]["LINK_TYPE_FALLBACK"], 1)
            self.assertTrue((root / "result" / "missing_2025_node_pairs.csv").is_file())


if __name__ == "__main__":
    unittest.main()
