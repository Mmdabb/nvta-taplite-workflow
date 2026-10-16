"""Apply scenario-local future-network speed-anchor and T overrides."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


PERIODS = ("am", "md", "pm")
TARGET_FIELDS = (
    "qvdf_start_speed_mph",
    "qvdf_end_speed_mph",
    "t0_hour",
    "t2_hour",
    "t3_hour",
)
T_FIELDS = frozenset({"t0_hour", "t2_hour", "t3_hour"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _replace_with_windows_retry(source: Path, target: Path) -> None:
    """Atomically replace a CSV despite short-lived scanner/Dropbox locks."""

    for attempt in range(60):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.25)


def _load_dictionary(path: Path) -> pd.DataFrame:
    payload = np.load(path, allow_pickle=True).item()
    if not isinstance(payload, dict):
        raise TypeError("Speed/T override resource must contain one dictionary")
    rows: list[dict[str, object]] = []
    expected = {
        f"{field}_{period}" for period in PERIODS for field in TARGET_FIELDS
    }
    for key, values in payload.items():
        if not isinstance(key, tuple) or len(key) != 2 or not isinstance(values, dict):
            raise ValueError("Invalid speed/T node-pair dictionary entry")
        missing = expected.difference(values)
        if missing:
            raise ValueError(
                f"Speed/T entry {key} is missing: " + ", ".join(sorted(missing))
            )
        rows.append(
            {
                "from_node_id": int(key[0]),
                "to_node_id": int(key[1]),
                **{field: float(values[field]) for field in expected},
            }
        )
    frame = pd.DataFrame(rows)
    if frame.duplicated(["from_node_id", "to_node_id"]).any():
        raise ValueError("Speed/T override dictionary contains duplicate node pairs")
    return frame


def _selected_periods(
    periods: Iterable[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    requested = tuple(
        dict.fromkeys(str(period).strip().lower() for period in (periods or PERIODS))
    )
    selected = tuple(period for period in requested if period in PERIODS)
    skipped = tuple(period for period in requested if period not in PERIODS)
    return selected, skipped


def _closed_mask(frame: pd.DataFrame) -> pd.Series:
    if "allowed_use" not in frame:
        return pd.Series(False, index=frame.index)
    return (
        frame["allowed_use"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.casefold()
        .eq("closed")
    )


def apply_boundary_overrides(
    assignment_root: Path,
    dictionary_path: Path,
    backup_root: Path,
    *,
    periods: Iterable[str] | None = None,
) -> dict[str, object]:
    """Overlay future speed/T lineage and intentionally blank unsupported links."""

    assignment_root = Path(assignment_root).resolve()
    dictionary_path = Path(dictionary_path).resolve()
    backup_root = Path(backup_root).resolve()
    if not dictionary_path.is_file():
        raise FileNotFoundError(dictionary_path)
    if backup_root.exists():
        raise FileExistsError(
            f"Speed/T pre-overlay backup already exists: {backup_root}"
        )

    lookup = _load_dictionary(dictionary_path)
    selected_periods, skipped_periods = _selected_periods(periods)
    results: list[dict[str, object]] = []
    for period in selected_periods:
        link_path = assignment_root / period / "link.csv"
        if not link_path.is_file():
            raise FileNotFoundError(link_path)
        frame = pd.read_csv(link_path, low_memory=False)
        required = {"from_node_id", "to_node_id", *TARGET_FIELDS}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{link_path} is missing: {', '.join(sorted(missing))}")

        source_columns = {
            f"{target}_{period}": target for target in TARGET_FIELDS
        }
        selected = lookup[["from_node_id", "to_node_id", *source_columns]].rename(
            columns=source_columns
        )
        merged = frame.merge(
            selected,
            on=["from_node_id", "to_node_id"],
            how="left",
            validate="many_to_one",
            suffixes=("", "__override"),
            indicator=True,
        )
        unmatched = int(merged["_merge"].ne("both").sum())
        if unmatched:
            raise ValueError(
                f"{period.upper()} converted link.csv has {unmatched:,} node pairs "
                "missing from the speed/T override dictionary"
            )

        closed = _closed_mask(merged)
        assigned_counts: dict[str, int] = {}
        changed_cells = 0
        for target in TARGET_FIELDS:
            override = pd.to_numeric(
                merged[f"{target}__override"], errors="coerce"
            )
            assign = np.isfinite(override)
            if target in T_FIELDS:
                assign &= ~closed
            original = pd.to_numeric(merged[target], errors="coerce")
            changed_cells += int(
                (
                    assign
                    & (~np.isclose(original, override, equal_nan=True))
                ).sum()
            )
            changed_cells += int((~assign & original.notna()).sum())
            merged[target] = override.where(assign, "")
            assigned_counts[target] = int(assign.sum())

        merged = merged[frame.columns]
        period_backup = backup_root / period
        period_backup.mkdir(parents=True, exist_ok=False)
        backup_path = period_backup / "link.csv"
        shutil.copy2(link_path, backup_path)
        before_hash = _sha256(link_path)
        temporary_path = link_path.with_suffix(".csv.speed-t-overlay.tmp")
        try:
            merged.to_csv(temporary_path, index=False)
            _replace_with_windows_retry(temporary_path, link_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        results.append(
            {
                "period": period.upper(),
                "links": int(len(merged)),
                "changed_cells": changed_cells,
                "assigned_fields": assigned_counts,
                "closed_links": int(closed.sum()),
                "link_csv": str(link_path),
                "pre_overlay_backup": str(backup_path),
                "sha256_before": before_hash,
                "sha256_after": _sha256(link_path),
            }
        )

    manifest = {
        "status": "PASS",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "assignment_root": str(assignment_root),
        "dictionary_path": str(dictionary_path),
        "dictionary_sha256": _sha256(dictionary_path),
        "dictionary_node_pairs": int(len(lookup)),
        "selected_periods": [period.upper() for period in selected_periods],
        "skipped_periods": [period.upper() for period in skipped_periods],
        "skipped_period_policy": "retain normal network-converter values",
        "blank_policy": (
            "blank means no accepted link-level lineage; TAPlite uses its "
            "analytical/free-speed behavior"
        ),
        "periods": results,
    }
    manifest_path = backup_root.parent / "speed_t_override_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def validate_boundary_overrides(
    assignment_root: Path,
    dictionary_path: Path,
    *,
    periods: Iterable[str] | None = None,
) -> dict[str, object]:
    """Verify converted link files against a future speed/T dictionary."""

    assignment_root = Path(assignment_root).resolve()
    dictionary_path = Path(dictionary_path).resolve()
    lookup = _load_dictionary(dictionary_path)
    selected_periods, skipped_periods = _selected_periods(periods)
    results: list[dict[str, object]] = []
    for period in selected_periods:
        link_path = assignment_root / period / "link.csv"
        frame = pd.read_csv(link_path, low_memory=False)
        source_columns = {
            f"{target}_{period}": target for target in TARGET_FIELDS
        }
        selected = lookup[["from_node_id", "to_node_id", *source_columns]].rename(
            columns={source: f"expected_{target}" for source, target in source_columns.items()}
        )
        merged = frame.merge(
            selected,
            on=["from_node_id", "to_node_id"],
            how="left",
            validate="many_to_one",
            indicator=True,
        )
        if not merged["_merge"].eq("both").all():
            raise ValueError(
                f"{period.upper()} has links absent from speed/T dictionary"
            )
        closed = _closed_mask(merged)
        maximum_difference: dict[str, float] = {}
        for target in TARGET_FIELDS:
            expected = pd.to_numeric(
                merged[f"expected_{target}"], errors="coerce"
            )
            if target in T_FIELDS:
                expected = expected.mask(closed)
            actual = pd.to_numeric(merged[target], errors="coerce")
            if not actual.isna().equals(expected.isna()):
                raise ValueError(
                    f"{period.upper()} {target} blank/assigned mask differs"
                )
            finite = actual.notna()
            difference = (actual[finite] - expected[finite]).abs()
            maximum = float(difference.max()) if len(difference) else 0.0
            if maximum > 1e-9:
                raise ValueError(
                    f"{period.upper()} {target} differs by as much as {maximum}"
                )
            maximum_difference[target] = maximum
        results.append(
            {
                "period": period.upper(),
                "links": int(len(frame)),
                "maximum_absolute_difference": maximum_difference,
            }
        )
    return {
        "status": "PASS",
        "dictionary": str(dictionary_path),
        "selected_periods": [period.upper() for period in selected_periods],
        "skipped_periods": [period.upper() for period in skipped_periods],
        "periods": results,
    }


__all__ = [
    "PERIODS",
    "TARGET_FIELDS",
    "apply_boundary_overrides",
    "validate_boundary_overrides",
]
