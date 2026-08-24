from __future__ import annotations

import argparse
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .pipeline import (
    get_diff_stats,
    link_performance_preprocess,
    performance_summary,
    time_period_duration,
)
from .pipeline.linkperformance_fieldconfig import LINK_FILENAME, LINK_PERFORMANCE_FILENAME

logger = logging.getLogger(__name__)


@dataclass
class PostprocessingConfig:
    catalog_dir: Path
    scenario_names: list[str]

    performance_stats: bool = True
    link_performance_comparison: bool = False
    bus_delay_analysis: bool = False

    time_periods: list[str] = field(
        default_factory=lambda: ["am", "md", "pm", "nt"]
    )
    time_period_duration_list: list[str] = field(
        default_factory=lambda: ["0600_0900", "0900_1500", "1500_1900", "1900_0600"]
    )

    @classmethod
    def from_dict(cls, data: dict) -> "PostprocessingConfig":
        parsed = data.copy()
        parsed["catalog_dir"] = Path(parsed["catalog_dir"])
        return cls(**parsed)

    def validate(self) -> None:
        if not self.catalog_dir.exists():
            raise FileNotFoundError(f"Catalog directory does not exist: {self.catalog_dir}")

        if not self.scenario_names:
            raise ValueError("scenario_names cannot be empty.")

        if len(self.time_periods) != len(self.time_period_duration_list):
            raise ValueError(
                "time_periods and time_period_duration_list must have the same length."
            )

        if self.link_performance_comparison and len(self.scenario_names) != 2:
            raise ValueError(
                "Exactly two scenario folders are required for comparison."
            )


def resolve_period_output_dirs(scenario_dir: Path, time_periods: list[str]) -> dict[str, Path]:
    period_dirs: dict[str, Path] = {}
    for period in time_periods:
        period_key = period.lower()
        period_dir = scenario_dir / period_key
        if (period_dir / LINK_PERFORMANCE_FILENAME).exists() and (period_dir / LINK_FILENAME).exists():
            period_dirs[period_key] = period_dir
            continue

        nested_legacy_backmapped_period_dir = period_dir / f"{period_key}_origIDs"
        if (
            (nested_legacy_backmapped_period_dir / LINK_PERFORMANCE_FILENAME).exists()
            and (
                (nested_legacy_backmapped_period_dir / LINK_FILENAME).exists()
                or (period_dir / LINK_FILENAME).exists()
            )
        ):
            period_dirs[period_key] = nested_legacy_backmapped_period_dir
            continue

        sibling_legacy_backmapped_period_dir = scenario_dir / f"{period_key}_origIDs"
        if (
            (sibling_legacy_backmapped_period_dir / LINK_PERFORMANCE_FILENAME).exists()
            and (
                (sibling_legacy_backmapped_period_dir / LINK_FILENAME).exists()
                or (period_dir / LINK_FILENAME).exists()
            )
        ):
            period_dirs[period_key] = sibling_legacy_backmapped_period_dir
            continue

        legacy_dir = scenario_dir / "Outputs" / "DTALite"
        legacy_lp = legacy_dir / f"link_performance_{period_key}.csv"
        legacy_link = legacy_dir / f"link_{period_key}.csv"
        if legacy_lp.exists() and legacy_link.exists():
            period_dirs[period_key] = legacy_dir
            continue

        flat_lp = scenario_dir / f"link_performance_{period_key}.csv"
        flat_link = scenario_dir / f"link_{period_key}.csv"
        if flat_lp.exists() and flat_link.exists():
            fallback_dir = scenario_dir / "Outputs" / "DTALite"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(flat_lp, fallback_dir / flat_lp.name)
            shutil.copy2(flat_link, fallback_dir / flat_link.name)
            period_dirs[period_key] = fallback_dir
            logger.info("Copied legacy flat files for %s into %s", period_key, fallback_dir)
            continue

        raise FileNotFoundError(
            f"Could not locate postprocessing files for {period_key}. Expected "
            f"{period_dir / LINK_PERFORMANCE_FILENAME} and {period_dir / LINK_FILENAME}."
        )
    return period_dirs


def resolve_dtalite_workdir(scenario_dir: Path) -> Path:
    candidate_dtalite = scenario_dir / "Outputs" / "DTALite"
    if candidate_dtalite.exists():
        return candidate_dtalite
    return scenario_dir


def prepare_dtalite_outputs(scenario_dir: Path, time_periods: list[str]) -> Path:
    dtalite_workdir = resolve_dtalite_workdir(scenario_dir)
    resolve_period_output_dirs(dtalite_workdir, time_periods)
    return dtalite_workdir


def preprocess_and_summarize_scenario(
    *,
    scenario_dir: Path,
    time_periods: list[str],
    period_range_list: list[str],
    time_duration_dict: dict,
):
    network_path = prepare_dtalite_outputs(scenario_dir, time_periods)
    logger.info("Generating performance summary for: %s", network_path)

    combined_link_performance = link_performance_preprocess(
        str(network_path),
        time_periods,
        period_range_list=period_range_list,
    )

    performance_summary(
        combined_link_performance,
        str(network_path),
        time_duration_dict,
    )

    return combined_link_performance


def run_comparison(config: PostprocessingConfig, time_duration_dict: dict) -> None:
    processed_link_performance_dict = {}

    for scenario in config.scenario_names:
        scenario_dir = config.catalog_dir / scenario

        if not scenario_dir.exists():
            raise FileNotFoundError(f"Scenario directory does not exist: {scenario_dir}")

        logger.info("Preprocessing link performance for comparison: %s", scenario_dir)

        processed_link_performance_dict[scenario] = preprocess_and_summarize_scenario(
            scenario_dir=scenario_dir,
            time_periods=config.time_periods,
            period_range_list=config.time_period_duration_list,
            time_duration_dict=time_duration_dict,
        )

    bd_net = config.scenario_names[0]
    nb_net = config.scenario_names[1]

    output_path = config.catalog_dir / f"{bd_net}_VS_{nb_net}"
    output_path.mkdir(parents=True, exist_ok=True)

    logger.info("Creating comparison stats: %s vs %s", bd_net, nb_net)

    get_diff_stats(
        str(output_path),
        processed_link_performance_dict[bd_net],
        processed_link_performance_dict[nb_net],
        config.time_periods,
    )


def run_performance_stats(config: PostprocessingConfig, time_duration_dict: dict) -> None:
    for scenario in config.scenario_names:
        scenario_dir = config.catalog_dir / scenario

        if not scenario_dir.exists():
            raise FileNotFoundError(f"Scenario directory does not exist: {scenario_dir}")

        preprocess_and_summarize_scenario(
            scenario_dir=scenario_dir,
            time_periods=config.time_periods,
            period_range_list=config.time_period_duration_list,
            time_duration_dict=time_duration_dict,
        )


def run_postprocessing(config: PostprocessingConfig) -> None:
    config.validate()
    logger.info("Running postprocessing in catalog: %s", config.catalog_dir)

    time_duration_dict = time_period_duration(
        config.time_periods,
        config.time_period_duration_list,
    )

    if config.link_performance_comparison:
        run_comparison(config, time_duration_dict)
    elif config.performance_stats:
        run_performance_stats(config, time_duration_dict)

    if config.bus_delay_analysis:
        logger.warning("bus_delay_analysis is not implemented yet.")

    logger.info("Postprocessing complete.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DTALite postprocessing for one or more scenarios."
    )

    parser.add_argument("--catalog-dir", required=True, help="Root folder containing scenario folders")
    parser.add_argument("--scenario-names", nargs="+", required=True, help="Scenario folder names")

    parser.add_argument("--performance-stats", action="store_true")
    parser.add_argument("--link-performance-comparison", action="store_true")
    parser.add_argument("--bus-delay-analysis", action="store_true")

    parser.add_argument(
        "--time-periods",
        nargs="+",
        default=["am", "md", "pm", "nt"],
    )
    parser.add_argument(
        "--time-period-duration-list",
        nargs="+",
        default=["0600_0900", "0900_1500", "1500_1900", "1900_0600"],
    )

    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    parser = build_arg_parser()
    args = parser.parse_args()

    config = PostprocessingConfig(
        catalog_dir=Path(args.catalog_dir),
        scenario_names=args.scenario_names,
        performance_stats=args.performance_stats,
        link_performance_comparison=args.link_performance_comparison,
        bus_delay_analysis=args.bus_delay_analysis,
        time_periods=args.time_periods,
        time_period_duration_list=args.time_period_duration_list,
    )

    run_postprocessing(config)


if __name__ == "__main__":
    main()
