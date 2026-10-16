"""Automatic, scenario-local calibration transfer for network conversion runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .future_attribute_transfer import (
    BoundaryResourcePaths,
    FrameworkConfig,
    _load_network,
    _network_fingerprint,
    _sha256,
    run_transfer,
)


logger = logging.getLogger(__name__)

TRANSFER_ROOT_NAME = "NVTA_Attribute_Transfer"
TRANSFER_SETS_NAME = "sets"
ACTIVE_TRANSFER_NAME = "active_transfer.json"
REFERENCE_ROOT = Path(__file__).resolve().parent / "resources" / "future_transfer_reference"
BASE_NETWORK_REFERENCE = REFERENCE_ROOT / "base_network_reference.gpkg"
CORRIDOR_REFERENCE_FILES = (
    "corridor_qvdf_dictionary.csv",
    "corridor_node_pair_lookup.csv",
)
QVDF_DICTIONARY_NAME = "future_qvdf_node_pair_overrides.npy"
SPEED_T_DICTIONARY_NAME = "future_speed_t_overrides.npy"


def _complete_network_fingerprint(frame: gpd.GeoDataFrame) -> str:
    """Hash every source attribute and geometry so any network edit is detected."""

    columns = sorted(column for column in frame.columns if column != frame.geometry.name)
    digest = hashlib.sha256(str(frame.crs).encode("utf-8"))
    digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
    hashes = pd.util.hash_pandas_object(frame[columns], index=True)
    digest.update(hashes.to_numpy(dtype="uint64").tobytes())
    for geometry in frame.geometry:
        digest.update(geometry.wkb)
    return digest.hexdigest().upper()


def _reference_signature(
    *,
    base_network_path: Path,
    calibration_dictionary_path: Path,
    corridor_reference_dir: Path,
    boundary_resources: BoundaryResourcePaths,
    framework_config: FrameworkConfig,
) -> dict[str, object]:
    return {
        "base_network_sha256": _sha256(base_network_path),
        "calibration_dictionary_sha256": _sha256(calibration_dictionary_path),
        "corridor_reference_sha256": {
            name: _sha256(corridor_reference_dir / name)
            for name in CORRIDOR_REFERENCE_FILES
        },
        "boundary_resource_sha256": {
            name: _sha256(path)
            for name, path in boundary_resources.resolved_files().items()
        },
        "framework_configuration": framework_config.__dict__,
    }


def _transfer_set_key(
    source_network_fingerprint: str,
    source_network_folder: Path,
    reference_signature: dict[str, object],
) -> str:
    payload = {
        "schema_version": 2,
        "source_network_fingerprint": source_network_fingerprint,
        "source_network_folder": os.path.normcase(
            str(source_network_folder.resolve())
        ),
        "reference_signature": reference_signature,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _seed_corridor_reference(set_dir: Path, reference_dir: Path) -> None:
    for name in CORRIDOR_REFERENCE_FILES:
        source = reference_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"Packaged corridor reference is missing: {source}")
        target = set_dir / name
        if not target.exists():
            shutil.copy2(source, target)


def _write_active_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def _require_folder_local_dictionary(path: Path, transfer_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(transfer_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            "Automatic transfer dictionary resolved outside the selected "
            f"shapefile folder: {resolved}"
        ) from exc
    return resolved


def ensure_automatic_future_transfer(
    network_path: Path,
    calibration_dictionary_path: Path,
    *,
    base_network_path: Path = BASE_NETWORK_REFERENCE,
    corridor_reference_dir: Path = REFERENCE_ROOT,
    boundary_resources: BoundaryResourcePaths | None = None,
    framework_config: FrameworkConfig | None = None,
    write_spatial: bool = True,
) -> dict[str, object]:
    """Create or reuse the dictionary set matching the current source network.

    The cache lives beside the selected network shapefile. The resolved source
    folder is part of the set identity, so moving an unchanged shapefile to a
    different folder is a first-time run there. A different source fingerprint,
    folder, or packaged reference bundle produces a different immutable set.
    """

    base_network_path = Path(base_network_path).resolve()
    corridor_reference_dir = Path(corridor_reference_dir).resolve()
    calibration_dictionary_path = Path(calibration_dictionary_path).resolve()
    boundary_resources = boundary_resources or BoundaryResourcePaths()
    framework_config = framework_config or FrameworkConfig()

    for required in (
        base_network_path,
        calibration_dictionary_path,
        *(corridor_reference_dir / name for name in CORRIDOR_REFERENCE_FILES),
    ):
        if not required.is_file():
            raise FileNotFoundError(f"Automatic transfer reference is missing: {required}")

    future_network_file, future = _load_network(network_path)
    source_network_folder = future_network_file.parent.resolve()
    source_fingerprint = _complete_network_fingerprint(future)
    transfer_fingerprint = _network_fingerprint(future)
    reference_signature = _reference_signature(
        base_network_path=base_network_path,
        calibration_dictionary_path=calibration_dictionary_path,
        corridor_reference_dir=corridor_reference_dir,
        boundary_resources=boundary_resources,
        framework_config=framework_config,
    )
    set_key = _transfer_set_key(
        source_fingerprint,
        source_network_folder,
        reference_signature,
    )

    transfer_root = source_network_folder / TRANSFER_ROOT_NAME
    set_dir = transfer_root / TRANSFER_SETS_NAME / f"network-{set_key[:20].lower()}"
    set_dir.mkdir(parents=True, exist_ok=True)
    _seed_corridor_reference(set_dir, corridor_reference_dir)

    logger.info(
        "Checking automatic calibration transfer for %s (fingerprint %s)",
        future_network_file,
        source_fingerprint[:12],
    )
    summary = run_transfer(
        base_network_path=base_network_path,
        future_network_path=future_network_file,
        calibration_dictionary_path=calibration_dictionary_path,
        output_dir=set_dir,
        method="framework",
        config=framework_config,
        write_spatial=write_spatial,
        corridor_root=corridor_reference_dir,
        boundary_resources=boundary_resources,
    )

    qvdf_dictionary = _require_folder_local_dictionary(
        set_dir / QVDF_DICTIONARY_NAME,
        transfer_root,
    )
    speed_t_dictionary = _require_folder_local_dictionary(
        set_dir / SPEED_T_DICTIONARY_NAME,
        transfer_root,
    )
    if not qvdf_dictionary.is_file() or not speed_t_dictionary.is_file():
        raise RuntimeError("Automatic transfer did not create both assignment dictionaries")

    transfer_root.mkdir(parents=True, exist_ok=True)
    active_manifest = transfer_root / ACTIVE_TRANSFER_NAME
    active_payload = {
        "schema_version": 2,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "network_file": str(future_network_file),
        "source_network_folder": str(source_network_folder),
        "source_network_fingerprint": source_fingerprint,
        "transfer_network_fingerprint": transfer_fingerprint,
        "transfer_set": str(set_dir),
        "qvdf_dictionary": str(qvdf_dictionary),
        "speed_t_dictionary": str(speed_t_dictionary),
        "qvdf_cache_action": summary["expanded_dictionary_action"],
        "speed_t_cache_action": summary["expanded_boundary_dictionary_action"],
    }
    _write_active_manifest(active_manifest, active_payload)

    logger.info(
        "Automatic calibration transfer ready: set=%s qvdf=%s speed_t=%s",
        set_dir.name,
        summary["expanded_dictionary_action"],
        summary["expanded_boundary_dictionary_action"],
    )
    return {
        **active_payload,
        "transfer_root": str(transfer_root),
        "active_manifest": str(active_manifest),
        "summary": summary,
    }


__all__ = [
    "ACTIVE_TRANSFER_NAME",
    "BASE_NETWORK_REFERENCE",
    "REFERENCE_ROOT",
    "TRANSFER_ROOT_NAME",
    "ensure_automatic_future_transfer",
]
