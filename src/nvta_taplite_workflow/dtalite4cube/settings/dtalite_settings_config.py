from __future__ import annotations

SETTINGS_FILENAME = "settings.csv"
MODE_TYPE_FILENAME = "mode_type.csv"

SETTINGS_HEADER = [
    "number_of_iterations",
    "number_of_processors",
    "demand_period_starting_hours",
    "demand_period_ending_hours",
    "first_through_node_id",
    "base_demand_mode",
    "route_output",
    "vehicle_output",
    "log_file",
    "odme_mode",
    "odme_vmt",
    "demand_format",
]

MODE_TYPE_HEADER = [
    "mode_type_id",
    "mode_type",
    "name",
    "vot",
    "pce",
    "occ",
    "demand_file",
]

DEFAULT_SETTINGS = {
    "number_of_iterations": 10,
    "number_of_processors": 8,
    "first_through_node_id": -1,
    "base_demand_mode": 0,
    "route_output": 0,
    "vehicle_output": 0,
    "log_file": 0,
    "odme_mode": 0,
    "odme_vmt": 0,
    "demand_format": 0,
}

TIME_PERIODS = {
    "am": {"start_hour": 6, "end_hour": 9},
    "md": {"start_hour": 9, "end_hour": 15},
    "pm": {"start_hour": 15, "end_hour": 19},
    "nt": {"start_hour": 19, "end_hour": 6},
}

MODE_TYPE_CONFIG = {
    "am": [
        {"mode_type_id": 1, "mode_type": "sov", "name": "sov", "vot": 24, "pce": 1, "occ": 1},
        {"mode_type_id": 2, "mode_type": "hov2", "name": "hov2", "vot": 40, "pce": 1, "occ": 2},
        {"mode_type_id": 3, "mode_type": "hov3", "name": "hov3", "vot": 60, "pce": 1, "occ": 3.5},
        {"mode_type_id": 4, "mode_type": "com", "name": "com", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 5, "mode_type": "trk", "name": "trk", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 6, "mode_type": "apv", "name": "apv", "vot": 30, "pce": 1, "occ": 1.6},
    ],
    "md": [
        {"mode_type_id": 1, "mode_type": "sov", "name": "sov", "vot": 20, "pce": 1, "occ": 1},
        {"mode_type_id": 2, "mode_type": "hov2", "name": "hov2", "vot": 15, "pce": 1, "occ": 2},
        {"mode_type_id": 3, "mode_type": "hov3", "name": "hov3", "vot": 15, "pce": 1, "occ": 3.5},
        {"mode_type_id": 4, "mode_type": "com", "name": "com", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 5, "mode_type": "trk", "name": "trk", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 6, "mode_type": "apv", "name": "apv", "vot": 30, "pce": 1, "occ": 1.6},
    ],
    "pm": [
        {"mode_type_id": 1, "mode_type": "sov", "name": "sov", "vot": 20, "pce": 1, "occ": 1},
        {"mode_type_id": 2, "mode_type": "hov2", "name": "hov2", "vot": 30, "pce": 1, "occ": 2},
        {"mode_type_id": 3, "mode_type": "hov3", "name": "hov3", "vot": 60, "pce": 1, "occ": 3.5},
        {"mode_type_id": 4, "mode_type": "com", "name": "com", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 5, "mode_type": "trk", "name": "trk", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 6, "mode_type": "apv", "name": "apv", "vot": 30, "pce": 1, "occ": 1.6},
    ],
    "nt": [
        {"mode_type_id": 1, "mode_type": "sov", "name": "sov", "vot": 20, "pce": 1, "occ": 1},
        {"mode_type_id": 2, "mode_type": "hov2", "name": "hov2", "vot": 15, "pce": 1, "occ": 2},
        {"mode_type_id": 3, "mode_type": "hov3", "name": "hov3", "vot": 15, "pce": 1, "occ": 3.5},
        {"mode_type_id": 4, "mode_type": "com", "name": "com", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 5, "mode_type": "trk", "name": "trk", "vot": 30, "pce": 1, "occ": 1},
        {"mode_type_id": 6, "mode_type": "apv", "name": "apv", "vot": 30, "pce": 1, "occ": 1.6},
    ],
}

ALLOWED_SETTINGS_OVERRIDES = {
    "number_of_iterations",
    "number_of_processors",
    "route_output",
    "first_through_node_id",
    "vehicle_output",
    "demand_format",
}

DEMAND_LANE_USE_TO_MODE_TYPE = {
    "apv": "apv",
    "com": "com",
    "hv2": "hov2",
    "hv3": "hov3",
    "sov": "sov",
    "trk": "trk",
}

DEMAND_LANE_USES = ["apv", "com", "hv2", "hv3", "sov", "trk"]
SUPPORTED_MODE_TYPES = ["sov", "hov2", "hov3", "com", "trk", "apv"]


def demand_file_name(mode_or_lane_use: str, period_key: str) -> str:
    normalized = mode_or_lane_use.lower()
    mode_type = DEMAND_LANE_USE_TO_MODE_TYPE.get(normalized, normalized)
    return f"{mode_type}_{period_key.lower()}.csv"
