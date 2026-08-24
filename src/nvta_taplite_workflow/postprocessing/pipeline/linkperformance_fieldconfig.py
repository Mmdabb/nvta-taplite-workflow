LINK_PERFORMANCE_FILENAME = "link_performance.csv"
LINK_FILENAME = "link.csv"

LINK_ID_FIELD = "link_id"
FROM_NODE_ID_FIELD = "from_node_id"
TO_NODE_ID_FIELD = "to_node_id"
LENGTH_FIELD = "length"
LEGACY_LENGTH_MILE_FIELD = "length_in_mile"
FREE_SPEED_FIELD = "free_speed"
LINK_TYPE_FIELD = "link_type"
SPEED_FIELD = "speed_mph"
TRAVEL_TIME_FIELD = "travel_time"
VOLUME_FIELD = "volume"
VEHICLE_VOLUME_FIELD = "vehicle_volume"
PERSON_VOLUME_FIELD = "volume"
TRUCK_VOLUME_FIELD = "mod_vol_trk"
SEVERE_CONGESTION_FIELD = "Severe_Congestion_P"
DISTRICT_ID_FIELD = "district_id"
TAZ_FIELD = "TAZ"
FACILITY_TYPE_FIELD = "FT"
TOLL_GROUP_FIELD = "TOLLGRP"
ALLOWED_USES_FIELD = "allowed_uses"
PERIOD_LIMIT_TEMPLATE = "{period}LIMIT"
FFTT_FIELD = "fftt"
SPEED_RATIO_FIELD = "speed_ratio"
PAIR_FIELD = "pair"
GEOMETRY_FIELD = "geometry"

link_required_fields_mapping = {
    "link_id": LINK_ID_FIELD,
    "from_node_id": FROM_NODE_ID_FIELD,
    "to_node_id": TO_NODE_ID_FIELD,
    "length": LENGTH_FIELD,
    "legacy_length": LEGACY_LENGTH_MILE_FIELD,
    "free_speed": FREE_SPEED_FIELD,
    "link_type": LINK_TYPE_FIELD,
    "taz_code": TAZ_FIELD,
    "district_id": DISTRICT_ID_FIELD,
    "field_type": FACILITY_TYPE_FIELD,
    "toll_grp": TOLL_GROUP_FIELD,
    "allowed_uses": ALLOWED_USES_FIELD,
    "pair": PAIR_FIELD,
}

link_performance_fields_mapping = {
    "fftt": FFTT_FIELD,
    "tt": TRAVEL_TIME_FIELD,
    "speed": SPEED_FIELD,
    "speed_ratio": SPEED_RATIO_FIELD,
    "volume": VOLUME_FIELD,
    "vehicle_volume": VEHICLE_VOLUME_FIELD,
    "person_volume": PERSON_VOLUME_FIELD,
    "truck_volume": TRUCK_VOLUME_FIELD,
    "severe_congestion": SEVERE_CONGESTION_FIELD,
}

project_specific_fields = {
    "taz_code": TAZ_FIELD,
    "district_id": DISTRICT_ID_FIELD,
    "field_type": FACILITY_TYPE_FIELD,
    "toll_grp": TOLL_GROUP_FIELD,
    "period_limit": PERIOD_LIMIT_TEMPLATE,
    "legacy_length": LEGACY_LENGTH_MILE_FIELD,
    "truck_volume": TRUCK_VOLUME_FIELD,
}

district_id_name_mapping = {
    2: "Arlington",
    1: "Alexandria",
    3: "Fairfax",
    4: "Fairfax City",
    5: "Falls Church",
    6: "Loudoun",
    9: "Prince William",
    7: "Manassas",
    8: "Manassas Park",
    10: "Others",
}

length_unit = "mile"
speed_unit = "mph"


def period_limit_field(period: str) -> str:
    return PERIOD_LIMIT_TEMPLATE.format(period=period.upper())
