from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from nvta_taplite_workflow.dtalite4cube.future_attribute_transfer import (
    BOUNDARY_FIELDS,
    BoundaryResourcePaths,
    FrameworkConfig,
    QVDF_FIELDS,
    _build_future_boundary_dictionary,
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

    def test_speed_t_expansion_uses_only_link_level_lineage(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path.cwd(), ignore_cleanup_errors=True
        ) as temporary_directory:
            root = Path(temporary_directory)
            speed_dir = root / "speed"
            observed_t_dir = root / "observed-t"
            congestion_dir = root / "congestion"
            speed_dir.mkdir()
            observed_t_dir.mkdir()
            congestion_dir.mkdir()
            pairs = [(1, 2), (10, 20)]
            packed = [(a << 32) | b for a, b in pairs]

            speed_dtype = [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                *[
                    (f"qvdf_{side}_speed_mph_{period}", "<f4")
                    for period in ("am", "md", "pm")
                    for side in ("start", "end")
                ],
            ]
            speed_rows = []
            for pair_index, ((a, b), key) in enumerate(zip(pairs, packed)):
                speed_rows.append(
                    (key, a, b, *[40.0 + pair_index + i for i in range(6)])
                )
            np.save(
                speed_dir / "observed_link_speed_boundaries.npy",
                np.array(speed_rows, dtype=np.dtype(speed_dtype)),
            )

            observed_t_dtype = [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                *[
                    (f"observed_{field}_hour_{period}", "<f4")
                    for period in ("am", "md", "pm")
                    for field in ("t0", "t2", "t3")
                ],
            ]
            np.save(
                observed_t_dir / "observed_link_t2.npy",
                np.array(
                    [
                        (
                            packed[0],
                            1,
                            2,
                            6.0,
                            7.0,
                            8.0,
                            np.nan,
                            np.nan,
                            np.nan,
                            15.0,
                            16.0,
                            17.0,
                        )
                    ],
                    dtype=np.dtype(observed_t_dtype),
                ),
            )
            base_t_dtype = np.dtype(
                [
                    ("packed_key", "<u8"),
                    ("from_node_id", "<u4"),
                    ("to_node_id", "<u4"),
                    ("t0_hour", "<f4"),
                    ("t2_hour", "<f4"),
                    ("t3_hour", "<f4"),
                ]
            )
            for period_index, period in enumerate(("am", "md", "pm")):
                np.save(
                    congestion_dir / f"{period}_node_pair_boundaries.npy",
                    np.array(
                        [
                            (
                                key,
                                a,
                                b,
                                5.0 + period_index,
                                6.0 + period_index,
                                7.0 + period_index,
                            )
                            for (a, b), key in zip(pairs, packed)
                        ],
                        dtype=base_t_dtype,
                    ),
                )

            resources = BoundaryResourcePaths(
                speed_directory=speed_dir,
                observed_t_directory=observed_t_dir,
                congestion_t_directory=congestion_dir,
            )
            source_bytes = {
                path: path.read_bytes()
                for path in resources.resolved_files().values()
            }
            crosswalk = pd.DataFrame(
                [
                    {"future_a": 1, "future_b": 2, "base_a": 1, "base_b": 2, "match_method": "EXACT_AB"},
                    {"future_a": 1, "future_b": 3, "base_a": 1, "base_b": 2, "match_method": "REPLACEMENT_PATH"},
                    {"future_a": 101, "future_b": 102, "base_a": 10, "base_b": 20, "match_method": "GEOMETRY"},
                    {"future_a": 201, "future_b": 202, "base_a": np.nan, "base_b": np.nan, "match_method": "CORRIDOR"},
                    {"future_a": 301, "future_b": 302, "base_a": np.nan, "base_b": np.nan, "match_method": "LINK_TYPE_FALLBACK"},
                ]
            )
            expanded, audit, _, _ = _build_future_boundary_dictionary(
                crosswalk, resources
            )

            for pair in ((1, 2), (1, 3), (101, 102)):
                self.assertTrue(
                    np.isfinite(
                        [expanded[pair][field] for field in BOUNDARY_FIELDS]
                    ).any()
                )
            self.assertEqual(expanded[(1, 2)]["t0_hour_am"], 6.0)
            self.assertTrue(np.isnan(expanded[(1, 2)]["t0_hour_md"]))
            self.assertEqual(expanded[(1, 2)]["t0_hour_pm"], 15.0)
            for pair in ((201, 202), (301, 302)):
                self.assertTrue(
                    np.isnan([expanded[pair][field] for field in BOUNDARY_FIELDS]).all()
                )
            self.assertEqual(
                audit["boundary_transfer_policy"].tolist(),
                [
                    "BASE_LINK_LINEAGE",
                    "BASE_LINK_LINEAGE",
                    "BASE_LINK_LINEAGE",
                    "INTENTIONALLY_BLANK",
                    "INTENTIONALLY_BLANK",
                ],
            )
            for path, content in source_bytes.items():
                self.assertEqual(path.read_bytes(), content)

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
            boundary_values = np.load(
                result_dir / "future_speed_t_overrides.npy", allow_pickle=True
            ).item()[(101, 102)]
            self.assertTrue(
                np.isnan([boundary_values[field] for field in BOUNDARY_FIELDS]).all()
            )
            self.assertTrue((result_dir / "expanded_speed_t_overrides.csv").is_file())
            self.assertFalse(summary["base_dictionary_modified"])
            self.assertFalse(summary["base_boundary_resources_modified"])
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
            self.assertEqual(
                reused["expanded_boundary_dictionary_action"], "reused"
            )
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
