"""Unit contract shared by Cube conversion and the TAPlite kernel inputs."""

from __future__ import annotations

import math
from typing import Final


METERS_PER_MILE: Final = 1609.344
KPH_PER_MPH: Final = 1.609344

TAPLITE_GENERIC_LENGTH_UNIT: Final = "meter"
TAPLITE_GENERIC_SPEED_UNIT: Final = "kph"

# TAPlite's GMNS schema deliberately mixes generic metric columns with
# unambiguous QVDF/override columns. Keep this mapping in one place so the
# converter, run manifest, documentation, and tests cannot drift apart.
TAPLITE_LINK_UNIT_CONTRACT: Final[dict[str, str]] = {
    "length": "meter",
    "free_speed": "kph",
    "vdf_length_mi": "mile",
    "vdf_free_speed_mph": "mph",
    "cutoff_speed": "mph",
    "qvdf_start_speed_mph": "mph",
    "qvdf_end_speed_mph": "mph",
    "vdf_fftt": "minute",
    "capacity": "vehicle/hour/lane",
    "t0_hour": "decimal hour",
    "t2_hour": "decimal hour",
    "t3_hour": "decimal hour",
}


def miles_to_taplite_length(length_miles: float) -> float:
    """Return the generic GMNS ``length`` value expected by TAPlite."""

    return float(length_miles) * METERS_PER_MILE


def mph_to_taplite_free_speed(speed_mph: float) -> float:
    """Return the generic GMNS ``free_speed`` value expected by TAPlite."""

    return float(speed_mph) * KPH_PER_MPH


def validate_taplite_converter_units(length_unit: str, speed_unit: str) -> None:
    """Reject converter settings that would violate the upstream schema."""

    if length_unit != TAPLITE_GENERIC_LENGTH_UNIT:
        raise ValueError(
            "TAPlite link.csv requires generic 'length' in meters; "
            f"received length_unit={length_unit!r}. Use vdf_length_mi for miles."
        )
    if speed_unit != TAPLITE_GENERIC_SPEED_UNIT:
        raise ValueError(
            "TAPlite link.csv requires generic 'free_speed' in km/h; "
            f"received speed_unit={speed_unit!r}. Use vdf_free_speed_mph for mph."
        )


def validate_taplite_link_values(
    *,
    length_meters: float,
    free_speed_kph: float,
    vdf_length_mi: float,
    vdf_free_speed_mph: float,
    vdf_fftt_minutes: float | None,
    link_id: object,
) -> None:
    """Verify that one converted link expresses the same values in both units."""

    values = (length_meters, free_speed_kph, vdf_length_mi, vdf_free_speed_mph)
    if not all(math.isfinite(float(value)) and float(value) >= 0.0 for value in values):
        raise ValueError(f"TAPlite link {link_id} has invalid length/speed unit values")

    expected_length_meters = miles_to_taplite_length(vdf_length_mi)
    if not math.isclose(
        float(length_meters), expected_length_meters, rel_tol=1e-9, abs_tol=1e-6
    ):
        raise ValueError(
            f"TAPlite link {link_id} unit mismatch: length={length_meters} must equal "
            f"vdf_length_mi*{METERS_PER_MILE}={expected_length_meters}"
        )

    expected_free_speed_kph = mph_to_taplite_free_speed(vdf_free_speed_mph)
    if not math.isclose(
        float(free_speed_kph), expected_free_speed_kph, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise ValueError(
            f"TAPlite link {link_id} unit mismatch: free_speed={free_speed_kph} must equal "
            f"vdf_free_speed_mph*{KPH_PER_MPH}={expected_free_speed_kph}"
        )

    if vdf_fftt_minutes is not None and float(vdf_free_speed_mph) > 0.0:
        expected_fftt = 60.0 * float(vdf_length_mi) / float(vdf_free_speed_mph)
        if not math.isclose(
            float(vdf_fftt_minutes), expected_fftt, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError(
                f"TAPlite link {link_id} unit mismatch: vdf_fftt={vdf_fftt_minutes} "
                f"minutes must equal 60*vdf_length_mi/vdf_free_speed_mph={expected_fftt}"
            )
