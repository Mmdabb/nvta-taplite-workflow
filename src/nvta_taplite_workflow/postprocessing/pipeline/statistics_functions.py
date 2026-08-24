import pandas as pd
import os
import traceback
import sys
import csv
import numpy as np
import logging
from dataclasses import dataclass
from nvta_taplite_workflow.dtalite4cube.settings.generate_dtalite_settings import (
    normalize_dtalite_period_hours,
    parse_period_time_hours,
)
from .linkperformance_fieldconfig import (
    DISTRICT_ID_FIELD,
    FROM_NODE_ID_FIELD,
    LINK_ID_FIELD,
    TO_NODE_ID_FIELD,
    link_performance_fields_mapping,
    district_id_name_mapping,
)
from .preprocessor import link_performance_preprocess

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeriodDurationDetails:
    original_duration: float
    effective_duration: float
    crosses_midnight: bool


def speed_class(speed):
    nb_range = np.floor(speed / 5)
    name = str(int(nb_range * 5)) + '_' + str(int((nb_range + 1) * 5))
    return name


def speed_class_series(speed_series):
    nb_range = np.floor(pd.to_numeric(speed_series, errors="coerce") / 5)
    lower = (nb_range * 5).astype("Int64").astype(str)
    upper = ((nb_range + 1) * 5).astype("Int64").astype(str)
    return lower + "_" + upper


def original_period_duration_hours(start_hour: int, end_hour: int) -> float:
    if end_hour < start_hour:
        end_hour += 24
    return float(end_hour - start_hour)


def effective_assignment_duration_hours(start_hour: int, end_hour: int) -> tuple[float, float, bool]:
    normalized_start, normalized_end, crosses_midnight = normalize_dtalite_period_hours(start_hour, end_hour)
    original_duration = original_period_duration_hours(start_hour, end_hour)
    effective_duration = float(normalized_end - normalized_start)
    return original_duration, effective_duration, crosses_midnight


def _period_duration_details(period_title: str, time_range: str) -> PeriodDurationDetails:
    start_hour, end_hour = parse_period_time_hours(time_range)
    original_duration, effective_duration, crosses_midnight = effective_assignment_duration_hours(
        start_hour,
        end_hour,
    )
    return PeriodDurationDetails(
        original_duration=original_duration,
        effective_duration=effective_duration,
        crosses_midnight=crosses_midnight,
    )


def time_period_duration_details(time_period_list, period_range_list):
    if isinstance(time_period_list, str) and isinstance(period_range_list, str):
        return _period_duration_details(time_period_list, period_range_list)

    duration_details_by_period = {}
    for period_title, time_range in zip(time_period_list, period_range_list):
        duration_details_by_period[period_title] = _period_duration_details(period_title, time_range)
    return duration_details_by_period


def time_period_duration(time_period_list, period_range_list):
    time_period_duration_dict = {}
    details = time_period_duration_details(time_period_list, period_range_list)
    for period_title, duration_info in details.items():
        original_duration = duration_info.original_duration
        effective_duration = duration_info.effective_duration
        if duration_info.crosses_midnight:
            logger.warning(
                "Postprocessing period %s uses truncated effective duration %.2f hours "
                "instead of original configured duration %.2f hours because DTALite "
                "assignment was truncated at midnight.",
                period_title,
                effective_duration,
                original_duration,
            )
        time_period_duration_dict[period_title] = effective_duration
    return time_period_duration_dict


def get_performance_stats(network_path, time_period_list, time_period_duration_list):
    combined_link_performance = link_performance_preprocess(
        network_path,
        time_period_list,
        period_range_list=time_period_duration_list,
    )
    time_duration_dict = time_period_duration(time_period_list, time_period_duration_list)
    performance_summary(combined_link_performance, network_path, time_duration_dict)


def get_diff_stats(output_path, bd_link_perf_combined, nb_link_perf_combined, time_period_list):

    speed_field_name = link_performance_fields_mapping['speed']
    person_volume_field_name = link_performance_fields_mapping['person_volume']
    severe_congestion_field_name = link_performance_fields_mapping['severe_congestion']

    performance_columns = [
        speed_field_name, 'length', person_volume_field_name, 'delay', 'person_delay', 'person_hour', 'person_mile',
        severe_congestion_field_name, 'length_weighted_P', 'vehicle_mile', 'vehicle_hour',
        'trk_vehicle_mile', 'trk_vehicle_hour', 'hov_delay', 'hov_person_delay',
        'hov_person_hour', 'hov_person_mile']

    # bd_link_perf_combined = link_performance_preprocess(bd_net_dir, time_period_list)
    # nb_link_perf_combined = link_performance_preprocess(nb_net_dir, time_period_list)

    for time_period in time_period_list:

        # Fixed the filter conditions
        nb_link_perf_filtered = nb_link_perf_combined[nb_link_perf_combined['time_period'] == time_period.lower()]
        bd_link_perf_filtered = bd_link_perf_combined[bd_link_perf_combined['time_period'] == time_period.lower()]

        nb_link_perf = nb_link_perf_filtered[[LINK_ID_FIELD, FROM_NODE_ID_FIELD, TO_NODE_ID_FIELD] + performance_columns].copy()
        bd_link_perf = bd_link_perf_filtered[[LINK_ID_FIELD, FROM_NODE_ID_FIELD, TO_NODE_ID_FIELD] + performance_columns].copy()

        # Merge on 'from_node_id' and 'to_node_id'
        merged_df = pd.merge(bd_link_perf, nb_link_perf, on=[FROM_NODE_ID_FIELD, TO_NODE_ID_FIELD], how='left',
                             suffixes=('_bd', '_nb'))
        for col in performance_columns:
            merged_df[f'{col}_diff'] = merged_df[f'{col}_bd'] - merged_df[f'{col}_nb']
            merged_df[f'{col}_diff'] = merged_df[f'{col}_diff'].fillna(-1)

        merged_df.to_csv(os.path.join(output_path, f'diff_{time_period}.csv'), index=False)
        spd_cls_stats(bd_link_perf, nb_link_perf, time_period, output_path)

        print(f'diff file for {time_period} generated')


def spd_cls_stats(bd_link_perf, nb_link_perf, period_name, output_folder):
    severe_congestion_field_name = link_performance_fields_mapping['severe_congestion']
    speed_field_name = link_performance_fields_mapping['speed']
    statistics_list = []

    nb_link_perf['speed_class'] = speed_class_series(nb_link_perf[speed_field_name])
    mile_over_hour_nb = nb_link_perf['person_mile'].sum() / np.maximum(nb_link_perf['person_hour'].sum(), 0.1)
    delay_over_hour_nb = nb_link_perf['person_delay'].sum() / np.maximum(nb_link_perf['person_hour'].sum(), 0.1)
    # keep 3 decimal places
    nb_stats_list = ['NB', format(nb_link_perf['delay'].sum(), ".7f"),
                     format(nb_link_perf['person_delay'].sum(), ".7f"),
                     format(nb_link_perf['person_hour'].sum(), ".7f"), format(nb_link_perf['person_mile'].sum(), ".7f"),
                     format(mile_over_hour_nb, ".7f"), format(delay_over_hour_nb, ".7f"),
                     nb_link_perf[severe_congestion_field_name].max(),
                     nb_link_perf[severe_congestion_field_name].mean(),
                     nb_link_perf['length_weighted_P'].sum(),
                     format(nb_link_perf['vehicle_mile'].sum(), ".7f"),
                     format(nb_link_perf['trk_vehicle_mile'].sum(), ".7f")]
    statistics_list.append(nb_stats_list)

    group = nb_link_perf.groupby('speed_class')
    for spd_class, sub_nb_df in group:
        sub_mile_over_hour_nb = sub_nb_df['person_mile'].sum() / np.maximum(sub_nb_df['person_hour'].sum(), 0.1)
        sub_delay_over_hour_nb = sub_nb_df['person_delay'].sum() / np.maximum(sub_nb_df['person_hour'].sum(), 0.1)
        nb_stats_list = [spd_class, format(sub_nb_df['delay'].sum(), ".7f"),
                         format(sub_nb_df['person_delay'].sum(), ".7f"),
                         format(sub_nb_df['person_hour'].sum(), ".7f"), format(sub_nb_df['person_mile'].sum(), ".7f"),
                         format(sub_mile_over_hour_nb, ".7f"), format(sub_delay_over_hour_nb, ".7f"),
                         sub_nb_df[severe_congestion_field_name].max(),
                         sub_nb_df[severe_congestion_field_name].mean(),
                         sub_nb_df['length_weighted_P'].sum(),
                         format(sub_nb_df['vehicle_mile'].sum(), ".7f"),
                         format(sub_nb_df['trk_vehicle_mile'].sum(), ".7f")]
        statistics_list.append(nb_stats_list)

    bd_link_perf['speed_class'] = speed_class_series(bd_link_perf[speed_field_name])
    mile_over_hour_bd = bd_link_perf['person_mile'].sum() / np.maximum(bd_link_perf['person_hour'].sum(), 0.1)
    delay_over_hour_bd = bd_link_perf['person_delay'].sum() / np.maximum(bd_link_perf['person_hour'].sum(), 0.1)
    # keep 3 decimal places
    bd_stats_list = ['BD', format(bd_link_perf['delay'].sum(), ".7f"),
                     format(bd_link_perf['person_delay'].sum(), ".7f"),
                     format(bd_link_perf['person_hour'].sum(), ".7f"), format(bd_link_perf['person_mile'].sum(), ".7f"),
                     format(mile_over_hour_bd, ".7f"), format(delay_over_hour_bd, ".7f"),
                     bd_link_perf[severe_congestion_field_name].max(),
                     bd_link_perf[severe_congestion_field_name].mean(),
                     bd_link_perf['length_weighted_P'].sum(),
                     format(bd_link_perf['vehicle_mile'].sum(), ".7f"),
                     format(bd_link_perf['trk_vehicle_mile'].sum(), ".7f")]
    statistics_list.append(bd_stats_list)

    group = bd_link_perf.groupby('speed_class')
    for spd_class, sub_bd_df in group:
        sub_mile_over_hour_nb = sub_bd_df['person_mile'].sum() / np.maximum(sub_bd_df['person_hour'].sum(), 0.1)
        sub_delay_over_hour_nb = sub_bd_df['person_delay'].sum() / np.maximum(sub_bd_df['person_hour'].sum(), 0.1)
        bd_stats_list = [spd_class, format(sub_bd_df['delay'].sum(), ".7f"),
                         format(sub_bd_df['person_delay'].sum(), ".7f"),
                         format(sub_bd_df['person_hour'].sum(), ".7f"), format(sub_bd_df['person_mile'].sum(), ".7f"),
                         format(sub_mile_over_hour_nb, ".7f"), format(sub_delay_over_hour_nb, ".7f"),
                         sub_bd_df[severe_congestion_field_name].max(),
                         sub_bd_df[severe_congestion_field_name].mean(),
                         sub_bd_df['length_weighted_P'].sum(),
                         format(sub_bd_df['vehicle_mile'].sum(), ".7f"),
                         format(sub_bd_df['trk_vehicle_mile'].sum(), ".7f")]
        statistics_list.append(bd_stats_list)

    diff_stats = ['DIFF', format(bd_link_perf['delay'].sum() - nb_link_perf['delay'].sum(), ".3f"),
                  format(bd_link_perf['person_delay'].sum() - nb_link_perf['person_delay'].sum(), ".3f"),
                  format(bd_link_perf['person_hour'].sum() - nb_link_perf['person_hour'].sum(), ".3f"),
                  format(bd_link_perf['person_mile'].sum() - nb_link_perf['person_mile'].sum(), ".3f"),
                  format(mile_over_hour_bd - mile_over_hour_nb, ".3f"),
                  format(delay_over_hour_bd - delay_over_hour_nb, ".3f"),
                  bd_link_perf[severe_congestion_field_name].max() - nb_link_perf[severe_congestion_field_name].max(),
                  bd_link_perf[severe_congestion_field_name].mean() - nb_link_perf[severe_congestion_field_name].mean(),
                  bd_link_perf['length_weighted_P'].sum() - nb_link_perf['length_weighted_P'].sum(),
                  format(bd_link_perf['vehicle_mile'].sum() - nb_link_perf['vehicle_mile'].sum(), ".3f"),
                  format(bd_link_perf['trk_vehicle_mile'].sum() - nb_link_perf['trk_vehicle_mile'].sum(), ".3f")]
    statistics_list.append(diff_stats)

    with open(output_folder + '/' + 'spd_class_statistics_' + period_name + '.csv', 'w', encoding='UTF8',
              newline='') as f:
        writer = csv.writer(f)
        header = [period_name, 'delay', 'person_delay', 'person_hour', 'person_mile',
                  'mile/hour', 'delay/hour', 'max_severe_congestion_duration', 'avg_severe_congestion_duration',
                  'length_weighted_congestion_duration', 'vehicle_mile', 'trk_vehicle_mile']
        writer.writerow(header)
        writer.writerows(statistics_list)


def performance_summary(link_performance_combined, network_dir, time_duration_dict, length_unit='mile',
                        speed_unit='mph', developer_mode=0):
    print("Generating performance summary ...")

    if length_unit not in {'mile', 'meter'} or speed_unit not in {'mph', 'kph'}:
        sys.exit("Error: Invalid units. Length must be 'mile' or 'meter', and speed must be 'mph' or 'kph'.")

    if (length_unit == 'mile' and speed_unit == 'kph') or (length_unit == 'meter' and speed_unit == 'mph'):
        sys.exit("Error: Invalid unit combination. Use 'mile' with 'mph' or 'meter' with 'kph'.")

    link_performance_field_names = list(link_performance_combined)
    link_performance_field_names = [fieldname for fieldname in link_performance_field_names if fieldname]
    link_performance_field_names_set = set(link_performance_field_names)

    severe_congestion_field_name = link_performance_fields_mapping['severe_congestion']

    aggregations = {
        'length': 'sum',
        'delay': 'sum',
        'person_delay': 'sum',
        'person_hour': 'sum',
        'person_mile': 'sum',
        severe_congestion_field_name: ['max', 'mean'],
        # Multiple aggregations for the same column # min (p_max, time period length) # min (p_max, time period length)
        'length_weighted_P': 'sum',
        'vehicle_mile': 'sum',
        'vehicle_hour': 'sum',
        'trk_vehicle_mile': 'sum',
        'trk_vehicle_hour': 'sum',
        'hov_delay': 'sum',
        'hov_person_delay': 'sum',
        'hov_person_hour': 'sum',
        'hov_person_mile': 'sum',
        'trip_person_delay': 'sum',
        'trip_person_hour': 'sum',
        'trip_person_mile': 'sum',
        'trip_vehicle_mile': 'sum',
        'trip_vehicle_hour': 'sum',
        'trip_trk_vehicle_mile': 'sum',
        'trip_trk_vehicle_hour': 'sum',
        'trip_hov_person_delay': 'sum',
        'trip_hov_person_hour': 'sum',
        'trip_hov_person_mile': 'sum'

    }

    available_columns = set(link_performance_combined.columns)
    missing_columns = [col for col in aggregations if col not in available_columns]
    if missing_columns:
        print(f"Warning: Missing columns: {missing_columns}")

    aggregations_filtered = {col: agg for col, agg in aggregations.items() if col in available_columns}
    aggregate_stats = {}
    if DISTRICT_ID_FIELD in link_performance_field_names_set:
        try:
            agg_by_district = link_performance_combined.groupby(['time_period', DISTRICT_ID_FIELD]).agg(
                aggregations_filtered).reset_index()
        except AttributeError as e:
            sys.exit(f"AttributeError: Invalid DataFrame or operation: {e}")
        except KeyError as e:
            # sys.exit(f"KeyError: Column not found: {e}")
            print(f"KeyError: Column not found: {e}")

        # agg_by_district['JUR_NAME'] = agg_by_district.apply(lambda x: district_id_name_mapping.setdefault(x.district_id, -1), axis=1)
        try:
            agg_by_district['jur_name'] = agg_by_district[DISTRICT_ID_FIELD].map(district_id_name_mapping).fillna(-1)
        except KeyError as e:
            print(f"KeyError: Missing 'district_id' in the mapping: {e}")


        try:
            agg_by_district['period_length'] = agg_by_district['time_period'].map(time_duration_dict).fillna(-1)
        except KeyError as e:
            print(f"KeyError: Missing 'time_period' in the mapping: {e}")

        try:
            agg_by_district['mile_over_hour'] = agg_by_district['person_mile'] / np.where(
                agg_by_district['person_hour'] > 0,
                agg_by_district['person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")

        try:
            agg_by_district['delay_over_hour'] = agg_by_district['person_delay'] / np.where(
                agg_by_district['person_hour'] > 0,
                agg_by_district['person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")

        try:
            agg_by_district['severe_congestion_length_weighted'] = agg_by_district['length_weighted_P'] / np.where(
                agg_by_district['length'] > 0,
                agg_by_district['length'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['severe_congestion_max'] = np.minimum(
                agg_by_district[(severe_congestion_field_name, 'max')],
                agg_by_district['period_length']
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['severe_congestion_mean'] = np.minimum(
                agg_by_district[(severe_congestion_field_name, 'mean')],
                agg_by_district['period_length']
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['vehicle_mile_over_hour'] = agg_by_district['vehicle_mile'] / np.where(
                agg_by_district['vehicle_hour'] > 0,
                agg_by_district['vehicle_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['trk_mile_over_hour'] = agg_by_district['trk_vehicle_mile'] / np.where(
                agg_by_district['trk_vehicle_hour'] > 0,
                agg_by_district['trk_vehicle_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['hov_mile_over_hour'] = agg_by_district['hov_person_mile'] / np.where(
                agg_by_district['hov_person_hour'] > 0,
                agg_by_district['hov_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['hov_delay_over_hour'] = agg_by_district['hov_person_delay'] / np.where(
                agg_by_district['hov_person_hour'] > 0,
                agg_by_district['hov_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")


        # trip based stats aggregated by district id
        #==============================================================================================================
        try:
            agg_by_district['trip_mile_over_hour'] = agg_by_district['trip_person_mile'] / np.where(
                agg_by_district['trip_person_hour'] > 0,
                agg_by_district['trip_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")

        try:
            agg_by_district['trip_delay_over_hour'] = agg_by_district['trip_person_delay'] / np.where(
                agg_by_district['trip_person_hour'] > 0,
                agg_by_district['trip_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")

        try:
            agg_by_district['trip_vehicle_mile_over_hour'] = agg_by_district['trip_vehicle_mile'] / np.where(
                agg_by_district['trip_vehicle_hour'] > 0,
                agg_by_district['trip_vehicle_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['trip_trk_mile_over_hour'] = agg_by_district['trip_trk_vehicle_mile'] / np.where(
                agg_by_district['trip_trk_vehicle_hour'] > 0,
                agg_by_district['trip_trk_vehicle_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['trip_hov_mile_over_hour'] = agg_by_district['trip_hov_person_mile'] / np.where(
                agg_by_district['trip_hov_person_hour'] > 0,
                agg_by_district['trip_hov_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        try:
            agg_by_district['trip_hov_delay_over_hour'] = agg_by_district['trip_hov_person_delay'] / np.where(
                agg_by_district['trip_hov_person_hour'] > 0,
                agg_by_district['trip_hov_person_hour'],
                np.nan
            )
        except KeyError as e:
            print(f"KeyError: Column not found: {e}")
            if developer_mode:
                traceback.print_exc()
                exc_type, exc_value, exc_tb = sys.exc_info()
                file_name = exc_tb.tb_frame.f_code.co_filename
                line_number = exc_tb.tb_lineno
                print(f"KeyError: {e} in file {file_name}, line {line_number}")

        #==========================================================================================================

        aggregate_stats['district'] = agg_by_district

    #         print("Aggregate stats by time period and district:")
    #         print(agg_by_district)
    #         agg_by_district.to_csv(os.path.join(network_dir, 'agg_by_district.csv'), index=False)

    try:
        agg_by_time = link_performance_combined.groupby('time_period').agg(aggregations_filtered).reset_index()
    except AttributeError as e:
        sys.exit(f"AttributeError: Invalid DataFrame or operation: {e}")
    except KeyError as e:
        sys.exit(f"KeyError: Column not found: {e}")


    try:
        agg_by_time['period_length'] = agg_by_time['time_period'].map(time_duration_dict).fillna(-1)
    except KeyError as e:
        sys.exit(f"KeyError: Column not found: {e}")


    agg_by_time['mile_over_hour'] = agg_by_time['person_mile'] / np.where(
        agg_by_time['person_hour'] > 0,
        agg_by_time['person_hour'],
        np.nan
    )

    agg_by_time['delay_over_hour'] = agg_by_time['person_delay'] / np.where(
        agg_by_time['person_hour'] > 0,
        agg_by_time['person_hour'],
        np.nan
    )

    try:
        agg_by_time['severe_congestion_length_weighted'] = agg_by_time['length_weighted_P'] / np.where(
            agg_by_time['length'] > 0,
            agg_by_time['length'],
            np.nan
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    try:
        agg_by_time['severe_congestion_max'] = np.minimum(
            agg_by_time[(severe_congestion_field_name, 'max')],
            agg_by_time['period_length']
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    try:
        agg_by_time['severe_congestion_mean'] = np.minimum(
            agg_by_time[(severe_congestion_field_name, 'mean')],
            agg_by_time['period_length']
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    agg_by_time['vehicle_mile_over_hour'] = agg_by_time['vehicle_mile'] / np.where(
        agg_by_time['vehicle_hour'] > 0,
        agg_by_time['vehicle_hour'],
        np.nan
    )

    agg_by_time['trk_mile_over_hour'] = agg_by_time['trk_vehicle_mile'] / np.where(
        agg_by_time['trk_vehicle_hour'] > 0,
        agg_by_time['trk_vehicle_hour'],
        np.nan
    )

    agg_by_time['hov_mile_over_hour'] = agg_by_time['hov_person_mile'] / np.where(
        agg_by_time['hov_person_hour'] > 0,
        agg_by_time['hov_person_hour'],
        np.nan
    )

    agg_by_time['hov_delay_over_hour'] = agg_by_time['hov_person_delay'] / np.where(
        agg_by_time['hov_person_hour'] > 0,
        agg_by_time['hov_person_hour'],
        np.nan
    )

    # trip based states time period aggregated
    # =====================================================================================================
    agg_by_time['trip_mile_over_hour'] = agg_by_time['trip_person_mile'] / np.where(
        agg_by_time['trip_person_hour'] > 0,
        agg_by_time['trip_person_hour'],
        np.nan
    )

    agg_by_time['trip_delay_over_hour'] = agg_by_time['trip_person_delay'] / np.where(
        agg_by_time['trip_person_hour'] > 0,
        agg_by_time['trip_person_hour'],
        np.nan
    )

    agg_by_time['trip_vehicle_mile_over_hour'] = agg_by_time['trip_vehicle_mile'] / np.where(
        agg_by_time['trip_vehicle_hour'] > 0,
        agg_by_time['trip_vehicle_hour'],
        np.nan
    )

    agg_by_time['trip_trk_mile_over_hour'] = agg_by_time['trip_trk_vehicle_mile'] / np.where(
        agg_by_time['trip_trk_vehicle_hour'] > 0,
        agg_by_time['trip_trk_vehicle_hour'],
        np.nan
    )

    agg_by_time['trip_hov_mile_over_hour'] = agg_by_time['trip_hov_person_mile'] / np.where(
        agg_by_time['trip_hov_person_hour'] > 0,
        agg_by_time['trip_hov_person_hour'],
        np.nan
    )

    agg_by_time['trip_hov_delay_over_hour'] = agg_by_time['trip_hov_person_delay'] / np.where(
        agg_by_time['trip_hov_person_hour'] > 0,
        agg_by_time['trip_hov_person_hour'],
        np.nan
    )
    # =======================================================================================================

    #     print("Aggregate stats by time period:")
    #     print(agg_by_time)
    agg_by_time['jur_name'] = 'overall'
    aggregate_stats['time'] = agg_by_time
    #     agg_by_time.to_csv(os.path.join(network_dir, 'agg_by_time.csv'), index=False)

    link_performance_combined['overall_flag'] = 1
    try:
        overall_agg = link_performance_combined.groupby('overall_flag').agg(aggregations).reset_index()
    except AttributeError as e:
        sys.exit(f"AttributeError: Invalid DataFrame or operation: {e}")
    except KeyError as e:
        sys.exit(f"KeyError: Column not found: {e}")


    overall_agg['time_period'] = 'overall'
    overall_agg['jur_name'] = 'overall'

    overall_agg['mile_over_hour'] = overall_agg['person_mile'] / np.where(
        overall_agg['person_hour'] > 0,
        overall_agg['person_hour'],
        np.nan
    )

    overall_agg['delay_over_hour'] = overall_agg['person_delay'] / np.where(
        overall_agg['person_hour'] > 0,
        overall_agg['person_hour'],
        np.nan
    )

    try:
        overall_agg['severe_congestion_length_weighted'] = overall_agg['length_weighted_P'] / np.where(
            overall_agg['length'] > 0,
            overall_agg['length'],
            np.nan
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    try:
        overall_agg['severe_congestion_max'] = np.max(
            agg_by_time['severe_congestion_max']
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    try:
        overall_agg['severe_congestion_mean'] = np.mean(
            agg_by_time['severe_congestion_mean']
        )
    except KeyError as e:
        print(f"KeyError: Column not found: {e}")
        if developer_mode:
            traceback.print_exc()
            exc_type, exc_value, exc_tb = sys.exc_info()
            file_name = exc_tb.tb_frame.f_code.co_filename
            line_number = exc_tb.tb_lineno
            print(f"KeyError: {e} in file {file_name}, line {line_number}")

    overall_agg['vehicle_mile_over_hour'] = overall_agg['vehicle_mile'] / np.where(
        overall_agg['vehicle_hour'] > 0,
        overall_agg['vehicle_hour'],
        np.nan
    )

    overall_agg['trk_mile_over_hour'] = overall_agg['trk_vehicle_mile'] / np.where(
        overall_agg['trk_vehicle_hour'] > 0,
        overall_agg['trk_vehicle_hour'],
        np.nan
    )

    overall_agg['hov_mile_over_hour'] = overall_agg['hov_person_mile'] / np.where(
        overall_agg['hov_person_hour'] > 0,
        overall_agg['hov_person_hour'],
        np.nan
    )

    overall_agg['hov_delay_over_hour'] = overall_agg['hov_person_delay'] / np.where(
        overall_agg['hov_person_hour'] > 0,
        overall_agg['hov_person_hour'],
        np.nan
    )


    # trip states overall
    # ==================================================================================================
    overall_agg['trip_mile_over_hour'] = overall_agg['trip_person_mile'] / np.where(
        overall_agg['trip_person_hour'] > 0,
        overall_agg['trip_person_hour'],
        np.nan
    )

    overall_agg['trip_delay_over_hour'] = overall_agg['trip_person_delay'] / np.where(
        overall_agg['trip_person_hour'] > 0,
        overall_agg['trip_person_hour'],
        np.nan
    )

    overall_agg['trip_vehicle_mile_over_hour'] = overall_agg['trip_vehicle_mile'] / np.where(
        overall_agg['trip_vehicle_hour'] > 0,
        overall_agg['trip_vehicle_hour'],
        np.nan
    )

    overall_agg['trip_trk_mile_over_hour'] = overall_agg['trip_trk_vehicle_mile'] / np.where(
        overall_agg['trip_trk_vehicle_hour'] > 0,
        overall_agg['trip_trk_vehicle_hour'],
        np.nan
    )

    overall_agg['trip_hov_mile_over_hour'] = overall_agg['trip_hov_person_mile'] / np.where(
        overall_agg['trip_hov_person_hour'] > 0,
        overall_agg['trip_hov_person_hour'],
        np.nan
    )

    overall_agg['trip_hov_delay_over_hour'] = overall_agg['trip_hov_person_delay'] / np.where(
        overall_agg['trip_hov_person_hour'] > 0,
        overall_agg['trip_hov_person_hour'],
        np.nan
    )


    # ==================================================================================================

    aggregate_stats['overall'] = overall_agg

    # overall_agg = link_performance_combined.agg(aggregations).reset_index()
    #     overall_agg.to_csv(os.path.join(network_dir, 'overall_agg.csv'), index=False)

    #     print("Overall Aggregate:")
    #     print(overall_agg)

    statistics_data = pd.concat([aggregate_stats[data_frame]
                                 for data_frame in aggregate_stats.keys()],
                                axis=0, ignore_index=True)

    statistics_data_columns = ['time_period', 'jur_name', 'delay', 'person_delay', 'person_hour', 'person_mile',
                               'mile_over_hour', 'delay_over_hour',
                               'severe_congestion_length_weighted', 'severe_congestion_max', 'severe_congestion_mean',
                               'vehicle_mile', 'vehicle_hour',
                               'vehicle_mile_over_hour', 'trk_vehicle_mile', 'trk_vehicle_hour', 'trk_mile_over_hour',
                               'hov_delay', 'hov_person_delay',
                               'hov_person_mile', 'hov_mile_over_hour', 'hov_delay_over_hour',
                               'trip_person_delay', 'trip_person_hour', 'trip_person_mile',
                               'trip_mile_over_hour', 'trip_delay_over_hour',
                               'trip_vehicle_mile', 'trip_vehicle_hour',
                               'trip_vehicle_mile_over_hour',
                               'trip_trk_vehicle_mile', 'trip_trk_vehicle_hour',
                               'trip_trk_mile_over_hour',
                               'trip_hov_person_delay', 'trip_hov_person_hour', 'trip_hov_person_mile',
                               'trip_hov_mile_over_hour', 'trip_hov_delay_over_hour'
                               ]
    statistics_data = statistics_data[statistics_data_columns]
    statistics_data_dir = os.path.join(network_dir, 'statistics_data.csv')
    statistics_data.to_csv(statistics_data_dir, index=False, float_format="%.2f")
    print(f"Performance statistics saved to: {statistics_data_dir}")
    print('============================================================================================================')
