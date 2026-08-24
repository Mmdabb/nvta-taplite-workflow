from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import numpy as np

from nvta_taplite_workflow.dtalite4cube.cube2gmns.fieldnameconfig import qvdf_params
from nvta_taplite_workflow.dtalite4cube.cube2gmns.congestion_boundaries import (
    DEFAULT_QVDF_PROFILE_MODE,
    PROFILE_MODE_FIELD,
    apply_congestion_boundaries,
)
from nvta_taplite_workflow.dtalite4cube.cube2gmns.funclib import (
    _assign_observed_speed_boundaries,
    _observed_qvdf_period_supported,
    _observed_plf_override,
    _select_vdf_record,
    _vdf_parameter_value,
)
from nvta_taplite_workflow.dtalite4cube.cube2gmns.vdf_lookup_tables import (
    NT_QVDF_DEFAULT_PLF,
    QVDF_RESOURCE_PATH,
    QVDF_VDF_DICT,
    get_vdf_dict,
)
from nvta_taplite_workflow.dtalite4cube.resources.observed_link_plf_lookup.load_observed_link_plf import (
    lookup as lookup_observed_plf,
)
from nvta_taplite_workflow.dtalite4cube.resources.observed_link_speed_boundary_lookup.load_observed_link_speed_boundaries import (
    lookup as lookup_observed_speed_boundaries,
)
from nvta_taplite_workflow.dtalite4cube.resources.observed_link_t2_lookup.load_observed_link_t2 import (
    lookup as lookup_observed_t2,
)


class QvdfResourceMappingTests(unittest.TestCase):
    def test_closed_link_skips_observed_plf_but_retains_speed_anchors(self):
        class Link:
            def __init__(self):
                self.other_attrs = {}

        plf = _observed_plf_override(
            0.8,
            link_is_closed=True,
            found=True,
            observed_value=0.35,
            from_node_id=10,
            to_node_id=20,
            time_period="AM",
        )
        link = Link()
        _assign_observed_speed_boundaries(
            link,
            link_is_closed=True,
            found=True,
            observed_values=np.array([45.0, 38.0]),
            from_node_id=10,
            to_node_id=20,
            time_period="AM",
        )

        self.assertEqual(plf, 0.8)
        self.assertEqual(link.other_attrs["qvdf_start_speed_mph"], 45.0)
        self.assertEqual(link.other_attrs["qvdf_end_speed_mph"], 38.0)

    def test_open_link_receives_observed_plf_and_speed_anchor_overrides(self):
        class Link:
            def __init__(self):
                self.other_attrs = {}

        plf = _observed_plf_override(
            0.8,
            link_is_closed=False,
            found=True,
            observed_value=0.35,
            from_node_id=10,
            to_node_id=20,
            time_period="AM",
        )
        link = Link()
        _assign_observed_speed_boundaries(
            link,
            link_is_closed=False,
            found=True,
            observed_values=np.array([45.0, 38.0]),
            from_node_id=10,
            to_node_id=20,
            time_period="AM",
        )

        self.assertEqual(plf, 0.35)
        self.assertEqual(link.other_attrs["qvdf_start_speed_mph"], 45.0)
        self.assertEqual(link.other_attrs["qvdf_end_speed_mph"], 38.0)

    def test_qvdf_lookup_comes_from_packaged_resource_csv(self):
        self.assertTrue(QVDF_RESOURCE_PATH.is_file())
        self.assertEqual(QVDF_RESOURCE_PATH.name, "link_qvdf.csv")
        self.assertEqual(QVDF_RESOURCE_PATH.parent.name, "resources")
        self.assertEqual(QVDF_RESOURCE_PATH.parent.parent.name, "dtalite4cube")

        mapping = get_vdf_dict("qvdf")
        with QVDF_RESOURCE_PATH.open("r", encoding="utf-8-sig", newline="") as stream:
            resource = {
                row["vdf_code"]: row
                for row in csv.DictReader(stream)
                if row.get("data_type") == "vdf_code"
            }

        self.assertIn("all", mapping)
        self.assertAlmostEqual(
            mapping["101"]["QVDF_plf1"], float(resource["101"]["QVDF_plf1"])
        )
        self.assertAlmostEqual(
            mapping["101"]["QVDF_n1"], float(resource["101"]["QVDF_n1"])
        )
        self.assertAlmostEqual(
            mapping["all"]["QVDF_cd3"], float(resource["all"]["QVDF_cd3"])
        )

    def test_exact_link_type_maps_every_qvdf_parameter_for_each_csv_period(self):
        mapping = get_vdf_dict("qvdf")
        selected_key, record = _select_vdf_record(mapping, 101)

        self.assertEqual(selected_key, "101")
        for period_sequence in (1, 2, 3):
            for parameter in qvdf_params:
                source_field = f"QVDF_{parameter}{period_sequence}"
                value = _vdf_parameter_value(
                    record,
                    vdf_type="qvdf",
                    vdf_field=parameter,
                    time_sequence=period_sequence,
                    selected_vdf_key=selected_key,
                    link_type=101,
                )
                self.assertEqual(value, mapping["101"][source_field])

    def test_missing_link_type_uses_all_row_for_every_qvdf_parameter(self):
        mapping = get_vdf_dict("qvdf")
        selected_key, record = _select_vdf_record(mapping, 999)

        self.assertEqual(selected_key, "all")
        for period_sequence in (1, 2, 3):
            for parameter in qvdf_params:
                source_field = f"QVDF_{parameter}{period_sequence}"
                value = _vdf_parameter_value(
                    record,
                    vdf_type="qvdf",
                    vdf_field=parameter,
                    time_sequence=period_sequence,
                    selected_vdf_key=selected_key,
                    link_type=999,
                )
                self.assertEqual(value, mapping["all"][source_field])

    def test_nt_uses_built_in_qvdf_defaults(self):
        mapping = get_vdf_dict("qvdf")
        selected_key, record = _select_vdf_record(mapping, 101)

        for parameter in qvdf_params:
            value = _vdf_parameter_value(
                record,
                vdf_type="qvdf",
                vdf_field=parameter,
                time_sequence=4,
                selected_vdf_key=selected_key,
                link_type=101,
            )
            if parameter == "plf":
                self.assertEqual(value, NT_QVDF_DEFAULT_PLF)
            else:
                self.assertEqual(value, QVDF_VDF_DICT["101"][f"QVDF_{parameter}4"])

    def test_nt_has_no_observed_qvdf_override(self):
        self.assertTrue(_observed_qvdf_period_supported("am"))
        self.assertTrue(_observed_qvdf_period_supported("MD"))
        self.assertTrue(_observed_qvdf_period_supported("pm"))
        self.assertFalse(_observed_qvdf_period_supported("nt"))

    def test_observed_link_plf_lookup_is_sorted_node_pair_mapping(self):
        dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("plf_am", "<f4"),
                ("plf_md", "<f4"),
                ("plf_pm", "<f4"),
            ]
        )
        table = np.array(
            [
                ((1 << 32) | 2, 1, 2, 0.5, 1.0, 0.75),
                ((3 << 32) | 4, 3, 4, 0.8, 0.9, 1.0),
            ],
            dtype=dtype,
        )

        values, found = lookup_observed_plf(
            table,
            "MD",
            np.array([3, 9]),
            np.array([4, 10]),
        )

        np.testing.assert_array_equal(found, [True, False])
        self.assertAlmostEqual(float(values[0]), 0.9, places=6)
        self.assertTrue(np.isnan(values[1]))

    def test_observed_speed_boundaries_are_selected_by_period_and_node_pair(self):
        dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("qvdf_start_speed_mph_am", "<f4"),
                ("qvdf_end_speed_mph_am", "<f4"),
                ("qvdf_start_speed_mph_md", "<f4"),
                ("qvdf_end_speed_mph_md", "<f4"),
                ("qvdf_start_speed_mph_pm", "<f4"),
                ("qvdf_end_speed_mph_pm", "<f4"),
            ]
        )
        table = np.array(
            [
                ((1 << 32) | 2, 1, 2, 62.0, 51.0, 51.0, 58.0, 58.0, 65.0),
                ((3 << 32) | 4, 3, 4, 55.0, 44.0, np.nan, 49.0, 49.0, 60.0),
            ],
            dtype=dtype,
        )

        values, found = lookup_observed_speed_boundaries(
            table,
            "MD",
            np.array([1, 3, 9]),
            np.array([2, 4, 10]),
        )

        np.testing.assert_array_equal(found, [True, True, False])
        np.testing.assert_allclose(values[0], [51.0, 58.0])
        self.assertTrue(np.isnan(values[1, 0]))
        self.assertAlmostEqual(float(values[1, 1]), 49.0, places=6)
        self.assertTrue(np.isnan(values[2]).all())

    def test_observed_episode_boundaries_are_selected_by_period_and_node_pair(self):
        dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("observed_t0_hour_am", "<f4"),
                ("observed_t2_hour_am", "<f4"),
                ("observed_t3_hour_am", "<f4"),
                ("observed_t0_hour_md", "<f4"),
                ("observed_t2_hour_md", "<f4"),
                ("observed_t3_hour_md", "<f4"),
                ("observed_t0_hour_pm", "<f4"),
                ("observed_t2_hour_pm", "<f4"),
                ("observed_t3_hour_pm", "<f4"),
            ]
        )
        table = np.array(
            [
                (
                    (1 << 32) | 2,
                    1,
                    2,
                    6.5,
                    7.25,
                    8.75,
                    np.nan,
                    np.nan,
                    np.nan,
                    14.0,
                    17.5,
                    20.0,
                ),
                (
                    (3 << 32) | 4,
                    3,
                    4,
                    np.nan,
                    np.nan,
                    np.nan,
                    10.0,
                    12.0,
                    14.0,
                    np.nan,
                    np.nan,
                    np.nan,
                ),
            ],
            dtype=dtype,
        )

        values, found = lookup_observed_t2(
            table,
            "PM",
            np.array([1, 3, 9]),
            np.array([2, 4, 10]),
        )

        np.testing.assert_array_equal(found, [True, True, False])
        np.testing.assert_allclose(values[0], [14.0, 17.5, 20.0])
        self.assertTrue(np.isnan(values[1]).all())
        self.assertTrue(np.isnan(values[2]).all())

    def test_boundary_assignment_sets_profile_mode_two_for_every_link(self):
        class Node:
            def __init__(self, node_id):
                self.node_id = node_id

        class Link:
            def __init__(self, from_node_id, to_node_id):
                self.from_node = Node(from_node_id)
                self.to_node = Node(to_node_id)
                self.other_attrs = {}

        class Network:
            def __init__(self):
                self.link_dict = {
                    1: Link(10, 20),
                    2: Link(30, 40),
                }

        network = Network()
        with tempfile.TemporaryDirectory() as directory:
            stats = apply_congestion_boundaries(
                network,
                "AM",
                lookup_directory=directory,
            )

        self.assertFalse(stats["available"])
        for link in network.link_dict.values():
            self.assertEqual(
                link.other_attrs[PROFILE_MODE_FIELD],
                DEFAULT_QVDF_PROFILE_MODE,
            )
            self.assertEqual(link.other_attrs[PROFILE_MODE_FIELD], 2)
            for field in ("t0_hour", "t2_hour", "t3_hour"):
                self.assertEqual(link.other_attrs[field], "")

    def test_nt_boundary_assignment_skips_observed_period_lookup(self):
        class Node:
            def __init__(self, node_id):
                self.node_id = node_id

        class Link:
            def __init__(self):
                self.from_node = Node(10)
                self.to_node = Node(20)
                self.other_attrs = {}

        class Network:
            def __init__(self):
                self.link_dict = {1: Link()}

        network = Network()
        with tempfile.TemporaryDirectory() as directory:
            stats = apply_congestion_boundaries(
                network,
                "nt",
                lookup_directory=directory,
            )

        self.assertFalse(stats["available"])
        self.assertFalse(stats["observed_t2_available"])
        self.assertEqual(network.link_dict[1].other_attrs[PROFILE_MODE_FIELD], 2)
        for field in ("t0_hour", "t2_hour", "t3_hour"):
            self.assertEqual(network.link_dict[1].other_attrs[field], "")

    def test_boundary_mapping_skips_explicitly_closed_links(self):
        class Node:
            def __init__(self, node_id):
                self.node_id = node_id

        class Link:
            def __init__(self, from_node_id, to_node_id, allowed_use):
                self.from_node = Node(from_node_id)
                self.to_node = Node(to_node_id)
                self.other_attrs = {"allowed_use": allowed_use}

        class Network:
            def __init__(self):
                self.link_dict = {
                    1: Link(10, 20, "closed"),
                    2: Link(30, 40, "sov;hov2;hov3;trk;apv;com"),
                }

        boundary_dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("t0_hour", "<f4"),
                ("t2_hour", "<f4"),
                ("t3_hour", "<f4"),
            ]
        )
        boundary_table = np.array(
            [
                ((10 << 32) | 20, 10, 20, 6.0, 7.0, 8.0),
                ((30 << 32) | 40, 30, 40, 6.5, 7.5, 8.5),
            ],
            dtype=boundary_dtype,
        )
        network = Network()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            np.save(root / "am_node_pair_boundaries.npy", boundary_table)
            stats = apply_congestion_boundaries(
                network,
                "AM",
                lookup_directory=root,
                observed_t2_lookup_directory=root / "missing-observed",
            )

        for field in ("t0_hour", "t2_hour", "t3_hour"):
            self.assertEqual(network.link_dict[1].other_attrs[field], "")
        self.assertEqual(network.link_dict[2].other_attrs["t0_hour"], 6.5)
        self.assertEqual(network.link_dict[2].other_attrs["t2_hour"], 7.5)
        self.assertEqual(network.link_dict[2].other_attrs["t3_hour"], 8.5)
        self.assertEqual(stats["closed_links_skipped"], 1)
        self.assertEqual(stats["eligible_links"], 1)
        self.assertEqual(stats["matched"], 1)

    def test_accepted_observed_episode_overrides_complete_matched_period_triplet(self):
        class Node:
            def __init__(self, node_id):
                self.node_id = node_id

        class Link:
            def __init__(self, from_node_id, to_node_id):
                self.from_node = Node(from_node_id)
                self.to_node = Node(to_node_id)
                self.other_attrs = {}

        class Network:
            def __init__(self):
                self.link_dict = {
                    1: Link(10, 20),
                    2: Link(30, 40),
                }

        boundary_dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("t0_hour", "<f4"),
                ("t2_hour", "<f4"),
                ("t3_hour", "<f4"),
            ]
        )
        observed_t2_dtype = np.dtype(
            [
                ("packed_key", "<u8"),
                ("from_node_id", "<u4"),
                ("to_node_id", "<u4"),
                ("observed_t0_hour_am", "<f4"),
                ("observed_t2_hour_am", "<f4"),
                ("observed_t3_hour_am", "<f4"),
                ("observed_t0_hour_md", "<f4"),
                ("observed_t2_hour_md", "<f4"),
                ("observed_t3_hour_md", "<f4"),
                ("observed_t0_hour_pm", "<f4"),
                ("observed_t2_hour_pm", "<f4"),
                ("observed_t3_hour_pm", "<f4"),
            ]
        )
        boundary_table = np.array(
            [
                ((10 << 32) | 20, 10, 20, 6.5, 7.25, 8.5),
                ((30 << 32) | 40, 30, 40, 6.75, 7.5, 8.75),
            ],
            dtype=boundary_dtype,
        )
        observed_t2_table = np.array(
            [
                (
                    (10 << 32) | 20,
                    10,
                    20,
                    5.5,
                    8.0,
                    9.5,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                ),
                (
                    (30 << 32) | 40,
                    30,
                    40,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                    np.nan,
                ),
            ],
            dtype=observed_t2_dtype,
        )
        network = Network()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            np.save(root / "am_node_pair_boundaries.npy", boundary_table)
            np.save(root / "observed_link_t2.npy", observed_t2_table)
            stats = apply_congestion_boundaries(
                network,
                "AM",
                lookup_directory=root,
                observed_t2_lookup_directory=root,
            )

        self.assertEqual(network.link_dict[1].other_attrs["t0_hour"], 5.5)
        self.assertEqual(network.link_dict[1].other_attrs["t2_hour"], 8.0)
        self.assertEqual(network.link_dict[1].other_attrs["t3_hour"], 9.5)
        self.assertEqual(network.link_dict[2].other_attrs["t0_hour"], "")
        self.assertEqual(network.link_dict[2].other_attrs["t2_hour"], "")
        self.assertEqual(network.link_dict[2].other_attrs["t3_hour"], "")
        self.assertEqual(stats["observed_t2_pair_matches"], 2)
        self.assertEqual(stats["observed_t2_assigned"], 1)
        self.assertEqual(stats["observed_t2_cleared_no_episode"], 1)


if __name__ == "__main__":
    unittest.main()
