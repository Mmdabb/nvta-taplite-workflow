import pandas as pd
import geopandas as gpd
from shapely import wkt, geometry
import numpy as np
import time
import csv
import sys
import os
import contextlib
import io
import pickle
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from .mapclass import Mapping, DependentMapping
from .netclass import Node, Link, Network
from .congestion_boundaries import BOUNDARY_FIELDS, apply_congestion_boundaries
from ..resources.observed_link_plf_lookup.load_observed_link_plf import (
    PLF_FIELDS as OBSERVED_PLF_FIELDS,
    load_lookup as load_observed_plf_lookup,
    lookup as lookup_observed_plf,
    lookup_path as observed_plf_lookup_path,
)
from ..resources.observed_link_speed_boundary_lookup.load_observed_link_speed_boundaries import (
    SPEED_FIELDS as OBSERVED_SPEED_FIELDS,
    load_lookup as load_observed_speed_boundary_lookup,
    lookup as lookup_observed_speed_boundaries,
    lookup_path as observed_speed_boundary_lookup_path,
)
from ..network_cache import (
    load_network_cache,
    network_source_fingerprint,
    save_network_cache,
)
from ..parallel_utils import choose_chunks_per_group, choose_worker_plan, chunk_ranges
from ..unit_contract import (
    TAPLITE_GENERIC_LENGTH_UNIT,
    TAPLITE_GENERIC_SPEED_UNIT,
    miles_to_taplite_length,
    mph_to_taplite_free_speed,
    validate_taplite_converter_units,
    validate_taplite_link_values,
)
from .fieldnameconfig import (
    bpr_vdf_params,
    bpr_vdf_source_field_template,
    capacity_field,
    crs_field,
    district_id_field,
    from_node_id_field,
    generated_jurisdiction_field,
    intersection_field,
    its_field,
    jurisdiction_name_field,
    length_mile_internal_field,
    link_file_template,
    link_id_field,
    new_allowed_uses_field,
    new_fftt_field,
    new_ref_cost_field,
    new_ref_volume_field,
    new_toll_field,
    new_vdf_free_speed_mph_field,
    new_vdf_length_mi_field,
    new_vdf_type_field,
    node_id_field,
    node_file_name,
    pair_field,
    period_link_file_name,
    qvdf_params,
    qvdf_source_field_template,
    supported_mode_toll_types,
    taz_field,
    taz_jurisdiction_file_name,
    time_period_dict,
    to_node_id_field,
)
from .fieldnameconfig import cube_node_mapping, cube_base_link_mapping, cube_link_dependent_mapping
from .fieldnameconfig import dtalite_node_mapping, dtalite_base_link_mapping, dtalite_additional_link_mapping
from .netconfig import alpha_dict, beta_dict, allowed_uses_dict, speed_class_dict, capacity_class_dict
from .vdf_lookup_tables import get_vdf_dict
from datetime import datetime, timedelta

OBSERVED_QVDF_PERIODS = frozenset(OBSERVED_PLF_FIELDS).intersection(
    OBSERVED_SPEED_FIELDS
)

try:
    from pyproj import CRS
except ImportError:  # pragma: no cover - geopandas environments should include pyproj
    CRS = None


def convert_to_datetime(time_str):
    return datetime.strptime(time_str, '%H%M')


def time_period_duration(time_period_list, period_range_list):
    time_period_duration_dict = {}
    for period_title, time_range in zip(time_period_list, period_range_list):
        start_time_str, end_time_str = time_range.split('_')
        start_time =  datetime.strptime(start_time_str, '%H%M')
        end_time = datetime.strptime(end_time_str, '%H%M')
        time_duration = end_time - start_time
        if time_duration.days < 0:
            time_duration = time_duration + timedelta(days=1)
        time_period_duration_dict[period_title] = time_duration.total_seconds() / 3600
    return time_period_duration_dict


def _observed_qvdf_period_supported(time_period):
    return str(time_period).upper() in OBSERVED_QVDF_PERIODS


def _resolve_prj_path(shapefile_path):
    root, extension = os.path.splitext(shapefile_path)
    if extension.lower() == ".shp":
        return f"{root}.prj"
    return os.path.join(shapefile_path, "DTALiteNetworkInput.prj")


def _format_crs_for_csv(crs):
    if crs is None:
        return ""

    try:
        authority = crs.to_authority()
        if authority:
            return f"{authority[0]}:{authority[1]}"
    except AttributeError:
        pass

    try:
        return crs.to_string()
    except AttributeError:
        return str(crs)


def _load_crs_from_prj(shapefile_path):
    prj_path = _resolve_prj_path(shapefile_path)
    if not os.path.exists(prj_path):
        print(f"WARNING: CRS sidecar .prj file was not found for shapefile: {prj_path}")
        return None

    if CRS is None:
        print("WARNING: pyproj is not available; CRS could not be parsed from .prj.")
        return None

    with open(prj_path, "r", encoding="utf-8", errors="replace") as prj_file:
        prj_wkt = prj_file.read().strip()

    if not prj_wkt:
        print(f"WARNING: CRS sidecar .prj file is empty: {prj_path}")
        return None

    return CRS.from_wkt(prj_wkt)


DEFAULT_OUTPUT_CRS = "EPSG:4326"


def _prepare_network_crs_and_geometry(network_shapefile, shapefile_path, target_crs=DEFAULT_OUTPUT_CRS):
    if network_shapefile.crs is None:
        prj_crs = _load_crs_from_prj(shapefile_path)
        if prj_crs is not None:
            network_shapefile = network_shapefile.set_crs(prj_crs, allow_override=True)

    source_crs_text = _format_crs_for_csv(network_shapefile.crs)
    if source_crs_text:
        print(f"Loaded shapefile CRS: {source_crs_text}")
    else:
        print("WARNING: Shapefile CRS is unknown; generated link.csv crs values will be blank.")
        return network_shapefile, "", ""

    if target_crs:
        network_shapefile = network_shapefile.to_crs(target_crs)
        output_crs_text = _format_crs_for_csv(network_shapefile.crs)
        print(f"Reprojected shapefile geometry to output CRS: {output_crs_text}")
    else:
        output_crs_text = source_crs_text

    return network_shapefile, output_crs_text, source_crs_text


def loadCSVfromSHP(shapefile_path, target_crs=DEFAULT_OUTPUT_CRS):
    print(f'Loading shapefile - {shapefile_path}  with geometry ...')
    network_shapefile = gpd.read_file(shapefile_path)
    network_shapefile, crs_text, source_crs_text = _prepare_network_crs_and_geometry(
        network_shapefile,
        shapefile_path,
        target_crs=target_crs,
    )
    network_shapefile.attrs["crs_text"] = crs_text
    network_shapefile.attrs["source_crs_text"] = source_crs_text
    network_shapefile[cube_base_link_mapping['link_id_field']] = range(1, len(network_shapefile) + 1)
    # network_shapefile.plot()
    print('Shapefile loaded successfully.')
    return network_shapefile


def _format_link_file_name(time_period):
    return link_file_template.format(time_period=time_period)


def _resolve_link_file_name(time_period, link_filename=None):
    return link_filename or _format_link_file_name(time_period)


def _bpr_vdf_source_field(vdf_param, period_sequence):
    return bpr_vdf_source_field_template.format(
        vdf_param=vdf_param,
        period_sequence=period_sequence,
    )


def _qvdf_source_field(qvdf_param, period_sequence):
    return qvdf_source_field_template.format(
        qvdf_param=qvdf_param,
        period_sequence=period_sequence,
    )


def _old_style_vdf_field(field_name):
    return (
        (field_name.startswith("VDF_") and field_name[-1:].isdigit())
        or (field_name.startswith("QVDF_") and field_name[-1:].isdigit())
    )


def _vdf_source_field(vdf_type, vdf_param, period_sequence):
    if vdf_type == "bpr":
        return _bpr_vdf_source_field(vdf_param, period_sequence)
    return _qvdf_source_field(vdf_param, period_sequence)


def _select_vdf_record(vdf_dict, link_type, *, require_all=True):
    """Return an exact link-type row, otherwise the required ``all`` row."""

    link_type_key = str(link_type)
    if link_type_key in vdf_dict:
        return link_type_key, vdf_dict[link_type_key]
    if "all" in vdf_dict:
        return "all", vdf_dict["all"]
    if not require_all:
        return link_type_key, {}
    raise KeyError(
        f"VDF mapping has no row for link_type={link_type_key} and no 'all' fallback"
    )


def _vdf_parameter_value(
    vdf_record,
    *,
    vdf_type,
    vdf_field,
    time_sequence,
    selected_vdf_key,
    link_type,
):
    source_field = _vdf_source_field(vdf_type, vdf_field, time_sequence)
    value = vdf_record.get(source_field)
    if value is not None and str(value).strip() != "":
        return value
    if vdf_type == "qvdf":
        raise ValueError(
            f"QVDF field {source_field!r} is missing for link_type={link_type}; "
            f"selected link_qvdf.csv row={selected_vdf_key!r}"
        )
    return 1 if vdf_field == "plf" else np.nan


def _node_value(node, semantic_key):
    if hasattr(node, semantic_key):
        return getattr(node, semantic_key)
    return node.other_attrs.get(semantic_key)


def _link_value(link, semantic_key):
    if semantic_key == from_node_id_field:
        return link.from_node.node_id
    if semantic_key == to_node_id_field:
        return link.to_node.node_id
    if semantic_key == "geometry":
        link_geometry = link.geometry
        return link_geometry.wkt if hasattr(link_geometry, "wkt") else link_geometry
    if hasattr(link, semantic_key):
        return getattr(link, semantic_key)
    return link.other_attrs.get(semantic_key, 0)


def _is_explicitly_closed_allowed_use(value):
    """Return true only for the converter's explicit closed sentinel."""

    if value is None or pd.isna(value):
        return False
    return str(value).strip().lower() == "closed"


def _observed_plf_override(
    default_value,
    *,
    link_is_closed,
    found,
    observed_value,
    from_node_id,
    to_node_id,
    time_period,
):
    """Apply a direct observed PLF only to an open mapped link."""

    if link_is_closed or not found:
        return default_value
    observed_value = float(observed_value)
    if not np.isfinite(observed_value) or observed_value <= 0.0:
        raise ValueError(
            "Observed-link PLF override must be finite and positive "
            f"for {from_node_id}->{to_node_id} {str(time_period).upper()}"
        )
    return observed_value


def _assign_observed_speed_boundaries(
    link,
    *,
    link_is_closed,
    found,
    observed_values,
    from_node_id,
    to_node_id,
    time_period,
):
    """Validate and assign anchors independently of period link availability."""

    fields = ("qvdf_start_speed_mph", "qvdf_end_speed_mph")
    for field_name in fields:
        link.other_attrs[field_name] = ""
    # Availability/allowed_use governs whether a link can carry flow. Speed
    # anchors describe the link's boundary condition and remain useful for a
    # mapped link even when it is closed during this period.
    if not found:
        return
    for field_name, observed_speed in zip(fields, observed_values):
        if np.isnan(observed_speed):
            continue
        observed_speed = float(observed_speed)
        if not np.isfinite(observed_speed) or not 0.0 < observed_speed <= 150.0:
            raise ValueError(
                "Observed speed boundary must be blank or within (0, 150] mph "
                f"for {from_node_id}->{to_node_id} {str(time_period).upper()} "
                f"({field_name}={observed_speed})"
            )
        link.other_attrs[field_name] = observed_speed


def _sort_key(value):
    if pd.isna(value):
        return (1, "")
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (0, str(value))


def _node_sort_key(node):
    return (
        _sort_key(_node_value(node, node_id_field)),
        _sort_key(_node_value(node, "zone_id")),
    )


def _link_sort_key(link):
    return (
        _sort_key(_link_value(link, from_node_id_field)),
        _sort_key(_link_value(link, to_node_id_field)),
    )


def _sort_dataframe_for_csv(df, columns):
    sort_columns = [column for column in columns if column in df.columns]
    if not sort_columns:
        return df

    sort_frame = df.copy()
    helper_columns = []
    for column in sort_columns:
        helper_column = f"__sort_{column}"
        helper_columns.append(helper_column)
        numeric_values = pd.to_numeric(sort_frame[column], errors="coerce")
        sort_frame[helper_column] = (
            numeric_values
            if numeric_values.notna().all()
            else sort_frame[column].astype(str)
        )

    sorted_frame = sort_frame.sort_values(by=helper_columns, kind="mergesort").drop(columns=helper_columns)
    return sorted_frame.reset_index(drop=True)


def linestring_to_points(feature, line):
    return {feature: line.coords}


def poly_to_points(feature, poly):
    return {feature: poly.exterior.coords}


def _line_geometry_parts(line_geometry):
    """Return the ordered LineString parts without changing source geometry."""
    if line_geometry is None or line_geometry.is_empty:
        raise ValueError("Link geometry is empty.")

    geometry_type = line_geometry.geom_type
    if geometry_type == "LineString":
        return (line_geometry,)
    if geometry_type == "MultiLineString":
        parts = tuple(part for part in line_geometry.geoms if not part.is_empty)
        if parts:
            return parts
        raise ValueError("Link MultiLineString geometry has no non-empty parts.")

    raise TypeError(
        f"Unsupported link geometry type {geometry_type!r}; "
        "expected LineString or MultiLineString."
    )


def _line_geometry_endpoints(line_geometry):
    """Return only the topological endpoints while retaining all shape vertices."""
    parts = _line_geometry_parts(line_geometry)
    first_part_coordinates = tuple(parts[0].coords)
    last_part_coordinates = tuple(parts[-1].coords)
    if not first_part_coordinates or not last_part_coordinates:
        raise ValueError("Link geometry has no coordinates.")
    return first_part_coordinates[0], last_part_coordinates[-1]


def _loadNodes(network_gmns, network_shapefile):
    print('Loading nodes ...')
    print('Node IDs below 10,000 will be treated as zone IDs.')

    field_mapping = Mapping(**cube_node_mapping)
    _node_required_fields = set(vars(field_mapping).values())

    fieldnames = list(network_shapefile)
    if '' in fieldnames:
        print('WARNING: columns with an empty header are detected in the network file. these columns will be skipped')
        fieldnames = [fieldname for fieldname in fieldnames if fieldname]

    fieldnames_set = set(fieldnames)
    for field in _node_required_fields:
        if field not in fieldnames_set:
            sys.exit(f'ERROR: required field ({field}) for generating node file does not exist in the network file')

    node_ids_seen = set()
    node_dict = {}

    geometries = network_shapefile[field_mapping.geometry_field].to_numpy(copy=False)
    from_node_ids = network_shapefile[field_mapping.from_node_field].to_numpy(copy=False)
    to_node_ids = network_shapefile[field_mapping.to_node_field].to_numpy(copy=False)

    for row_position, link_geometry in enumerate(geometries):
        from_coordinate, to_coordinate = _line_geometry_endpoints(link_geometry)

        # for one-way route-able road only
        for node_id, coordinate in (
            (int(from_node_ids[row_position]), from_coordinate),
            (int(to_node_ids[row_position]), to_coordinate),
        ):
            if node_id in node_ids_seen:
                continue

            node = Node(node_id)
            node.geometry = geometry.Point(coordinate)
            node.x_coord, node.y_coord = float(coordinate[0]), float(coordinate[1])

            if node.node_id < 10000:
                node.zone_id = node.node_id

            node_dict[node.node_id] = node
            node_ids_seen.add(node.node_id)

    network_gmns.node_dict = node_dict
    # node.csv contains only the DTALite node mapping above. Copying every DBF
    # link attribute into each node was unused and dominated conversion time.
    network_gmns.node_other_attrs = []

    print('%s nodes loaded successfully.' % len(node_ids_seen))


def _loadLinks(
    network_gmns,
    network_shapefile,
    time_period,
    time_period_list,
    vdf_dict,
    length_unit=TAPLITE_GENERIC_LENGTH_UNIT,
    speed_unit=TAPLITE_GENERIC_SPEED_UNIT,
    vdf_type='bpr',
):

    validate_taplite_converter_units(length_unit, speed_unit)
    print('Loading links ...')
    print(f'Time period: {time_period}')
    # Define dtalite field mappings
    dtalite_field_mapping = Mapping(**dtalite_base_link_mapping)
    dtalite_dep_field_mapping = DependentMapping(**dtalite_additional_link_mapping)

    # Define required and optional fields
    cube_field_mapping = Mapping(**cube_base_link_mapping)
    cube_timedep_field_mapping = DependentMapping(**cube_link_dependent_mapping)
    _link_required_fields = set(vars(cube_field_mapping).values())

    # for t_period in time_period_list:
    for class_key in vars(cube_timedep_field_mapping).keys():
        _link_required_fields.add(cube_timedep_field_mapping.get_field(class_key, time_period.upper()))

    _link_optional_fields = {}

    # Check for empty headers in field names
    fieldnames = list(network_shapefile)
    if '' in fieldnames:
        print('WARNING: columns with an empty header are detected in the network file. these columns will be skipped')
        fieldnames = [fieldname for fieldname in fieldnames if fieldname]

    fieldnames_set = set(fieldnames)

    # Check if all required fields exist
    for field in _link_required_fields:
        if field not in fieldnames_set:
            sys.exit(f'ERROR: required field ({field}) for generating link file does not exist in the network file')

    # Extract other fields
    other_fields = sorted(
        field
        for field in fieldnames_set.difference(_link_required_fields.union(_link_optional_fields))
        if not _old_style_vdf_field(field)
    )

    # Initialize dictionaries and variables
    node_dict = network_gmns.node_dict
    link_dict = {}
    network_crs = network_shapefile.attrs.get("crs_text", _format_crs_for_csv(network_shapefile.crs))
    column_values = {
        field: network_shapefile[field].to_numpy(copy=False)
        for field in fieldnames
    }
    observed_plf_values = np.full(len(network_shapefile), np.nan, dtype=np.float32)
    observed_plf_found = np.zeros(len(network_shapefile), dtype=bool)
    observed_period = str(time_period).upper()
    observed_period_supported = _observed_qvdf_period_supported(observed_period)
    if (
        vdf_type == "qvdf"
        and observed_period_supported
        and observed_plf_lookup_path().is_file()
    ):
        observed_plf_values, observed_plf_found = lookup_observed_plf(
            load_observed_plf_lookup(),
            time_period,
            column_values[cube_field_mapping.from_node_field],
            column_values[cube_field_mapping.to_node_field],
        )
    observed_speed_boundary_values = np.full(
        (len(network_shapefile), 2), np.nan, dtype=np.float32
    )
    observed_speed_boundary_found = np.zeros(len(network_shapefile), dtype=bool)
    if (
        vdf_type == "qvdf"
        and observed_period_supported
        and observed_speed_boundary_lookup_path().is_file()
    ):
        (
            observed_speed_boundary_values,
            observed_speed_boundary_found,
        ) = lookup_observed_speed_boundaries(
            load_observed_speed_boundary_lookup(),
            time_period,
            column_values[cube_field_mapping.from_node_field],
            column_values[cube_field_mapping.to_node_field],
        )
    if vdf_type == "qvdf" and not observed_period_supported:
        print(
            f"Observed QVDF lookup {observed_period}: no observed calibration "
            "for this period; using the built-in period defaults."
        )

    # A dictionary for allowed agents for toll pricing calculations
    toll_allowed_uses_dict = {}
    toll_allowed_uses_set = set()
    for usedict_key, usedict_value in allowed_uses_dict.items():
        if usedict_key < 6:  # Take only the first 5 items
            uses = usedict_value.split(';')
            toll_allowed_uses_list = []
            for use in uses:
                toll_allowed_uses_set.add(use)
                toll_allowed_uses_list.append(use)
            toll_allowed_uses_dict[usedict_key] = toll_allowed_uses_list

    # time_duration_dict = time_period_duration(time_period_list, time_period_duration_list)
    time_sequence = time_period_dict[time_period.upper()]
    # time_duration = time_duration_dict[time_sequence]

    # Process each link in the shapefile
    for row_position in range(len(network_shapefile)):

        cube_link_id_field = cube_field_mapping.link_id_field
        source_link_id = int(column_values[cube_link_id_field][row_position])
        link = Link(source_link_id)
        link.org_link_id = source_link_id

        # Extract from_node and to_node IDs
        from_node_field = cube_field_mapping.from_node_field
        to_node_field = cube_field_mapping.to_node_field
        from_node_id = int(column_values[from_node_field][row_position])
        to_node_id = int(column_values[to_node_field][row_position])
        # from_node_id, to_node_id = int(network_shapefile[cube_field_mapping.from_node_field][index]),
        # int(network_shapefile[cube_field_mapping.to_node_field][index])

        if from_node_id == to_node_id:
            print(f'WARNING: from_node and to_node of link {link.link_id} are the same')
            continue

        try:
            link.from_node = node_dict[from_node_id]
        except KeyError:
            print(f'WARNING: from_node {from_node_id} of link {link.link_id} does not exist in the node file')
            continue
        try:
            link.to_node = node_dict[to_node_id]
        except KeyError:
            print(f'WARNING: to_node {to_node_id} of link {link.link_id} does not exist in the node file')
            continue

        # TAPlite expects the generic GMNS length in meters and the explicit
        # VDF override in miles.
        cube_distance_field = cube_field_mapping.distance_field
        length_in_mile = column_values[cube_distance_field][row_position]

        length = miles_to_taplite_length(length_in_mile)

        try:
            link.length = float(length)
        except ValueError:
            print(f'WARNING: Non-numeric value encountered in "Distance" field for link ID {link.org_link_id}. '
                  f'Assigning zero to "length" for GMNS link ID {link.link_id}.')
            link.length = 0  # or some small values?

        try:
            link.other_attrs[length_mile_internal_field] = float(length_in_mile)
        except ValueError:
            link.other_attrs[length_mile_internal_field] = 0

        link.other_attrs[new_vdf_length_mi_field] = link.other_attrs[length_mile_internal_field]

        cube_lane_field = cube_timedep_field_mapping.get_field('lane_field', time_period.upper())
        try:
            lanes = int(column_values[cube_lane_field][row_position])
        except ValueError:
            print(f'WARNING: a non-integer value encountered in {cube_lane_field} field for '
                  f'link ID {link.org_link_id} in the network shapefile.'
                  f'This link will be removed from the link file.')
            continue

        if lanes <= 0:
            print(f'WARNING: Link ID {link.org_link_id} has {lanes} lanes. Skipping this link.')
            continue  # Skip the current link if it has 0 or fewer lanes

        link.lanes = lanes
        # Extract link geometry
        cube_geometry_field = cube_field_mapping.geometry_field
        link.geometry = column_values[cube_geometry_field][row_position]

        # Extract Facility and Area Types: AT and FT are will be used for link type calculation
        # The calculation is as follows: 10**2 * area type (AT) + facility type (FT)
        cube_at_field = cube_field_mapping.area_type_field
        try:
            AT = int(column_values[cube_at_field][row_position])
        except ValueError:
            print(
                f'WARNING: a non-integer value encountered in {cube_at_field} field for link ID {link.org_link_id} '
                f'in network shape file, hence 0 is assigned to "AT" for GMNS link ID {link.link_id}. \n'
                f'This will impact link type and vdf code assignment for the specified GMNS link'
            )
            AT = 0

        cube_ft_field = cube_field_mapping.facility_type_field
        try:
            FT = int(column_values[cube_ft_field][row_position])
        except ValueError:
            print(
                f'WARNING: a non-integer value encountered in {cube_ft_field} field for link ID {link.org_link_id} '
                f'in network shape file, hence 0 is assigned to "FT" for GMNS link ID {link.link_id} '
                f'(The link will be treated as a connector).  \n'
                f'This will impact link type and vdf code assignment for the specified GMNS link'
            )
            FT = 0

        link.other_attrs[cube_ft_field] = column_values[cube_ft_field][row_position]
        link_type = 10 ** 2 * int(AT) + int(FT)
        link.link_type = link_type
        link.vdf_code = link_type

        # Extract link capacity information
        cube_capclass_field = cube_field_mapping.capacity_class
        try:
            cap_class = int(column_values[cube_capclass_field][row_position])
        except ValueError:  # KeyError should be added with sys.exist
            print(
                f'WARNING: a non-integer value encountered in {cube_capclass_field} field for link ID {link.org_link_id} '
                f'in network shape file. Skipping this link.'
            )
            continue
            # cap_class = 13

        try:
            capacity = capacity_class_dict[cap_class]
            link.capacity = int(capacity)
        except KeyError:
            print(
                f'WARNING: the {cube_capclass_field} for link ID {link.org_link_id} in network shape file does not '
                f'exist in the defined capacity classes. Skipping this link.')
            continue
            # link.capacity = 2000

        # Extract link free speed information
        cube_spdclass_field = cube_field_mapping.speed_class
        try:
            spd_class = int(column_values[cube_spdclass_field][row_position])
        except ValueError:  # KeyError should be added with sys.exist
            print(
                f'WARNING: a non-integer value encountered in {cube_spdclass_field} field for link ID {link.org_link_id} '
                f'in network shape file. Skipping this link.'
            )
            continue

        try:
            free_speed = speed_class_dict[spd_class]
        except KeyError:
            print(
                f'WARNING: the {cube_spdclass_field} for link ID {link.org_link_id} in network shape file does not '
                f'exist in the defined capacity classes. Skipping this link.')
            continue

        # Resolve the period closure before applying any observed-link
        # resources. Closed links remain in link.csv for network fidelity, but
        # they must never receive TMC-derived PLF, speed anchors, or episode
        # boundaries.
        cube_limit_field = cube_timedep_field_mapping.get_field(
            'limit_field', time_period.upper()
        )
        link.other_attrs[cube_limit_field] = column_values[cube_limit_field][row_position]
        try:
            allowed_uses_key = int(column_values[cube_limit_field][row_position])
        except KeyError:
            sys.exit(
                f'ERROR: The field "{cube_limit_field}" was not found in network_shapefile.'
            )
        except (TypeError, ValueError):
            print(
                f'WARNING: A non-integer value encountered in {cube_limit_field} field for link ID {link.org_link_id} '
                'in network shape file. Assigning zero.'
            )
            allowed_uses_key = 0

        allowed_uses = allowed_uses_dict[allowed_uses_key]
        if allowed_uses_key >= 0:
            link.other_attrs[new_allowed_uses_field] = allowed_uses
        link_is_closed = _is_explicitly_closed_allowed_use(allowed_uses)

        link.other_attrs[new_vdf_free_speed_mph_field] = int(free_speed)

        link.free_speed = mph_to_taplite_free_speed(int(free_speed))

        vdf_fields = bpr_vdf_params if vdf_type == 'bpr' else qvdf_params
        selected_vdf_key, vdf_record = _select_vdf_record(
            vdf_dict,
            link_type,
            require_all=vdf_type == 'qvdf',
        )
        for vdf_field in vdf_fields:
            vdf_value = _vdf_parameter_value(
                vdf_record,
                vdf_type=vdf_type,
                vdf_field=vdf_field,
                time_sequence=time_sequence,
                selected_vdf_key=selected_vdf_key,
                link_type=link_type,
            )
            if vdf_field == "plf":
                vdf_value = _observed_plf_override(
                    vdf_value,
                    link_is_closed=link_is_closed,
                    found=observed_plf_found[row_position],
                    observed_value=observed_plf_values[row_position],
                    from_node_id=from_node_id,
                    to_node_id=to_node_id,
                    time_period=time_period,
                )

            dtalite_vdf_field = dtalite_dep_field_mapping.get_field('vdf_parameter', vdf_field)

            link.other_attrs[dtalite_vdf_field] = float(vdf_value) if vdf_value else vdf_value

        if vdf_type == "qvdf":
            # Keep the schema stable for every QVDF link. Blank values tell the
            # kernel to retain its existing modeled/free-speed boundary behavior.
            _assign_observed_speed_boundaries(
                link,
                link_is_closed=link_is_closed,
                found=observed_speed_boundary_found[row_position],
                observed_values=observed_speed_boundary_values[row_position],
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                time_period=time_period,
            )

        # Extract tolls after the closure-aware QVDF resource mapping.
        cube_toll_field = cube_timedep_field_mapping.get_field('toll_field', time_period.upper())
        try:
            toll = column_values[cube_toll_field][row_position] / 100  # cents -> dollars
        except KeyError:
            print(f'Warning: The field "{cube_toll_field}" was not found in network_shapefile.')
            toll = 0

        vdf_fftt = 0
        if free_speed > 0:
            vdf_fftt = 60 * length_in_mile / free_speed

        if vdf_fftt:
            link.other_attrs[new_fftt_field] = float(vdf_fftt)

        toll_value = float(toll) if toll else 0
        link.other_attrs.setdefault(new_toll_field, toll_value)
        for mode_type in supported_mode_toll_types:
            link.other_attrs.setdefault(f"toll_{mode_type}", toll_value)
        link.other_attrs.setdefault(new_ref_volume_field, 0)
        link.other_attrs.setdefault(new_ref_cost_field, 0)
        link.other_attrs[crs_field] = network_crs

        for field in other_fields:
            link.other_attrs[field] = column_values[field][row_position]

        # The calibrated node-pair overlay and TAPlite kernel use this numeric
        # selector independently from the Cube-derived link_type/vdf_code.
        if vdf_type == "qvdf":
            link.other_attrs[new_vdf_type_field] = 2

        validate_taplite_link_values(
            length_meters=link.length,
            free_speed_kph=link.free_speed,
            vdf_length_mi=link.other_attrs[new_vdf_length_mi_field],
            vdf_free_speed_mph=link.other_attrs[new_vdf_free_speed_mph_field],
            vdf_fftt_minutes=link.other_attrs.get(new_fftt_field),
            link_id=link.link_id,
        )

        link_dict[link.link_id] = link

    network_gmns.link_dict = link_dict
    network_gmns.link_other_attrs = list(
        dict.fromkeys([*other_fields, new_vdf_type_field, *BOUNDARY_FIELDS])
    )
    apply_congestion_boundaries(network_gmns, time_period)

    print('%s links loaded' % len(link_dict))


def _buildnet(
    shapfile_path,
    time_period,
    time_period_list,
    length_unit,
    node_generation,
    speed_unit='kph',
    vdf_type='bpr',
    target_crs=DEFAULT_OUTPUT_CRS,
):
    network = Network()
    if node_generation:
        raw_network = loadCSVfromSHP(shapfile_path, target_crs=target_crs)
        _loadNodes(network, raw_network)
    vdf_dict = get_vdf_dict(vdf_type)
    _loadLinks(
        network,
        raw_network,
        time_period,
        time_period_list,
        vdf_dict,
        length_unit=length_unit,
        speed_unit=speed_unit,
        vdf_type=vdf_type,
    )

    return network


def _outputNode(network, output_folder):
    print('Generating node file ...')

    os.makedirs(output_folder, exist_ok=True)
    node_filename = node_file_name
    node_filepath = os.path.join(output_folder, node_filename)

    outfile = open(node_filepath, 'w', newline='', errors='ignore')

    writer = csv.writer(outfile)

    node_header = list(dtalite_node_mapping.values())
    writer.writerow(node_header)
    for node in sorted(network.node_dict.values(), key=_node_sort_key):
        line = [_node_value(node, semantic_key) for semantic_key in dtalite_node_mapping]

        writer.writerow(line)
    outfile.close()
    print(f'{node_file_name} generated at {node_filepath}')


def _copy_node_template(source_path, output_folder):
    output_folder = os.path.abspath(output_folder)
    os.makedirs(output_folder, exist_ok=True)
    target_path = os.path.join(output_folder, node_file_name)
    if os.path.abspath(source_path) != os.path.abspath(target_path):
        shutil.copy2(source_path, target_path)
    print(f'{node_file_name} copied from prepared template to {target_path}')
    return target_path


def _outputLink(network, output_folder, time_period, link_filename=None):
    print('Generating link file ...')

    os.makedirs(output_folder, exist_ok=True)
    link_filename = _resolve_link_file_name(time_period, link_filename)
    link_filepath = os.path.join(output_folder, link_filename)
    outfile = open(link_filepath, 'w', newline='', errors='ignore')

    writer = csv.writer(outfile)
    link_header = list(dtalite_base_link_mapping.values())
    first_link = next(iter(network.link_dict.values()), None)
    other_link_header = [] if first_link is None else [
        field for field in first_link.other_attrs.keys()
        if field not in set(dtalite_base_link_mapping.values())
    ]
    link_header.extend(other_link_header)
    writer.writerow(link_header)

    for link in sorted(network.link_dict.values(), key=_link_sort_key):
        line = [_link_value(link, semantic_key) for semantic_key in dtalite_base_link_mapping]

        other_link_att_values = [link.other_attrs[field] for field in other_link_header]
        line.extend(other_link_att_values)
        writer.writerow(line)
    outfile.close()
    print(f'{link_filename} generated at {link_filepath}')


def district_id_map(net_dir, time_period, link_filename=None, jurisdiction_dir=None):
    print('Assigning district ids ...')

    link_filename = _resolve_link_file_name(time_period, link_filename)
    link_csv_path = os.path.join(net_dir, link_filename)
    try:
        link_net = pd.read_csv(link_csv_path)
    except FileNotFoundError:
        print(f"{link_filename} not found in directory: {net_dir}")
        return None

    node_generation = True
    node_csv_path = os.path.join(net_dir, node_file_name)
    try:
        node_net = pd.read_csv(node_csv_path)
    except FileNotFoundError:
        print(f"{node_file_name} not found in directory: {net_dir}")
        return None
    if district_id_field in node_net.columns:
        node_generation = False

    lookup_dir = jurisdiction_dir or net_dir
    link_taz_jurname_csv_path = os.path.join(lookup_dir, taz_jurisdiction_file_name)
    try:
        link_taz_jurname = pd.read_csv(link_taz_jurname_csv_path)
    except FileNotFoundError:
        print(f"{taz_jurisdiction_file_name} not found in directory: {net_dir}")
        return None

    link_net[pair_field] = link_net[from_node_id_field].astype(str) + '->' + link_net[to_node_id_field].astype(str)

    link_taz_jurname_dict = dict(zip(link_taz_jurname[taz_field], link_taz_jurname[jurisdiction_name_field]))

    district_id_dict = {'Arlington': 2,
                        'Alexandria': 1,
                        'Fairfax': 3,
                        'Fairfax City': 4,
                        'Falls Church': 5,
                        'Loudoun': 6,
                        'Prince William': 9,
                        'Manassas': 7,
                        'Manassas Park': 8
                        }

    link_net[generated_jurisdiction_field] = (
        link_net[taz_field].map(link_taz_jurname_dict).fillna(-1)
    )
    link_net[district_id_field] = (
        link_net[generated_jurisdiction_field].map(district_id_dict).fillna(10)
    )

    if node_generation:
        node_district_id_dict = dict(zip(link_net[from_node_id_field], link_net[district_id_field]))
        # node_district_id_dict_2 = dict(zip(link_net.to_node_id, link_net.district_id))
        node_net[district_id_field] = node_net.apply(lambda x: node_district_id_dict.setdefault(x[node_id_field], -1), axis=1)
        node_net = _sort_dataframe_for_csv(node_net, [node_id_field, "zone_id"])
        node_net.to_csv(os.path.join(net_dir, node_file_name), index=False)

    link_net = _sort_dataframe_for_csv(link_net, [from_node_id_field, to_node_id_field])
    link_net.to_csv(os.path.join(net_dir, link_filename), index=False)

    print('District ids assigned successfully.')


def cap_adjustment(net_dir, time_period, link_filename=None):
    print('Adjusting link capacity ...')
    link_filename = _resolve_link_file_name(time_period, link_filename)
    link_csv = os.path.join(net_dir, link_filename)
    if not os.path.exists(link_csv):
        print(f"File '{link_filename}' not found in directory: {net_dir}")
        return None

    df_link = pd.read_csv(link_csv)

    has_its = its_field in df_link.columns
    has_intersecti = intersection_field in df_link.columns

    data_seg_list = []

    # Check available columns and adjust dictionaries and filters accordingly
    if has_its and has_intersecti:
        cap_adj_dict = {
            1: [0, 0],
            1.05: [0, 1],
            1.01: [1, 0],
            1.06: [1, 1]
        }
    elif has_its:
        cap_adj_dict = {
            1: [0],
            1.01: [1]
        }
    elif has_intersecti:
        cap_adj_dict = {
            1: [0],
            1.05: [1]
        }
    else:
        print(f"Columns '{its_field}' and '{intersection_field}' not found in '{link_filename}'")
        return

    for adj_factor, cap_key in cap_adj_dict.items():
        ITS_code = cap_key[0] if has_its else None
        intersection_code = cap_key[-1] if has_intersecti else None

        # Filter the links based on available columns
        if ITS_code is not None and intersection_code is not None:
            link_adj_cap_net = df_link[
                (df_link.get(its_field) == ITS_code) & (df_link.get(intersection_field) == intersection_code)].copy()
        elif ITS_code is not None and intersection_code is None:
            link_adj_cap_net = df_link[(df_link.get(its_field) == ITS_code)].copy()
        elif intersection_code is not None and ITS_code is None:
            link_adj_cap_net = df_link[(df_link.get(intersection_field) == intersection_code)].copy()
        else:
            continue

        if not link_adj_cap_net.empty:
            link_adj_cap_net[capacity_field] *= adj_factor
            data_seg_list.append(link_adj_cap_net)

    if data_seg_list:
        df_bd_test = pd.concat(data_seg_list)
        df_bd_test = _sort_dataframe_for_csv(df_bd_test, [from_node_id_field, to_node_id_field])
        df_bd_test.to_csv(os.path.join(net_dir, link_filename), index=False)

    print('Link capacity  adjusted successfully.')


_PARALLEL_RAW_NETWORK = None
_PARALLEL_NODE_DICT = None
_PARALLEL_NODE_OTHER_ATTRS = None
_PARALLEL_VDF_DICT = None


def _initialize_network_worker(context_path):
    global _PARALLEL_RAW_NETWORK
    global _PARALLEL_NODE_DICT
    global _PARALLEL_NODE_OTHER_ATTRS
    global _PARALLEL_VDF_DICT

    # Do not allow native numeric libraries imported by pandas/geopandas to
    # create another thread team inside every conversion process.
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

    with open(context_path, "rb") as stream:
        payload = pickle.load(stream)
    _PARALLEL_RAW_NETWORK = payload["raw_network"]
    _PARALLEL_NODE_DICT = payload["node_dict"]
    _PARALLEL_NODE_OTHER_ATTRS = payload["node_other_attrs"]
    _PARALLEL_VDF_DICT = payload["vdf_dict"]


def _convert_network_chunk(task):
    if _PARALLEL_RAW_NETWORK is None or _PARALLEL_NODE_DICT is None:
        raise RuntimeError("Parallel network worker was not initialized")

    started = time.perf_counter()
    subset = _PARALLEL_RAW_NETWORK.iloc[task["row_start"] : task["row_stop"]].copy()
    subset.attrs.update(_PARALLEL_RAW_NETWORK.attrs)

    network = Network()
    network.node_dict = _PARALLEL_NODE_DICT
    network.node_other_attrs = _PARALLEL_NODE_OTHER_ATTRS

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        _loadLinks(
            network,
            subset,
            task["period"],
            task["time_period_list"],
            _PARALLEL_VDF_DICT,
            length_unit=task["length_unit"],
            speed_unit=task["speed_unit"],
            vdf_type=task["vdf_type"],
        )
        _outputLink(
            network,
            task["part_dir"],
            task["period"].lower(),
            link_filename=task["part_name"],
        )

    return {
        "period": task["period"].lower(),
        "part_path": os.path.join(task["part_dir"], task["part_name"]),
        "row_start": task["row_start"],
        "row_stop": task["row_stop"],
        "links": len(network.link_dict),
        "elapsed_sec": time.perf_counter() - started,
        "worker_log": captured.getvalue(),
    }


def _merge_network_parts(part_results, output_path):
    ordered_results = sorted(part_results, key=lambda item: item["row_start"])
    if not ordered_results:
        raise RuntimeError(f"No network chunks were produced for {output_path}")

    # The parent pre-sorts the source links before dividing them into contiguous
    # ranges. Each worker therefore emits an already-sorted CSV part. Copying the
    # raw rows preserves csv.writer's exact float and missing-value formatting;
    # parsing and re-serializing with pandas would change otherwise equivalent
    # values (for example, "NA" to an empty field).
    expected_header = None
    link_count = 0
    with open(output_path, "wb") as output_stream:
        for result in ordered_results:
            with open(result["part_path"], "rb") as part_stream:
                header = part_stream.readline()
                if expected_header is None:
                    expected_header = header
                    output_stream.write(header)
                elif header != expected_header:
                    raise RuntimeError(
                        f"Network chunk header mismatch in {result['part_path']}"
                    )
                shutil.copyfileobj(part_stream, output_stream, length=1024 * 1024)
            link_count += result["links"]
    return link_count


def get_gmns_from_cube(
    shapefile_path,
    time_period_list,
    length_unit=TAPLITE_GENERIC_LENGTH_UNIT,
    speed_unit=TAPLITE_GENERIC_SPEED_UNIT,
    district_id_assignment=True,
    capacity_adjustment=False,
    vdf_type='bpr',
    output_dir=None,
    period_folder_output=False,
    target_crs=DEFAULT_OUTPUT_CRS,
    *,
    conversion_workers=0,
    reserve_cores=1,
    chunks_per_period=0,
    adaptive=True,
    conversion_cache=True,
    cache_dir=None,
):
    """Convert Cube data under TAPlite's fixed GMNS/QVDF unit contract."""

    validate_taplite_converter_units(length_unit, speed_unit)

    started = time.perf_counter()
    periods = [str(period).lower() for period in time_period_list]
    vdf_dict = get_vdf_dict(vdf_type)
    output_root = os.path.abspath(output_dir or shapefile_path)
    os.makedirs(output_root, exist_ok=True)

    network = Network()
    cache_hit = False
    cache_root = None
    cache_fingerprint = None
    cache_source_files = []
    cached_node_path = None

    if conversion_cache:
        cache_root = os.path.abspath(
            cache_dir or os.path.join(output_root, ".dtalite_conversion_cache")
        )
        cache_fingerprint, cache_source_files = network_source_fingerprint(
            shapefile_path,
            target_crs=str(target_crs),
        )
        cached_payload, cached_node_path = load_network_cache(
            cache_root,
            expected_fingerprint=cache_fingerprint,
        )
        if cached_payload is not None:
            raw_network = cached_payload["raw_network"]
            network.node_dict = cached_payload["node_dict"]
            network.node_other_attrs = cached_payload.get("node_other_attrs", [])
            cache_hit = True
            print(f"Prepared network cache hit: {cache_root}")
        else:
            print(f"Prepared network cache miss: {cache_root}")

    if not cache_hit:
        # Read and reproject only once in the parent. The prepared context is
        # serialized once and loaded once by each spawned Windows worker.
        raw_network = loadCSVfromSHP(shapefile_path, target_crs=target_crs)
        _loadNodes(network, raw_network)

    node_output_dirs = (
        [os.path.join(output_root, period) for period in periods]
        if period_folder_output
        else [output_root]
    )
    first_node_output_dir = node_output_dirs[0]
    if cache_hit and cached_node_path is not None:
        node_template_path = _copy_node_template(
            cached_node_path,
            first_node_output_dir,
        )
    else:
        _outputNode(network, first_node_output_dir)
        node_template_path = os.path.join(first_node_output_dir, node_file_name)
        if conversion_cache:
            saved_cache = save_network_cache(
                cache_root,
                fingerprint=cache_fingerprint,
                source_files=cache_source_files,
                target_crs=str(target_crs),
                payload={
                    "raw_network": raw_network,
                    "node_dict": network.node_dict,
                    "node_other_attrs": network.node_other_attrs,
                },
                node_csv_source=node_template_path,
            )
            cached_node_path = saved_cache.node_csv
            print(f"Prepared network cache written: {cache_root}")

    for period_output_dir in node_output_dirs[1:]:
        _copy_node_template(node_template_path, period_output_dir)

    maximum_chunks = max(1, (len(raw_network) + 3_999) // 4_000)
    potential_tasks = len(periods) * maximum_chunks
    plan = choose_worker_plan(
        requested_workers=conversion_workers,
        reserve_cores=reserve_cores,
        task_count=potential_tasks,
        work_items=len(raw_network) * len(periods),
        min_work_items_per_worker=8_000,
        adaptive=adaptive,
    )
    chunks = choose_chunks_per_group(
        items_per_group=len(raw_network),
        group_count=len(periods),
        workers=plan.workers,
        requested_chunks=chunks_per_period,
        min_chunk_items=4_000,
    )
    print(
        "Network conversion plan: "
        f"periods={len(periods)}, chunks_per_period={chunks}, "
        f"tasks={len(periods) * chunks}, workers={plan.workers}; {plan.reason}"
    )

    output_details = []
    if plan.workers == 1:
        for period in periods:
            period_output_dir = os.path.join(output_root, period) if period_folder_output else output_root
            link_filename = period_link_file_name if period_folder_output else _format_link_file_name(period)
            period_started = time.perf_counter()
            _loadLinks(
                network,
                raw_network,
                period,
                periods,
                vdf_dict,
                length_unit=length_unit,
                speed_unit=speed_unit,
                vdf_type=vdf_type,
            )
            _outputLink(network, period_output_dir, period, link_filename=link_filename)
            output_details.append(
                {
                    "period": period,
                    "output": os.path.join(period_output_dir, link_filename),
                    "links": len(network.link_dict),
                    "elapsed_sec": time.perf_counter() - period_started,
                }
            )
    else:
        with tempfile.TemporaryDirectory(prefix=".network_parts_", dir=output_root) as temp_dir:
            context_path = os.path.join(temp_dir, "network_context.pkl")
            link_field_mapping = Mapping(**cube_base_link_mapping)
            parallel_raw_network = _sort_dataframe_for_csv(
                raw_network,
                [
                    link_field_mapping.from_node_field,
                    link_field_mapping.to_node_field,
                ],
            )
            parallel_raw_network.attrs.update(raw_network.attrs)
            with open(context_path, "wb") as stream:
                pickle.dump(
                    {
                        "raw_network": parallel_raw_network,
                        "node_dict": network.node_dict,
                        "node_other_attrs": network.node_other_attrs,
                        "vdf_dict": vdf_dict,
                    },
                    stream,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )

            tasks = []
            ranges = chunk_ranges(len(raw_network), chunks)
            for period in periods:
                part_dir = os.path.join(temp_dir, period)
                os.makedirs(part_dir, exist_ok=True)
                for chunk_number, (row_start, row_stop) in enumerate(ranges):
                    tasks.append(
                        {
                            "period": period,
                            "time_period_list": periods,
                            "length_unit": length_unit,
                            "speed_unit": speed_unit,
                            "vdf_type": vdf_type,
                            "row_start": row_start,
                            "row_stop": row_stop,
                            "part_dir": part_dir,
                            "part_name": f"part_{chunk_number:04d}.csv",
                        }
                    )

            results_by_period = {period: [] for period in periods}
            completed = 0
            with ProcessPoolExecutor(
                max_workers=plan.workers,
                initializer=_initialize_network_worker,
                initargs=(context_path,),
            ) as executor:
                futures = [executor.submit(_convert_network_chunk, task) for task in tasks]
                for future in as_completed(futures):
                    result = future.result()
                    results_by_period[result["period"]].append(result)
                    completed += 1
                    if result["worker_log"]:
                        print(result["worker_log"], end="")
                    print(
                        f"Network chunk {completed}/{len(tasks)} complete: "
                        f"{result['period']} rows {result['row_start']}:{result['row_stop']} "
                        f"-> {result['links']:,} links"
                    )

            for period in periods:
                period_output_dir = (
                    os.path.join(output_root, period) if period_folder_output else output_root
                )
                link_filename = (
                    period_link_file_name if period_folder_output else _format_link_file_name(period)
                )
                output_path = os.path.join(period_output_dir, link_filename)
                link_count = _merge_network_parts(results_by_period[period], output_path)
                print(f"{link_filename} generated at {output_path} ({link_count:,} links)")
                output_details.append(
                    {
                        "period": period,
                        "output": output_path,
                        "links": link_count,
                        "chunk_elapsed_sec": sum(
                            result["elapsed_sec"] for result in results_by_period[period]
                        ),
                    }
                )

    # These pandas transformations operate independently by period but are
    # relatively small compared with link construction. Keep them in the parent
    # to avoid another pool and make final writes deterministic.
    for period in periods:
        period_output_dir = os.path.join(output_root, period) if period_folder_output else output_root
        link_filename = period_link_file_name if period_folder_output else _format_link_file_name(period)
        if district_id_assignment:
            district_id_map(
                period_output_dir,
                period,
                link_filename=link_filename,
                jurisdiction_dir=shapefile_path,
            )
        if capacity_adjustment:
            cap_adjustment(period_output_dir, period, link_filename=link_filename)

    return {
        "stage": "network",
        "parallel": plan.workers > 1,
        "cache": {
            "enabled": conversion_cache,
            "hit": cache_hit,
            "directory": cache_root,
            "fingerprint": cache_fingerprint,
        },
        "worker_plan": plan.as_dict(),
        "chunks_per_period": chunks if plan.workers > 1 else 1,
        "task_count": len(periods) * (chunks if plan.workers > 1 else 1),
        "outputs": output_details,
        "elapsed_sec": time.perf_counter() - started,
    }
