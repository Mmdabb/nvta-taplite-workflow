"""Stable multi-command interface installed with the workflow package."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from . import __version__


USAGE = """usage: python -m nvta_taplite_workflow COMMAND [arguments]

commands:
  assignment   Run conversion, TAPLite assignment, smoothing, and aggregation
  postprocess  Run NVTA postprocessing
  doctor       Validate the installed environment and packaged resources
  version      Print the installed workflow version
"""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    command, *command_arguments = arguments
    if command in {"assignment", "run-assignment"}:
        from .assignment_cli import main as assignment_main

        return assignment_main(command_arguments)
    if command in {"postprocess", "postprocessing"}:
        from .postprocessing_cli import main as postprocessing_main

        return postprocessing_main(command_arguments)
    if command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(command_arguments)
    if command in {"version", "--version"}:
        print(__version__)
        return 0

    print(f"Unknown command: {command}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

