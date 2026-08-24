from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np


BOUNDARY_FIELDS = {
    period: tuple(
        f"observed_{boundary}_hour_{period.lower()}"
        for boundary in ("t0", "t2", "t3")
    )
    for period in ("AM", "MD", "PM")
}


def lookup_path(
    directory: Optional[Union[str, Path]] = None,
) -> Path:
    root = Path(directory) if directory is not None else Path(__file__).parent
    return root / "observed_link_t2.npy"


def _pack(from_node_id, to_node_id) -> np.ndarray:
    from_values = np.asarray(from_node_id, dtype=np.uint64)
    to_values = np.asarray(to_node_id, dtype=np.uint64)
    if from_values.shape != to_values.shape:
        raise ValueError("from_node_id and to_node_id shapes differ")
    return (from_values << np.uint64(32)) | to_values


def load_lookup(
    directory: Optional[Union[str, Path]] = None,
):
    """Memory-map accepted weekday-average observed episode boundaries."""

    return np.load(lookup_path(directory), mmap_mode="r", allow_pickle=False)


def lookup(
    table,
    period: str,
    from_node_id,
    to_node_id,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return one period observed T0/T2/T3 and a node-pair found mask."""

    period_name = str(period).upper()
    if period_name not in BOUNDARY_FIELDS:
        raise ValueError(f"Unsupported observed boundary period: {period}")
    packed = _pack(from_node_id, to_node_id)
    original_shape = packed.shape
    query = packed.reshape(-1)
    values = np.full((len(query), 3), np.nan, dtype=np.float32)
    if len(table) == 0:
        return values.reshape((*original_shape, 3)), np.zeros(
            original_shape, dtype=bool
        )
    keys = table["packed_key"]
    positions = np.searchsorted(keys, query)
    clipped = np.minimum(positions, len(keys) - 1)
    found = (positions < len(keys)) & (keys[clipped] == query)
    if found.any():
        selected = table[clipped[found]]
        for field_index, field in enumerate(BOUNDARY_FIELDS[period_name]):
            values[found, field_index] = selected[field]
    return values.reshape((*original_shape, 3)), found.reshape(original_shape)


def lookup_period(
    period: str,
    from_node_id,
    to_node_id,
    directory: Optional[Union[str, Path]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    return lookup(load_lookup(directory), period, from_node_id, to_node_id)
