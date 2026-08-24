"""Integrated QVDF time-dependent speed smoothing for assignment outputs."""

from __future__ import annotations

import csv
import sys
from argparse import Namespace
from dataclasses import dataclass, field
from pathlib import Path

from .batch import run_batch


__version__ = "1.0.0"


@dataclass(frozen=True)
class SmootherConfig:
    """Configuration for atomic smoothing of period link-performance files."""

    scenario_dir: Path
    periods: list[str]
    workers: str = "auto"
    backup: bool = True
    report: Path | None = None
    audit_links: list[str] = field(default_factory=list)
    chunk_size: int = 0
    speed_decimals: int = 9
    max_five_minute_change: float = 8.0
    rolling_window_intervals: int = 3
    max_rolling_average_change: float = 4.0
    max_acceleration: float = 576.0


def smooth_scenario(config: SmootherConfig) -> dict[str, object]:
    """Smooth and atomically replace ``spd_mph_*`` columns for all periods."""

    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    args = Namespace(
        scenario_dir=Path(config.scenario_dir),
        periods=list(config.periods),
        workers=str(config.workers),
        chunk_size=int(config.chunk_size),
        write_back=True,
        backup=bool(config.backup),
        report=Path(config.report) if config.report is not None else None,
        audit_link=list(config.audit_links),
        max_five_minute_change=float(config.max_five_minute_change),
        rolling_window_intervals=int(config.rolling_window_intervals),
        max_rolling_average_change=float(config.max_rolling_average_change),
        max_acceleration=float(config.max_acceleration),
        speed_decimals=int(config.speed_decimals),
    )
    return run_batch(args)


__all__ = ["SmootherConfig", "smooth_scenario", "__version__"]
