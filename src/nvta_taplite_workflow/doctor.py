"""Installed-environment and resource diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import platform
import struct
import sys
from importlib.resources import files
from pathlib import Path
from typing import Sequence


WORKFLOW_DISTRIBUTION = "nvta-taplite-workflow"
TAPLITE_DISTRIBUTION = "taplite4mpo-pre-release"
EXPECTED_TAPLITE_VERSION = "0.4.0rc1"
REQUIRED_IMPORTS = (
    "numpy",
    "pandas",
    "openmatrix",
    "tqdm",
    "geopandas",
    "shapely",
    "fiona",
    "pyproj",
    "psutil",
)


def _sha256(resource) -> str:
    digest = hashlib.sha256()
    with resource.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the installed NVTA workflow.")
    parser.add_argument(
        "--skip-native",
        action="store_true",
        help="Skip the Windows/TAPLite native-extension check (for package build CI only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    failures: list[str] = []

    def check(passed: bool, message: str) -> None:
        print(f"[{'OK' if passed else 'ERROR'}] {message}")
        if not passed:
            failures.append(message)

    try:
        workflow_version = importlib.metadata.version(WORKFLOW_DISTRIBUTION)
        check(True, f"{WORKFLOW_DISTRIBUTION} {workflow_version}")
    except importlib.metadata.PackageNotFoundError:
        check(False, f"{WORKFLOW_DISTRIBUTION} distribution metadata is unavailable")

    check(sys.version_info[:2] == (3, 11), f"Python {platform.python_version()}")
    check(struct.calcsize("P") * 8 == 64, "64-bit Python")

    for module_name in REQUIRED_IMPORTS:
        try:
            importlib.import_module(module_name)
            check(True, f"import {module_name}")
        except Exception as exc:
            check(False, f"import {module_name}: {type(exc).__name__}: {exc}")

    resource_root = files("nvta_taplite_workflow.dtalite4cube").joinpath("resources")
    dictionary = resource_root.joinpath(
        "qvdf_parameter_override_lookup", "qvdf_node_pair_overrides.npy"
    )
    manifest_resource = resource_root.joinpath(
        "qvdf_parameter_override_lookup", "source_manifest.json"
    )
    required_resources = (
        resource_root.joinpath("link_qvdf.csv"),
        dictionary,
        manifest_resource,
        resource_root.joinpath(
            "observed_link_speed_boundary_lookup", "observed_link_speed_boundaries.npy"
        ),
        resource_root.joinpath("observed_link_t2_lookup", "observed_link_t2.npy"),
    )
    for resource in required_resources:
        check(resource.is_file(), f"resource {resource.name}")

    if dictionary.is_file() and manifest_resource.is_file():
        manifest = json.loads(manifest_resource.read_text(encoding="utf-8"))
        expected_hash = str(manifest.get("output_sha256", "")).upper()
        actual_hash = _sha256(dictionary)
        check(bool(expected_hash) and actual_hash == expected_hash, f"QVDF dictionary SHA-256 {actual_hash}")

    if args.skip_native:
        print("[SKIP] native TAPLite verification")
    else:
        check(sys.platform == "win32", f"Windows runtime ({platform.system()})")
        try:
            installed_version = importlib.metadata.version(TAPLITE_DISTRIBUTION)
            native = importlib.import_module("pytaplite._native")
            status = native.openmp_status(2)
            check(
                installed_version == EXPECTED_TAPLITE_VERSION,
                f"{TAPLITE_DISTRIBUTION} {installed_version}",
            )
            check(
                bool(status.get("compiled")) and int(status.get("probe_team_size", 0)) >= 2,
                f"native TAPLite/OpenMP {status}",
            )
        except Exception as exc:
            check(False, f"native TAPLite: {type(exc).__name__}: {exc}")

    if failures:
        print(f"Doctor failed with {len(failures)} issue(s).")
        return 1
    print("Doctor passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

