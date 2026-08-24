"""DTAB v1 sparse binary demand helpers.

Layout (little-endian, packed), matching ``ReadBinaryDemandFile`` in
``kernel/src/TAPLite.cpp``:

    header: b"DTAB", int32 version=1, int64 n_records
    record: int32 o_zone, int32 d_zone, float64 volume
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Iterable

import numpy as np


DTAB_MAGIC = b"DTAB"
DTAB_VERSION = 1
DTAB_HEADER = struct.Struct("<4siq")
DTAB_RECORD_DTYPE = np.dtype(
    [
        ("o_zone_id", "<i4"),
        ("d_zone_id", "<i4"),
        ("volume", "<f8"),
    ],
    align=False,
)
DTAB_RECORD_SIZE = DTAB_RECORD_DTYPE.itemsize


def demand_binary_path(csv_path: str | Path) -> Path:
    path = Path(csv_path)
    return path.with_suffix(".bin") if path.suffix.lower() == ".csv" else Path(f"{path}.bin")


def write_dtab_record_part(
    path: str | Path,
    origins: np.ndarray,
    destinations: np.ndarray,
    volumes: np.ndarray,
    *,
    block_records: int = 1 << 20,
) -> int:
    """Write headerless packed DTAB records for later deterministic merging."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = int(len(volumes))
    if len(origins) != count or len(destinations) != count:
        raise ValueError("DTAB origin, destination, and volume arrays must have equal lengths")

    with target.open("wb") as stream:
        for start in range(0, count, block_records):
            stop = min(count, start + block_records)
            records = np.empty(stop - start, dtype=DTAB_RECORD_DTYPE)
            records["o_zone_id"] = origins[start:stop]
            records["d_zone_id"] = destinations[start:stop]
            records["volume"] = volumes[start:stop]
            records.tofile(stream)
    return count


def write_dtab_file(
    path: str | Path,
    origins: np.ndarray,
    destinations: np.ndarray,
    volumes: np.ndarray,
) -> int:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = int(len(volumes))
    with tempfile.TemporaryDirectory(prefix=".dtab_part_", dir=output.parent) as temp_dir:
        part_path = Path(temp_dir) / "records.bin"
        write_dtab_record_part(part_path, origins, destinations, volumes)
        merge_dtab_parts(
            output,
            [
                {
                    "row_start": 0,
                    "rows": count,
                    "binary_part_path": str(part_path),
                }
            ],
        )
    return count


def merge_dtab_parts(
    output_path: str | Path,
    parts: Iterable[dict],
) -> int:
    ordered = sorted(parts, key=lambda item: item["row_start"])
    count = sum(int(part["rows"]) for part in ordered)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as target:
        target.write(DTAB_HEADER.pack(DTAB_MAGIC, DTAB_VERSION, count))
        for part in ordered:
            part_path = Path(part["binary_part_path"])
            expected_size = int(part["rows"]) * DTAB_RECORD_SIZE
            if part_path.stat().st_size != expected_size:
                raise ValueError(
                    f"DTAB part size mismatch for {part_path}: "
                    f"expected {expected_size}, found {part_path.stat().st_size}"
                )
            with part_path.open("rb") as source:
                shutil.copyfileobj(source, target, length=1 << 20)
    return count


def inspect_dtab(path: str | Path) -> dict[str, int | str]:
    source = Path(path)
    with source.open("rb") as stream:
        header = stream.read(DTAB_HEADER.size)
    if len(header) != DTAB_HEADER.size:
        raise ValueError(f"DTAB header is truncated: {source}")
    magic, version, count = DTAB_HEADER.unpack(header)
    if magic != DTAB_MAGIC or version != DTAB_VERSION or count < 0:
        raise ValueError(f"Not a supported DTAB v1 file: {source}")
    expected_size = DTAB_HEADER.size + count * DTAB_RECORD_SIZE
    actual_size = source.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"DTAB size mismatch for {source}: expected {expected_size}, found {actual_size}"
        )
    return {
        "path": str(source),
        "version": version,
        "records": count,
        "bytes": actual_size,
    }


def iter_dtab_chunks(
    path: str | Path,
    *,
    chunk_records: int = 1 << 20,
):
    info = inspect_dtab(path)
    remaining = int(info["records"])
    with Path(path).open("rb") as stream:
        stream.seek(DTAB_HEADER.size)
        while remaining:
            take = min(remaining, chunk_records)
            records = np.fromfile(stream, dtype=DTAB_RECORD_DTYPE, count=take)
            if len(records) != take:
                raise ValueError(f"DTAB record data is truncated: {path}")
            yield records
            remaining -= take


def _numeric_zone_map(zone_map: dict[str, str]) -> dict[int, int]:
    numeric: dict[int, int] = {}
    for source, target in zone_map.items():
        numeric[int(float(source))] = int(float(target))
    return numeric


def remap_dtab(
    source_path: str | Path,
    target_path: str | Path,
    zone_map: dict[str, str],
) -> int:
    """Stream a DTAB file while replacing external zone IDs with compact IDs."""

    source_info = inspect_dtab(source_path)
    numeric_map = _numeric_zone_map(zone_map)
    if not numeric_map and int(source_info["records"]) > 0:
        raise ValueError("Cannot remap nonempty DTAB demand with an empty zone map")

    max_source = max(numeric_map, default=0)
    dense_lookup = np.full(max_source + 1, -1, dtype=np.int32)
    for source, target in numeric_map.items():
        if source < 0:
            raise ValueError(f"DTAB zone IDs must be nonnegative; found {source}")
        dense_lookup[source] = target

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(handle)
    temporary = Path(temporary_name)
    written = 0
    try:
        with temporary.open("wb") as stream:
            stream.write(
                DTAB_HEADER.pack(
                    DTAB_MAGIC,
                    DTAB_VERSION,
                    int(source_info["records"]),
                )
            )
            for records in iter_dtab_chunks(source_path):
                origins = records["o_zone_id"]
                destinations = records["d_zone_id"]
                if (
                    np.any(origins < 0)
                    or np.any(destinations < 0)
                    or np.any(origins > max_source)
                    or np.any(destinations > max_source)
                ):
                    raise ValueError(f"DTAB demand references a zone missing from the zone map: {source_path}")
                mapped_origins = dense_lookup[origins]
                mapped_destinations = dense_lookup[destinations]
                if np.any(mapped_origins < 0) or np.any(mapped_destinations < 0):
                    raise ValueError(f"DTAB demand references a zone missing from the zone map: {source_path}")
                records["o_zone_id"] = mapped_origins
                records["d_zone_id"] = mapped_destinations
                records.tofile(stream)
                written += len(records)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return written


def read_dtab_records(path: str | Path) -> np.ndarray:
    """Load a DTAB file for tests and small-file verification."""

    chunks = list(iter_dtab_chunks(path))
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=DTAB_RECORD_DTYPE)
