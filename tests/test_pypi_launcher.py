from __future__ import annotations

import unittest

from nvta_taplite_workflow.dtalite4cube.reproducible_run import _build_dtalite_command


class PypiLauncherTests(unittest.TestCase):
    def test_pypi_launcher_imports_os_for_working_directory(self) -> None:
        command = _build_dtalite_command("pypi")
        self.assertEqual(command[-2], "-c")
        self.assertIn("import os", command[-1])
        self.assertIn("pytaplite.assign(os.getcwd(), in_place=True)", command[-1])


if __name__ == "__main__":
    unittest.main()
