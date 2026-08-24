from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nvta_taplite_workflow.assignment_cli import (
    build_arg_parser,
    build_config,
    preferred_run_log_dirs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RunAssignmentLoggingTests(unittest.TestCase):
    def test_output_directory_owns_default_log_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = root / "scenario"
            output = root / "requested-run-output"
            scenario.mkdir()

            argv = ["--scenario-dir", str(scenario), "--output-dir", str(output)]
            self.assertEqual(preferred_run_log_dirs(argv)[0], output / "logs")

            config = build_config(build_arg_parser().parse_args(argv))
            self.assertEqual(
                config.conversion_cache_dir,
                output / ".dtalite_conversion_cache",
            )

    def test_early_failure_is_logged_under_requested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing_scenario = root / "missing-scenario"
            output = root / "requested-run-output"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nvta_taplite_workflow",
                    "assignment",
                    "--scenario-dir",
                    str(missing_scenario),
                    "--output-dir",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            latest_log = output / "logs" / "run_assignment_latest.log"
            self.assertTrue(latest_log.is_file(), completed.stdout + completed.stderr)
            log_text = latest_log.read_text(encoding="utf-8").lower()
            self.assertIn("run_assignment failed", log_text)
            self.assertIn("does not exist", log_text)

    def test_relative_output_and_cache_are_anchored_to_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = root / "scenario"
            scenario.mkdir()
            argv = [
                "--scenario-dir",
                str(scenario),
                "--output-dir",
                "assignment-output",
                "--conversion-cache-dir",
                "cache",
            ]
            config = build_config(build_arg_parser().parse_args(argv))
            self.assertEqual(config.output_dir, scenario / "assignment-output")
            self.assertEqual(
                config.conversion_cache_dir,
                scenario / "assignment-output" / "cache",
            )

    def test_logs_use_output_then_callers_folder_without_local_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            scenario = Path(temporary_directory) / "scenario"
            output = scenario / "output"
            scenario.mkdir()
            candidates = preferred_run_log_dirs(
                ["--scenario-dir", str(scenario), "--output-dir", "output"]
            )
            self.assertEqual(candidates, [output / "logs", Path.cwd().resolve() / "logs"])
            self.assertNotIn(
                "localappdata",
                (
                    PROJECT_ROOT
                    / "src"
                    / "nvta_taplite_workflow"
                    / "assignment_cli.py"
                ).read_text(encoding="utf-8").lower(),
            )

    def test_missing_source_is_rejected_instead_of_using_current_directory(self) -> None:
        args = build_arg_parser().parse_args([])
        with self.assertRaisesRegex(ValueError, "source network folder is required"):
            build_config(args)

    def test_existing_folder_without_shapefile_fails_with_source_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = root / "empty-source"
            output = root / "output"
            scenario.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nvta_taplite_workflow",
                    "assignment",
                    "--scenario-dir",
                    str(scenario),
                    "--output-dir",
                    str(output),
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            log_text = (output / "logs" / "run_assignment_latest.log").read_text(
                encoding="utf-8"
            )
            self.assertIn("No .shp network file was found", log_text)
            self.assertIn("--scenario-dir", log_text)


if __name__ == "__main__":
    unittest.main()
