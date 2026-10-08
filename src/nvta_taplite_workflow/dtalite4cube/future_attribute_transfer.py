"""Transfer calibrated 2025 QVDF attributes to a future-year network.

The module exposes two deliberately different policies:

``simple``
    Copy calibration by exact directed node pair.  Links that are absent from
    the calibration dictionary receive the normal link-type QVDF defaults.

``framework``
    Apply the hierarchy documented in the CUBE future-network transfer
    framework: exact directed pair, constrained replacement path, directed
    geometry match, then link-type fallback.

Both policies write a complete future-network QVDF dictionary plus an
auditable crosswalk and an explicit log of every future pair that was missing
from the 2025 dictionary.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from .cube2gmns.fieldnameconfig import qvdf_params
from .cube2gmns.vdf_lookup_tables import get_vdf_dict


PERIOD_SEQUENCE = {"am": 1, "md": 2, "pm": 3}
QVDF_FIELDS = tuple(
    f"QVDF_{parameter}{sequence}"
    for sequence in PERIOD_SEQUENCE.values()
    for parameter in qvdf_params
)
REQUIRED_NETWORK_FIELDS = ("A", "B", "ATYPE", "FTYPE")
OPTIONAL_NETWORK_FIELDS = ("STREETNAME", "DISTANCE", "LINKID")


@dataclass(frozen=True)
class FrameworkConfig:
    """Thresholds for topology and geometry recovery in projected feet."""

    path_corridor_ft: float = 250.0
    path_overlap_buffer_ft: float = 80.0
    path_max_hops: int = 8
    path_max_length_ratio: float = 1.8
    path_min_score: float = 0.72
    geometry_search_ft: float = 250.0
    geometry_overlap_buffer_ft: float = 80.0
    geometry_candidate_min_score: float = 0.72
    geometry_candidate_min_margin: float = 0.08
    geometry_auto_min_score: float = 0.82
    geometry_auto_min_margin: float = 0.15


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _load_dictionary(path: Path) -> dict[tuple[int, int], dict[str, float]]:
    payload = np.load(path, allow_pickle=True).item()
    if not isinstance(payload, dict):
        raise TypeError("QVDF calibration resource must contain one dictionary")
    result: dict[tuple[int, int], dict[str, float]] = {}
    for key, values in payload.items():
        if not isinstance(key, tuple) or len(key) != 2 or not isinstance(values, dict):
            raise ValueError("Invalid QVDF node-pair dictionary entry")
        pair = (int(key[0]), int(key[1]))
        missing = set(QVDF_FIELDS).difference(values)
        if missing:
            raise ValueError(
                f"QVDF calibration entry {pair} is missing: {', '.join(sorted(missing))}"
            )
        result[pair] = {field: float(values[field]) for field in QVDF_FIELDS}
    return result


def _resolve_network_path(path: Path) -> Path:
    path = Path(path).resolve()
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(path)
    preferred = (
        "DTALiteNetworkInput_TRUESHP.shp",
        "DTALiteNetworkInput.shp",
    )
    files = {candidate.name.casefold(): candidate for candidate in path.glob("*.shp")}
    for name in preferred:
        if name.casefold() in files:
            return files[name.casefold()]
    if len(files) == 1:
        return next(iter(files.values()))
    raise FileNotFoundError(
        f"Could not select a network shapefile in {path}; pass the .shp path explicitly"
    )


def _load_network(path: Path) -> tuple[Path, gpd.GeoDataFrame]:
    resolved = _resolve_network_path(path)
    frame = gpd.read_file(resolved)
    missing = set(REQUIRED_NETWORK_FIELDS).difference(frame.columns)
    if missing:
        raise ValueError(f"{resolved} is missing: {', '.join(sorted(missing))}")
    if frame.crs is None:
        raise ValueError(f"{resolved} has no coordinate reference system")
    frame = frame.copy()
    frame["A"] = pd.to_numeric(frame["A"], errors="raise").astype("int64")
    frame["B"] = pd.to_numeric(frame["B"], errors="raise").astype("int64")
    if frame.duplicated(["A", "B"]).any():
        raise ValueError(f"{resolved} contains duplicate directed node pairs")
    for column in ("ATYPE", "FTYPE"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    for column in OPTIONAL_NETWORK_FIELDS:
        if column not in frame:
            frame[column] = ""
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError(f"{resolved} contains missing or empty geometry")
    return resolved, frame


def _street_name(value: object) -> str:
    text = re.sub(r"[^A-Z0-9]+", " ", str(value).upper()).strip()
    return "" if text in {"", "0", "NA", "NAN", "NONE"} else text


def _heading(geometry: LineString) -> float:
    start = geometry.coords[0]
    end = geometry.coords[-1]
    return math.atan2(end[1] - start[1], end[0] - start[0])


def _direction_score(base_geometry: LineString, future_geometry: LineString) -> float:
    difference = abs(_heading(base_geometry) - _heading(future_geometry))
    difference = min(difference, 2 * math.pi - difference)
    return max(0.0, math.cos(difference))


def _facility_score(base_row: pd.Series, future_row: pd.Series) -> float:
    return (
        float(int(base_row["ATYPE"]) == int(future_row["ATYPE"]))
        + float(int(base_row["FTYPE"]) == int(future_row["FTYPE"]))
    ) / 2.0


def _corridor_score(base_row: pd.Series, future_row: pd.Series) -> float:
    base_name = _street_name(base_row.get("STREETNAME", ""))
    future_name = _street_name(future_row.get("STREETNAME", ""))
    if base_name and future_name:
        return float(base_name == future_name)
    return 0.5


def _length_score(base_length: float, future_length: float) -> float:
    if base_length <= 0 or future_length <= 0:
        return 0.0
    return min(base_length, future_length) / max(base_length, future_length)


def _path_score(
    base_row: pd.Series,
    future_rows: gpd.GeoDataFrame,
    config: FrameworkConfig,
) -> float:
    base_geometry = base_row.geometry
    future_length = float(future_rows.geometry.length.sum())
    base_length = float(base_geometry.length)
    if not future_length or not base_length:
        return 0.0
    buffer = base_geometry.buffer(config.path_overlap_buffer_ft)
    overlap = sum(float(geometry.intersection(buffer).length) for geometry in future_rows.geometry)
    overlap_score = min(1.0, overlap / future_length)
    direction = float(
        np.average(
            [_direction_score(base_geometry, geometry) for geometry in future_rows.geometry],
            weights=[max(float(geometry.length), 1.0) for geometry in future_rows.geometry],
        )
    )
    facility = float(
        np.mean([_facility_score(base_row, row) for _, row in future_rows.iterrows()])
    )
    corridor = float(
        np.mean([_corridor_score(base_row, row) for _, row in future_rows.iterrows()])
    )
    length = _length_score(base_length, future_length)
    return 0.40 * overlap_score + 0.20 * direction + 0.15 * facility + 0.15 * corridor + 0.10 * length


def _candidate_path(
    base_row: pd.Series,
    future_unmatched: gpd.GeoDataFrame,
    adjacency: dict[int, list[tuple[int, int, float]]],
    config: FrameworkConfig,
    excluded_edges: set[int] | None = None,
) -> tuple[list[int], float] | None:
    start, end = int(base_row["A"]), int(base_row["B"])
    if start not in adjacency:
        return None
    base_geometry = base_row.geometry
    excluded_edges = excluded_edges or set()
    base_length = max(float(base_geometry.length), 1.0)
    maximum_length = config.path_max_length_ratio * base_length
    queue: list[tuple[float, int, tuple[int, ...], frozenset[int]]] = [
        (0.0, start, (), frozenset({start}))
    ]
    best_cost: dict[tuple[int, int], float] = {(start, 0): 0.0}
    while queue:
        cost, node, path, visited = heapq.heappop(queue)
        if node == end and path:
            rows = future_unmatched.loc[list(path)]
            score = _path_score(base_row, rows, config)
            if score >= config.path_min_score:
                return list(path), score
            continue
        if len(path) >= config.path_max_hops:
            continue
        for edge_index, next_node, edge_length in adjacency.get(node, ()):
            if edge_index in excluded_edges:
                continue
            if next_node in visited or cost + edge_length > maximum_length:
                continue
            geometry = future_unmatched.at[edge_index, "geometry"]
            if float(geometry.distance(base_geometry)) > config.path_corridor_ft:
                continue
            new_cost = cost + edge_length
            state = (next_node, len(path) + 1)
            if new_cost >= best_cost.get(state, float("inf")):
                continue
            best_cost[state] = new_cost
            heapq.heappush(
                queue,
                (new_cost, next_node, path + (edge_index,), visited | {next_node}),
            )
    return None


def _geometry_score(
    base_row: pd.Series,
    future_row: pd.Series,
    config: FrameworkConfig,
) -> float:
    base_geometry = base_row.geometry
    future_geometry = future_row.geometry
    future_length = max(float(future_geometry.length), 1.0)
    overlap = float(
        future_geometry.intersection(
            base_geometry.buffer(config.geometry_overlap_buffer_ft)
        ).length
    ) / future_length
    overlap = min(1.0, overlap)
    direction = _direction_score(base_geometry, future_geometry)
    facility = _facility_score(base_row, future_row)
    corridor = _corridor_score(base_row, future_row)
    length = _length_score(float(base_geometry.length), float(future_geometry.length))
    lateral = math.exp(
        -float(future_geometry.distance(base_geometry))
        / max(config.geometry_overlap_buffer_ft, 1.0)
    )
    return (
        0.32 * overlap
        + 0.20 * direction
        + 0.15 * facility
        + 0.15 * corridor
        + 0.08 * length
        + 0.10 * lateral
    )


def _fallback_values(row: pd.Series, lookup: dict[str, dict]) -> tuple[dict[str, float], str]:
    vdf_code = str(100 * int(row["ATYPE"]) + int(row["FTYPE"]))
    source_code = vdf_code if vdf_code in lookup else "all"
    record = lookup[source_code]
    values: dict[str, float] = {}
    for field in QVDF_FIELDS:
        value = record.get(field)
        if value in (None, ""):
            raise ValueError(f"Link-type QVDF fallback {source_code!r} has no {field}")
        values[field] = float(value)
    return values, source_code


def build_transfer_crosswalk(
    base_network: gpd.GeoDataFrame,
    future_network: gpd.GeoDataFrame,
    calibrated_pairs: Iterable[tuple[int, int]],
    *,
    method: str,
    config: FrameworkConfig | None = None,
) -> pd.DataFrame:
    """Build one provenance row for every future directed link."""

    method = method.strip().lower()
    if method not in {"simple", "framework"}:
        raise ValueError("method must be 'simple' or 'framework'")
    config = config or FrameworkConfig()
    calibrated = {(int(a), int(b)) for a, b in calibrated_pairs}

    base = base_network.copy()
    future = future_network.copy()
    base["_pair"] = list(zip(base["A"].astype(int), base["B"].astype(int)))
    future["_pair"] = list(zip(future["A"].astype(int), future["B"].astype(int)))
    all_base_pairs = set(base["_pair"])
    base = base[base["_pair"].isin(calibrated)].copy()
    base_by_pair = {pair: index for index, pair in base["_pair"].items()}

    rows: dict[int, dict[str, object]] = {}
    for index, row in future.iterrows():
        pair = row["_pair"]
        if pair in calibrated:
            rows[index] = {
                "future_a": pair[0],
                "future_b": pair[1],
                "base_a": pair[0],
                "base_b": pair[1],
                "match_method": "EXACT_AB",
                "confidence_class": "B",
                "match_score": 1.0,
                "score_margin": 1.0,
                "review_flag": 0,
                "attribute_source": "BASE_CALIBRATION",
                "candidate_base_a": np.nan,
                "candidate_base_b": np.nan,
                "fallback_vdf_code": "",
                "notes": "exact directed node pair",
            }

    if method == "framework":
        unmatched_indices = [index for index in future.index if index not in rows]
        future_unmatched = future.loc[unmatched_indices]
        adjacency: dict[int, list[tuple[int, int, float]]] = {}
        for index, row in future_unmatched.iterrows():
            adjacency.setdefault(int(row["A"]), []).append(
                (index, int(row["B"]), max(float(row.geometry.length), 1.0))
            )

        missing_base_pairs = sorted(calibrated.difference(set(future["_pair"])))
        claimed: set[int] = set()
        for pair in missing_base_pairs:
            base_index = base_by_pair.get(pair)
            if base_index is None:
                continue
            candidate = _candidate_path(
                base.loc[base_index],
                future_unmatched,
                adjacency,
                config,
                excluded_edges=claimed,
            )
            if candidate is None:
                continue
            path_indices, score = candidate
            if any(index in rows or index in claimed for index in path_indices):
                continue
            for index in path_indices:
                future_pair = future.at[index, "_pair"]
                rows[index] = {
                    "future_a": future_pair[0],
                    "future_b": future_pair[1],
                    "base_a": pair[0],
                    "base_b": pair[1],
                    "match_method": "REPLACEMENT_PATH",
                    "confidence_class": "C",
                    "match_score": score,
                    "score_margin": np.nan,
                    "review_flag": int(score < 0.82),
                    "attribute_source": "BASE_CALIBRATION",
                    "candidate_base_a": np.nan,
                    "candidate_base_b": np.nan,
                    "fallback_vdf_code": "",
                    "notes": f"directed replacement path with {len(path_indices)} links",
                }
                claimed.add(index)

        remaining = [index for index in future.index if index not in rows]
        base_index = base.sindex
        for index in remaining:
            future_row = future.loc[index]
            # An unchanged future pair that exists in the 2025 network but has
            # no calibrated record must remain a fallback.  Spatially matching
            # it to a nearby calibrated carriageway would invent lineage.
            if future_row["_pair"] in all_base_pairs:
                continue
            search_geometry = future_row.geometry.buffer(config.geometry_search_ft)
            candidate_positions = list(base_index.query(search_geometry, predicate="intersects"))
            scored: list[tuple[float, int]] = []
            for position in candidate_positions:
                candidate_index = base.index[int(position)]
                score = _geometry_score(base.loc[candidate_index], future_row, config)
                scored.append((score, candidate_index))
            scored.sort(reverse=True)
            if not scored:
                continue
            best_score, best_index = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            margin = best_score - runner_up
            if (
                best_score < config.geometry_candidate_min_score
                or margin < config.geometry_candidate_min_margin
            ):
                continue
            base_pair = base.at[best_index, "_pair"]
            future_pair = future_row["_pair"]
            if (
                best_score >= config.geometry_auto_min_score
                and margin >= config.geometry_auto_min_margin
            ):
                rows[index] = {
                    "future_a": future_pair[0],
                    "future_b": future_pair[1],
                    "base_a": base_pair[0],
                    "base_b": base_pair[1],
                    "match_method": "GEOMETRY",
                    "confidence_class": "D",
                    "match_score": best_score,
                    "score_margin": margin,
                    "review_flag": 0,
                    "attribute_source": "BASE_CALIBRATION",
                    "candidate_base_a": np.nan,
                    "candidate_base_b": np.nan,
                    "fallback_vdf_code": "",
                    "notes": "accepted directed geometry match",
                }
            else:
                rows[index] = {
                    "future_a": future_pair[0],
                    "future_b": future_pair[1],
                    "base_a": np.nan,
                    "base_b": np.nan,
                    "match_method": "GEOMETRY_CANDIDATE",
                    "confidence_class": "E",
                    "match_score": best_score,
                    "score_margin": margin,
                    "review_flag": 1,
                    "attribute_source": "LINK_TYPE_FALLBACK",
                    "candidate_base_a": base_pair[0],
                    "candidate_base_b": base_pair[1],
                    "fallback_vdf_code": str(
                        100 * int(future_row["ATYPE"]) + int(future_row["FTYPE"])
                    ),
                    "notes": "geometry candidate retained for review; fallback applied",
                }

    for index, row in future.iterrows():
        if index in rows:
            continue
        pair = row["_pair"]
        rows[index] = {
            "future_a": pair[0],
            "future_b": pair[1],
            "base_a": np.nan,
            "base_b": np.nan,
            "match_method": "LINK_TYPE_FALLBACK",
            "confidence_class": "E",
            "match_score": np.nan,
            "score_margin": np.nan,
            "review_flag": 1,
            "attribute_source": "LINK_TYPE_FALLBACK",
            "candidate_base_a": np.nan,
            "candidate_base_b": np.nan,
            "fallback_vdf_code": str(100 * int(row["ATYPE"]) + int(row["FTYPE"])),
            "notes": "no accepted 2025 lineage; QVDF-only link-type fallback",
        }

    return pd.DataFrame.from_dict(rows, orient="index").loc[future.index]


def run_transfer(
    *,
    base_network_path: Path,
    future_network_path: Path,
    calibration_dictionary_path: Path,
    output_dir: Path,
    method: str,
    config: FrameworkConfig | None = None,
    write_spatial: bool = True,
) -> dict[str, object]:
    """Run a transfer policy and write the future dictionary and QA artifacts."""

    method = method.strip().lower()
    config = config or FrameworkConfig()
    dictionary_path = Path(calibration_dictionary_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    base_path, base = _load_network(base_network_path)
    future_path, future = _load_network(future_network_path)
    if base.crs != future.crs:
        future = future.to_crs(base.crs)
    dictionary = _load_dictionary(dictionary_path)
    crosswalk = build_transfer_crosswalk(
        base,
        future,
        dictionary,
        method=method,
        config=config,
    )

    link_type_lookup = get_vdf_dict("qvdf")
    future_dictionary: dict[tuple[int, int], dict[str, float]] = {}
    fallback_sources: dict[int, str] = {}
    for index, transfer in crosswalk.iterrows():
        future_pair = (int(transfer["future_a"]), int(transfer["future_b"]))
        if transfer["attribute_source"] == "LINK_TYPE_FALLBACK":
            values, source_code = _fallback_values(future.loc[index], link_type_lookup)
            fallback_sources[index] = source_code
        else:
            base_pair = (int(transfer["base_a"]), int(transfer["base_b"]))
            values = dictionary[base_pair]
        future_dictionary[future_pair] = dict(values)
    if len(future_dictionary) != len(future):
        raise RuntimeError("Future QVDF dictionary does not cover every future node pair")

    dictionary_output = output_dir / "future_qvdf_node_pair_overrides.npy"
    np.save(dictionary_output, future_dictionary, allow_pickle=True)

    crosswalk_output = output_dir / "attribute_transfer_crosswalk.csv"
    crosswalk_to_write = crosswalk.copy()
    for index, source in fallback_sources.items():
        crosswalk_to_write.at[index, "fallback_vdf_code"] = source
    crosswalk_to_write.to_csv(crosswalk_output, index=False)

    exact_pairs = set(dictionary).intersection(zip(future["A"], future["B"]))
    missing_mask = [
        (int(a), int(b)) not in dictionary for a, b in zip(future["A"], future["B"])
    ]
    missing_columns = [
        column
        for column in ("A", "B", "ATYPE", "FTYPE", "STREETNAME", "DISTANCE", "LINKID")
        if column in future.columns
    ]
    missing = future.loc[missing_mask, missing_columns].copy()
    missing = missing.rename(columns={"A": "future_a", "B": "future_b"})
    missing = missing.merge(
        crosswalk_to_write,
        on=["future_a", "future_b"],
        how="left",
        validate="one_to_one",
    )
    missing_output = output_dir / "missing_2025_node_pairs.csv"
    missing.to_csv(missing_output, index=False)

    spatial_output: Path | None = None
    if write_spatial:
        spatial_output = output_dir / "attribute_transfer_crosswalk.gpkg"
        spatial = future[["A", "B", "ATYPE", "FTYPE", "STREETNAME", "geometry"]].copy()
        spatial = spatial.rename(columns={"A": "future_a", "B": "future_b"})
        spatial = spatial.merge(
            crosswalk_to_write,
            on=["future_a", "future_b"],
            how="left",
            validate="one_to_one",
        )
        spatial.to_file(spatial_output, layer="qvdf_transfer", driver="GPKG")

    counts = {
        key: int(value)
        for key, value in crosswalk["match_method"].value_counts().sort_index().items()
    }
    review_count = int(crosswalk["review_flag"].sum())
    summary = {
        "status": "PASS",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "base_network": str(base_path),
        "future_network": str(future_path),
        "calibration_dictionary": str(dictionary_path),
        "calibration_dictionary_sha256": _sha256(dictionary_path),
        "base_links": int(len(base)),
        "calibrated_2025_pairs": int(len(dictionary)),
        "future_links": int(len(future)),
        "exact_2025_pairs": int(len(exact_pairs)),
        "missing_2025_pairs": int(sum(missing_mask)),
        "match_method_counts": counts,
        "review_links": review_count,
        "review_share_pct": round(100.0 * review_count / len(future), 4),
        "qvdf_fields_transferred": list(QVDF_FIELDS),
        "non_qvdf_unmatched_policy": "leave empty; this transfer dictionary contains only QVDF parameters",
        "configuration": config.__dict__,
        "outputs": {
            "future_dictionary": str(dictionary_output),
            "crosswalk_csv": str(crosswalk_output),
            "missing_pair_log": str(missing_output),
            "crosswalk_gpkg": str(spatial_output) if spatial_output else None,
        },
    }
    summary_output = output_dir / "attribute_transfer_summary.json"
    summary_output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summary["outputs"]["summary_json"] = str(summary_output)
    return summary


__all__ = [
    "FrameworkConfig",
    "PERIOD_SEQUENCE",
    "QVDF_FIELDS",
    "build_transfer_crosswalk",
    "run_transfer",
]
