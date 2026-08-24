from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np


BOUNDARY_FIELDS = ["t0_hour", "t2_hour", "t3_hour"]


def period_path(
    period: str,
    directory: Optional[Union[str, Path]] = None,
) -> Path:
    root = Path(directory) if directory is not None else Path(__file__).parent
    return root / f"{period.lower()}_node_pair_boundaries.npy"


def _pack(from_node_id, to_node_id) -> np.ndarray:
    from_values = np.asarray(from_node_id, dtype=np.uint64)
    to_values = np.asarray(to_node_id, dtype=np.uint64)
    if from_values.shape != to_values.shape:
        raise ValueError("from_node_id and to_node_id shapes differ.")
    return (from_values << np.uint64(32)) | to_values


def load_period(
    period: str,
    directory: Optional[Union[str, Path]] = None,
):
    """Memory-map one period lookup without reading the full file into RAM."""
    return np.load(period_path(period, directory), mmap_mode="r", allow_pickle=False)


def lookup(
    table,
    from_node_id,
    to_node_id,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return [..., 3] T0/T2/T3 array and a same-shape found mask."""
    packed = _pack(from_node_id, to_node_id)
    original_shape = packed.shape
    query = packed.reshape(-1)
    keys = table["packed_key"]
    values = np.full((len(query), len(BOUNDARY_FIELDS)), np.nan, dtype=np.float32)
    if len(keys) == 0:
        return values.reshape((*original_shape, len(BOUNDARY_FIELDS))), np.zeros(
            original_shape,
            dtype=bool,
        )

    positions = np.searchsorted(keys, query)
    clipped = np.minimum(positions, len(keys) - 1)
    found = (positions < len(keys)) & (keys[clipped] == query)
    if found.any():
        selected = table[clipped[found]]
        for field_index, field in enumerate(BOUNDARY_FIELDS):
            values[found, field_index] = selected[field]
    return values.reshape((*original_shape, len(BOUNDARY_FIELDS))), found.reshape(original_shape)


def lookup_period(
    period: str,
    from_node_id,
    to_node_id,
    directory: Optional[Union[str, Path]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    return lookup(load_period(period, directory), from_node_id, to_node_id)
