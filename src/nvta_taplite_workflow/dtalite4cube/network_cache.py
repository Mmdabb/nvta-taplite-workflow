"""Fingerprint and cache prepared Cube network conversion inputs."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


CACHE_VERSION = 1
SHAPEFILE_COMPONENT_EXTENSIONS = {
    ".cpg",
    ".dbf",
    ".prj",
    ".qix",
    ".sbn",
    ".sbx",
    ".shp",
    ".shx",
}


@dataclass(frozen=True)
class NetworkCachePaths:
    root: Path
    manifest: Path
    payload: Path
    node_csv: Path


def default_network_cache_dir(shapefile_path: str | Path) -> Path:
    source = Path(shapefile_path).resolve()
    parent = source if source.is_dir() else source.parent
    return parent / ".dtalite_conversion_cache"


def _source_components(shapefile_path: str | Path) -> list[Path]:
    source = Path(shapefile_path).resolve()
    if source.is_file():
        candidates = source.parent.glob(f"{source.stem}.*")
    else:
        candidates = source.iterdir()
    return sorted(
        (
            path
            for path in candidates
            if path.is_file() and path.suffix.lower() in SHAPEFILE_COMPONENT_EXTENSIONS
        ),
        key=lambda path: path.name.lower(),
    )


def network_source_fingerprint(
    shapefile_path: str | Path,
    *,
    target_crs: str,
) -> tuple[str, list[dict[str, int | str]]]:
    components = _source_components(shapefile_path)
    if not components:
        raise FileNotFoundError(f"No shapefile components found in {shapefile_path}")

    digest = hashlib.sha256()
    digest.update(f"network-cache-v{CACHE_VERSION}\0{target_crs}\0".encode("utf-8"))
    metadata: list[dict[str, int | str]] = []
    for path in components:
        stat = path.stat()
        digest.update(path.name.lower().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while block := stream.read(1 << 20):
                digest.update(block)
        metadata.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return digest.hexdigest(), metadata


def cache_paths(cache_dir: str | Path) -> NetworkCachePaths:
    root = Path(cache_dir).resolve()
    return NetworkCachePaths(
        root=root,
        manifest=root / "manifest.json",
        payload=root / "prepared_network.pkl",
        node_csv=root / "node.csv",
    )


def load_network_cache(
    cache_dir: str | Path,
    *,
    expected_fingerprint: str,
) -> tuple[dict | None, Path | None]:
    paths = cache_paths(cache_dir)
    try:
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None

    if (
        manifest.get("cache_version") != CACHE_VERSION
        or manifest.get("fingerprint") != expected_fingerprint
        or not paths.payload.is_file()
        or not paths.node_csv.is_file()
    ):
        return None, None

    try:
        with paths.payload.open("rb") as stream:
            payload = pickle.load(stream)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
        return None, None
    return payload, paths.node_csv


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_network_cache(
    cache_dir: str | Path,
    *,
    fingerprint: str,
    source_files: list[dict[str, int | str]],
    target_crs: str,
    payload: dict,
    node_csv_source: str | Path,
) -> NetworkCachePaths:
    paths = cache_paths(cache_dir)
    paths.root.mkdir(parents=True, exist_ok=True)

    payload_handle, payload_temporary_name = tempfile.mkstemp(
        prefix=".prepared_network.",
        suffix=".tmp",
        dir=paths.root,
    )
    os.close(payload_handle)
    payload_temporary = Path(payload_temporary_name)
    try:
        with payload_temporary.open("wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(payload_temporary, paths.payload)
    finally:
        payload_temporary.unlink(missing_ok=True)

    _atomic_copy(Path(node_csv_source), paths.node_csv)

    manifest = {
        "cache_version": CACHE_VERSION,
        "fingerprint": fingerprint,
        "target_crs": target_crs,
        "source_files": source_files,
    }
    manifest_handle, manifest_temporary_name = tempfile.mkstemp(
        prefix=".manifest.",
        suffix=".tmp",
        dir=paths.root,
    )
    os.close(manifest_handle)
    manifest_temporary = Path(manifest_temporary_name)
    try:
        manifest_temporary.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_temporary, paths.manifest)
    finally:
        manifest_temporary.unlink(missing_ok=True)
    return paths
