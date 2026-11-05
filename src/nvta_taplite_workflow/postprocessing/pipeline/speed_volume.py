"""One row per directed link, with S3-shaped interval volumes conserved by period.

S3 equations and inversion safeguards follow CBI's fundamental_diagram.py
(Mmdabb/CBI-TAPLite-Pipeline, commit 1ce571f80e34efa2afdde7ca266740ba057d0523).
The assignment-total correction is multiplicative, as requested for this export.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from nvta_taplite_workflow.dtalite4cube.unit_contract import KPH_PER_MPH

from .preprocessor import _normalized_node_ids, resolve_period_file
from .time_dependent_speeds import (
    parse_period_range_minutes,
    speed_column_minutes,
    time_dependent_speed_columns,
)

logger = logging.getLogger(__name__)
KEYS = ["from_node_id", "to_node_id"]
BASE_COLUMNS = ["link_id", *KEYS, "link_type", "free_speed", "capacity", "lanes"]
OPTIONAL_FD = ["speed_at_capacity_mph", "vc_mph", "fd_vc_mph", "s3_shape_m", "s3_m"]
NETWORK_COLUMNS = [*BASE_COLUMNS, "vdf_free_speed_mph", *OPTIONAL_FD]


def _positive(frame: pd.DataFrame, candidates: list[tuple[str, float]]) -> tuple[np.ndarray, np.ndarray]:
    values = np.full(len(frame), np.nan)
    sources = np.full(len(frame), "", dtype=object)
    for name, factor in candidates:
        if name not in frame:
            continue
        candidate = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float) * factor
        use = ~np.isfinite(values) & np.isfinite(candidate) & (candidate > 0)
        values[use] = candidate[use]
        sources[use] = name
    return values, sources


def s3_parameters(frame: pd.DataFrame, speeds: np.ndarray) -> pd.DataFrame:
    """Resolve parameters row by row; network generic free_speed is km/h."""
    vf, vf_source = _positive(frame, [
        ("network_vdf_free_speed_mph", 1), ("free_speed_mph", 1),
        ("network_free_speed", 1 / KPH_PER_MPH),
    ])
    vc, vc_source = _positive(frame, [(c, 1) for c in OPTIONAL_FD[:3]] + [
        ("network_" + c, 1) for c in OPTIONAL_FD[:3]
    ])
    m, m_source = _positive(frame, [(c, 1) for c in OPTIONAL_FD[3:]] + [
        ("network_" + c, 1) for c in OPTIONAL_FD[3:]
    ])
    derive_vf = ~np.isfinite(vf) & np.isfinite(vc) & np.isfinite(m)
    with np.errstate(over="ignore"):
        vf[derive_vf] = vc[derive_vf] * 2 ** (2 / m[derive_vf])
    vf_source[derive_vf] = "derived_from_vc_and_m"
    missing_vf = ~np.isfinite(vf)
    if missing_vf.any():
        # CBI's speed-only fallback. Known network speeds are never clipped.
        observed = pd.DataFrame(speeds).where(speeds >= 0).quantile(0.95, axis=1).to_numpy()
        vf[missing_vf] = np.clip(np.nan_to_num(observed[missing_vf], nan=35), 35, 85)
        vf_source[missing_vf] = "assumed_observed_p95_clipped_35_85_mph"

    valid_vc = np.isfinite(vc) & (vc < vf)
    derived_vc = ~valid_vc & np.isfinite(m)
    vc[derived_vc] = vf[derived_vc] / 2 ** (2 / m[derived_vc])
    vc_source[derived_vc] = "derived_from_vf_and_m"
    assumed_vc = ~(np.isfinite(vc) & (vc > 0) & (vc < vf))
    vc[assumed_vc] = 0.75 * vf[assumed_vc]
    vc_source[assumed_vc] = "assumed_0.75_vf"
    # vc is authoritative when supplied; this also repairs inconsistent supplied m.
    derived_m = 2 * np.log(2) / np.log(vf / vc)
    repair_m = ~np.isfinite(m) | ~np.isclose(m, derived_m, rtol=1e-6, atol=1e-8)
    m[repair_m] = derived_m[repair_m]
    m_source[repair_m] = "derived_from_vf_and_vc"

    lanes, lanes_source = _positive(frame, [("network_lanes", 1)])
    lanes[~np.isfinite(lanes)] = 1
    lanes_source[lanes_source == ""] = "assumed_1_lane"
    capacity, cap_source = _positive(frame, [("link_capacity", 1)])
    for name in ["network_capacity", "lane_capacity"]:
        per_lane, source = _positive(frame, [(name, 1)])
        use = ~np.isfinite(capacity) & np.isfinite(per_lane)
        capacity[use] = per_lane[use] * lanes[use]
        cap_source[use] = np.char.add(source[use].astype(str), "*lanes")
    assume_capacity = ~np.isfinite(capacity)
    capacity[assume_capacity] = 1800 * lanes[assume_capacity]
    cap_source[assume_capacity] = "assumed_1800_vphpl*lanes"

    return pd.DataFrame({
        "free_speed": vf, "critical_speed_mph": vc, "s3_m": m,
        "capacity": capacity, "lanes": lanes, "critical_density_veh_per_mile": capacity / vc,
        "free_speed_source": vf_source, "critical_speed_source": vc_source,
        "s3_m_source": m_source, "capacity_source": cap_source, "lanes_source": lanes_source,
    }, index=frame.index)


def inverse_s3_link_flow(speeds: np.ndarray, parameters: pd.DataFrame) -> np.ndarray:
    """Vehicles/hour for the whole directed link, before period scaling."""
    vf = parameters["free_speed"].to_numpy()[:, None]
    m = parameters["s3_m"].to_numpy()[:, None]
    kc = parameters["critical_density_veh_per_mile"].to_numpy()[:, None]
    capacity = parameters["capacity"].to_numpy()[:, None]
    valid = np.isfinite(speeds) & (speeds >= 0)
    clipped = np.minimum(np.where(valid, speeds, 0), 0.99 * vf)
    ratio = np.maximum(vf / np.maximum(clipped, 1), 1.00001)
    # Log form avoids overflow for large supplied m close to free-flow speed.
    z = m / 2 * np.log(ratio)
    log_term = np.maximum(z, 1e-8) + np.log(-np.expm1(-np.maximum(z, 1e-8)))
    density = kc * np.exp(log_term / m)
    flow = np.minimum(clipped * density, capacity)
    return np.where(valid, flow, 0)


def _interval_columns(columns, time_range: str) -> tuple[list[str | None], np.ndarray, list[int]]:
    start, end = parse_period_range_minutes(time_range)
    duration = (end - start) % 1440 or 1440
    # Canonicalize speeds to mph, selecting one representation for each timestamp.
    by_minute = {}
    for column in time_dependent_speed_columns(columns, time_range):
        minute = speed_column_minutes(column)
        priority = 0 if str(column).startswith("spd_mph_") else 1 if str(column).startswith("spd_") and "kmph" not in str(column) else 2
        if minute not in by_minute or priority < by_minute[minute][0]:
            by_minute[minute] = (priority, column)
    minutes = sorted(by_minute, key=lambda t: (t - start) % 1440)
    if not minutes:
        return [], np.array([]), []
    offsets = np.array([(minute - start) % 1440 for minute in minutes])
    if offsets[0] != 0:
        raise ValueError(f"Speed profile for {time_range} must start at the period boundary.")
    # Infer the time grid from supplied timestamps and the period boundary.
    # This exposes absent overnight bins rather than stretching 23:55 to 06:00.
    increments = np.diff(np.append(offsets, duration))
    step = int(np.gcd.reduce(increments)) if len(minutes) > 1 else min(5, duration)
    offsets = np.arange(0, duration, step)
    minutes = [int((start + offset) % 1440) for offset in offsets]
    hours = np.diff(np.append(offsets, duration)) / 60
    return [by_minute[t][1] if t in by_minute else None for t in minutes], hours, minutes


def resolve_export_period_ranges(
    network_dir: str | Path,
    time_periods: list[str],
    period_ranges: list[str | None] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Use actual kernel settings when present; configured bounds are fallback."""
    if period_ranges is not None and len(period_ranges) != len(time_periods):
        raise ValueError("time_periods and period_ranges must have the same length.")
    ranges, sources = {}, {}
    for index, raw_period in enumerate(time_periods):
        period = raw_period.lower()
        if period in ranges:
            raise ValueError("Duplicate configured time periods.")
        configured = period_ranges[index] if period_ranges is not None else None
        try:
            path = resolve_period_file(network_dir, period, "settings.csv")
        except FileNotFoundError:
            path = None
        if path is not None:
            settings = pd.read_csv(path)
            if len(settings) != 1:
                raise ValueError(f"Expected one settings row in {path}.")
            try:
                start, end = [float(settings.loc[0, key]) * 60 for key in (
                    "demand_period_starting_hours", "demand_period_ending_hours"
                )]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid demand period bounds in {path}.") from exc
            if not (np.isfinite(start) and np.isfinite(end) and 0 <= start < 1440
                    and 0 <= end <= 2880 and np.isclose(start, round(start))
                    and np.isclose(end, round(end))):
                raise ValueError(f"Invalid demand period hours in {path}.")
            start, end = round(start), round(end)
            if end > start + 1440:
                raise ValueError(f"Period longer than 24 hours in {path}.")
            end = 1440 if end == 1440 else end % 1440
            effective = f"{start // 60:02d}{start % 60:02d}_{end // 60:02d}{end % 60:02d}"
            ranges[period], sources[period] = effective, "settings.csv"
            if configured and parse_period_range_minutes(configured) != (start, end):
                logger.warning("%s export uses actual settings range %s instead of configured %s.", period, effective, configured)
        elif configured:
            parse_period_range_minutes(configured)
            ranges[period], sources[period] = configured, "configured"
        else:
            raise ValueError(f"Missing period range for {period}: supply a range or {period}/settings.csv.")
    return ranges, sources


def _hourly_timeline(ranges: dict[str, str]) -> tuple[dict[str, int], list[tuple[int, int]], list[str]]:
    starts = {}
    previous_end = None
    owned_minutes = set()
    for period, time_range in ranges.items():
        start, end = parse_period_range_minutes(time_range)
        duration = (end - start) % 1440 or 1440
        owned = {(start + t) % 1440 for t in range(duration)}
        if owned_minutes.intersection(owned):
            raise ValueError("Configured periods overlap in the speed/volume export.")
        owned_minutes.update(owned)
        while previous_end is not None and start < previous_end:
            start += 1440
        starts[period] = start
        previous_end = start + duration
    beginning = next(iter(starts.values()))
    if previous_end is None or previous_end - beginning > 1440:
        raise ValueError("Configured periods must form an ordered span of at most 24 hours.")
    bins = [(t, min(t + 60, previous_end)) for t in range(beginning, previous_end, 60)]
    labels = [f"{a // 60:02d}:{a % 60:02d}_{b // 60:02d}:{b % 60:02d}" for a, b in bins]
    return starts, bins, labels


def _hourly_contributions(speeds, estimated, interval_hours, absolute_start, bins):
    interval_start = absolute_start + np.r_[0, np.cumsum(interval_hours[:-1]) * 60]
    interval_end = interval_start + interval_hours * 60
    overlap = np.maximum(0, np.minimum(interval_end[:, None], np.array(bins)[:, 1])
                         - np.maximum(interval_start[:, None], np.array(bins)[:, 0]))
    fractions = overlap / (interval_hours[:, None] * 60)
    volume = estimated @ fractions
    # Harmonic speed = total duration / sum(duration / speed).
    inverse = np.divide(1, speeds, out=np.zeros_like(speeds), where=np.isfinite(speeds) & (speeds > 0))
    travel = inverse @ overlap
    stopped = (speeds == 0).astype(float) @ overlap > 0
    travel[stopped] = np.inf
    invalid = (~np.isfinite(speeds) | (speeds < 0)).astype(float) @ overlap
    durations = np.broadcast_to(overlap.sum(axis=0), volume.shape).copy()
    empty = durations == 0
    volume[empty] = np.nan
    return volume, durations, travel, invalid


def write_speed_volume(
    combined: pd.DataFrame,
    network_dir: str | Path,
    time_periods: list[str],
    period_ranges: list[str | None] | None = None,
    *,
    output_filename: str,
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Write one hourly CSV; return period diagnostics without extra output files.

    Generic output free_speed is mph; capacity is whole-link vehicles/hour.
    Static attributes come from the first configured period containing the link.
    Effective period attributes and all assumptions are retained in the audit.
    """
    ranges, range_sources = resolve_export_period_ranges(network_dir, time_periods, period_ranges)
    period_starts, hourly_bins, labels = _hourly_timeline(ranges)
    if not time_dependent_speed_columns(combined.columns):
        logger.warning("No incremental speeds; skipping %s for legacy performance files.", output_filename)
        # Prevent a previous successful export from looking current after a legacy run.
        for path in [Path(network_dir) / output_filename]:
            path.unlink(missing_ok=True)
        return None

    wide_parts, audits = [], []
    for period, time_range in ranges.items():
        frame = combined.loc[combined["time_period"] == period].copy().reset_index(drop=True)
        if frame.empty:
            continue
        for key in KEYS:
            frame[key] = _normalized_node_ids(frame[key])
        if frame[KEYS].isna().any().any() or frame.duplicated(KEYS).any():
            raise ValueError(f"Invalid or duplicate directed link pairs in {period} performance.")
        columns, interval_hours, minutes = _interval_columns(frame.columns, time_range)
        if not columns:
            raise ValueError(f"Missing incremental speeds for {period}; cannot conserve its assigned volumes.")
        speeds = np.full((len(frame), len(columns)), np.nan)
        missing_columns = np.array([name is None for name in columns])
        for i, name in enumerate(columns):
            if name is None:
                # Assignment average for the same link/period, not an external prior.
                speeds[:, i] = pd.to_numeric(frame["speed_mph"], errors="coerce").to_numpy(dtype=float)
                continue
            speeds[:, i] = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
            if "kmph" in name:
                speeds[:, i] /= KPH_PER_MPH
        links = pd.read_csv(
            resolve_period_file(network_dir, period, "link.csv"),
            usecols=lambda c: c in NETWORK_COLUMNS, low_memory=False,
        )
        for key in KEYS:
            links[key] = _normalized_node_ids(links[key])
        if links[KEYS].isna().any().any() or links.duplicated(KEYS).any():
            raise ValueError(f"Invalid or duplicate directed node pairs in {period}/link.csv.")
        frame = frame.merge(
            links.rename(columns={c: "network_" + c for c in links if c not in KEYS}),
            on=KEYS, how="left", validate="one_to_one", indicator=True,
        )
        if (frame["_merge"] != "both").any():
            raise ValueError(f"Unmatched directed node pairs in {period}; check link.csv against assignment outputs.")
        params = s3_parameters(frame, speeds)
        target = pd.to_numeric(frame["volume"], errors="coerce").to_numpy(dtype=float)
        if (~np.isfinite(target) | (target < 0)).any():
            raise ValueError(f"Invalid assigned volume in {period}.")
        raw = inverse_s3_link_flow(speeds, params) * interval_hours[None, :]
        raw_total = raw.sum(axis=1)
        scale = np.divide(target, raw_total, out=np.zeros_like(target), where=raw_total > 0)
        estimated = raw * scale[:, None]
        fallback = (raw_total == 0) & (target > 0)
        estimated[fallback] = target[fallback, None] * interval_hours / interval_hours.sum()
        # Remove floating-point summation residue without rounding vehicle counts.
        largest = estimated.argmax(axis=1)
        estimated[np.arange(len(frame)), largest] += target - estimated.sum(axis=1)
        scale[fallback] = np.nan

        part = pd.DataFrame({
            "link_id": frame["network_link_id"].to_numpy(),
            **{key: frame[key].to_numpy() for key in KEYS},
            "link_type": frame["network_link_type"].to_numpy(),
            "free_speed": params["free_speed"].to_numpy(),
            "capacity": params["capacity"].to_numpy(), "lanes": params["lanes"].to_numpy(),
        })
        hour_volume, duration, travel, invalid = _hourly_contributions(
            speeds, estimated, interval_hours, period_starts[period], hourly_bins,
        )
        volume_names = ["approx_volume_" + label for label in labels]
        part = pd.concat([part,
            pd.DataFrame(hour_volume, columns=volume_names),
            pd.DataFrame(duration, columns=["__duration_" + label for label in labels]),
            pd.DataFrame(travel, columns=["__travel_" + label for label in labels]),
            pd.DataFrame(invalid, columns=["__invalid_" + label for label in labels]),
        ], axis=1)
        wide_parts.append(part)
        audit = pd.concat([part[BASE_COLUMNS], params.drop(columns=["free_speed", "capacity", "lanes"])], axis=1)
        audit["time_period"] = period
        audit["period_range"] = time_range
        audit["period_range_source"] = range_sources[period]
        audit["assignment_link_id"] = frame["link_id"].to_numpy()
        audit["assigned_volume"] = target
        audit["raw_s3_volume"] = raw_total
        audit["scale_factor"] = scale
        audit["estimated_volume_sum"] = estimated.sum(axis=1)
        audit["conservation_error"] = estimated.sum(axis=1) - target
        audit["hourly_volume_sum"] = np.nansum(hour_volume, axis=1)
        audit["hourly_conservation_error"] = np.nansum(hour_volume, axis=1) - target
        audit["invalid_speed_intervals"] = (~np.isfinite(speeds) | (speeds < 0)).sum(axis=1)
        audit["imputed_speed_intervals"] = int(missing_columns.sum())
        audit["imputation_source"] = "speed_mph" if missing_columns.any() else "none"
        audit["interval_count"] = len(minutes)
        audit["interval_minutes_min"] = interval_hours.min() * 60
        audit["interval_minutes_max"] = interval_hours.max() * 60
        audit["status"] = np.where(fallback, "uniform_duration_fallback", np.where(target == 0, "zero_assignment", "s3_scaled"))
        audit["above_capacity_intervals"] = (estimated / interval_hours > params["capacity"].to_numpy()[:, None] + 1e-6).sum(axis=1)
        audits.append(audit)

    # Sum already-scaled contributions, including bins crossing period boundaries.
    indexed = [part.set_index(KEYS) for part in wide_parts]
    grouped = pd.concat(indexed).groupby(level=KEYS, sort=False)
    output = grouped.first()[[c for c in BASE_COLUMNS if c not in KEYS]]
    sums = grouped.sum(min_count=1, numeric_only=True)
    speeds_out = {}
    for label in labels:
        duration = sums["__duration_" + label]
        travel = sums["__travel_" + label]
        harmonic = duration / travel
        harmonic[(duration == 0) | (sums["__invalid_" + label] > 0)] = np.nan
        speeds_out["spd_mph_" + label] = harmonic
    output = pd.concat([output, pd.DataFrame(speeds_out), sums[volume_names]], axis=1).reset_index()
    output = output[BASE_COLUMNS + list(speeds_out) + volume_names]
    audit = pd.concat(audits, ignore_index=True)
    if audit.imputed_speed_intervals.sum():
        logger.warning(
            "%s: %s missing link/time speed cells filled with the same period's "
            "assigned average speed; see the imputation columns in the audit.",
            output_filename, audit.imputed_speed_intervals.sum(),
        )
    out_path = Path(network_dir) / output_filename
    output.to_csv(out_path, index=False)
    logger.info("Wrote %s: %s links; %s link/period audits; %s duration fallbacks.", out_path, len(output), len(audit), (audit.status == "uniform_duration_fallback").sum())
    return output, audit
