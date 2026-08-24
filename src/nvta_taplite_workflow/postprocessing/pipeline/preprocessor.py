from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .linkperformance_fieldconfig import (
    ALLOWED_USES_FIELD,
    DISTRICT_ID_FIELD,
    FACILITY_TYPE_FIELD,
    FFTT_FIELD,
    FREE_SPEED_FIELD,
    FROM_NODE_ID_FIELD,
    GEOMETRY_FIELD,
    LEGACY_LENGTH_MILE_FIELD,
    LENGTH_FIELD,
    LINK_FILENAME,
    LINK_ID_FIELD,
    LINK_PERFORMANCE_FILENAME,
    LINK_TYPE_FIELD,
    PAIR_FIELD,
    PERSON_VOLUME_FIELD,
    SEVERE_CONGESTION_FIELD,
    SPEED_FIELD,
    SPEED_RATIO_FIELD,
    TAZ_FIELD,
    TO_NODE_ID_FIELD,
    TOLL_GROUP_FIELD,
    TRAVEL_TIME_FIELD,
    TRUCK_VOLUME_FIELD,
    VEHICLE_VOLUME_FIELD,
    VOLUME_FIELD,
    district_id_name_mapping,
    link_performance_fields_mapping,
    link_required_fields_mapping,
    period_limit_field,
)
from .time_dependent_speeds import (
    period_range_mapping,
    retain_relevant_speed_columns,
)

logger = logging.getLogger(__name__)


def resolve_period_file(network_dir: str | Path, time_period: str, file_name: str) -> Path:
    network_path = Path(network_dir)
    period_key = time_period.lower()
    period_dir = network_path / period_key
    period_path = period_dir / file_name
    if period_path.exists():
        return period_path

    nested_legacy_backmapped_period_path = period_dir / f"{period_key}_origIDs" / file_name
    if nested_legacy_backmapped_period_path.exists():
        return nested_legacy_backmapped_period_path

    sibling_legacy_backmapped_period_path = network_path / f"{period_key}_origIDs" / file_name
    if sibling_legacy_backmapped_period_path.exists():
        return sibling_legacy_backmapped_period_path

    legacy_name = f"{Path(file_name).stem}_{period_key}{Path(file_name).suffix}"
    legacy_path = network_path / legacy_name
    if legacy_path.exists():
        return legacy_path

    legacy_outputs_path = network_path / "Outputs" / "DTALite" / legacy_name
    if legacy_outputs_path.exists():
        return legacy_outputs_path

    raise FileNotFoundError(
        f"Missing {file_name} for period {period_key}: checked "
        f"{period_path}, {nested_legacy_backmapped_period_path}, "
        f"{sibling_legacy_backmapped_period_path}, {legacy_path}, "
        f"and {legacy_outputs_path}"
    )


def _read_nonempty_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        raise pd.errors.EmptyDataError(f"File is empty: {path}")
    return df


def _preferred_existing(columns: set[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _ensure_link_fields(link_df: pd.DataFrame, time_period: str) -> pd.DataFrame:
    if FACILITY_TYPE_FIELD not in link_df.columns:
        if LINK_TYPE_FIELD not in link_df.columns:
            raise KeyError(f"Missing {FACILITY_TYPE_FIELD} and cannot derive it without {LINK_TYPE_FIELD}.")
        link_df[FACILITY_TYPE_FIELD] = (pd.to_numeric(link_df[LINK_TYPE_FIELD], errors="coerce") % 10).astype("Int64")

    if LEGACY_LENGTH_MILE_FIELD not in link_df.columns and LENGTH_FIELD in link_df.columns:
        link_df[LEGACY_LENGTH_MILE_FIELD] = link_df[LENGTH_FIELD]

    limit_field = period_limit_field(time_period)
    if limit_field not in link_df.columns and ALLOWED_USES_FIELD in link_df.columns:
        hov_allowed = link_df[ALLOWED_USES_FIELD].astype(str).str.contains("hov2|hov3", case=False, na=False)
        link_df[limit_field] = np.where(hov_allowed, 2, 0)

    if TOLL_GROUP_FIELD not in link_df.columns:
        link_df[TOLL_GROUP_FIELD] = 0

    return link_df


def _merge_missing_fields(link_performance_df: pd.DataFrame, link_df: pd.DataFrame, required_fields: list[str]) -> pd.DataFrame:
    missing_fields = [field for field in required_fields if field not in link_performance_df.columns and field in link_df.columns]
    if not missing_fields:
        return link_performance_df

    link_performance_df = link_performance_df.copy()
    link_df = link_df.copy()
    link_performance_df[LINK_ID_FIELD] = link_performance_df[LINK_ID_FIELD].astype(str)
    link_df[LINK_ID_FIELD] = link_df[LINK_ID_FIELD].astype(str)
    link_df = link_df.drop_duplicates(subset=LINK_ID_FIELD, keep="first")
    link_performance_df = link_performance_df.drop_duplicates(subset=LINK_ID_FIELD, keep="first")
    return link_performance_df.merge(link_df[[LINK_ID_FIELD] + missing_fields], on=LINK_ID_FIELD, how="left")


def _add_hov_flag(df: pd.DataFrame, time_period: str) -> pd.DataFrame:
    limit_field = period_limit_field(time_period)
    toll_group = df[TOLL_GROUP_FIELD] if TOLL_GROUP_FIELD in df.columns else 0
    period_limit = df[limit_field] if limit_field in df.columns else 0
    df["is_hov"] = ((toll_group == 2) | (period_limit == 2) | (period_limit == 3)).astype(int)
    return df


def _filter_regional_links(df: pd.DataFrame) -> pd.DataFrame:
    if TAZ_FIELD not in df.columns or FACILITY_TYPE_FIELD not in df.columns:
        return df
    return df[(df[TAZ_FIELD] > 1404) & (df[TAZ_FIELD] < 2820) & (df[FACILITY_TYPE_FIELD] > 0)].copy()


def _ensure_performance_columns(df: pd.DataFrame, length_unit: str, speed_unit: str) -> pd.DataFrame:
    columns = set(df.columns)
    length_source = _preferred_existing(columns, [LEGACY_LENGTH_MILE_FIELD, LENGTH_FIELD])
    if length_source is None:
        raise KeyError(f"Missing length field. Expected {LEGACY_LENGTH_MILE_FIELD} or {LENGTH_FIELD}.")

    df[length_source] = pd.to_numeric(df[length_source], errors="coerce")
    df["length"] = df[length_source] * 1.609 if length_unit == "meter" else df[length_source]

    if FFTT_FIELD not in df.columns:
        df[FREE_SPEED_FIELD] = pd.to_numeric(df[FREE_SPEED_FIELD], errors="coerce")
        df[FFTT_FIELD] = df["length"] / np.where(df[FREE_SPEED_FIELD] > 0, df[FREE_SPEED_FIELD], np.nan)

    if TRAVEL_TIME_FIELD not in df.columns:
        df[SPEED_FIELD] = pd.to_numeric(df[SPEED_FIELD], errors="coerce")
        df[TRAVEL_TIME_FIELD] = df["length"] / np.where(df[SPEED_FIELD] > 0, df[SPEED_FIELD], np.nan)

    if SPEED_RATIO_FIELD not in df.columns:
        df[SPEED_RATIO_FIELD] = df[SPEED_FIELD] / np.where(df[FREE_SPEED_FIELD] > 0, df[FREE_SPEED_FIELD], np.nan)

    if SEVERE_CONGESTION_FIELD not in df.columns:
        df[SEVERE_CONGESTION_FIELD] = 0

    volume_source = _preferred_existing(set(df.columns), [VEHICLE_VOLUME_FIELD, PERSON_VOLUME_FIELD, "volume"])
    if volume_source is None:
        raise KeyError("Missing volume field. Expected volume or vehicle_volume.")
    df[VEHICLE_VOLUME_FIELD] = pd.to_numeric(df[volume_source], errors="coerce").fillna(0)
    df[PERSON_VOLUME_FIELD] = pd.to_numeric(df.get(PERSON_VOLUME_FIELD, df[VEHICLE_VOLUME_FIELD]), errors="coerce").fillna(0)

    if TRUCK_VOLUME_FIELD not in df.columns:
        df[TRUCK_VOLUME_FIELD] = 0

    df["delay"] = np.where(
        (df[TRAVEL_TIME_FIELD] - df[FFTT_FIELD] > 0) & (df[SPEED_RATIO_FIELD] < 1),
        df[TRAVEL_TIME_FIELD] - df[FFTT_FIELD],
        0,
    )
    df["person_delay"] = df[PERSON_VOLUME_FIELD] * df["delay"]
    df["person_hour"] = df[PERSON_VOLUME_FIELD] * df[TRAVEL_TIME_FIELD]
    df["person_mile"] = df[PERSON_VOLUME_FIELD] * df["length"]
    df["length_weighted_P"] = df[SEVERE_CONGESTION_FIELD] * df["length"]
    df["vehicle_mile"] = df[VEHICLE_VOLUME_FIELD] * df["length"]
    df["vehicle_hour"] = df[VEHICLE_VOLUME_FIELD] * df[TRAVEL_TIME_FIELD]
    df["trk_vehicle_mile"] = df[TRUCK_VOLUME_FIELD] * df["length"]
    df["trk_vehicle_hour"] = df[TRUCK_VOLUME_FIELD] * df[TRAVEL_TIME_FIELD]
    return df


def _add_trip_columns(df: pd.DataFrame) -> pd.DataFrame:
    mode_type_to_occupancy = {"sov": 1, "hov2": 2, "hov3": 3.5, "com": 1, "trk": 1, "apv": 1.6}
    df["trip_person_delay"] = 0.0
    df["trip_person_hour"] = 0.0
    df["trip_person_mile"] = 0.0
    df["trip_vehicle_mile"] = 0.0
    df["trip_vehicle_hour"] = 0.0
    df["trip_trk_vehicle_mile"] = 0.0
    df["trip_trk_vehicle_hour"] = 0.0

    if DISTRICT_ID_FIELD not in df.columns:
        return df

    for district_id in district_id_name_mapping:
        district_mask = df[DISTRICT_ID_FIELD] == district_id
        if not district_mask.any():
            continue
        total_trip_vol = np.zeros(district_mask.sum(), dtype=float)
        total_veh_volume = np.zeros(district_mask.sum(), dtype=float)
        for mode_type, occupancy in mode_type_to_occupancy.items():
            person_col = f"person_vol_district_{district_id}_{mode_type}"
            if person_col not in df.columns:
                continue
            values = df.loc[district_mask, person_col].fillna(0).to_numpy()
            total_trip_vol += values
            total_veh_volume += values / occupancy

        df.loc[district_mask, f"person_vol_district_{district_id}"] = total_trip_vol
        df.loc[district_mask, f"veh_vol_district_{district_id}"] = total_veh_volume
        df.loc[district_mask, "trip_person_delay"] = total_trip_vol * df.loc[district_mask, "delay"].fillna(0).to_numpy()
        df.loc[district_mask, "trip_person_hour"] = total_trip_vol * df.loc[district_mask, TRAVEL_TIME_FIELD].fillna(0).to_numpy()
        df.loc[district_mask, "trip_person_mile"] = total_trip_vol * df.loc[district_mask, "length"].fillna(0).to_numpy()
        df.loc[district_mask, "trip_vehicle_mile"] = total_veh_volume * df.loc[district_mask, "length"].fillna(0).to_numpy()
        df.loc[district_mask, "trip_vehicle_hour"] = total_veh_volume * df.loc[district_mask, TRAVEL_TIME_FIELD].fillna(0).to_numpy()

        truck_col = f"person_vol_district_{district_id}_trk"
        if truck_col in df.columns:
            truck_values = df.loc[district_mask, truck_col].fillna(0).to_numpy()
            df.loc[district_mask, "trip_trk_vehicle_mile"] = truck_values * df.loc[district_mask, "length"].fillna(0).to_numpy()
            df.loc[district_mask, "trip_trk_vehicle_hour"] = truck_values * df.loc[district_mask, TRAVEL_TIME_FIELD].fillna(0).to_numpy()

    return df


def link_performance_preprocess(
    network_dir,
    time_period_list,
    length_unit="mile",
    speed_unit="mph",
    developer_mode=0,
    period_range_list=None,
):
    if length_unit not in {"mile", "meter"} or speed_unit not in {"mph", "kph"}:
        raise ValueError("Invalid units. Length must be 'mile' or 'meter', and speed must be 'mph' or 'kph'.")
    if (length_unit == "mile" and speed_unit == "kph") or (length_unit == "meter" and speed_unit == "mph"):
        raise ValueError("Invalid unit combination. Use 'mile' with 'mph' or 'meter' with 'kph'.")

    period_ranges = (
        period_range_mapping(time_period_list, period_range_list)
        if period_range_list is not None
        else {}
    )
    processed_by_period = {}
    for time_period in time_period_list:
        period_key = time_period.lower()
        link_performance_path = resolve_period_file(network_dir, period_key, LINK_PERFORMANCE_FILENAME)
        link_path = resolve_period_file(network_dir, period_key, LINK_FILENAME)
        logger.info("Loading %s link performance from %s", period_key, link_performance_path)

        link_performance_df = _read_nonempty_csv(link_performance_path)
        if period_key in period_ranges:
            link_performance_df = retain_relevant_speed_columns(
                link_performance_df,
                period_ranges[period_key],
            )
        link_performance_df["time_period"] = period_key
        link_df = _ensure_link_fields(_read_nonempty_csv(link_path), period_key)

        required_fields = [
            LINK_ID_FIELD,
            FROM_NODE_ID_FIELD,
            TO_NODE_ID_FIELD,
            LINK_TYPE_FIELD,
            FREE_SPEED_FIELD,
            LENGTH_FIELD,
            LEGACY_LENGTH_MILE_FIELD,
            TAZ_FIELD,
            DISTRICT_ID_FIELD,
            FACILITY_TYPE_FIELD,
            TOLL_GROUP_FIELD,
            period_limit_field(period_key),
            GEOMETRY_FIELD,
        ]
        link_performance_df = _merge_missing_fields(link_performance_df, link_df, required_fields)
        link_performance_df = _add_hov_flag(link_performance_df, period_key)
        link_performance_df = _filter_regional_links(link_performance_df)

        if link_performance_df.empty or pd.to_numeric(link_performance_df.get(VOLUME_FIELD, 0), errors="coerce").sum() < 0.5:
            logger.warning("%s link performance is empty after filtering or has very low volume.", period_key)
            continue

        link_performance_df = _ensure_performance_columns(link_performance_df, length_unit, speed_unit)
        link_performance_df = _add_trip_columns(link_performance_df)
        hov_flag_mask = link_performance_df["is_hov"] == 1
        link_performance_df["hov_delay"] = link_performance_df["delay"].where(hov_flag_mask)
        link_performance_df["hov_person_delay"] = link_performance_df["person_delay"].where(hov_flag_mask)
        link_performance_df["hov_person_hour"] = link_performance_df["person_hour"].where(hov_flag_mask)
        link_performance_df["hov_person_mile"] = link_performance_df["person_mile"].where(hov_flag_mask)
        link_performance_df["trip_hov_person_delay"] = link_performance_df["trip_person_delay"].where(hov_flag_mask)
        link_performance_df["trip_hov_person_hour"] = link_performance_df["trip_person_hour"].where(hov_flag_mask)
        link_performance_df["trip_hov_person_mile"] = link_performance_df["trip_person_mile"].where(hov_flag_mask)
        processed_by_period[period_key] = link_performance_df

    if not processed_by_period:
        raise ValueError("No valid link_performance files were loaded to combine.")

    combined = pd.concat(
        [
            processed_by_period[period.lower()]
            for period in time_period_list
            if period.lower() in processed_by_period
        ],
        axis=0,
        ignore_index=True,
        sort=False,
    )
    output_path = Path(network_dir) / "link_performance_combined_processed.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(output_path, index=False)
    logger.info("Processed link performance data saved to: %s", output_path)
    return combined
