from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from nvta_taplite_workflow.dtalite4cube.future_attribute_transfer import (
    FrameworkConfig,
    QVDF_FIELDS,
    _fallback_values,
    build_transfer_crosswalk,
    run_transfer,
)
from nvta_taplite_workflow.dtalite4cube.cube2gmns.vdf_lookup_tables import (
    get_vdf_dict,
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
        self.assertEqual(result["confidence_class"].tolist(), ["B", "F"])

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

    def test_framework_does_not_emit_geometry_review_candidates(self) -> None:
        future = _network(
            [
                {
                    "A": 101,
                    "B": 102,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(2, 5), (1002, 5)]),
                }
            ]
        )
        result = build_transfer_crosswalk(
            self.base,
            future,
            self.dictionary,
            method="framework",
            config=FrameworkConfig(
                geometry_auto_min_score=1.01,
                geometry_auto_min_margin=1.01,
            ),
        )
        self.assertEqual(result.loc[0, "match_method"], "LINK_TYPE_FALLBACK")
        self.assertNotIn("GEOMETRY_CANDIDATE", set(result["match_method"]))

    def test_corridor_name_alone_cannot_override_direction_or_link_type(self) -> None:
        future = _network(
            [
                {
                    "A": 101,
                    "B": 102,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(1002, 5), (2, 5)]),
                },
                {
                    "A": 103,
                    "B": 104,
                    "ATYPE": 9,
                    "FTYPE": 9,
                    "STREETNAME": "Main St",
                    "geometry": LineString([(2, 5), (1002, 5)]),
                },
            ]
        )
        result = build_transfer_crosswalk(
            self.base,
            future,
            self.dictionary,
            method="framework",
            config=FrameworkConfig(geometry_auto_min_score=1.01),
            corridor_pair_lookup={(1, 2): "MAIN_EB"},
            corridor_reference_lookup={
                (1, 2): {"direction": "EASTBOUND", "tmc_code": "TEST001"}
            },
        )
        self.assertEqual(
            result["match_method"].tolist(),
            ["LINK_TYPE_FALLBACK", "LINK_TYPE_FALLBACK"],
        )
        self.assertIn("direction_misaligned", result.loc[0, "corridor_rejection_reason"])
        self.assertIn(
            "link_type_incompatible", result.loc[1, "corridor_rejection_reason"]
        )

    def test_corridor_fallback_and_run_local_expanded_dictionary(self) -> None:
        future = _network(
            [
                {
                    "A": 101,
                    "B": 102,
                    "ATYPE": 1,
                    "FTYPE": 1,
                    "STREETNAME": "Main St",
                    "DISTANCE": 0.2,
                    "LINKID": 1,
                    "geometry": LineString([(2, 5), (1002, 5)]),
                }
            ]
        )
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
            root = Path(temporary_directory)
            base_path = root / "base.gpkg"
            future_path = root / "future.gpkg"
            dictionary_path = root / "base.npy"
            result_dir = root / "result"
            corridor_dir = root / "corridors" / "MAIN_EB"
            calibration_dir = corridor_dir / "06-qvdf-calibration"
            reference_dir = corridor_dir / "01-input-and-qc"
            calibration_dir.mkdir(parents=True)
            reference_dir.mkdir(parents=True)
            self.base.to_file(base_path, driver="GPKG")
            future.to_file(future_path, driver="GPKG")
            np.save(dictionary_path, self.dictionary, allow_pickle=True)
            source_bytes = dictionary_path.read_bytes()
            pd.DataFrame(
                [
                    {
                        "corridor": "MAIN_EB",
                        "sensor_uid": "all",
                        "calibration_period": "AM",
                        "status": "ok",
                        "n_episodes": 12,
                        "f_d": 6.1,
                        "n": 1.1,
                        "f_p": 0.7,
                        "s": 1.2,
                        "alpha": 2.3,
                        "beta": 1.32,
                    },
                    {
                        "corridor": "MAIN_EB",
                        "sensor_uid": "all",
                        "calibration_period": "PM",
                        "status": "insufficient_sample",
                        "n_episodes": 1,
                        "f_d": np.nan,
                        "n": np.nan,
                        "f_p": np.nan,
                        "s": np.nan,
                        "alpha": np.nan,
                        "beta": np.nan,
                    },
                    {
                        "corridor": "MAIN_EB",
                        "sensor_uid": "MAIN_EB::sensor",
                        "calibration_period": "MD",
                        "status": "ok",
                        "n_episodes": 20,
                        "f_d": 9.0,
                        "n": 9.0,
                        "f_p": 9.0,
                        "s": 9.0,
                        "alpha": 9.0,
                        "beta": 9.0,
                    },
                ]
            ).to_csv(calibration_dir / "primary_qvdf_fit_catalog.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "corridor": "MAIN_EB",
                        "network_from_node_id": 1,
                        "network_to_node_id": 2,
                        "network_selected_for_node_pair_lookup": True,
                        "direction": "EASTBOUND",
                        "tmc_code": "TEST001",
                        "road_order": 1,
                    }
                ]
            ).to_csv(reference_dir / "link_reference.csv", index=False)

            summary = run_transfer(
                base_network_path=base_path,
                future_network_path=future_path,
                calibration_dictionary_path=dictionary_path,
                output_dir=result_dir,
                method="framework",
                config=FrameworkConfig(geometry_auto_min_score=1.01),
                corridor_root=root / "corridors",
                write_spatial=False,
            )
            transferred = np.load(
                result_dir / "future_qvdf_node_pair_overrides.npy",
                allow_pickle=True,
            ).item()[(101, 102)]
            fallback, _ = _fallback_values(future.iloc[0], get_vdf_dict("qvdf"))
            self.assertEqual(summary["match_method_counts"], {"CORRIDOR": 1})
            self.assertEqual(transferred["QVDF_n1"], 1.1)
            self.assertEqual(transferred["QVDF_cp1"], 0.7)
            self.assertEqual(transferred["QVDF_cd1"], 6.1)
            self.assertEqual(transferred["QVDF_qdf1"], fallback["QVDF_qdf1"])
            self.assertEqual(transferred["QVDF_plf1"], fallback["QVDF_plf1"])
            self.assertEqual(transferred["QVDF_n2"], fallback["QVDF_n2"])
            self.assertTrue((result_dir / "expanded_node_pair_qvdf.csv").is_file())
            self.assertFalse(summary["base_dictionary_modified"])
            self.assertIn("read-only input", summary["base_dictionary_policy"])
            self.assertEqual(dictionary_path.read_bytes(), source_bytes)

            reused = run_transfer(
                base_network_path=base_path,
                future_network_path=future_path,
                calibration_dictionary_path=dictionary_path,
                output_dir=result_dir,
                method="framework",
                config=FrameworkConfig(geometry_auto_min_score=1.01),
                corridor_root=root / "corridors",
                write_spatial=False,
            )
            self.assertEqual(reused["expanded_dictionary_action"], "reused")
            self.assertEqual(reused["corridor_dictionary"]["action"], "reused")
            self.assertFalse(reused["base_dictionary_modified"])
            self.assertEqual(dictionary_path.read_bytes(), source_bytes)

    def test_rejects_output_path_that_would_replace_base_dictionary(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
            root = Path(temporary_directory)
            dictionary_path = root / "future_qvdf_node_pair_overrides.npy"
            np.save(dictionary_path, self.dictionary, allow_pickle=True)
            source_bytes = dictionary_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "cannot replace the base"):
                run_transfer(
                    base_network_path=root / "does-not-need-to-exist.gpkg",
                    future_network_path=root / "does-not-need-to-exist.gpkg",
                    calibration_dictionary_path=dictionary_path,
                    output_dir=root,
                    method="simple",
                    write_spatial=False,
                )

            self.assertEqual(dictionary_path.read_bytes(), source_bytes)

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
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
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
