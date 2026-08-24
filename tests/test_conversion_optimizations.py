from __future__ import annotations

import csv
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import numpy as np

from nvta_taplite_workflow.assignment_cli import build_arg_parser as build_public_arg_parser
from nvta_taplite_workflow.dtalite4cube.dtab import demand_binary_path, inspect_dtab, write_dtab_file
from nvta_taplite_workflow.dtalite4cube.file_utils import copy_files_parallel
from nvta_taplite_workflow.dtalite4cube.network_cache import (
    load_network_cache,
    network_source_fingerprint,
    save_network_cache,
)
from nvta_taplite_workflow.dtalite4cube.omx2csv import _close_worker_omx_files, _worker_omx_file
from nvta_taplite_workflow.dtalite4cube.reproducible_run import preflight
from nvta_taplite_workflow.dtalite4cube.runner import (
    AssignmentConfig,
    build_arg_parser as build_internal_arg_parser,
    run_assignment_pipeline,
    run_reproducible_dtalite,
)
from nvta_taplite_workflow.dtalite4cube.settings.generate_dtalite_settings import (
    generate_dtalite_input_files,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceArtifactTestCase(unittest.TestCase):
    def setUp(self):
        self.root = WORKFLOW_ROOT / ".test-artifacts" / str(uuid4())
        self.root.mkdir(parents=True)

    def tearDown(self):
        _close_worker_omx_files()
        shutil.rmtree(self.root)


class ParallelPackageDefaultTests(unittest.TestCase):
    def test_config_and_both_clis_use_csv_cache_and_bounded_auto_workers(self):
        config = AssignmentConfig(network_path=WORKFLOW_ROOT)
        self.assertEqual(config.conversion_workers, 0)
        self.assertTrue(config.conversion_cache)
        self.assertEqual(config.demand_output_format, "csv")
        self.assertEqual(config.processors, 20)
        self.assertEqual(config.route_output, 0)
        self.assertEqual(config.vehicle_output, 0)
        self.assertEqual(config.vdf_type, "qvdf")
        self.assertTrue(config.qvdf_parameter_override)
        self.assertTrue(config.qvdf_smoothing)
        self.assertEqual(config.qvdf_smoother_workers, "auto")
        self.assertTrue(config.qvdf_smoother_backup)
        for removed_name in (
            "use_sequential_ids_for_dtalite",
            "renumber_link_ids_if_needed",
            "backmap_dtalite_outputs",
        ):
            self.assertFalse(hasattr(config, removed_name), removed_name)

        public = build_public_arg_parser().parse_args([str(WORKFLOW_ROOT)])
        self.assertEqual(public.conversion_workers, 0)
        self.assertTrue(public.conversion_cache)
        self.assertEqual(public.demand_output_format, "csv")
        self.assertEqual(public.processors, 20)
        self.assertEqual(public.route_output, 0)
        self.assertEqual(public.vehicle_output, 0)
        self.assertEqual(public.vdf_type, "qvdf")
        self.assertTrue(public.qvdf_parameter_override)
        self.assertTrue(public.qvdf_smoothing)
        self.assertEqual(public.qvdf_smoother_workers, "auto")
        self.assertTrue(public.qvdf_smoother_backup)

        internal = build_internal_arg_parser().parse_args(
            ["--network-path", str(WORKFLOW_ROOT)]
        )
        self.assertEqual(internal.conversion_workers, 0)
        self.assertTrue(internal.conversion_cache)
        self.assertEqual(internal.demand_output_format, "csv")
        self.assertEqual(internal.processors, 20)
        self.assertEqual(internal.route_output, 0)
        self.assertEqual(internal.vehicle_output, 0)
        self.assertEqual(internal.vdf_type, "qvdf")
        self.assertTrue(internal.qvdf_parameter_override)
        self.assertTrue(internal.qvdf_smoothing)
        self.assertEqual(internal.qvdf_smoother_workers, "auto")
        self.assertTrue(internal.qvdf_smoother_backup)


class DirectAssignmentRoutingTests(WorkspaceArtifactTestCase):
    def test_pipeline_runs_each_prepared_period_folder_directly(self):
        scenario = self.root / "scenario"
        scenario.mkdir()
        output = self.root / "output"
        period_dir = output / "am"
        period_dir.mkdir(parents=True)
        readiness = Mock(log_path=period_dir / "gmns_readiness.log")
        config = AssignmentConfig(
            network_path=scenario,
            scenario_name="direct-native-id-test",
            time_periods=["am"],
            period_times=["0600_0900"],
            output_dir=output,
            dtalite_assignment=True,
        )

        with (
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.prepare_dtalite_period_folders",
                return_value={"am": period_dir},
            ),
            patch("nvta_taplite_workflow.dtalite4cube.runner.remove_root_period_duplicates"),
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.run_period_readiness_check",
                return_value=readiness,
            ),
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.run_reproducible_dtalite",
                return_value=period_dir,
            ) as direct_run,
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.smooth_scenario",
                return_value={
                    "status": "complete",
                    "staging": {"unique_links": 1},
                },
            ) as smoother,
            patch("nvta_taplite_workflow.dtalite4cube.runner._write_pipeline_manifest"),
        ):
            self.assertTrue(run_assignment_pipeline(config))

        direct_run.assert_called_once()
        call = direct_run.call_args.kwargs
        self.assertEqual(call["source_network_path"], period_dir)
        self.assertEqual(call["readiness_result"], readiness)
        self.assertNotIn("_internal", call["source_network_path"].parts)
        self.assertFalse((period_dir / "_internal").exists())
        smoother.assert_called_once()
        smoother_config = smoother.call_args.args[0]
        self.assertEqual(smoother_config.scenario_dir, output)
        self.assertEqual(smoother_config.periods, ["am"])
        self.assertEqual(smoother_config.workers, "auto")
        self.assertTrue(smoother_config.backup)

    def test_smoother_runs_after_every_period_and_before_pass_manifest(self):
        scenario = self.root / "scenario"
        scenario.mkdir()
        output = self.root / "output"
        period_dirs = {period: output / period for period in ("am", "md")}
        for period_dir in period_dirs.values():
            period_dir.mkdir(parents=True)

        config = AssignmentConfig(
            network_path=scenario,
            time_periods=["am", "md"],
            period_times=["0600_0900", "0900_1500"],
            output_dir=output,
            dtalite_assignment=True,
        )
        events = []

        def record_assignment(**kwargs):
            events.append(f"assignment:{kwargs['time_period']}")
            return kwargs["source_network_path"]

        def record_smoothing(_config):
            events.append("smoother")
            return {"status": "complete", "staging": {"unique_links": 1}}

        def record_manifest(*_args, **_kwargs):
            events.append("manifest")

        with (
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.prepare_dtalite_period_folders",
                return_value=period_dirs,
            ),
            patch("nvta_taplite_workflow.dtalite4cube.runner.remove_root_period_duplicates"),
            patch("nvta_taplite_workflow.dtalite4cube.runner.run_period_readiness_check"),
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.run_reproducible_dtalite",
                side_effect=record_assignment,
            ),
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner.smooth_scenario",
                side_effect=record_smoothing,
            ),
            patch(
                "nvta_taplite_workflow.dtalite4cube.runner._write_pipeline_manifest",
                side_effect=record_manifest,
            ),
        ):
            self.assertTrue(run_assignment_pipeline(config))

        self.assertEqual(
            events,
            ["assignment:am", "assignment:md", "smoother", "manifest"],
        )


class NetworkCacheTests(WorkspaceArtifactTestCase):
    def test_cache_hit_and_source_content_invalidation(self):
        source = self.root / "network"
        source.mkdir()
        (source / "DTALiteNetworkInput.shp").write_bytes(b"shape-v1")
        (source / "DTALiteNetworkInput.dbf").write_bytes(b"attributes-v1")
        fingerprint_v1, files_v1 = network_source_fingerprint(
            source,
            target_crs="EPSG:4326",
        )

        node_csv = self.root / "node.csv"
        node_csv.write_text(
            "node_id,zone_id,x_coord,y_coord\n1,1,0,0\n",
            encoding="utf-8",
        )
        cache_dir = self.root / "cache"
        save_network_cache(
            cache_dir,
            fingerprint=fingerprint_v1,
            source_files=files_v1,
            target_crs="EPSG:4326",
            payload={"prepared": [1, 2, 3]},
            node_csv_source=node_csv,
        )

        payload, cached_node = load_network_cache(
            cache_dir,
            expected_fingerprint=fingerprint_v1,
        )
        self.assertEqual(payload, {"prepared": [1, 2, 3]})
        self.assertEqual(cached_node.read_bytes(), node_csv.read_bytes())

        (source / "DTALiteNetworkInput.dbf").write_bytes(b"attributes-v2")
        fingerprint_v2, _ = network_source_fingerprint(
            source,
            target_crs="EPSG:4326",
        )
        self.assertNotEqual(fingerprint_v1, fingerprint_v2)
        payload, cached_node = load_network_cache(
            cache_dir,
            expected_fingerprint=fingerprint_v2,
        )
        self.assertIsNone(payload)
        self.assertIsNone(cached_node)


class OMXWorkerCacheTests(WorkspaceArtifactTestCase):
    def test_worker_reuses_open_omx_handle(self):
        import openmatrix as omx

        matrix_path = self.root / "tiny.omx"
        with omx.open_file(str(matrix_path), "w") as matrix_file:
            matrix_file["AM_SOVs"] = np.eye(3)

        first = _worker_omx_file(str(matrix_path))
        second = _worker_omx_file(str(matrix_path))
        self.assertIs(first, second)
        self.assertEqual(np.asarray(first["AM_SOVs"]).shape, (3, 3))


class ParallelFileCopyTests(WorkspaceArtifactTestCase):
    def test_parallel_copy_preserves_order_and_bytes(self):
        sources = self.root / "sources"
        targets = self.root / "targets"
        sources.mkdir()
        pairs = []
        for index in range(5):
            source = sources / f"source-{index}.bin"
            target = targets / f"target-{index}.bin"
            source.write_bytes(bytes([index]) * (index + 1) * 17)
            pairs.append((source, target))

        copied = copy_files_parallel(
            pairs,
            workers=3,
            preserve_metadata=False,
        )

        self.assertEqual(copied, pairs)
        for source, target in pairs:
            self.assertEqual(source.read_bytes(), target.read_bytes())


class BinaryDemandTests(WorkspaceArtifactTestCase):
    def _write_period_inputs(self, period_dir: Path) -> None:
        period_dir.mkdir(parents=True)
        with (period_dir / "node.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(["node_id", "zone_id", "x_coord", "y_coord"])
            writer.writerows([[10, 10, 0, 0], [20, 20, 1, 1], [100, "", 2, 2]])
        with (period_dir / "link.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(["link_id", "from_node_id", "to_node_id"])
            writer.writerows([[7, 10, 100], [8, 100, 20]])

        generate_dtalite_input_files(
            period_dir,
            "am",
            overrides={"demand_format": 1},
        )
        with (period_dir / "mode_type.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(
                [
                    "mode_type_id",
                    "mode_type",
                    "name",
                    "vot",
                    "pce",
                    "occ",
                    "demand_file",
                ]
            )
            writer.writerow([1, "sov", "sov", 20, 1, 1, "sov_am.csv"])

        write_dtab_file(
            period_dir / "sov_am.bin",
            np.array([10, 20], dtype=np.int32),
            np.array([20, 10], dtype=np.int32),
            np.array([1.25, 2.5], dtype=np.float64),
        )

    def test_binary_preflight_accepts_source_ids_without_external_remap(self):
        source = self.root / "am"
        self._write_period_inputs(source)

        source_info = preflight(source)
        binary_info = inspect_dtab(source / "sov_am.bin")

        self.assertEqual(source_info["counts"]["demand_files"], 1)
        self.assertEqual(source_info["files"]["sov_am.bin"]["rows"], 2)
        self.assertEqual(binary_info["records"], 2)
        self.assertFalse((source / "_internal").exists())

    def test_direct_dry_run_uses_source_period_folder(self):
        source = self.root / "am"
        self._write_period_inputs(source)
        config = AssignmentConfig(
            network_path=source,
            time_periods=["am"],
            period_times=["0600_0900"],
            dtalite_assignment=True,
            dry_run=True,
        )

        result = run_reproducible_dtalite(
            config=config,
            source_network_path=source,
            work_dir=self.root / "unused-work-dir",
            label="direct-dry-run",
            time_period="am",
        )

        self.assertEqual(result, source)
        self.assertFalse((source / "_internal").exists())

    def test_binary_path_replaces_csv_suffix(self):
        self.assertEqual(
            demand_binary_path(self.root / "sov_am.csv"),
            self.root / "sov_am.bin",
        )


if __name__ == "__main__":
    unittest.main()
