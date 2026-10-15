"""Transfer calibrated 2025 QVDF attributes to a future-year network.

The module exposes two deliberately different policies:

``simple``
    Copy calibration by exact directed node pair.  Links that are absent from
    the calibration dictionary receive the normal link-type QVDF defaults.

``framework``
    Apply the hierarchy documented in the CUBE future-network transfer
    framework: exact directed pair, constrained replacement path, directed
    high-confidence geometry match, accepted corridor calibration, then
    link-type fallback.

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
CORRIDOR_FIT_TO_QVDF = {
    "n": "n",
    "s": "s",
    "cp": "f_p",
    "cd": "f_d",
    "alpha": "alpha",
    "beta": "beta",
}
CORRIDOR_CACHE_COLUMNS = (
    "corridor",
    "period",
    "n",
    "s",
    "cp",
    "cd",
    "alpha",
    "beta",
    "n_episodes",
    "source_catalog",
)
EXPANDED_DICTIONARY_NAME = "expanded_node_pair_qvdf.csv"
EXPANDED_MANIFEST_NAME = "expanded_node_pair_qvdf_manifest.json"


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
    geometry_auto_min_score: float = 0.82
    geometry_auto_min_margin: float = 0.15
    corridor_min_score: float = 0.72
    corridor_min_margin: float = 0.08
    corridor_min_direction_score: float = 0.70
    corridor_min_link_type_score: float = 0.50


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


def _network_fingerprint(frame: gpd.GeoDataFrame) -> str:
    """Hash the attributes and geometry that control transfer decisions."""

    columns = [
        column
        for column in (*REQUIRED_NETWORK_FIELDS, *OPTIONAL_NETWORK_FIELDS)
        if column in frame.columns
    ]
    digest = hashlib.sha256(str(frame.crs).encode("utf-8"))
    hashes = pd.util.hash_pandas_object(frame[columns], index=False)
    digest.update(hashes.to_numpy(dtype="uint64").tobytes())
    for geometry in frame.geometry:
        digest.update(geometry.wkb)
    return digest.hexdigest().upper()


def _load_corridor_cache(
    output_dir: Path,
    corridor_root: Path,
    *,
    rebuild: bool,
) -> tuple[
    dict[str, dict[str, dict[str, float]]],
    dict[tuple[int, int], str],
    dict[tuple[int, int], dict[str, object]],
    dict[str, object],
]:
    """Create or reuse the run-local accepted corridor calibration lookup."""

    fit_cache = output_dir / "corridor_qvdf_dictionary.csv"
    pair_cache = output_dir / "corridor_node_pair_lookup.csv"
    cache_files_exist = fit_cache.is_file() and pair_cache.is_file()
    if (fit_cache.exists() != pair_cache.exists()) and not rebuild:
        raise ValueError(
            "The corridor cache is incomplete; use a new output directory or "
            "request a corridor-cache rebuild"
        )

    if cache_files_exist and not rebuild:
        fits = pd.read_csv(fit_cache, low_memory=False)
        pair_rows = pd.read_csv(pair_cache, low_memory=False)
        action = "reused"
    else:
        root = Path(corridor_root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        fit_frames: list[pd.DataFrame] = []
        pair_frames: list[pd.DataFrame] = []
        for corridor_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            catalog_path = (
                corridor_dir
                / "06-qvdf-calibration"
                / "primary_qvdf_fit_catalog.csv"
            )
            reference_path = corridor_dir / "01-input-and-qc" / "link_reference.csv"
            if catalog_path.is_file():
                catalog = pd.read_csv(catalog_path, low_memory=False)
                required = {
                    "corridor",
                    "sensor_uid",
                    "calibration_period",
                    "status",
                    "n_episodes",
                    *CORRIDOR_FIT_TO_QVDF.values(),
                }
                missing = required.difference(catalog.columns)
                if missing:
                    raise ValueError(
                        f"{catalog_path} is missing: {', '.join(sorted(missing))}"
                    )
                accepted = catalog[
                    catalog["sensor_uid"].astype(str).str.strip().str.casefold().eq("all")
                    & catalog["status"].astype(str).str.strip().str.casefold().eq("ok")
                    & catalog["calibration_period"]
                    .astype(str)
                    .str.strip()
                    .str.upper()
                    .isin({period.upper() for period in PERIOD_SEQUENCE})
                ].copy()
                if not accepted.empty:
                    mapped = pd.DataFrame(
                        {
                            "corridor": accepted["corridor"].astype(str).str.strip(),
                            "period": accepted["calibration_period"]
                            .astype(str)
                            .str.strip()
                            .str.lower(),
                            "n_episodes": pd.to_numeric(
                                accepted["n_episodes"], errors="coerce"
                            ),
                            "source_catalog": str(catalog_path.resolve()),
                        }
                    )
                    for qvdf_name, fit_name in CORRIDOR_FIT_TO_QVDF.items():
                        mapped[qvdf_name] = pd.to_numeric(
                            accepted[fit_name], errors="coerce"
                        )
                    fit_frames.append(mapped)
            if reference_path.is_file():
                reference = pd.read_csv(reference_path, low_memory=False)
                required = {
                    "corridor",
                    "direction",
                    "network_from_node_id",
                    "network_to_node_id",
                    "network_selected_for_node_pair_lookup",
                }
                missing = required.difference(reference.columns)
                if missing:
                    raise ValueError(
                        f"{reference_path} is missing: {', '.join(sorted(missing))}"
                    )
                selected = reference[
                    reference["network_selected_for_node_pair_lookup"]
                    .astype(str)
                    .str.strip()
                    .str.casefold()
                    .isin({"true", "1", "yes", "y"})
                ].copy()
                selected["from_node_id"] = pd.to_numeric(
                    selected["network_from_node_id"], errors="coerce"
                )
                selected["to_node_id"] = pd.to_numeric(
                    selected["network_to_node_id"], errors="coerce"
                )
                selected = selected.dropna(subset=["from_node_id", "to_node_id"])
                for column in ("tmc_code", "road_order"):
                    if column not in selected:
                        selected[column] = ""
                pair_frames.append(
                    selected[
                        [
                            "from_node_id",
                            "to_node_id",
                            "corridor",
                            "direction",
                            "tmc_code",
                            "road_order",
                        ]
                    ]
                )

        if not fit_frames:
            raise ValueError(f"No accepted corridor calibration rows found under {root}")
        fits = pd.concat(fit_frames, ignore_index=True)
        parameter_columns = list(CORRIDOR_FIT_TO_QVDF)
        if (
            fits[parameter_columns].isna().any().any()
            or not np.isfinite(fits[parameter_columns].to_numpy(dtype=float)).all()
        ):
            raise ValueError("Accepted corridor calibration contains non-finite values")
        if fits.duplicated(["corridor", "period"]).any():
            duplicates = fits.loc[
                fits.duplicated(["corridor", "period"], keep=False),
                ["corridor", "period"],
            ]
            raise ValueError(
                "Accepted corridor calibration contains duplicate corridor-period rows: "
                f"{duplicates.drop_duplicates().to_dict('records')[:10]}"
            )
        accepted_corridors = set(fits["corridor"])
        pair_rows = pd.concat(pair_frames, ignore_index=True)
        pair_rows["corridor"] = pair_rows["corridor"].astype(str).str.strip()
        pair_rows = pair_rows[pair_rows["corridor"].isin(accepted_corridors)].copy()
        pair_rows[["from_node_id", "to_node_id"]] = pair_rows[
            ["from_node_id", "to_node_id"]
        ].astype("int64")
        conflicts = (
            pair_rows.groupby(["from_node_id", "to_node_id"])["corridor"]
            .nunique()
            .gt(1)
        )
        if conflicts.any():
            raise ValueError("A corridor node pair maps to more than one corridor")
        fits = fits.loc[:, CORRIDOR_CACHE_COLUMNS].sort_values(
            ["corridor", "period"]
        )
        pair_rows = pair_rows.drop_duplicates(
            ["from_node_id", "to_node_id"]
        ).sort_values(["from_node_id", "to_node_id"])
        fits.to_csv(fit_cache, index=False)
        pair_rows.to_csv(pair_cache, index=False)
        action = "created" if not cache_files_exist else "rebuilt"

    required_fit_columns = set(CORRIDOR_CACHE_COLUMNS)
    missing = required_fit_columns.difference(fits.columns)
    if missing:
        raise ValueError(f"{fit_cache} is missing: {', '.join(sorted(missing))}")
    required_pair_columns = {
        "from_node_id",
        "to_node_id",
        "corridor",
        "direction",
        "tmc_code",
        "road_order",
    }
    missing = required_pair_columns.difference(pair_rows.columns)
    if missing:
        raise ValueError(f"{pair_cache} is missing: {', '.join(sorted(missing))}")

    corridor_dictionary: dict[str, dict[str, dict[str, float]]] = {}
    for row in fits.itertuples(index=False):
        corridor_dictionary.setdefault(str(row.corridor), {})[str(row.period)] = {
            parameter: float(getattr(row, parameter))
            for parameter in CORRIDOR_FIT_TO_QVDF
        }
    pair_lookup = {
        (int(row.from_node_id), int(row.to_node_id)): str(row.corridor)
        for row in pair_rows.itertuples(index=False)
    }
    reference_lookup = {
        (int(row.from_node_id), int(row.to_node_id)): {
            "direction": str(row.direction),
            "tmc_code": str(row.tmc_code),
            "road_order": row.road_order,
        }
        for row in pair_rows.itertuples(index=False)
    }
    metadata = {
        "action": action,
        "fit_cache": str(fit_cache),
        "pair_cache": str(pair_cache),
        "accepted_corridor_periods": int(len(fits)),
        "accepted_corridors": int(len(corridor_dictionary)),
        "mapped_node_pairs": int(len(pair_lookup)),
        "fit_cache_sha256": _sha256(fit_cache),
        "pair_cache_sha256": _sha256(pair_cache),
    }
    return corridor_dictionary, pair_lookup, reference_lookup, metadata


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
    return (
        0.40 * overlap_score
        + 0.20 * direction
        + 0.15 * facility
        + 0.15 * corridor
        + 0.10 * length
    )


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
    corridor_pair_lookup: dict[tuple[int, int], str] | None = None,
    corridor_reference_lookup: dict[tuple[int, int], dict[str, object]] | None = None,
) -> pd.DataFrame:
    """Build one provenance row for every future directed link."""

    method = method.strip().lower()
    if method not in {"simple", "framework"}:
        raise ValueError("method must be 'simple' or 'framework'")
    config = config or FrameworkConfig()
    calibrated = {(int(a), int(b)) for a, b in calibrated_pairs}

    base_all = base_network.copy()
    future = future_network.copy()
    base_all["_pair"] = list(
        zip(base_all["A"].astype(int), base_all["B"].astype(int))
    )
    future["_pair"] = list(zip(future["A"].astype(int), future["B"].astype(int)))
    all_base_pairs = set(base_all["_pair"])
    base = base_all[base_all["_pair"].isin(calibrated)].copy()
    base_by_pair = {pair: index for index, pair in base["_pair"].items()}

    rows: dict[int, dict[str, object]] = {}
    corridor_rejections: dict[int, dict[str, object]] = {}
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

        corridor_pair_lookup = corridor_pair_lookup or {}
        corridor_reference_lookup = corridor_reference_lookup or {}
        if corridor_pair_lookup:
            corridor_base = base_all[
                base_all["_pair"].isin(corridor_pair_lookup)
            ].copy()
            corridor_base["_corridor"] = corridor_base["_pair"].map(
                corridor_pair_lookup
            )
            corridor_base["_corridor_direction"] = corridor_base["_pair"].map(
                lambda pair: corridor_reference_lookup.get(pair, {}).get(
                    "direction", ""
                )
            )
            corridor_link_types = {
                str(corridor): sorted(
                    {
                        (int(row["ATYPE"]), int(row["FTYPE"]))
                        for _, row in group.iterrows()
                    }
                )
                for corridor, group in corridor_base.groupby("_corridor")
            }
            base_all_by_pair = {
                pair: index for index, pair in base_all["_pair"].items()
            }
            corridor_index = corridor_base.sindex
            remaining = [index for index in future.index if index not in rows]
            for index in remaining:
                future_row = future.loc[index]
                future_pair = future_row["_pair"]
                direct_corridor = corridor_pair_lookup.get(future_pair)
                candidate_pair: tuple[int, int] | None = None
                if direct_corridor is not None:
                    corridor = direct_corridor
                    candidate_index = base_all_by_pair[future_pair]
                    candidate_row = base_all.loc[candidate_index]
                    score = 1.0
                    margin = 1.0
                    candidate_pair = future_pair
                    note = "exact 2025 corridor membership; accepted corridor-period fit"
                else:
                    search_geometry = future_row.geometry.buffer(
                        config.geometry_search_ft
                    )
                    candidate_positions = list(
                        corridor_index.query(search_geometry, predicate="intersects")
                    )
                    by_corridor: dict[
                        str, tuple[float, tuple[int, int], int]
                    ] = {}
                    for position in candidate_positions:
                        candidate_index = corridor_base.index[int(position)]
                        candidate_row = corridor_base.loc[candidate_index]
                        candidate_score = _geometry_score(
                            candidate_row, future_row, config
                        )
                        candidate_corridor = str(candidate_row["_corridor"])
                        previous = by_corridor.get(candidate_corridor)
                        if previous is None or candidate_score > previous[0]:
                            by_corridor[candidate_corridor] = (
                                candidate_score,
                                candidate_row["_pair"],
                                candidate_index,
                            )
                    ranked = sorted(
                        (
                            (candidate_score, candidate_corridor, pair, row_index)
                            for candidate_corridor, (
                                candidate_score,
                                pair,
                                row_index,
                            ) in by_corridor.items()
                        ),
                        reverse=True,
                    )
                    if not ranked:
                        continue
                    score, corridor, candidate_pair, candidate_index = ranked[0]
                    candidate_row = corridor_base.loc[candidate_index]
                    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
                    margin = score - runner_up
                    note = (
                        "corridor inferred from directed geometry; accepted "
                        "corridor-period fit"
                    )
                direction_score = _direction_score(
                    candidate_row.geometry, future_row.geometry
                )
                observed_link_types = corridor_link_types[corridor]
                type_scores = [
                    (
                        (
                            float(int(future_row["ATYPE"]) == atype)
                            + float(int(future_row["FTYPE"]) == ftype)
                        )
                        / 2.0,
                        atype,
                        ftype,
                    )
                    for atype, ftype in observed_link_types
                ]
                link_type_score, matched_atype, matched_ftype = max(type_scores)
                name_score = _corridor_score(candidate_row, future_row)
                rejection_reasons = []
                if score < config.corridor_min_score:
                    rejection_reasons.append("score_below_minimum")
                if margin < config.corridor_min_margin:
                    rejection_reasons.append("corridor_margin_below_minimum")
                if direction_score < config.corridor_min_direction_score:
                    rejection_reasons.append("direction_misaligned")
                if link_type_score < config.corridor_min_link_type_score:
                    rejection_reasons.append("link_type_incompatible")
                reference = corridor_reference_lookup.get(candidate_pair, {})
                if rejection_reasons:
                    corridor_rejections[index] = {
                        "corridor_candidate_id": corridor,
                        "corridor_direction": reference.get("direction", ""),
                        "corridor_tmc_code": reference.get("tmc_code", ""),
                        "corridor_direction_score": direction_score,
                        "corridor_link_type_score": link_type_score,
                        "corridor_name_score": name_score,
                        "corridor_reference_atype": int(candidate_row["ATYPE"]),
                        "corridor_reference_ftype": int(candidate_row["FTYPE"]),
                        "corridor_observed_link_types": ";".join(
                            f"{atype}:{ftype}"
                            for atype, ftype in observed_link_types
                        ),
                        "corridor_matched_atype": matched_atype,
                        "corridor_matched_ftype": matched_ftype,
                        "corridor_rejection_reason": ";".join(rejection_reasons),
                        "candidate_base_a": candidate_pair[0],
                        "candidate_base_b": candidate_pair[1],
                        "match_score": score,
                        "score_margin": margin,
                    }
                    continue
                rows[index] = {
                    "future_a": future_pair[0],
                    "future_b": future_pair[1],
                    "base_a": np.nan,
                    "base_b": np.nan,
                    "match_method": "CORRIDOR",
                    "confidence_class": "E",
                    "match_score": score,
                    "score_margin": margin,
                    "review_flag": 0,
                    "attribute_source": "CORRIDOR_CALIBRATION",
                    "candidate_base_a": candidate_pair[0] if candidate_pair else np.nan,
                    "candidate_base_b": candidate_pair[1] if candidate_pair else np.nan,
                    "corridor_id": corridor,
                    "corridor_candidate_id": corridor,
                    "corridor_direction": reference.get("direction", ""),
                    "corridor_tmc_code": reference.get("tmc_code", ""),
                    "corridor_direction_score": direction_score,
                    "corridor_link_type_score": link_type_score,
                    "corridor_name_score": name_score,
                    "corridor_reference_atype": int(candidate_row["ATYPE"]),
                    "corridor_reference_ftype": int(candidate_row["FTYPE"]),
                    "corridor_observed_link_types": ";".join(
                        f"{atype}:{ftype}" for atype, ftype in observed_link_types
                    ),
                    "corridor_matched_atype": matched_atype,
                    "corridor_matched_ftype": matched_ftype,
                    "corridor_rejection_reason": "",
                    "fallback_vdf_code": "",
                    "notes": note,
                }

    for index, row in future.iterrows():
        if index in rows:
            continue
        pair = row["_pair"]
        rejection = corridor_rejections.get(index, {})
        fallback_row = {
            "future_a": pair[0],
            "future_b": pair[1],
            "base_a": np.nan,
            "base_b": np.nan,
            "match_method": "LINK_TYPE_FALLBACK",
            "confidence_class": "F",
            "match_score": np.nan,
            "score_margin": np.nan,
            "review_flag": 1,
            "attribute_source": "LINK_TYPE_FALLBACK",
            "candidate_base_a": np.nan,
            "candidate_base_b": np.nan,
            "corridor_id": "",
            "corridor_candidate_id": "",
            "corridor_direction": "",
            "corridor_tmc_code": "",
            "corridor_direction_score": np.nan,
            "corridor_link_type_score": np.nan,
            "corridor_name_score": np.nan,
            "corridor_reference_atype": np.nan,
            "corridor_reference_ftype": np.nan,
            "corridor_observed_link_types": "",
            "corridor_matched_atype": np.nan,
            "corridor_matched_ftype": np.nan,
            "corridor_rejection_reason": "",
            "fallback_vdf_code": str(100 * int(row["ATYPE"]) + int(row["FTYPE"])),
            "notes": "no accepted 2025 lineage; QVDF-only link-type fallback",
        }
        fallback_row.update(rejection)
        if rejection:
            fallback_row["notes"] = (
                "corridor candidate rejected by explicit evidence gates; "
                "QVDF-only link-type fallback"
            )
        rows[index] = fallback_row

    result = pd.DataFrame.from_dict(rows, orient="index").loc[future.index]
    for column in (
        "corridor_id",
        "corridor_candidate_id",
        "corridor_direction",
        "corridor_tmc_code",
        "corridor_observed_link_types",
        "corridor_rejection_reason",
    ):
        if column not in result:
            result[column] = ""
        result[column] = result[column].fillna("")
    return result


def _expanded_cache_signature(
    *,
    method: str,
    base: gpd.GeoDataFrame,
    future: gpd.GeoDataFrame,
    dictionary_path: Path,
    corridor_metadata: dict[str, object] | None,
    config: FrameworkConfig,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "method": method,
        "base_network_fingerprint": _network_fingerprint(base),
        "future_network_fingerprint": _network_fingerprint(future),
        "calibration_dictionary_sha256": _sha256(dictionary_path),
        "corridor_fit_cache_sha256": (
            corridor_metadata.get("fit_cache_sha256") if corridor_metadata else None
        ),
        "corridor_pair_cache_sha256": (
            corridor_metadata.get("pair_cache_sha256") if corridor_metadata else None
        ),
        "configuration": config.__dict__,
    }


def _load_expanded_dictionary(
    path: Path,
) -> tuple[
    dict[tuple[int, int], dict[str, float]],
    pd.DataFrame,
]:
    frame = pd.read_csv(path, low_memory=False)
    required = {"future_a", "future_b", "match_method", *QVDF_FIELDS}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing: {', '.join(sorted(missing))}")
    if frame.duplicated(["future_a", "future_b"]).any():
        raise ValueError(f"{path} contains duplicate future node pairs")
    values = frame.loc[:, QVDF_FIELDS].apply(pd.to_numeric, errors="coerce")
    if values.isna().any().any() or not np.isfinite(values.to_numpy()).all():
        raise ValueError(f"{path} contains invalid QVDF values")
    dictionary = {
        (int(row.future_a), int(row.future_b)): {
            field: float(getattr(row, field)) for field in QVDF_FIELDS
        }
        for row in frame.itertuples(index=False)
    }
    return dictionary, frame.drop(columns=list(QVDF_FIELDS))


def _write_expanded_dictionary(
    path: Path,
    crosswalk: pd.DataFrame,
    dictionary: dict[tuple[int, int], dict[str, float]],
) -> None:
    values = pd.DataFrame(
        [
            {"future_a": pair[0], "future_b": pair[1], **parameters}
            for pair, parameters in dictionary.items()
        ]
    )
    expanded = crosswalk.merge(
        values,
        on=["future_a", "future_b"],
        how="left",
        validate="one_to_one",
    )
    expanded.to_csv(path, index=False)


def run_transfer(
    *,
    base_network_path: Path,
    future_network_path: Path,
    calibration_dictionary_path: Path,
    output_dir: Path,
    method: str,
    config: FrameworkConfig | None = None,
    write_spatial: bool = True,
    corridor_root: Path | None = None,
    rebuild_corridor_dictionary: bool = False,
    rebuild_expanded_dictionary: bool = False,
) -> dict[str, object]:
    """Run a transfer policy and write the future dictionary and QA artifacts."""

    method = method.strip().lower()
    config = config or FrameworkConfig()
    dictionary_path = Path(calibration_dictionary_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    dictionary_output = output_dir / "future_qvdf_node_pair_overrides.npy"
    if dictionary_output.resolve() == dictionary_path:
        raise ValueError(
            "The expanded future dictionary output cannot replace the base "
            "calibration dictionary"
        )
    base_path, base = _load_network(base_network_path)
    future_path, future = _load_network(future_network_path)
    if base.crs != future.crs:
        future = future.to_crs(base.crs)
    base_dictionary_sha256 = _sha256(dictionary_path)
    dictionary = _load_dictionary(dictionary_path)

    corridor_dictionary: dict[str, dict[str, dict[str, float]]] = {}
    corridor_pair_lookup: dict[tuple[int, int], str] = {}
    corridor_reference_lookup: dict[tuple[int, int], dict[str, object]] = {}
    corridor_metadata: dict[str, object] | None = None
    if corridor_root is not None:
        (
            corridor_dictionary,
            corridor_pair_lookup,
            corridor_reference_lookup,
            corridor_metadata,
        ) = (
            _load_corridor_cache(
                output_dir,
                Path(corridor_root),
                rebuild=rebuild_corridor_dictionary,
            )
        )

    expanded_output = output_dir / EXPANDED_DICTIONARY_NAME
    expanded_manifest_output = output_dir / EXPANDED_MANIFEST_NAME
    signature = _expanded_cache_signature(
        method=method,
        base=base,
        future=future,
        dictionary_path=dictionary_path,
        corridor_metadata=corridor_metadata,
        config=config,
    )
    expanded_action = "created"
    if expanded_output.is_file() or expanded_manifest_output.is_file():
        if not expanded_output.is_file() or not expanded_manifest_output.is_file():
            raise ValueError(
                "The expanded node-pair cache is incomplete; use a new output "
                "directory or request an expanded-dictionary rebuild"
            )
        existing_manifest = json.loads(
            expanded_manifest_output.read_text(encoding="utf-8")
        )
        existing_signature = existing_manifest.get("input_signature")
        if existing_signature != signature and not rebuild_expanded_dictionary:
            raise ValueError(
                "The existing expanded node-pair dictionary was built from different "
                "inputs. Use a new output directory or --rebuild-expanded-dictionary."
            )
        if existing_signature == signature and not rebuild_expanded_dictionary:
            future_dictionary, crosswalk = _load_expanded_dictionary(expanded_output)
            expanded_action = "reused"
        else:
            expanded_action = "rebuilt"

    if expanded_action != "reused":
        crosswalk = build_transfer_crosswalk(
            base,
            future,
            dictionary,
            method=method,
            config=config,
            corridor_pair_lookup=corridor_pair_lookup,
            corridor_reference_lookup=corridor_reference_lookup,
        )

        link_type_lookup = get_vdf_dict("qvdf")
        future_dictionary = {}
        for index, transfer in crosswalk.iterrows():
            future_pair = (int(transfer["future_a"]), int(transfer["future_b"]))
            source = transfer["attribute_source"]
            if source == "BASE_CALIBRATION":
                base_pair = (int(transfer["base_a"]), int(transfer["base_b"]))
                values = dict(dictionary[base_pair])
                source_by_period = {
                    period: "BASE_CALIBRATION" for period in PERIOD_SEQUENCE
                }
            else:
                values, source_code = _fallback_values(
                    future.loc[index], link_type_lookup
                )
                crosswalk.at[index, "fallback_vdf_code"] = source_code
                source_by_period = {
                    period: "LINK_TYPE_FALLBACK" for period in PERIOD_SEQUENCE
                }
                if source == "CORRIDOR_CALIBRATION":
                    corridor = str(transfer["corridor_id"])
                    for period, parameters in corridor_dictionary[corridor].items():
                        sequence = PERIOD_SEQUENCE[period]
                        for parameter, value in parameters.items():
                            values[f"QVDF_{parameter}{sequence}"] = float(value)
                        source_by_period[period] = "CORRIDOR_CALIBRATION"
            for period, period_source in source_by_period.items():
                crosswalk.at[index, f"{period}_parameter_source"] = period_source
            future_dictionary[future_pair] = values

        if len(future_dictionary) != len(future):
            raise RuntimeError(
                "Future QVDF dictionary does not cover every future node pair"
            )
        _write_expanded_dictionary(expanded_output, crosswalk, future_dictionary)
        expanded_manifest_output.write_text(
            json.dumps(
                {
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "input_signature": signature,
                    "rows": len(future_dictionary),
                    "expanded_csv_sha256": _sha256(expanded_output),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    expected_pairs = set(zip(future["A"].astype(int), future["B"].astype(int)))
    if set(future_dictionary) != expected_pairs:
        raise ValueError(
            "Expanded node-pair dictionary does not match the future network"
        )

    np.save(dictionary_output, future_dictionary, allow_pickle=True)

    crosswalk_output = output_dir / "attribute_transfer_crosswalk.csv"
    crosswalk_to_write = crosswalk.copy()
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
    final_base_dictionary_sha256 = _sha256(dictionary_path)
    if final_base_dictionary_sha256 != base_dictionary_sha256:
        raise RuntimeError(
            "Base calibration dictionary changed during future-network transfer"
        )
    review_count = int(crosswalk["review_flag"].sum())
    summary = {
        "status": "PASS",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "base_network": str(base_path),
        "future_network": str(future_path),
        "calibration_dictionary": str(dictionary_path),
        "calibration_dictionary_sha256": base_dictionary_sha256,
        "base_dictionary_modified": False,
        "base_dictionary_policy": (
            "read-only input; expanded CSV and NPY are written only under output_dir"
        ),
        "base_links": int(len(base)),
        "calibrated_2025_pairs": int(len(dictionary)),
        "future_links": int(len(future)),
        "exact_2025_pairs": int(len(exact_pairs)),
        "missing_2025_pairs": int(sum(missing_mask)),
        "match_method_counts": counts,
        "review_links": review_count,
        "review_share_pct": round(100.0 * review_count / len(future), 4),
        "expanded_dictionary_action": expanded_action,
        "corridor_dictionary": corridor_metadata,
        "qvdf_fields_transferred": list(QVDF_FIELDS),
        "corridor_parameter_policy": (
            "Accepted sensor_uid=all/status=ok corridor fits replace n, s, cp, "
            "cd, alpha, and beta for available AM/MD/PM periods; PLF/QDF and "
            "unavailable periods retain the link-type fallback"
            if corridor_metadata
            else "No corridor source supplied"
        ),
        "non_qvdf_unmatched_policy": (
            "leave empty; this transfer dictionary contains only QVDF parameters"
        ),
        "configuration": config.__dict__,
        "outputs": {
            "future_dictionary": str(dictionary_output),
            "expanded_dictionary_csv": str(expanded_output),
            "expanded_dictionary_manifest": str(expanded_manifest_output),
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
