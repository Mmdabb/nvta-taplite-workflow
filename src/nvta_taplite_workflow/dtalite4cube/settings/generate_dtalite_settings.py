from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path
from typing import Any

try:
    from .dtalite_settings_config import (
        ALLOWED_SETTINGS_OVERRIDES,
        DEFAULT_SETTINGS,
        MODE_TYPE_CONFIG,
        MODE_TYPE_FILENAME,
        MODE_TYPE_HEADER,
        SETTINGS_FILENAME,
        SETTINGS_HEADER,
        TIME_PERIODS,
        demand_file_name,
    )
except ImportError:
    from dtalite_settings_config import (
        ALLOWED_SETTINGS_OVERRIDES,
        DEFAULT_SETTINGS,
        MODE_TYPE_CONFIG,
        MODE_TYPE_FILENAME,
        MODE_TYPE_HEADER,
        SETTINGS_FILENAME,
        SETTINGS_HEADER,
        TIME_PERIODS,
        demand_file_name,
    )


logger = logging.getLogger(__name__)


def normalize_period_key(period_key: str) -> str:
    normalized = period_key.lower()
    if normalized not in TIME_PERIODS:
        valid_periods = ", ".join(sorted(TIME_PERIODS))
        raise ValueError(f"Unknown DTALite period '{period_key}'. Expected one of: {valid_periods}")
    return normalized


def normalize_dtalite_period_hours(start_hour: int, end_hour: int) -> tuple[int, int, bool]:
    """
    Normalize DTALite assignment hours.

    DTALite currently cannot handle periods crossing midnight, e.g. 19 -> 6.
    Temporary behavior: truncate cross-midnight periods to start -> 24.

    Returns:
        normalized_start_hour
        normalized_end_hour
        crosses_midnight
    """
    if end_hour < start_hour:
        return start_hour, 24, True
    return start_hour, end_hour, False


def parse_period_time_hours(period_time: str) -> tuple[int, int]:
    try:
        start_text, end_text = period_time.split("_", maxsplit=1)
    except ValueError as exc:
        raise ValueError(f"period_time must use HHMM_HHMM format. Got: {period_time}") from exc

    if len(start_text) != 4 or len(end_text) != 4 or not start_text.isdigit() or not end_text.isdigit():
        raise ValueError(f"period_time must use HHMM_HHMM format. Got: {period_time}")

    start_hour = int(start_text[:2])
    end_hour = int(end_text[:2])
    if not 0 <= start_hour <= 24 or not 0 <= end_hour <= 24:
        raise ValueError(f"period_time hours must be between 0 and 24. Got: {period_time}")
    return start_hour, end_hour


def configure_time_period_hours(time_periods: list[str], period_times: list[str]) -> None:
    if len(time_periods) != len(period_times):
        raise ValueError("time_periods and period_times must have the same length.")

    for raw_period, period_time in zip(time_periods, period_times):
        period_key = normalize_period_key(raw_period)
        start_hour, end_hour = parse_period_time_hours(period_time)
        TIME_PERIODS[period_key]["start_hour"] = start_hour
        TIME_PERIODS[period_key]["end_hour"] = end_hour


def build_settings_row(period_key: str, overrides: dict[str, Any] | None = None) -> list[Any]:
    period_key = normalize_period_key(period_key)
    period = TIME_PERIODS[period_key]
    settings = dict(DEFAULT_SETTINGS)

    if overrides:
        unknown_keys = sorted(set(overrides) - ALLOWED_SETTINGS_OVERRIDES)
        if unknown_keys:
            raise ValueError(f"Unsupported settings override(s): {', '.join(unknown_keys)}")
        settings.update({key: value for key, value in overrides.items() if value is not None})

    original_start_hour = period["start_hour"]
    original_end_hour = period["end_hour"]
    start_hour, end_hour, crosses_midnight = normalize_dtalite_period_hours(original_start_hour, original_end_hour)
    if crosses_midnight:
        logger.warning(
            "Time period %s crosses midnight (%s -> %s). "
            "DTALite settings will temporarily use %s -> 24 only. "
            "The post-midnight portion is not assigned in this run.",
            period_key,
            original_start_hour,
            original_end_hour,
            original_start_hour,
        )

    settings["demand_period_starting_hours"] = start_hour
    settings["demand_period_ending_hours"] = end_hour

    return [settings[field] for field in SETTINGS_HEADER]


def build_mode_type_rows(period_key: str) -> list[list[Any]]:
    period_key = normalize_period_key(period_key)
    rows = []

    for mode in MODE_TYPE_CONFIG[period_key]:
        mode_type = mode["mode_type"]
        rows.append(
            [
                mode["mode_type_id"],
                mode_type,
                mode["name"],
                mode["vot"],
                mode["pce"],
                mode["occ"],
                demand_file_name(mode_type, period_key),
            ]
        )

    return rows


def generate_settings_csv(
    output_dir: str | Path,
    period_key: str,
    overrides: dict[str, Any] | None = None,
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    settings_path = output_path / SETTINGS_FILENAME

    with settings_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(SETTINGS_HEADER)
        writer.writerow(build_settings_row(period_key, overrides))

    return settings_path


def generate_mode_type_csv(output_dir: str | Path, period_key: str) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    mode_type_path = output_path / MODE_TYPE_FILENAME

    with mode_type_path.open("w", newline="", encoding="utf-8") as f:
        # The C++ parser does not trim a trailing CR from the final demand_file
        # field, so CRLF makes it try to open filenames such as "sov_am.csv\r".
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(MODE_TYPE_HEADER)
        writer.writerows(build_mode_type_rows(period_key))

    return mode_type_path


def generate_dtalite_input_files(
    output_dir: str | Path,
    period_key: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Path]:
    return {
        SETTINGS_FILENAME: generate_settings_csv(output_dir, period_key, overrides),
        MODE_TYPE_FILENAME: generate_mode_type_csv(output_dir, period_key),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate current DTALite settings.csv and mode_type.csv files.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--period", required=True, help="One of am, md, pm, nt, or all.")
    parser.add_argument("--number-of-iterations", type=int)
    parser.add_argument("--number-of-processors", type=int)
    parser.add_argument("--route-output", type=int)
    parser.add_argument("--vehicle-output", type=int)
    return parser


def collect_overrides(args: argparse.Namespace) -> dict[str, Any]:
    override_args = {
        "number_of_iterations": args.number_of_iterations,
        "number_of_processors": args.number_of_processors,
        "route_output": args.route_output,
        "vehicle_output": args.vehicle_output,
    }
    return {key: value for key, value in override_args.items() if value is not None}


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    overrides = collect_overrides(args)
    period = args.period.lower()

    if period == "all":
        for period_key in TIME_PERIODS:
            generate_dtalite_input_files(args.output_dir / period_key, period_key, overrides)
        return

    generate_dtalite_input_files(args.output_dir, period, overrides)


if __name__ == "__main__":
    main()
