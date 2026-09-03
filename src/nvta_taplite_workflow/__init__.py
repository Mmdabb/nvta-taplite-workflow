"""Public API for the NVTA TAPLite workflow."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .api import (
    AssignmentConfig,
    PostprocessingConfig,
    run_assignment,
    run_postprocessing,
)

try:
    __version__ = version("nvta-taplite-workflow")
except PackageNotFoundError:
    __version__ = "0.1.0rc2"

__all__ = [
    "AssignmentConfig",
    "PostprocessingConfig",
    "run_assignment",
    "run_postprocessing",
    "__version__",
]
