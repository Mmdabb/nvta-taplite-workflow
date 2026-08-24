"""Stable programmatic entry points for the NVTA workflow."""

from __future__ import annotations

from .dtalite4cube.runner import AssignmentConfig, run_assignment_pipeline
from .postprocessing.runner import PostprocessingConfig, run_postprocessing


def run_assignment(config: AssignmentConfig) -> bool:
    """Run the configured conversion and assignment pipeline."""

    return run_assignment_pipeline(config)


__all__ = [
    "AssignmentConfig",
    "PostprocessingConfig",
    "run_assignment",
    "run_postprocessing",
]

