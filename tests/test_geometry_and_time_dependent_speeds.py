from __future__ import annotations

import sys
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

import pandas as pd
from shapely.geometry import LineString, MultiLineString


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKFLOW_ROOT))

from nvta_taplite_workflow.dtalite4cube.cube2gmns.funclib import (
    _line_geometry_endpoints,
    _loadNodes,
    _outputLink,
    _outputNode,
)
from nvta_taplite_workflow.dtalite4cube.cube2gmns.netclass import Link, Network, Node
from nvta_taplite_workflow.postprocessing.pipeline.link_perf_comparison import (
    aggregate_period_link_performance,
)
from nvta_taplite_workflow.postprocessing.pipeline.time_dependent_speeds import (
    minute_is_in_period,
    period_range_mapping,
)


class GeometryPreservationTests(unittest.TestCase):
    def test_node_loading_ignores_unused_link_attributes(self):
        raw_network = pd.DataFrame(
            {
                "A": [1, 2],
                "B": [2, 10001],
                "geometry": [
                    LineString([(0, 0), (1, 1)]),
                    LineString([(1, 1), (2, 2)]),
                ],
                "UNUSED_LINK_ATTRIBUTE": ["first", "second"],
            }
        )
        network = Network()
        _loadNodes(network, raw_network)

        self.assertEqual(set(network.node_dict), {1, 2, 10001})
        self.assertEqual(network.node_dict[1].zone_id, 1)
        self.assertIsNone(network.node_dict[10001].zone_id)
        self.assertEqual(network.node_other_attrs, [])
        self.assertEqual(network.node_dict[1].other_attrs, {})

        output_dir = WORKFLOW_ROOT / ".test-artifacts" / str(uuid4())
        output_dir.mkdir(parents=True)
        try:
            _outputNode(network, output_dir)
            result = pd.read_csv(output_dir / "node.csv")
        finally:
            shutil.rmtree(output_dir)

        self.assertEqual(list(result.columns), ["node_id", "zone_id", "x_coord", "y_coord"])
        self.assertEqual(result["node_id"].tolist(), [1, 2, 10001])

    def test_curved_linestring_uses_only_first_and_last_topology_points(self):
        line = LineString([(0, 0), (1, 2), (2, 0)])

        self.assertEqual(
            _line_geometry_endpoints(line),
            ((0.0, 0.0), (2.0, 0.0)),
        )
        self.assertEqual(len(line.coords), 3)

    def test_multiline_uses_outer_endpoints_without_flattening_geometry(self):
        line = MultiLineString(
            [
                [(2, 0), (3, 2)],
                [(3, 2), (4, 0)],
            ]
        )

        self.assertEqual(
            _line_geometry_endpoints(line),
            ((2.0, 0.0), (4.0, 0.0)),
        )
        self.assertEqual(line.geom_type, "MultiLineString")
        self.assertEqual(sum(len(part.coords) for part in line.geoms), 4)

    def test_multiline_wkt_is_written_without_discarding_parts(self):
        line = MultiLineString(
            [
                [(0, 0), (1, 2)],
                [(1, 2), (2, 0)],
            ]
        )
        network = Network()
        from_node = Node(10)
        to_node = Node(20)
        link = Link(1)
        link.from_node = from_node
        link.to_node = to_node
        link.geometry = line
        network.link_dict[link.link_id] = link

        output_dir = WORKFLOW_ROOT / ".test-artifacts" / str(uuid4())
        output_dir.mkdir(parents=True)
        try:
            _outputLink(network, output_dir, "am")
            result = pd.read_csv(output_dir / "link_am.csv")
        finally:
            shutil.rmtree(output_dir)

        self.assertEqual(result.loc[0, "geometry"], line.wkt)


class TimeDependentSpeedTests(unittest.TestCase):
    def test_cross_midnight_period_membership(self):
        self.assertTrue(minute_is_in_period(23 * 60 + 55, "1900_0600"))
        self.assertTrue(minute_is_in_period(5 * 60 + 55, "1900_0600"))
        self.assertFalse(minute_is_in_period(6 * 60, "1900_0600"))

    def test_aggregate_copies_each_speed_from_its_configured_period(self):
        keys = {
            "link_id": [1],
            "from_node_id": [10],
            "to_node_id": [20],
        }
        geometry = "LINESTRING (0 0, 1 1)"
        am = pd.DataFrame(
            {
                **keys,
                "speed_mph": [10.0],
                "volume": [100.0],
                "geometry": [geometry],
                "link_type": [1],
                "spd_mph_06:55": [11.0],
                "spd_mph_09:05": [999.0],
            }
        )
        md = pd.DataFrame(
            {
                **keys,
                "speed_mph": [20.0],
                "volume": [200.0],
                "geometry": [geometry],
                "link_type": [1],
                "spd_mph_06:55": [888.0],
                "spd_mph_09:05": [22.0],
            }
        )
        ranges = period_range_mapping(
            ["am", "md"],
            ["0600_0900", "0900_1500"],
        )

        aggregate = aggregate_period_link_performance(
            {"am": am, "md": md},
            {"am": 3, "md": 6},
            ranges,
        )

        self.assertEqual(aggregate.loc[0, "spd_mph_06:55"], 11.0)
        self.assertEqual(aggregate.loc[0, "spd_mph_09:05"], 22.0)
        self.assertEqual(aggregate.loc[0, "volume"], 300.0)
        self.assertAlmostEqual(
            aggregate.loc[0, "speed"],
            (10 * 3 + 20 * 6) / 9,
        )


if __name__ == "__main__":
    unittest.main()
