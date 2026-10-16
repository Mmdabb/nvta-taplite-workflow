from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nvta_taplite_workflow.assignment_cli import (
    build_arg_parser,
    build_config,
    resolve_cube_paths,
)
from nvta_taplite_workflow.dtalite4cube import runner
from nvta_taplite_workflow.dtalite4cube.cube2gmns.funclib import (
    _resolve_network_shapefile,
)
from nvta_taplite_workflow.dtalite4cube.future_attribute_transfer import (
    _resolve_network_path,
)
from nvta_taplite_workflow.dtalite4cube.omx2csv import get_gmns_demand_from_omx
from nvta_taplite_workflow.dtalite4cube.runner import AssignmentConfig
from nvta_taplite_workflow.path_resolution import (
    resolve_dtalite_workdir,
    resolve_scenario_and_dtalite_paths,
)
from nvta_taplite_workflow.postprocessing.runner import (
    preprocess_and_summarize_scenario,
    resolve_dtalite_workdir as resolve_postprocessing_workdir,
)


class ScenarioPathResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.scenario = self.root / "Scenario"
        self.dtalite = self.scenario / "Outputs" / "DTALite"
        self.dtalite.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_scenario_prefers_existing_outputs_dtalite_for_every_stage_mode(self) -> None:
        expected = (self.scenario.resolve(), self.dtalite.resolve())
        self.assertEqual(resolve_scenario_and_dtalite_paths(self.scenario), expected)
        self.assertEqual(
            resolve_cube_paths(self.scenario, network_conversion=True),
            expected,
        )
        self.assertEqual(
            resolve_cube_paths(self.scenario, network_conversion=False),
            expected,
        )
        self.assertEqual(resolve_postprocessing_workdir(self.scenario), self.dtalite.resolve())

        config = build_config(
            build_arg_parser().parse_args(["--scenario-dir", str(self.scenario)])
        )
        self.assertEqual(config.network_path, self.dtalite.resolve())
        self.assertEqual(
            config.conversion_cache_dir,
            self.dtalite.resolve() / ".dtalite_conversion_cache",
        )

    def test_scenario_itself_is_fallback_when_outputs_dtalite_is_absent(self) -> None:
        fallback = self.root / "FlatScenario"
        fallback.mkdir()

        self.assertEqual(resolve_dtalite_workdir(fallback), fallback.resolve())
        self.assertEqual(
            resolve_scenario_and_dtalite_paths(fallback),
            (fallback.resolve(), fallback.resolve()),
        )
        self.assertEqual(resolve_postprocessing_workdir(fallback), fallback.resolve())

    def test_exact_outputs_dtalite_path_preserves_scenario_identity(self) -> None:
        self.assertEqual(
            resolve_scenario_and_dtalite_paths(self.dtalite),
            (self.scenario.resolve(), self.dtalite.resolve()),
        )

    def test_low_level_network_and_transfer_loaders_resolve_scenario_root(self) -> None:
        network = self.dtalite / "DTALiteNetworkInput_TRUESHP.shp"
        network.touch()

        self.assertEqual(_resolve_network_shapefile(self.scenario), network.resolve())
        self.assertEqual(_resolve_network_path(self.scenario), network.resolve())

    def test_low_level_demand_converter_searches_resolved_workdir(self) -> None:
        with self.assertRaises(FileNotFoundError) as context:
            get_gmns_demand_from_omx(self.scenario, ["am"])

        self.assertIn(str(self.dtalite.resolve()), str(context.exception))

    def test_network_and_demand_conversion_use_resolved_workdir(self) -> None:
        config = AssignmentConfig(
            network_path=self.scenario,
            network_conversion=True,
            demand_conversion=True,
            qvdf_parameter_override=False,
            time_periods=["am"],
            period_times=["0600_0900"],
        )
        self.assertEqual(config.network_path, self.dtalite.resolve())

        conversion_result = {
            "elapsed_sec": 0.0,
            "worker_plan": {"workers": 1},
            "outputs": [],
        }
        with (
            patch(
                "nvta_taplite_workflow.dtalite4cube.cube2gmns.get_gmns_from_cube",
                return_value=conversion_result,
            ) as network_conversion,
            patch.object(
                runner,
                "validate_network_mapping_outputs",
                return_value={"qvdf_mapped_links": 0, "periods": []},
            ),
        ):
            runner.run_network_conversion(config)

        self.assertEqual(network_conversion.call_args.args[0], str(self.dtalite.resolve()))
        self.assertEqual(
            network_conversion.call_args.kwargs["output_dir"],
            str(self.dtalite.resolve()),
        )

        with patch(
            "nvta_taplite_workflow.dtalite4cube.omx2csv.get_gmns_demand_from_omx",
            return_value=conversion_result,
        ) as demand_conversion:
            runner.run_demand_conversion(config)

        self.assertEqual(demand_conversion.call_args.args[0], str(self.dtalite.resolve()))
        self.assertEqual(
            demand_conversion.call_args.kwargs["output_base_dir"],
            self.dtalite.resolve(),
        )

    def test_assignment_runs_from_period_folder_under_resolved_workdir(self) -> None:
        period_dir = self.dtalite / "am"
        config = AssignmentConfig(
            network_path=self.scenario,
            dtalite_assignment=True,
            qvdf_smoothing=False,
            time_periods=["am"],
            period_times=["0600_0900"],
        )
        config.run_gmns_readiness_check = False

        with (
            patch.object(
                runner,
                "prepare_dtalite_period_folders",
                return_value={"am": period_dir},
            ) as prepare_periods,
            patch.object(runner, "remove_root_period_duplicates"),
            patch.object(runner, "run_reproducible_dtalite", return_value=period_dir) as assign,
        ):
            runner.run_assignment_pipeline(config)

        self.assertEqual(
            prepare_periods.call_args.kwargs["scenario_input_dir"],
            self.dtalite.resolve(),
        )
        self.assertEqual(
            prepare_periods.call_args.kwargs["scenario_output_dir"],
            self.dtalite.resolve(),
        )
        self.assertEqual(assign.call_args.kwargs["source_network_path"], period_dir)

    def test_postprocessing_reads_and_writes_in_resolved_workdir(self) -> None:
        processed = object()
        with (
            patch(
                "nvta_taplite_workflow.postprocessing.runner.resolve_period_output_dirs"
            ),
            patch(
                "nvta_taplite_workflow.postprocessing.runner.link_performance_preprocess",
                return_value=processed,
            ) as preprocess,
            patch(
                "nvta_taplite_workflow.postprocessing.runner.performance_summary"
            ) as summarize,
        ):
            preprocess_and_summarize_scenario(
                scenario_dir=self.scenario,
                time_periods=["am"],
                period_range_list=["0600_0900"],
                time_duration_dict={"am": 3.0},
            )

        self.assertEqual(preprocess.call_count, 2)
        self.assertTrue(
            all(call.args[0] == str(self.dtalite.resolve()) for call in preprocess.call_args_list)
        )
        self.assertEqual(summarize.call_count, 2)
        self.assertTrue(
            all(call.args[1] == str(self.dtalite.resolve()) for call in summarize.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
