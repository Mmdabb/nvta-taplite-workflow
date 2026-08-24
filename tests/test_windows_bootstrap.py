from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_ROOT = PROJECT_ROOT / "client"


class WindowsBootstrapTests(unittest.TestCase):
    def test_miniforge_installer_is_verified_and_not_bundled(self) -> None:
        setup_dir = CLIENT_ROOT / "setup"
        self.assertEqual(list(setup_dir.glob("*.exe")), [])
        self.assertEqual(list(setup_dir.glob("*.ps1")), [])

        ensure = (setup_dir / "ensure_miniforge.bat").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("miniforge3-windows-x86_64.exe", ensure)
        self.assertIn("api.github.com/repos/conda-forge/miniforge/releases/latest", ensure)
        self.assertIn("get-authenticodesignature", ensure)
        self.assertIn("/registerpython=0", ensure)
        self.assertIn("/addtopath=0", ensure)
        self.assertIn(r"%localappdata%\miniforge3", ensure)
        self.assertNotIn(r"%localappdata%\nvta\miniforge3", ensure)

    def test_conda_discovery_prefers_miniforge_without_activation(self) -> None:
        finder = (CLIENT_ROOT / "setup" / "find_conda.bat").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("powershell.exe", finder)
        self.assertNotIn(".ps1", finder)
        self.assertIn("miniforge3", finder)
        self.assertIn("miniconda3", finder)
        self.assertIn("anaconda3", finder)
        self.assertIn("nvta_miniforge_exe", finder)
        self.assertLess(finder.index("miniforge3"), finder.index("miniconda3"))
        self.assertNotIn("conda activate", finder)

    def test_setup_rebuilds_environment_and_installs_one_workflow_package(self) -> None:
        setup = (CLIENT_ROOT / "setup_environment.bat").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("ensure_miniforge.bat", setup)
        self.assertIn("env remove --name", setup)
        self.assertIn("create --name", setup)
        self.assertIn("--override-channels", setup)
        self.assertIn("https://conda.anaconda.org/conda-forge", setup)
        self.assertIn("--strict-channel-priority", setup)
        self.assertIn("https://pypi.org/simple", setup)
        self.assertIn("nvta-taplite-workflow", setup)
        self.assertIn("python -m nvta_taplite_workflow doctor", setup)
        self.assertNotIn("runtime_requirements.txt", setup)
        self.assertNotIn("install_pypi_release.py", setup)
        self.assertNotIn("tos accept", setup)
        self.assertNotIn("conda activate", setup)

    def test_launchers_only_forward_to_the_installed_package(self) -> None:
        assignment = (CLIENT_ROOT / "run_assignment.py").read_text(encoding="utf-8")
        postprocessing = (CLIENT_ROOT / "run_postprocessing.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("nvta_taplite_workflow.assignment_cli", assignment)
        self.assertIn("nvta_taplite_workflow.postprocessing_cli", postprocessing)

        assignment_bat = (CLIENT_ROOT / "run_assignment.bat").read_text(
            encoding="utf-8"
        ).lower()
        postprocessing_bat = (CLIENT_ROOT / "run_postprocessing.bat").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("python -m nvta_taplite_workflow assignment %*", assignment_bat)
        self.assertIn("python -m nvta_taplite_workflow postprocess %*", postprocessing_bat)
        for launcher in (assignment_bat, postprocessing_bat):
            self.assertIn(r"%~dp0setup\find_conda.bat", launcher)
            self.assertNotIn("pushd", launcher)
            self.assertNotIn("cd /d", launcher)

    def test_windows_command_scripts_use_crlf(self) -> None:
        for script in CLIENT_ROOT.rglob("*.bat"):
            data = script.read_bytes()
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), script)

    @unittest.skipUnless(os.name == "nt", "Windows-only launcher simulation")
    def test_assignment_launcher_returns_conda_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            client = root / "client"
            client.mkdir()
            shutil.copy2(CLIENT_ROOT / "run_assignment.bat", client)
            shutil.copytree(CLIENT_ROOT / "setup", client / "setup")

            fake_conda = root / "Miniforge3" / "condabin" / "conda.bat"
            fake_conda.parent.mkdir(parents=True)
            fake_conda.write_bytes(
                b"@echo off\r\n"
                b'if /I "%~1"=="--version" (echo conda 26.3.2& exit /b 0)\r\n'
                b'if /I "%~1"=="run" exit /b %NVTA_FAKE_RUN_EXIT%\r\n'
                b"exit /b 0\r\n"
            )
            environment = os.environ.copy()
            environment["NVTA_MINIFORGE_EXE"] = str(fake_conda)

            environment["NVTA_FAKE_RUN_EXIT"] = "7"
            failed = subprocess.run(
                ["cmd.exe", "/d", "/c", "run_assignment.bat", "--help"],
                cwd=client,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
            )
            self.assertEqual(failed.returncode, 7, failed.stdout + failed.stderr)

            environment["NVTA_FAKE_RUN_EXIT"] = "0"
            succeeded = subprocess.run(
                ["cmd.exe", "/d", "/c", "run_assignment.bat", "--help"],
                cwd=client,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
            )
            self.assertEqual(succeeded.returncode, 0, succeeded.stdout + succeeded.stderr)


if __name__ == "__main__":
    unittest.main()
