from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

import pandas as pd


TIME_DEPENDENT_SPEED_PATTERN = re.compile(
    r"^spd(?:_(?:mph|kmph))?_(?P<hour>\d{1,2}):(?P<minute>\d{2})$",
    flags=re.IGNORECASE,
)


def _clock_minutes(hour: int, minute: int) -> int:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid clock time: {hour:02d}:{minute:02d}")
    return hour * 60 + minute


def parse_period_range_minutes(time_range: str) -> tuple[int, int]:
    try:
        raw_start, raw_end = time_range.split("_", maxsplit=1)
    except ValueError as exc:
        raise ValueError(
            f"Invalid period range {time_range!r}; expected HHMM_HHMM."
        ) from exc

    def parse_clock(value: str) -> int:
        normalized = value.strip().replace(":", "")
        if len(normalized) not in {3, 4} or not normalized.isdigit():
            raise ValueError(
                f"Invalid clock value {value!r} in period range {time_range!r}."
            )
        normalized = normalized.zfill(4)
        return _clock_minutes(int(normalized[:2]), int(normalized[2:]))

    return parse_clock(raw_start), parse_clock(raw_end)


def speed_column_minutes(column_name: str) -> int | None:
    match = TIME_DEPENDENT_SPEED_PATTERN.fullmatch(str(column_name))
    if match is None:
        return None
    return _clock_minutes(int(match.group("hour")), int(match.group("minute")))


def minute_is_in_period(minute: int, time_range: str) -> bool:
    start, end = parse_period_range_minutes(time_range)
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def time_dependent_speed_columns(
    columns: Iterable[str],
    time_range: str | None = None,
) -> list[str]:
    selected: list[str] = []
    for column in columns:
        minute = speed_column_minutes(column)
        if minute is None:
            continue
        if time_range is None or minute_is_in_period(minute, time_range):
            selected.append(column)
    return selected


def period_range_mapping(
    time_periods: Iterable[str],
    period_ranges: Iterable[str],
) -> dict[str, str]:
    normalized_periods = [str(period).lower() for period in time_periods]
    normalized_ranges = list(period_ranges)
    if len(normalized_periods) != len(normalized_ranges):
        raise ValueError("time_periods and period_ranges must have the same length.")
    return dict(zip(normalized_periods, normalized_ranges))


def retain_relevant_speed_columns(
    frame: pd.DataFrame,
    time_range: str,
) -> pd.DataFrame:
    """Keep only 5-minute speed columns owned by this configured period."""
    all_speed_columns = time_dependent_speed_columns(frame.columns)
    relevant_columns = set(time_dependent_speed_columns(frame.columns, time_range))
    unrelated_columns = [
        column for column in all_speed_columns if column not in relevant_columns
    ]
    if not unrelated_columns:
        return frame
    return frame.drop(columns=unrelated_columns)


def merge_relevant_speed_columns(
    base_frame: pd.DataFrame,
    link_performance_by_period: Mapping[str, pd.DataFrame],
    period_ranges: Mapping[str, str],
    key_columns: list[str],
) -> pd.DataFrame:
    """Merge each 5-minute speed column from its configured source period."""
    result = base_frame.copy()
    claimed_columns: dict[str, str] = {}

    for raw_period, period_frame in link_performance_by_period.items():
        period = raw_period.lower()
        if period not in period_ranges:
            raise KeyError(f"Missing configured time range for period {period!r}.")

        speed_columns = time_dependent_speed_columns(
            period_frame.columns,
            period_ranges[period],
        )
        for column in speed_columns:
            previous_period = claimed_columns.get(column)
            if previous_period is not None and previous_period != period:
                raise ValueError(
                    f"5-minute speed column {column!r} belongs to overlapping "
                    f"periods {previous_period!r} and {period!r}."
                )
            claimed_columns[column] = period

        if not speed_columns:
            continue

        missing_keys = [
            column for column in key_columns if column not in period_frame.columns
        ]
        if missing_keys:
            raise KeyError(
                f"Period {period!r} is missing link key column(s): "
                + ", ".join(missing_keys)
            )

        period_speeds = period_frame[key_columns + speed_columns].drop_duplicates(
            subset=key_columns,
            keep="last",
        )
        result = result.drop(
            columns=[column for column in speed_columns if column in result.columns],
            errors="ignore",
        )
        result = result.merge(
            period_speeds,
            on=key_columns,
            how="left",
            validate="one_to_one",
        )

    return result
