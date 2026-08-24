from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from uuid import uuid4

import numpy as np
import openmatrix as omx

from nvta_taplite_workflow.dtalite4cube.cube2gmns.funclib import _merge_network_parts
from nvta_taplite_workflow.dtalite4cube.dtab import read_dtab_records
from nvta_taplite_workflow.dtalite4cube.omx2csv import get_gmns_demand_from_omx
from nvta_taplite_workflow.dtalite4cube.parallel_utils import (
    choose_chunks_per_group,
    choose_worker_plan,
    chunk_ranges,
)
from nvta_taplite_workflow.dtalite4cube.settings.dtalite_settings_config import DEMAND_LANE_USES
from nvta_taplite_workflow.dtalite4cube.settings.dtalite_settings_config import demand_file_name


class WorkerPlanningTests(unittest.TestCase):
    def test_parallel_plan_is_bounded_by_physical_capacity_and_tasks(self):
        plan = choose_worker_plan(
            requested_workers=20,
            reserve_cores=2,
            task_count=6,
            work_items=1_000_000,
            min_work_items_per_worker=1_000,
            adaptive=False,
            logical_cores=16,
            physical_cores=8,
        )
        self.assertEqual(plan.workers, 6)

    def test_busy_machine_falls_back_to_serial(self):
        plan = choose_worker_plan(
            requested_workers=0,
            reserve_cores=1,
            task_count=24,
            work_items=10_000_000,
            min_work_items_per_worker=1_000,
            adaptive=True,
            logical_cores=16,
            physical_cores=8,
            idle_physical_cores=1,
        )
        self.assertEqual(plan.workers, 1)
        self.assertIn("fewer than two", plan.reason)

    def test_small_workload_falls_back_to_serial(self):
        plan = choose_worker_plan(
            requested_workers=8,
            reserve_cores=0,
            task_count=8,
            work_items=100,
            min_work_items_per_worker=1_000,
            adaptive=False,
            logical_cores=16,
            physical_cores=8,
        )
        self.assertEqual(plan.workers, 1)

    def test_chunk_count_feeds_pool_without_tiny_chunks(self):
        chunks = choose_chunks_per_group(
            items_per_group=10_000,
            group_count=4,
            workers=8,
            requested_chunks=0,
            min_chunk_items=1_000,
        )
        self.assertEqual(chunks, 4)
        self.assertEqual(chunk_ranges(10, 3), [(0, 4), (4, 8), (8, 10)])


class DemandConversionTests(unittest.TestCase):
    def _write_test_omx(self, path: Path, size: int = 600) -> None:
        with omx.open_file(str(path), "w") as matrix_file:
            for mode_number, mode in enumerate(DEMAND_LANE_USES, start=1):
                matrix = np.zeros((size, size), dtype=np.float64)
                rows = np.arange(0, size, 37)
                cols = (rows * (mode_number + 1) + mode_number) % size
                matrix[rows, cols] = mode_number + rows / 1000.0
                matrix_file[f"AM_{mode.upper()}s"] = matrix

    def test_parallel_demand_output_matches_serial_bytes(self):
        workflow_root = Path(__file__).resolve().parents[1]
        root = workflow_root / ".test-artifacts" / str(uuid4())
        root.mkdir(parents=True)
        try:
            self._write_test_omx(root / "test_AM_Trips.omx")
            serial_dir = root / "serial"
            parallel_dir = root / "parallel"

            serial = get_gmns_demand_from_omx(
                root,
                ["am"],
                output_base_dir=serial_dir,
                conversion_workers=1,
                reserve_cores=0,
                chunks_per_mode=1,
                adaptive=False,
            )
            parallel = get_gmns_demand_from_omx(
                root,
                ["am"],
                output_base_dir=parallel_dir,
                conversion_workers=2,
                reserve_cores=0,
                chunks_per_mode=3,
                adaptive=False,
            )

            self.assertFalse(serial["parallel"])
            self.assertTrue(parallel["parallel"])
            for mode in DEMAND_LANE_USES:
                name = demand_file_name(mode, "am")
                self.assertEqual(
                    (serial_dir / "am" / name).read_bytes(),
                    (parallel_dir / "am" / name).read_bytes(),
                    name,
                )
        finally:
            shutil.rmtree(root)

    def test_parallel_binary_and_csv_outputs_match_serial_bytes(self):
        workflow_root = Path(__file__).resolve().parents[1]
        root = workflow_root / ".test-artifacts" / str(uuid4())
        root.mkdir(parents=True)
        try:
            self._write_test_omx(root / "test_AM_Trips.omx")
            serial_dir = root / "serial"
            parallel_dir = root / "parallel"

            get_gmns_demand_from_omx(
                root,
                ["am"],
                output_base_dir=serial_dir,
                conversion_workers=1,
                reserve_cores=0,
                chunks_per_mode=1,
                adaptive=False,
                output_format="both",
            )
            get_gmns_demand_from_omx(
                root,
                ["am"],
                output_base_dir=parallel_dir,
                conversion_workers=2,
                reserve_cores=0,
                chunks_per_mode=3,
                adaptive=False,
                output_format="both",
            )

            for mode in DEMAND_LANE_USES:
                csv_name = demand_file_name(mode, "am")
                bin_name = Path(csv_name).with_suffix(".bin").name
                serial_csv = serial_dir / "am" / csv_name
                parallel_csv = parallel_dir / "am" / csv_name
                serial_bin = serial_dir / "am" / bin_name
                parallel_bin = parallel_dir / "am" / bin_name
                self.assertEqual(serial_csv.read_bytes(), parallel_csv.read_bytes(), csv_name)
                self.assertEqual(serial_bin.read_bytes(), parallel_bin.read_bytes(), bin_name)

                records = read_dtab_records(serial_bin)
                csv_rows = np.loadtxt(
                    serial_csv,
                    delimiter=",",
                    skiprows=1,
                    ndmin=2,
                )
                self.assertEqual(len(records), len(csv_rows))
                self.assertTrue(
                    np.array_equal(records["o_zone_id"], csv_rows[:, 0].astype(np.int32))
                )
                self.assertTrue(
                    np.array_equal(records["d_zone_id"], csv_rows[:, 1].astype(np.int32))
                )
                self.assertTrue(np.array_equal(records["volume"], csv_rows[:, 2]))
        finally:
            shutil.rmtree(root)


class NetworkMergeTests(unittest.TestCase):
    def test_merge_preserves_exact_worker_serialization(self):
        workflow_root = Path(__file__).resolve().parents[1]
        root = workflow_root / ".test-artifacts" / str(uuid4())
        root.mkdir(parents=True)
        try:
            header = b"from_node_id,to_node_id,value,project\r\n"
            first_rows = b"1,2,0.16093400000000002,NA\r\n2,3,0.0,\r\n"
            second_rows = b"4,5,0.14484059999999999,NA\r\n"
            first_part = root / "part_0000.csv"
            second_part = root / "part_0001.csv"
            first_part.write_bytes(header + first_rows)
            second_part.write_bytes(header + second_rows)
            output = root / "link.csv"

            count = _merge_network_parts(
                [
                    {
                        "part_path": str(second_part),
                        "row_start": 2,
                        "links": 1,
                    },
                    {
                        "part_path": str(first_part),
                        "row_start": 0,
                        "links": 2,
                    },
                ],
                output,
            )

            self.assertEqual(count, 3)
            self.assertEqual(output.read_bytes(), header + first_rows + second_rows)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
