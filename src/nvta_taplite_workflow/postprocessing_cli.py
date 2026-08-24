from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from nvta_taplite_workflow.postprocessing.runner import PostprocessingConfig, run_postprocessing
from nvta_taplite_workflow.dtalite4cube.run_logging import install_root_log_capture


logger = logging.getLogger(__name__)

DEFAULT_TIME_PERIODS = ["am", "md", "pm", "nt"]
DEFAULT_PERIOD_TIMES = ["0600_0900", "0900_1500", "1500_1900", "1900_0600"]


def str_to_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value

    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False

    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def parse_list(values: list[str]) -> list[str]:
    parsed: list[str] = []
    for value in values:
        parsed.extend(part.strip() for part in value.split(",") if part.strip())
    return parsed


def resolve_scenario_root(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if path.name == "DTALite" and path.parent.name == "Outputs":
        return path.parent.parent
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DTALite postprocessing from Cube scenario folders."
    )
    parser.add_argument("scenario_or_root_dir", help="Scenario folder, or root folder for comparisons")
    parser.add_argument("--scenario-a", help="First scenario folder name for comparison")
    parser.add_argument("--scenario-b", help="Second scenario folder name for comparison")
    parser.add_argument("--performance-stats", type=str_to_bool, default=True)
    parser.add_argument("--link-performance-comparison", type=str_to_bool, default=False)
    parser.add_argument("--bus-delay-analysis", type=str_to_bool, default=False)
    parser.add_argument("--time-periods", nargs="+", default=DEFAULT_TIME_PERIODS)
    parser.add_argument("--period-times", nargs="+", default=DEFAULT_PERIOD_TIMES)
    return parser


def build_config(args: argparse.Namespace) -> PostprocessingConfig:
    scenario_or_root_dir = Path(args.scenario_or_root_dir).expanduser().resolve()
    time_periods = parse_list(args.time_periods)
    period_times = parse_list(args.period_times)

    if args.link_performance_comparison:
        if not args.scenario_a or not args.scenario_b:
            raise ValueError(
                "--scenario-a and --scenario-b are required when --link-performance-comparison is true."
            )
        catalog_dir = scenario_or_root_dir
        scenario_names = [args.scenario_a, args.scenario_b]
    else:
        scenario_root = resolve_scenario_root(args.scenario_or_root_dir)
        catalog_dir = scenario_root.parent
        scenario_names = [scenario_root.name]

    return PostprocessingConfig(
        catalog_dir=catalog_dir,
        scenario_names=scenario_names,
        performance_stats=args.performance_stats,
        link_performance_comparison=args.link_performance_comparison,
        bus_delay_analysis=args.bus_delay_analysis,
        time_periods=time_periods,
        time_period_duration_list=period_times,
    )


def validate_postprocessing_inputs(config: PostprocessingConfig) -> None:
    if not config.catalog_dir.exists():
        raise FileNotFoundError(f"Catalog directory does not exist: {config.catalog_dir}")
    if len(config.time_periods) != len(config.time_period_duration_list):
        raise ValueError("time_periods and period_times must have the same length.")

    missing = [name for name in config.scenario_names if not (config.catalog_dir / name).is_dir()]
    if missing:
        raise FileNotFoundError(
            "Scenario folder(s) not found under "
            f"{config.catalog_dir}: {', '.join(missing)}"
        )


def log_config(config: PostprocessingConfig) -> None:
    logger.info("Catalog directory: %s", config.catalog_dir)
    logger.info("Scenario names: %s", config.scenario_names)
    logger.info("Time periods: %s", config.time_periods)
    logger.info("Period times: %s", config.time_period_duration_list)
    logger.info(
        "Enabled stages: performance_stats=%s, link_performance_comparison=%s, bus_delay_analysis=%s",
        config.performance_stats,
        config.link_performance_comparison,
        config.bus_delay_analysis,
    )


def _log_directories(argv: Sequence[str]) -> list[Path]:
    if argv and not str(argv[0]).startswith("-"):
        scenario = resolve_scenario_root(str(argv[0]))
        dtalite_output = scenario / "Outputs" / "DTALite"
        if dtalite_output.is_dir():
            return [dtalite_output / "logs", Path.cwd().resolve() / "logs"]
        return [scenario / "logs", Path.cwd().resolve() / "logs"]
    return [Path.cwd().resolve() / "logs"]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    install_root_log_capture(
        "run_postprocessing",
        log_dirs=_log_directories(arguments),
    )
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args(arguments)
    config = build_config(args)
    validate_postprocessing_inputs(config)
    log_config(config)
    run_postprocessing(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
