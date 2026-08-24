"""Reconstruct TAPLite QVDF profiles and apply five-minute rate limiters.

The TAPLite reconstruction mirrors ``kernel/src/TAPLite.cpp`` exactly.  The
enforced profile is then produced sample by sample.  Each desired five-minute
increment is projected onto the intersection of a single-step limit, a rolling
mean-absolute-change budget, and a change-of-increment (acceleration) limit.
Samples already inside every limit are untouched; violating samples sit on the
most restrictive active bound.  An adjusted period endpoint is carried into
the next period as its start anchor.  Finally, each adjacent period boundary is
rewritten as a monotone cubic-Hermite connection between the sample immediately
before the prior period endpoint and the sample immediately after the next
period start.  The two shoulder samples stay fixed and only the two samples
inside that connector are changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable


STEP_MINUTES = 5
STEP_HOURS = STEP_MINUTES / 60.0


@dataclass(frozen=True)
class ConnectorLimits:
    max_five_minute_change_mph: float = 8.0
    rolling_window_intervals: int = 3
    max_rolling_average_change_mph: float = 4.0
    max_acceleration_mph_per_hour2: float = 576.0


@dataclass(frozen=True)
class QvdfInputs:
    period: str
    period_start: float
    period_end: float
    volume: float
    lanes: float
    lane_capacity: float
    vdf_plf: float
    free_speed: float
    length_miles: float
    cutoff_speed: float
    vdf_alpha: float
    vdf_beta: float
    q_cp: float
    q_cd: float
    q_n: float
    q_s: float
    vdf_type: int
    profile_mode: int
    observed_t0: float | None
    observed_t2: float | None
    observed_t3: float | None
    start_anchor: float | None
    end_anchor: float | None
    assigned_speed: float


@dataclass(frozen=True)
class QvdfScalars:
    incoming_demand: float
    doc: float
    p: float
    t0: float
    t2: float
    t3: float
    vt2: float
    boundary_speed: float
    congestion_ref_speed: float
    avg_queue_speed: float


@dataclass
class LimiterState:
    """State carried through AM, MD, and PM without resetting the limiter."""

    last_speed: float | None = None
    recent_deltas: list[float] = field(default_factory=list)


def _optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _float(row: dict[str, str], name: str, default: float) -> float:
    value = row.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _read_row(path: Path, key: str, value: str) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get(key) == value:
                return row
    raise ValueError(f"{key}={value} not found in {path}")


def _read_settings(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.DictReader(handle))


def inputs_from_rows(
    period: str,
    link: dict[str, str],
    performance: dict[str, str],
    settings: dict[str, str],
) -> QvdfInputs:
    """Build compact QVDF inputs from already-loaded scenario rows."""

    free_speed = _float(link, "vdf_free_speed_mph", _float(link, "free_speed", 60.0) / 1.609)
    cutoff_speed = _float(link, "cutoff_speed", 0.75 * free_speed)
    length_miles = _float(link, "vdf_length_mi", _float(link, "length_in_mile", 0.0))
    assigned_speed = _float(performance, "speed_mph", free_speed)

    return QvdfInputs(
        period=period,
        period_start=float(settings["demand_period_starting_hours"]),
        period_end=float(settings["demand_period_ending_hours"]),
        volume=float(performance["volume"]),
        lanes=_float(link, "lanes", 1.0),
        lane_capacity=_float(link, "capacity", 1800.0),
        vdf_plf=_float(link, "vdf_plf", 1.0),
        free_speed=free_speed,
        length_miles=length_miles,
        cutoff_speed=cutoff_speed,
        vdf_alpha=_float(link, "vdf_alpha", 0.15),
        vdf_beta=_float(link, "vdf_beta", 4.0),
        q_cp=_float(link, "vdf_cp", 0.28125),
        q_cd=_float(link, "vdf_cd", 1.0),
        q_n=_float(link, "vdf_n", 1.0),
        q_s=_float(link, "vdf_s", 4.0),
        vdf_type=int(float(link.get("vdf_type") or 0)),
        profile_mode=int(float(link.get("qvdf_profile_mode") or -1)),
        observed_t0=_optional_float(link.get("t0_hour")),
        observed_t2=_optional_float(link.get("t2_hour")),
        observed_t3=_optional_float(link.get("t3_hour")),
        start_anchor=_optional_float(link.get("qvdf_start_speed_mph")),
        end_anchor=_optional_float(link.get("qvdf_end_speed_mph")),
        assigned_speed=assigned_speed,
    )


def load_inputs(period_dir: Path, period: str, link_id: str) -> tuple[QvdfInputs, dict[str, str]]:
    link = _read_row(period_dir / "link.csv", "link_id", link_id)
    performance = _read_row(period_dir / "link_performance.csv", "link_id", link_id)
    settings = _read_settings(period_dir / "settings.csv")
    inputs = inputs_from_rows(period, link, performance, settings)
    return inputs, performance


def _smoothstep(value: float) -> float:
    position = min(1.0, max(0.0, value))
    return position * position * (3.0 - 2.0 * position)


def _minutes(hour: float) -> int:
    return int(hour * 60.0)


def _time_label(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _profile_minutes(inputs: QvdfInputs) -> list[int]:
    start = _minutes(inputs.period_start)
    end = _minutes(inputs.period_end)
    return list(range(start, end, STEP_MINUTES))


def _qvdf_is_generated(inputs: QvdfInputs) -> bool:
    if inputs.volume <= 0.0 or inputs.profile_mode == 0:
        return False
    if inputs.profile_mode == 1:
        return True
    if inputs.profile_mode == 2:
        return inputs.observed_t2 is not None
    return inputs.vdf_type == 2 or inputs.observed_t2 is not None


def build_raw_qvdf(inputs: QvdfInputs) -> tuple[list[int], list[float], QvdfScalars]:
    period_hours = max(inputs.period_end - inputs.period_start, 0.001)
    lane_hourly_volume = inputs.volume / max(0.01, inputs.lanes) / period_hours
    incoming_demand = lane_hourly_volume / max(0.0001, inputs.vdf_plf)
    doc = incoming_demand / max(0.1, inputs.lane_capacity)

    q_alpha = inputs.vdf_alpha
    q_beta = inputs.vdf_beta
    if inputs.vdf_type not in (0, 2):
        q_alpha, q_beta = 0.15, 4.0

    congestion_ref_speed = inputs.cutoff_speed
    if doc < 1.0:
        congestion_ref_speed = (1.0 - doc) * inputs.free_speed + doc * inputs.cutoff_speed
    avg_queue_speed = congestion_ref_speed / (1.0 + q_alpha * doc**q_beta)
    boundary_speed = max(congestion_ref_speed, avg_queue_speed)
    p = inputs.q_cd * doc**inputs.q_n
    vt2 = inputs.cutoff_speed / max(0.001, inputs.q_cp * p**inputs.q_s + 1.0)
    t2 = inputs.observed_t2
    if t2 is None:
        t2 = (inputs.period_start + inputs.period_end) / 2.0

    observed_left_fraction = 0.5
    if (
        inputs.observed_t0 is not None
        and inputs.observed_t2 is not None
        and inputs.observed_t3 is not None
        and inputs.observed_t0 < inputs.observed_t2 < inputs.observed_t3
    ):
        observed_duration = inputs.observed_t3 - inputs.observed_t0
        observed_left_fraction = (inputs.observed_t2 - inputs.observed_t0) / max(1e-6, observed_duration)
        observed_left_fraction = min(0.95, max(0.05, observed_left_fraction))

    t0 = max(inputs.period_start, t2 - observed_left_fraction * p)
    t3 = min(inputs.period_end, t2 + (1.0 - observed_left_fraction) * p)
    rtt = inputs.length_miles / max(0.01, congestion_ref_speed)
    wt2 = inputs.length_miles / max(0.001, vt2) - rtt

    minutes = _profile_minutes(inputs)
    speeds: list[float] = []
    for minute in minutes:
        t = minute / 60.0
        if t0 <= t <= t3:
            window_span = t3 - t0
            queue_shape = 0.0
            if window_span > 1e-9:
                x = min(1.0, max(0.0, (t - t0) / window_span))
                peak_fraction = min(1.0, max(0.0, (t2 - t0) / window_span))
                if peak_fraction <= 1e-9:
                    queue_shape = (1.0 - x) ** 4.0
                elif peak_fraction >= 1.0 - 1e-9:
                    queue_shape = x**4.0
                else:
                    left_exponent = 4.0 * peak_fraction
                    right_exponent = 4.0 * (1.0 - peak_fraction)
                    queue_shape = (
                        (x / peak_fraction) ** left_exponent
                        * ((1.0 - x) / (1.0 - peak_fraction)) ** right_exponent
                    )
            elif abs(t - t2) <= 1e-9:
                queue_shape = 1.0
            queue_shape = min(1.0, max(0.0, queue_shape))
            speed = inputs.length_miles / (wt2 * queue_shape + rtt)
        elif t < t0:
            factor = (t - inputs.period_start) / max(0.001, t0 - inputs.period_start)
            smooth = _smoothstep(factor)
            speed = (1.0 - smooth) * inputs.free_speed + smooth * boundary_speed
        else:
            factor = (t - t3) / max(0.001, inputs.period_end - t3)
            smooth = _smoothstep(factor)
            speed = (1.0 - smooth) * boundary_speed + smooth * inputs.free_speed
        speeds.append(speed)

    scalars = QvdfScalars(
        incoming_demand=incoming_demand,
        doc=doc,
        p=p,
        t0=t0,
        t2=t2,
        t3=t3,
        vt2=vt2,
        boundary_speed=boundary_speed,
        congestion_ref_speed=congestion_ref_speed,
        avg_queue_speed=avg_queue_speed,
    )
    return minutes, speeds, scalars


def _hermite_value(
    start_speed: float,
    end_speed: float,
    start_slope: float,
    end_slope: float,
    span_hours: float,
    factor: float,
) -> float:
    position = min(1.0, max(0.0, factor))
    position2 = position * position
    position3 = position2 * position
    h00 = 2.0 * position3 - 3.0 * position2 + 1.0
    h10 = position3 - 2.0 * position2 + position
    h01 = -2.0 * position3 + 3.0 * position2
    h11 = position3 - position2
    value = (
        h00 * start_speed
        + h10 * span_hours * start_slope
        + h01 * end_speed
        + h11 * span_hours * end_slope
    )
    return min(max(start_speed, end_speed), max(min(start_speed, end_speed), value))


def _candidate_metrics(values: list[float], start_index: int, end_index: int) -> dict[str, float]:
    first = max(0, start_index - 1)
    window = values[first : end_index + 1]
    steps = [window[index + 1] - window[index] for index in range(len(window) - 1)]
    slopes = [step / STEP_HOURS for step in steps]
    accelerations = [
        (slopes[index + 1] - slopes[index]) / STEP_HOURS
        for index in range(len(slopes) - 1)
    ]
    return {
        "max_step_mph": max((abs(value) for value in steps), default=0.0),
        "max_slope_mph_per_hour": max((abs(value) for value in slopes), default=0.0),
        "max_acceleration_mph_per_hour2": max(
            (abs(value) for value in accelerations), default=0.0
        ),
        "roughness": sum(value * value for value in accelerations),
    }


def _apply_taplite_anchors(
    inputs: QvdfInputs,
    minutes: list[int],
    raw_speeds: list[float],
    scalars: QvdfScalars,
) -> tuple[list[float], list[dict[str, object]]]:
    speeds = raw_speeds.copy()
    diagnostics: list[dict[str, object]] = []
    start_minute = minutes[0]
    last_minute = minutes[-1]
    pivot_hour = min(last_minute / 60.0, max(start_minute / 60.0, scalars.t2))
    anchor_margin = max(2.0, 0.10 * max(0.0, scalars.boundary_speed - scalars.vt2))
    has_observed_t2 = inputs.observed_t2 is not None

    def is_low(anchor: float) -> bool:
        return (
            has_observed_t2
            and anchor < scalars.boundary_speed
            and anchor <= scalars.vt2 + anchor_margin
        )

    def is_hermite(anchor: float) -> bool:
        return (
            has_observed_t2
            and anchor > scalars.vt2 + anchor_margin
            and anchor < scalars.boundary_speed
        )

    if inputs.start_anchor is not None:
        anchor = inputs.start_anchor
        pivot_indices = [index for index, minute in enumerate(minutes) if minute / 60.0 <= pivot_hour]
        if is_low(anchor):
            span = pivot_hour - start_minute / 60.0
            for index in pivot_indices:
                smooth = _smoothstep((minutes[index] / 60.0 - start_minute / 60.0) / max(1e-9, span))
                speeds[index] = (1.0 - smooth) * anchor + smooth * scalars.vt2
            diagnostics.append({"side": "start", "method": "low-anchor-smoothstep"})
        elif is_hermite(anchor):
            candidates: list[tuple[float, int, list[float], dict[str, float]]] = []
            for join_index in pivot_indices[1:]:
                join_speed = speeds[join_index]
                if join_speed >= anchor:
                    continue
                span_hours = (minutes[join_index] - start_minute) / 60.0
                secant = (join_speed - anchor) / span_hours
                next_index = min(len(minutes) - 1, join_index + 1)
                slope_span = (minutes[next_index] - minutes[join_index]) / 60.0
                join_slope = (
                    (speeds[next_index] - join_speed) / slope_span if slope_span > 1e-9 else 0.0
                )
                if join_slope > 0.0 or join_slope / secant > 3.0:
                    continue
                candidate = speeds.copy()
                for index in range(0, join_index + 1):
                    factor = (minutes[index] - start_minute) / (minutes[join_index] - start_minute)
                    candidate[index] = _hermite_value(
                        anchor, join_speed, 0.0, join_slope, span_hours, factor
                    )
                metrics = _candidate_metrics(candidate, 0, join_index)
                candidates.append((metrics["roughness"], join_index, candidate, metrics))
                break
            if candidates:
                _, join_index, speeds, metrics = candidates[0]
                diagnostics.append(
                    {
                        "side": "start",
                        "method": "taplite-hermite",
                        "join_time": _time_label(minutes[join_index]),
                        "connector_intervals": join_index,
                        "valid_candidates": len(candidates),
                        **metrics,
                    }
                )
            else:
                diagnostics.append({"side": "start", "method": "historical-blend-no-valid-hermite"})
                _apply_start_blend(speeds, raw_speeds, minutes, pivot_hour, anchor)
        else:
            diagnostics.append({"side": "start", "method": "historical-blend"})
            _apply_start_blend(speeds, raw_speeds, minutes, pivot_hour, anchor)
        speeds[0] = anchor

    if inputs.end_anchor is not None:
        anchor = inputs.end_anchor
        pivot_indices = [index for index, minute in enumerate(minutes) if minute / 60.0 >= pivot_hour]
        if is_low(anchor):
            span = last_minute / 60.0 - pivot_hour
            for index in pivot_indices:
                smooth = _smoothstep((minutes[index] / 60.0 - pivot_hour) / max(1e-9, span))
                speeds[index] = (1.0 - smooth) * scalars.vt2 + smooth * anchor
            diagnostics.append({"side": "end", "method": "low-anchor-smoothstep"})
        elif is_hermite(anchor):
            candidates: list[tuple[float, int, list[float], dict[str, float]]] = []
            for join_index in reversed(pivot_indices[:-1]):
                join_speed = speeds[join_index]
                if join_speed >= anchor:
                    continue
                span_hours = (last_minute - minutes[join_index]) / 60.0
                secant = (anchor - join_speed) / span_hours
                if minutes[join_index] > pivot_hour * 60.0 + 1e-9:
                    adjacent_index = max(0, join_index - 1)
                    join_slope = (join_speed - speeds[adjacent_index]) / STEP_HOURS
                else:
                    adjacent_index = min(len(minutes) - 1, join_index + 1)
                    join_slope = (speeds[adjacent_index] - join_speed) / STEP_HOURS
                if join_slope < 0.0 or join_slope / secant > 3.0:
                    continue
                candidate = speeds.copy()
                for index in range(join_index, len(minutes)):
                    factor = (minutes[index] - minutes[join_index]) / (last_minute - minutes[join_index])
                    candidate[index] = _hermite_value(
                        join_speed, anchor, join_slope, 0.0, span_hours, factor
                    )
                metrics = _candidate_metrics(candidate, join_index, len(minutes) - 1)
                candidates.append((metrics["roughness"], join_index, candidate, metrics))
                break
            if candidates:
                _, join_index, speeds, metrics = candidates[0]
                diagnostics.append(
                    {
                        "side": "end",
                        "method": "taplite-hermite",
                        "join_time": _time_label(minutes[join_index]),
                        "connector_intervals": len(minutes) - 1 - join_index,
                        "valid_candidates": len(candidates),
                        **metrics,
                    }
                )
            else:
                diagnostics.append({"side": "end", "method": "historical-blend-no-valid-hermite"})
                _apply_end_blend(speeds, raw_speeds, minutes, pivot_hour, anchor)
        else:
            diagnostics.append({"side": "end", "method": "historical-blend"})
            _apply_end_blend(speeds, raw_speeds, minutes, pivot_hour, anchor)
        speeds[-1] = anchor

    return speeds, diagnostics


def _apply_start_blend(
    speeds: list[float],
    raw_speeds: list[float],
    minutes: list[int],
    pivot_hour: float,
    anchor: float,
) -> None:
    start_hour = minutes[0] / 60.0
    span = pivot_hour - start_hour
    for index, minute in enumerate(minutes):
        t = minute / 60.0
        if t > pivot_hour:
            continue
        observed_weight = 1.0 - _smoothstep((t - start_hour) / max(1e-9, span))
        speeds[index] = observed_weight * anchor + (1.0 - observed_weight) * raw_speeds[index]


def _apply_end_blend(
    speeds: list[float],
    raw_speeds: list[float],
    minutes: list[int],
    pivot_hour: float,
    anchor: float,
) -> None:
    last_hour = minutes[-1] / 60.0
    span = last_hour - pivot_hour
    for index, minute in enumerate(minutes):
        t = minute / 60.0
        if t < pivot_hour:
            continue
        observed_weight = _smoothstep((t - pivot_hour) / max(1e-9, span))
        speeds[index] = (1.0 - observed_weight) * raw_speeds[index] + observed_weight * anchor


def build_profile(
    inputs: QvdfInputs,
) -> tuple[list[int], list[float], QvdfScalars | None, list[dict[str, object]]]:
    if _qvdf_is_generated(inputs):
        minutes, raw, scalars = build_raw_qvdf(inputs)
        speeds, diagnostics = _apply_taplite_anchors(
            inputs,
            minutes,
            raw,
            scalars,
        )
        return minutes, speeds, scalars, diagnostics

    minutes = _profile_minutes(inputs)
    start_speed = inputs.start_anchor if inputs.start_anchor is not None else inputs.assigned_speed
    end_speed = inputs.end_anchor if inputs.end_anchor is not None else inputs.assigned_speed
    span = max(1, minutes[-1] - minutes[0])
    speeds = []
    for minute in minutes:
        smooth = _smoothstep((minute - minutes[0]) / span)
        speeds.append((1.0 - smooth) * start_speed + smooth * end_speed)
    return minutes, speeds, None, [{"side": "both", "method": "observed-only-smoothstep"}]


def _rate_limiter(
    minutes: list[int],
    targets: list[float],
    limits: ConnectorLimits,
    state: LimiterState,
    *,
    flatten_next_boundary: bool,
) -> tuple[list[float], dict[str, object], list[dict[str, object]]]:
    """Project each desired increment onto all active rate-limit intervals.

    The rolling constraint uses the mean absolute five-minute change, not the
    net secant, so an up/down oscillation cannot cancel itself.  At an adjacent
    period boundary the next first sample is carried flat; the last increment
    is therefore also limited so it can decelerate to zero without violating
    the acceleration guard.
    """

    if not minutes or len(minutes) != len(targets):
        raise ValueError("rate limiter requires aligned non-empty samples")
    if limits.rolling_window_intervals < 1:
        raise ValueError("rolling_window_intervals must be at least 1")
    if limits.max_five_minute_change_mph <= 0.0:
        raise ValueError("max_five_minute_change_mph must be positive")
    if limits.max_rolling_average_change_mph <= 0.0:
        raise ValueError("max_rolling_average_change_mph must be positive")
    if limits.max_acceleration_mph_per_hour2 <= 0.0:
        raise ValueError("max_acceleration_mph_per_hour2 must be positive")

    acceleration_delta_limit = (
        limits.max_acceleration_mph_per_hour2 * STEP_HOURS * STEP_HOURS
    )
    window = limits.rolling_window_intervals
    adjusted: list[float] = []
    sample_diagnostics: list[dict[str, object]] = []
    local_deltas: list[float] = []
    local_accelerations: list[float] = []
    local_rolling_averages: list[float] = []

    if state.last_speed is None:
        first_speed = targets[0]
        boundary_acceleration = 0.0
    else:
        first_speed = state.last_speed
        previous_delta = state.recent_deltas[-1] if state.recent_deltas else 0.0
        boundary_acceleration = abs(previous_delta) / (STEP_HOURS * STEP_HOURS)
        if boundary_acceleration > limits.max_acceleration_mph_per_hour2 + 1e-7:
            raise AssertionError(
                "period boundary cannot flatten without violating acceleration limit"
            )
        state.recent_deltas.append(0.0)
        local_deltas.append(0.0)
        local_accelerations.append(boundary_acceleration)
        if len(state.recent_deltas) >= window:
            local_rolling_averages.append(
                sum(abs(value) for value in state.recent_deltas[-window:]) / window
            )

    adjusted.append(first_speed)
    state.last_speed = first_speed
    sample_diagnostics.append(
        {
            "minute": minutes[0],
            "time": _time_label(minutes[0]),
            "target_speed_mph": targets[0],
            "adjusted_speed_mph": first_speed,
            "desired_change_mph": targets[0] - first_speed,
            "applied_change_mph": 0.0,
            "active_constraints": (
                ["carried-period-anchor"]
                if abs(targets[0] - first_speed) > 1e-9
                else []
            ),
        }
    )

    for index in range(1, len(targets)):
        previous_speed = adjusted[-1]
        previous_delta = state.recent_deltas[-1] if state.recent_deltas else 0.0
        desired_delta = targets[index] - previous_speed
        constraint_intervals: list[tuple[str, float, float]] = [
            (
                "five-minute-change",
                -limits.max_five_minute_change_mph,
                limits.max_five_minute_change_mph,
            ),
            (
                "acceleration",
                previous_delta - acceleration_delta_limit,
                previous_delta + acceleration_delta_limit,
            ),
        ]

        if len(state.recent_deltas) >= window - 1:
            prior_window = (
                state.recent_deltas[-(window - 1) :]
                if window > 1
                else []
            )
            remaining_variation = max(
                0.0,
                window * limits.max_rolling_average_change_mph
                - sum(abs(value) for value in prior_window),
            )
            constraint_intervals.append(
                ("rolling-average-change", -remaining_variation, remaining_variation)
            )

        if flatten_next_boundary and index == len(targets) - 1:
            constraint_intervals.append(
                (
                    "next-period-flat-start",
                    -acceleration_delta_limit,
                    acceleration_delta_limit,
                )
            )

        lower = max(item[1] for item in constraint_intervals)
        upper = min(item[2] for item in constraint_intervals)
        if lower > upper + 1e-9:
            raise ValueError(
                f"rate limiter constraints are infeasible at {_time_label(minutes[index])}: "
                f"lower={lower:.6f}, upper={upper:.6f}"
            )
        if lower > upper:
            midpoint = (lower + upper) / 2.0
            lower = midpoint
            upper = midpoint

        applied_delta = min(upper, max(lower, desired_delta))
        viability_limited = False
        if index < len(targets) - 1:

            def next_interval(candidate_delta: float) -> tuple[float, float]:
                future_constraints: list[tuple[float, float]] = [
                    (
                        -limits.max_five_minute_change_mph,
                        limits.max_five_minute_change_mph,
                    ),
                    (
                        candidate_delta - acceleration_delta_limit,
                        candidate_delta + acceleration_delta_limit,
                    ),
                ]
                future_recent = [*state.recent_deltas, candidate_delta]
                if len(future_recent) >= window - 1:
                    future_prior_window = (
                        future_recent[-(window - 1) :] if window > 1 else []
                    )
                    future_remaining_variation = max(
                        0.0,
                        window * limits.max_rolling_average_change_mph
                        - sum(abs(value) for value in future_prior_window),
                    )
                    future_constraints.append(
                        (-future_remaining_variation, future_remaining_variation)
                    )
                if flatten_next_boundary and index + 1 == len(targets) - 1:
                    future_constraints.append(
                        (-acceleration_delta_limit, acceleration_delta_limit)
                    )
                return (
                    max(item[0] for item in future_constraints),
                    min(item[1] for item in future_constraints),
                )

            def next_is_feasible(candidate_delta: float) -> bool:
                future_lower, future_upper = next_interval(candidate_delta)
                return future_lower <= future_upper + 1e-9

            if not next_is_feasible(applied_delta):
                # The feasible projection in one dimension is an interval.  Its
                # point nearest zero is guaranteed to be the safest rolling and
                # acceleration choice whenever a continuation exists.
                feasible_delta = min(upper, max(lower, 0.0))
                if not next_is_feasible(feasible_delta):
                    future_lower, future_upper = next_interval(feasible_delta)
                    raise ValueError(
                        "rate limiter cannot preserve next-sample feasibility at "
                        f"{_time_label(minutes[index])}: "
                        f"next_lower={future_lower:.6f}, "
                        f"next_upper={future_upper:.6f}"
                    )
                infeasible_delta = applied_delta
                for _ in range(42):
                    midpoint = (feasible_delta + infeasible_delta) / 2.0
                    if next_is_feasible(midpoint):
                        feasible_delta = midpoint
                    else:
                        infeasible_delta = midpoint
                applied_delta = feasible_delta
                viability_limited = True

        adjusted_speed = previous_speed + applied_delta
        if desired_delta > upper + 1e-9:
            active = [
                name
                for name, _, interval_upper in constraint_intervals
                if abs(interval_upper - upper) <= 1e-8
            ]
        elif desired_delta < lower - 1e-9:
            active = [
                name
                for name, interval_lower, _ in constraint_intervals
                if abs(interval_lower - lower) <= 1e-8
            ]
        else:
            active = []
        if viability_limited:
            active.append("next-sample-feasibility")

        acceleration = abs(applied_delta - previous_delta) / (
            STEP_HOURS * STEP_HOURS
        )
        state.recent_deltas.append(applied_delta)
        if len(state.recent_deltas) >= window:
            rolling_average = (
                sum(abs(value) for value in state.recent_deltas[-window:]) / window
            )
            local_rolling_averages.append(rolling_average)
        else:
            rolling_average = None

        keep = max(1, window - 1)
        if len(state.recent_deltas) > keep:
            state.recent_deltas = state.recent_deltas[-keep:]

        state.last_speed = adjusted_speed
        adjusted.append(adjusted_speed)
        local_deltas.append(applied_delta)
        local_accelerations.append(acceleration)
        sample_diagnostics.append(
            {
                "minute": minutes[index],
                "time": _time_label(minutes[index]),
                "target_speed_mph": targets[index],
                "adjusted_speed_mph": adjusted_speed,
                "desired_change_mph": desired_delta,
                "applied_change_mph": applied_delta,
                "rolling_average_abs_change_mph": rolling_average,
                "acceleration_mph_per_hour2": acceleration,
                "active_constraints": active,
            }
        )

    active_counts: dict[str, int] = {}
    for sample in sample_diagnostics:
        for name in sample["active_constraints"]:
            active_counts[name] = active_counts.get(name, 0) + 1

    summary: dict[str, object] = {
        "method": "sample-by-sample-rate-limiter",
        "adjusted_samples": sum(
            abs(value - target) > 1e-9 for value, target in zip(adjusted, targets)
        ),
        "max_abs_adjustment_mph": max(
            (abs(value - target) for value, target in zip(adjusted, targets)),
            default=0.0,
        ),
        "max_five_minute_change_mph": max(
            (abs(value) for value in local_deltas), default=0.0
        ),
        "max_rolling_average_abs_change_mph": max(
            local_rolling_averages, default=0.0
        ),
        "max_acceleration_mph_per_hour2": max(
            local_accelerations, default=0.0
        ),
        "requested_end_speed_mph": targets[-1],
        "adjusted_end_speed_mph": adjusted[-1],
        "end_adjustment_mph": adjusted[-1] - targets[-1],
        "active_constraint_counts": active_counts,
    }
    return adjusted, summary, sample_diagnostics


def _constraint_metrics(
    values: list[float], limits: ConnectorLimits
) -> dict[str, float]:
    deltas = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    window = limits.rolling_window_intervals
    rolling = [
        sum(abs(value) for value in deltas[index - window + 1 : index + 1])
        / window
        for index in range(window - 1, len(deltas))
    ]
    accelerations = [
        abs(deltas[index] - deltas[index - 1]) / (STEP_HOURS * STEP_HOURS)
        for index in range(1, len(deltas))
    ]
    return {
        "max_five_minute_change_mph": max(
            (abs(value) for value in deltas), default=0.0
        ),
        "max_rolling_average_abs_change_mph": max(rolling, default=0.0),
        "max_acceleration_mph_per_hour2": max(accelerations, default=0.0),
    }


def _within_constraints(values: list[float], limits: ConnectorLimits) -> bool:
    metrics = _constraint_metrics(values, limits)
    return (
        metrics["max_five_minute_change_mph"]
        <= limits.max_five_minute_change_mph + 1e-9
        and metrics["max_rolling_average_abs_change_mph"]
        <= limits.max_rolling_average_change_mph + 1e-9
        and metrics["max_acceleration_mph_per_hour2"]
        <= limits.max_acceleration_mph_per_hour2 + 1e-7
    )


def _monotone_tangents(
    left_speed: float,
    right_speed: float,
    left_delta: float,
    right_delta: float,
    span_intervals: int,
) -> tuple[float, float]:
    """Limit endpoint tangents so a cubic Hermite connector cannot overshoot."""

    secant_delta = (right_speed - left_speed) / span_intervals
    if abs(secant_delta) <= 1e-12:
        return 0.0, 0.0

    if left_delta * secant_delta <= 0.0:
        left_delta = 0.0
    if right_delta * secant_delta <= 0.0:
        right_delta = 0.0

    alpha = left_delta / secant_delta
    beta = right_delta / secant_delta
    magnitude = math.hypot(alpha, beta)
    if magnitude > 3.0:
        scale = 3.0 / magnitude
        left_delta *= scale
        right_delta *= scale
    return left_delta, right_delta


def _boundary_hermite_value(
    left_speed: float,
    right_speed: float,
    left_delta: float,
    right_delta: float,
    span_intervals: int,
    interval: int,
) -> float:
    t = interval / span_intervals
    h00 = 2.0 * t**3 - 3.0 * t**2 + 1.0
    h10 = t**3 - 2.0 * t**2 + t
    h01 = -2.0 * t**3 + 3.0 * t**2
    h11 = t**3 - t**2
    return (
        h00 * left_speed
        + h10 * span_intervals * left_delta
        + h01 * right_speed
        + h11 * span_intervals * right_delta
    )


def _connector_has_no_reversal(values: list[float]) -> bool:
    total_change = values[-1] - values[0]
    deltas = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    if total_change > 1e-10:
        return all(value >= -1e-10 for value in deltas)
    if total_change < -1e-10:
        return all(value <= 1e-10 for value in deltas)
    return all(abs(value) <= 1e-10 for value in deltas)


def _roughness(values: list[float]) -> float:
    deltas = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    return sum(
        (deltas[index + 1] - deltas[index]) ** 2
        for index in range(len(deltas) - 1)
    )


def _local_within_constraints(
    values: list[float],
    limits: ConnectorLimits,
    changed_start: int,
    changed_end: int,
) -> bool:
    """Check only constraints whose values can change after a local rewrite."""

    delta_count = len(values) - 1
    affected_delta_start = max(0, changed_start - 1)
    affected_delta_end = min(delta_count - 1, changed_end)

    def delta(index: int) -> float:
        return values[index + 1] - values[index]

    for index in range(affected_delta_start, affected_delta_end + 1):
        if abs(delta(index)) > limits.max_five_minute_change_mph + 1e-9:
            return False

    acceleration_start = max(1, affected_delta_start)
    acceleration_end = min(delta_count - 1, affected_delta_end + 1)
    for index in range(acceleration_start, acceleration_end + 1):
        acceleration = abs(delta(index) - delta(index - 1)) / (
            STEP_HOURS * STEP_HOURS
        )
        if acceleration > limits.max_acceleration_mph_per_hour2 + 1e-7:
            return False

    window = limits.rolling_window_intervals
    rolling_end_start = max(window - 1, affected_delta_start)
    rolling_end_end = min(delta_count - 1, affected_delta_end + window - 1)
    for end_index in range(rolling_end_start, rolling_end_end + 1):
        average = sum(
            abs(delta(index))
            for index in range(end_index - window + 1, end_index + 1)
        ) / window
        if average > limits.max_rolling_average_change_mph + 1e-9:
            return False
    return True


def _direction_fraction_interval(
    original_connector: list[float], hermite_connector: list[float]
) -> tuple[float, float] | None:
    """Return fractions whose blended connector never reverses direction."""

    total_change = original_connector[-1] - original_connector[0]
    if abs(total_change) <= 1e-10:
        return (0.0, 0.0)
    direction = 1.0 if total_change > 0.0 else -1.0
    lower = 0.0
    upper = 1.0
    tolerance = 1e-10
    for index in range(len(original_connector) - 1):
        original_delta = original_connector[index + 1] - original_connector[index]
        hermite_delta = hermite_connector[index + 1] - hermite_connector[index]
        intercept = direction * original_delta + tolerance
        slope = direction * (hermite_delta - original_delta)
        if abs(slope) <= 1e-15:
            if intercept < 0.0:
                return None
            continue
        crossing = -intercept / slope
        if slope > 0.0:
            lower = max(lower, crossing)
        else:
            upper = min(upper, crossing)
        if lower > upper + 1e-12:
            return None
    return max(0.0, lower), min(1.0, upper)


def _roughness_optimal_fraction(
    original_context: list[float], hermite_context: list[float]
) -> float:
    """Find the unconstrained minimum of the quadratic roughness blend."""

    original_second_differences = [
        original_context[index + 2]
        - 2.0 * original_context[index + 1]
        + original_context[index]
        for index in range(len(original_context) - 2)
    ]
    hermite_second_differences = [
        hermite_context[index + 2]
        - 2.0 * hermite_context[index + 1]
        + hermite_context[index]
        for index in range(len(hermite_context) - 2)
    ]
    directions = [
        smooth - original
        for original, smooth in zip(
            original_second_differences, hermite_second_differences
        )
    ]
    denominator = sum(value * value for value in directions)
    if denominator <= 1e-24:
        return 1.0
    return -sum(
        original * direction
        for original, direction in zip(original_second_differences, directions)
    ) / denominator


def _smooth_boundary_connections(
    profile: list[dict[str, object]], limits: ConnectorLimits
) -> list[dict[str, object]]:
    """Rewrite the two samples inside every adjacent-period boundary.

    For AM/MD this connects 08:50 to 09:05 while rewriting 08:55 and 09:00.
    Endpoint tangents come from 08:45→08:50 and 09:05→09:10, then receive a
    monotonicity limiter.  A full Hermite rewrite is preferred.  If it would
    violate an enforcer, the smoothest feasible blend with the rate-limited
    profile is selected.
    """

    if not profile:
        return []

    for row in profile:
        row["python_rate_limited"] = row["python_enforced"]

    values = [float(row["python_enforced"]) for row in profile]
    transition_indices = [
        index
        for index in range(1, len(profile))
        if profile[index - 1]["period"] != profile[index]["period"]
    ]
    diagnostics: list[dict[str, object]] = []

    for right_start in transition_indices:
        left_shoulder = right_start - 2
        right_shoulder = right_start + 1
        if left_shoulder - 1 < 0 or right_shoulder + 1 >= len(profile):
            continue
        connector_indices = list(range(left_shoulder, right_shoulder + 1))
        connector_minutes = [int(profile[index]["minute"]) for index in connector_indices]
        if any(
            connector_minutes[index + 1] - connector_minutes[index] != STEP_MINUTES
            for index in range(len(connector_minutes) - 1)
        ):
            continue

        left_period = str(profile[right_start - 1]["period"])
        right_period = str(profile[right_start]["period"])
        if (
            profile[left_shoulder]["period"] != left_period
            or profile[right_shoulder]["period"] != right_period
        ):
            continue

        left_speed = values[left_shoulder]
        right_speed = values[right_shoulder]
        requested_left_delta = left_speed - values[left_shoulder - 1]
        requested_right_delta = values[right_shoulder + 1] - right_speed
        left_delta, right_delta = _monotone_tangents(
            left_speed,
            right_speed,
            requested_left_delta,
            requested_right_delta,
            span_intervals=3,
        )
        hermite_interior = [
            _boundary_hermite_value(
                left_speed,
                right_speed,
                left_delta,
                right_delta,
                span_intervals=3,
                interval=interval,
            )
            for interval in (1, 2)
        ]

        original_interior = [values[right_start - 1], values[right_start]]
        original_connector = [left_speed, *original_interior, right_speed]
        hermite_connector = [left_speed, *hermite_interior, right_speed]
        direction_interval = _direction_fraction_interval(
            original_connector, hermite_connector
        )

        def trial_for_fraction(fraction: float) -> tuple[list[float], list[float]]:
            trial_interior = [
                original + fraction * (smooth - original)
                for original, smooth in zip(original_interior, hermite_interior)
            ]
            trial = values.copy()
            trial[right_start - 1 : right_start + 1] = trial_interior
            return trial, trial_interior

        accepted_fraction: float | None = None
        accepted_values: list[float] | None = None
        accepted_roughness: float | None = None
        if direction_interval is not None:
            direction_lower, direction_upper = direction_interval

            constraint_upper = 1.0
            full_trial, _ = trial_for_fraction(1.0)
            if not _local_within_constraints(
                full_trial, limits, right_start - 1, right_start
            ):
                feasible = 0.0
                infeasible = 1.0
                for _ in range(42):
                    midpoint = (feasible + infeasible) / 2.0
                    midpoint_trial, _ = trial_for_fraction(midpoint)
                    if _local_within_constraints(
                        midpoint_trial, limits, right_start - 1, right_start
                    ):
                        feasible = midpoint
                    else:
                        infeasible = midpoint
                constraint_upper = feasible

            feasible_lower = max(0.0, direction_lower)
            feasible_upper = min(1.0, direction_upper, constraint_upper)
            if feasible_lower <= feasible_upper + 1e-12:
                before_context_for_fit = values[
                    left_shoulder - 1 : right_shoulder + 2
                ]
                full_context_for_fit = before_context_for_fit.copy()
                full_context_for_fit[2:4] = hermite_interior
                optimum = _roughness_optimal_fraction(
                    before_context_for_fit, full_context_for_fit
                )
                optimum = min(feasible_upper, max(feasible_lower, optimum))
                candidate_fractions = {
                    feasible_lower,
                    feasible_upper,
                    optimum,
                }
                for fraction in candidate_fractions:
                    trial, trial_interior = trial_for_fraction(fraction)
                    connector = [left_speed, *trial_interior, right_speed]
                    if not _connector_has_no_reversal(connector):
                        continue
                    if not _local_within_constraints(
                        trial, limits, right_start - 1, right_start
                    ):
                        continue
                    trial_context = trial[
                        left_shoulder - 1 : right_shoulder + 2
                    ]
                    trial_roughness = _roughness(trial_context)
                    if (
                        accepted_roughness is None
                        or trial_roughness < accepted_roughness - 1e-12
                        or (
                            abs(trial_roughness - accepted_roughness) <= 1e-12
                            and fraction > (accepted_fraction or 0.0)
                        )
                    ):
                        accepted_fraction = fraction
                        accepted_values = trial_interior
                        accepted_roughness = trial_roughness

        before_context = values[left_shoulder - 1 : right_shoulder + 2]
        if accepted_values is not None:
            values[right_start - 1 : right_start + 1] = accepted_values
            profile[right_start - 1]["python_enforced"] = accepted_values[0]
            profile[right_start]["python_enforced"] = accepted_values[1]
        after_context = values[left_shoulder - 1 : right_shoulder + 2]
        diagnostics.append(
            {
                "method": "monotone-cubic-hermite",
                "boundary": f"{left_period}/{right_period}",
                "left_shoulder_time": profile[left_shoulder]["time"],
                "right_shoulder_time": profile[right_shoulder]["time"],
                "rewritten_times": [
                    profile[right_start - 1]["time"],
                    profile[right_start]["time"],
                ],
                "left_shoulder_speed_mph": left_speed,
                "right_shoulder_speed_mph": right_speed,
                "requested_tangents_mph_per_five_minutes": [
                    requested_left_delta,
                    requested_right_delta,
                ],
                "limited_tangents_mph_per_five_minutes": [
                    left_delta,
                    right_delta,
                ],
                "rate_limited_interior_speeds_mph": original_interior,
                "hermite_interior_speeds_mph": hermite_interior,
                "final_interior_speeds_mph": (
                    accepted_values if accepted_values is not None else original_interior
                ),
                "accepted_smoothing_fraction": accepted_fraction,
                "roughness_before": _roughness(before_context),
                "roughness_after": _roughness(after_context),
                "direction_reversal": not _connector_has_no_reversal(
                    [
                        left_speed,
                        *(accepted_values or original_interior),
                        right_speed,
                    ]
                ),
            }
        )

    return diagnostics


def build_enforced_period_profiles(
    period_inputs: list[QvdfInputs],
    limits: ConnectorLimits,
    *,
    smooth_boundaries: bool = True,
) -> tuple[dict[str, list[tuple[int, float]]], dict[str, object]]:
    """Build final profiles for one link across ordered adjacent periods."""

    if not period_inputs:
        raise ValueError("at least one period is required")

    limiter_state = LimiterState()
    stitched: list[dict[str, object]] = []
    limiter_summaries: dict[str, dict[str, object]] = {}
    for index, inputs in enumerate(period_inputs):
        enforced_inputs = inputs
        if limiter_state.last_speed is not None:
            enforced_inputs = replace(
                inputs,
                start_anchor=limiter_state.last_speed,
            )
        minutes, targets, _, _ = build_profile(enforced_inputs)
        enforced, summary, _ = _rate_limiter(
            minutes,
            targets,
            limits,
            limiter_state,
            flatten_next_boundary=index < len(period_inputs) - 1,
        )
        limiter_summaries[inputs.period.lower()] = summary
        stitched.extend(
            {
                "period": inputs.period.upper(),
                "minute": minute,
                "time": _time_label(minute),
                "python_enforced": speed,
            }
            for minute, speed in zip(minutes, enforced)
        )

    boundary_smoother = (
        _smooth_boundary_connections(stitched, limits) if smooth_boundaries else []
    )
    if not smooth_boundaries:
        for row in stitched:
            row["python_rate_limited"] = row["python_enforced"]

    profiles: dict[str, list[tuple[int, float]]] = {}
    for row in stitched:
        period = str(row["period"]).lower()
        profiles.setdefault(period, []).append(
            (int(row["minute"]), float(row["python_enforced"]))
        )

    metrics = _constraint_metrics(
        [float(row["python_enforced"]) for row in stitched], limits
    )
    return profiles, {
        "rate_limiter": limiter_summaries,
        "boundary_smoother": boundary_smoother,
        "final_constraint_metrics": metrics,
    }


def _csv_profile(performance: dict[str, str]) -> dict[int, float]:
    result: dict[int, float] = {}
    for name, value in performance.items():
        if not name.startswith("spd_mph_"):
            continue
        hour, minute = name.removeprefix("spd_mph_").split(":")
        result[int(hour) * 60 + int(minute)] = float(value)
    return result


def _max_abs_error(expected: dict[int, float], minutes: list[int], values: list[float]) -> float:
    return max(abs(expected[minute] - value) for minute, value in zip(minutes, values))


def _parse_windows(values: Iterable[str]) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for value in values:
        period, bounds = value.split("=", 1)
        start, end = bounds.split(",", 1)
        result[period.lower()] = (float(start), float(end))
    return result


def _carried_speed(
    minutes: list[int], values: list[float], target_minute: int
) -> tuple[float, int]:
    """Return the prior enforced profile speed at the next period's start.

    For overlapping periods the value comes from the shared timestamp.  For
    adjacent periods TAPLite's half-open profile has no sample at the exact
    boundary, so the final emitted five-minute sample is carried forward.
    """

    if target_minute <= minutes[0]:
        return values[0], minutes[0]
    if target_minute >= minutes[-1]:
        return values[-1], minutes[-1]
    for index in range(1, len(minutes)):
        right_minute = minutes[index]
        if target_minute > right_minute:
            continue
        left_minute = minutes[index - 1]
        if target_minute == right_minute:
            return values[index], right_minute
        factor = (target_minute - left_minute) / (right_minute - left_minute)
        value = values[index - 1] + factor * (values[index] - values[index - 1])
        return value, target_minute
    return values[-1], minutes[-1]


def run_experiment(
    assignment: Path,
    link_id: str,
    windows: dict[str, tuple[float, float]],
    limits: ConnectorLimits,
    *,
    smooth_boundaries: bool = True,
) -> dict[str, object]:
    stitched: list[dict[str, object]] = []
    period_results: dict[str, object] = {}
    limiter_state = LimiterState()
    previous_period: str | None = None
    previous_end_minute: int | None = None
    for period in ("am", "md", "pm"):
        inputs, performance = load_inputs(assignment / period, period, link_id)
        csv_values = _csv_profile(performance)
        exact_minutes, exact_values, exact_scalars, exact_diagnostics = build_profile(inputs)

        enforced_inputs = inputs
        start_anchor_adjustment: dict[str, object] | None = None
        if limiter_state.last_speed is not None:
            carried = limiter_state.last_speed
            enforced_inputs = replace(inputs, start_anchor=carried)
            start_anchor_adjustment = {
                "source_period": previous_period.upper() if previous_period else None,
                "source_time": (
                    _time_label(previous_end_minute)
                    if previous_end_minute is not None
                    else None
                ),
                "requested_anchor_mph": inputs.start_anchor,
                "adjusted_anchor_mph": carried,
                "anchor_adjustment_mph": (
                    carried - inputs.start_anchor
                    if inputs.start_anchor is not None
                    else None
                ),
            }

        enforced_minutes, target_values, _, target_diagnostics = build_profile(
            enforced_inputs
        )
        enforced_values, limiter_summary, limiter_samples = _rate_limiter(
            enforced_minutes,
            target_values,
            limits,
            limiter_state,
            flatten_next_boundary=period != "pm",
        )
        if exact_minutes != enforced_minutes:
            raise AssertionError("Exact and enforced profile timestamps diverged")
        error = _max_abs_error(csv_values, exact_minutes, exact_values)

        previous_period = period
        previous_end_minute = enforced_minutes[-1]

        start_hour, end_hour = windows[period]
        selected = []
        for minute, exact, target, enforced in zip(
            exact_minutes, exact_values, target_values, enforced_values
        ):
            if start_hour * 60 <= minute < end_hour * 60:
                record = {
                    "period": period.upper(),
                    "minute": minute,
                    "time": _time_label(minute),
                    "taplite": csv_values[minute],
                    "python_exact": exact,
                    "python_target": target,
                    "python_enforced": enforced,
                }
                selected.append(record)
                stitched.append(record)

        period_results[period] = {
            "inputs": asdict(inputs),
            "enforced_inputs": asdict(enforced_inputs),
            "scalars": asdict(exact_scalars) if exact_scalars is not None else None,
            "taplite_status": performance.get("qvdf_profile_status"),
            "exact_max_abs_error_mph": error,
            "exact_connectors": exact_diagnostics,
            "target_connectors": target_diagnostics,
            "rate_limiter": limiter_summary,
            "rate_limiter_samples": limiter_samples,
            "start_anchor_adjustment": start_anchor_adjustment,
            "enforced_start_speed_mph": enforced_values[0],
            "enforced_end_speed_mph": enforced_values[-1],
            "selected_samples": len(selected),
        }

    boundary_smoother = (
        _smooth_boundary_connections(stitched, limits) if smooth_boundaries else []
    )
    if not smooth_boundaries:
        for row in stitched:
            row["python_rate_limited"] = row["python_enforced"]

    for period in ("am", "md", "pm"):
        period_profile = [
            row for row in stitched if str(row["period"]).lower() == period
        ]
        if not period_profile:
            continue
        period_results[period]["rate_limited_start_speed_mph"] = period_profile[0][
            "python_rate_limited"
        ]
        period_results[period]["rate_limited_end_speed_mph"] = period_profile[-1][
            "python_rate_limited"
        ]
        period_results[period]["enforced_start_speed_mph"] = period_profile[0][
            "python_enforced"
        ]
        period_results[period]["enforced_end_speed_mph"] = period_profile[-1][
            "python_enforced"
        ]

    final_metrics = _constraint_metrics(
        [float(row["python_enforced"]) for row in stitched], limits
    )

    return {
        "assignment": str(assignment.resolve()),
        "link_id": link_id,
        "limits": asdict(limits),
        "windows": {key: list(value) for key, value in windows.items()},
        "periods": period_results,
        "boundary_smoother_enabled": smooth_boundaries,
        "boundary_smoother": boundary_smoother,
        "final_constraint_metrics": final_metrics,
        "profile": stitched,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assignment", type=Path, required=True)
    parser.add_argument("--link-id", default="28275")
    parser.add_argument("--window", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-five-minute-change", type=float, default=8.0)
    parser.add_argument("--rolling-window-intervals", type=int, default=3)
    parser.add_argument("--max-rolling-average-change", type=float, default=4.0)
    parser.add_argument("--max-acceleration", type=float, default=576.0)
    parser.add_argument("--no-boundary-smoother", action="store_true")
    args = parser.parse_args()

    limits = ConnectorLimits(
        max_five_minute_change_mph=args.max_five_minute_change,
        rolling_window_intervals=args.rolling_window_intervals,
        max_rolling_average_change_mph=args.max_rolling_average_change,
        max_acceleration_mph_per_hour2=args.max_acceleration,
    )
    result = run_experiment(
        args.assignment.resolve(),
        args.link_id,
        _parse_windows(args.window),
        limits,
        smooth_boundaries=not args.no_boundary_smoother,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    errors = {
        period: values["exact_max_abs_error_mph"]
        for period, values in result["periods"].items()
    }
    print(f"output={args.output.resolve()}")
    print(f"samples={len(result['profile'])}")
    print(f"exact_max_abs_error_mph={errors}")
    print("boundary_smoother=" + json.dumps(result["boundary_smoother"]))
    print(
        "rate_limiter="
        + json.dumps(
            {
                period: values["rate_limiter"]
                for period, values in result["periods"].items()
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
