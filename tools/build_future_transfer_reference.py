"""Build the compact, read-only reference used by automatic future transfer.

This maintainer utility deliberately keeps only the network fields required by
the transfer matcher and removes workstation-specific paths from the accepted
corridor-fit cache before the resources are packaged.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd


NETWORK_COLUMNS = ["A", "B", "ATYPE", "FTYPE", "STREETNAME", "DISTANCE", "LINKID"]


def build_reference(
    base_network: Path,
    corridor_fit_cache: Path,
    corridor_pair_cache: Path,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    network = gpd.read_file(base_network)
    selected = [column for column in NETWORK_COLUMNS if column in network.columns]
    network = network[[*selected, "geometry"]].copy()
    network.to_file(
        output_dir / "base_network_reference.gpkg",
        layer="base_network",
        driver="GPKG",
    )

    fits = pd.read_csv(corridor_fit_cache, low_memory=False)
    if "source_catalog" in fits:
        fits["source_catalog"] = "packaged_corridor_reference"
    fits.to_csv(output_dir / "corridor_qvdf_dictionary.csv", index=False)

    pairs = pd.read_csv(corridor_pair_cache, low_memory=False)
    pairs.to_csv(output_dir / "corridor_node_pair_lookup.csv", index=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-network", required=True, type=Path)
    parser.add_argument("--corridor-fit-cache", required=True, type=Path)
    parser.add_argument("--corridor-pair-cache", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    build_reference(
        args.base_network,
        args.corridor_fit_cache,
        args.corridor_pair_cache,
        args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
