from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np


SPEED_FIELDS = {
    "AM": ("qvdf_start_speed_mph_am", "qvdf_end_speed_mph_am"),
    "MD": ("qvdf_start_speed_mph_md", "qvdf_end_speed_mph_md"),
    "PM": ("qvdf_start_speed_mph_pm", "qvdf_end_speed_mph_pm"),
}


def lookup_path(
    directory: Optional[Union[str, Path]] = None,
) -> Path:
    root = Path(directory) if directory is not None else Path(__file__).parent
    return root / "observed_link_speed_boundaries.npy"


def _pack(from_node_id, to_node_id) -> np.ndarray:
    from_values = np.asarray(from_node_id, dtype=np.uint64)
    to_values = np.asarray(to_node_id, dtype=np.uint64)
    if from_values.shape != to_values.shape:
        raise ValueError("from_node_id and to_node_id shapes differ")
    return (from_values << np.uint64(32)) | to_values


def load_lookup(
    directory: Optional[Union[str, Path]] = None,
):
    """Memory-map the observed speed-boundary table."""

    return np.load(lookup_path(directory), mmap_mode="r", allow_pickle=False)


def lookup(
    table,
    period: str,
    from_node_id,
    to_node_id,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return [..., 2] start/end speeds and a node-pair found mask."""

    period_name = str(period).upper()
    if period_name not in SPEED_FIELDS:
        raise ValueError(f"Unsupported observed speed-boundary period: {period}")
    packed = _pack(from_node_id, to_node_id)
    original_shape = packed.shape
    query = packed.reshape(-1)
    values = np.full((len(query), 2), np.nan, dtype=np.float32)
    if len(table) == 0:
        return values.reshape((*original_shape, 2)), np.zeros(
            original_shape, dtype=bool
        )
    keys = table["packed_key"]
    positions = np.searchsorted(keys, query)
    clipped = np.minimum(positions, len(keys) - 1)
    found = (positions < len(keys)) & (keys[clipped] == query)
    if found.any():
        start_field, end_field = SPEED_FIELDS[period_name]
        selected = table[clipped[found]]
        values[found, 0] = selected[start_field]
        values[found, 1] = selected[end_field]
    return values.reshape((*original_shape, 2)), found.reshape(original_shape)


def lookup_period(
    period: str,
    from_node_id,
    to_node_id,
    directory: Optional[Union[str, Path]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    return lookup(load_lookup(directory), period, from_node_id, to_node_id)
