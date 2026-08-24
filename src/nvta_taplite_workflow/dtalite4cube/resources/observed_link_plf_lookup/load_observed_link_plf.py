from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np


PLF_FIELDS = {"AM": "plf_am", "MD": "plf_md", "PM": "plf_pm"}


def lookup_path(
    directory: Optional[Union[str, Path]] = None,
) -> Path:
    root = Path(directory) if directory is not None else Path(__file__).parent
    return root / "observed_link_plf_overrides.npy"


def _pack(from_node_id, to_node_id) -> np.ndarray:
    from_values = np.asarray(from_node_id, dtype=np.uint64)
    to_values = np.asarray(to_node_id, dtype=np.uint64)
    if from_values.shape != to_values.shape:
        raise ValueError("from_node_id and to_node_id shapes differ")
    return (from_values << np.uint64(32)) | to_values


def load_lookup(
    directory: Optional[Union[str, Path]] = None,
):
    """Memory-map the observed-link PLF table without loading it into RAM."""

    return np.load(lookup_path(directory), mmap_mode="r", allow_pickle=False)


def lookup(
    table,
    period: str,
    from_node_id,
    to_node_id,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return one period PLF and a same-shape node-pair found mask."""

    period_name = str(period).upper()
    if period_name not in PLF_FIELDS:
        raise ValueError(f"Unsupported observed PLF period: {period}")
    packed = _pack(from_node_id, to_node_id)
    original_shape = packed.shape
    query = packed.reshape(-1)
    values = np.full(len(query), np.nan, dtype=np.float32)
    if len(table) == 0:
        return values.reshape(original_shape), np.zeros(original_shape, dtype=bool)
    keys = table["packed_key"]
    positions = np.searchsorted(keys, query)
    clipped = np.minimum(positions, len(keys) - 1)
    found = (positions < len(keys)) & (keys[clipped] == query)
    if found.any():
        values[found] = table[PLF_FIELDS[period_name]][clipped[found]]
    return values.reshape(original_shape), found.reshape(original_shape)


def lookup_period(
    period: str,
    from_node_id,
    to_node_id,
    directory: Optional[Union[str, Path]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    return lookup(load_lookup(directory), period, from_node_id, to_node_id)
