"""Command-line entry point for future-network QVDF transfer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .dtalite4cube.future_attribute_transfer import run_transfer
from .dtalite4cube.runner import DEFAULT_QVDF_OVERRIDE_DICTIONARY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Transfer calibrated 2025 QVDF parameters to a future network and "
            "write a complete audit crosswalk."
        )
    )
    parser.add_argument("--base-network", required=True, type=Path)
    parser.add_argument("--future-network", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--method",
        required=True,
        choices=("simple", "framework"),
        help=(
            "simple uses exact pairs plus link-type fallback; framework adds "
            "replacement-path and directed-geometry recovery."
        ),
    )
    parser.add_argument(
        "--calibration-dictionary",
        type=Path,
        default=DEFAULT_QVDF_OVERRIDE_DICTIONARY,
    )
    parser.add_argument(
        "--no-spatial-output",
        action="store_true",
        help="Skip the GeoPackage crosswalk.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_transfer(
        base_network_path=args.base_network,
        future_network_path=args.future_network,
        calibration_dictionary_path=args.calibration_dictionary,
        output_dir=args.output_dir,
        method=args.method,
        write_spatial=not args.no_spatial_output,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
