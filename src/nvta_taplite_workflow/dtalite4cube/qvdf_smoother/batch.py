"""Batch QVDF profile construction and atomic scenario write-back.

The batch path stages compact per-link inputs once, computes each link across
all selected periods in a process pool, and then streams each wide
``link_performance.csv`` exactly once while replacing only ``spd_mph_*``
columns.  The period-level ``speed_mph`` field and every other logical CSV
value are preserved.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .profile import (
    ConnectorLimits,
    QvdfInputs,
    _constraint_metrics,
    build_enforced_period_profiles,
    inputs_from_rows,
)


LINK_INPUT_COLUMNS = {
    "link_id",
    "lanes",
    "capacity",
    "vdf_plf",
    "vdf_free_speed_mph",
    "free_speed",
    "vdf_length_mi",
    "length_in_mile",
    "cutoff_speed",
    "vdf_alpha",
    "vdf_beta",
    "vdf_cp",
    "vdf_cd",
    "vdf_n",
    "vdf_s",
    "vdf_type",
    "qvdf_profile_mode",
    "t0_hour",
    "t2_hour",
    "t3_hour",
    "qvdf_start_speed_mph",
    "qvdf_end_speed_mph",
}

MAX_WORKERS = 20


@dataclass(frozen=True)
class PeriodSpec:
    name: str
    directory: Path
    start_hour: float
    end_hour: float
    speed_columns: tuple[str, ...]
    minutes: tuple[int, ...]


class BatchError(RuntimeError):
    pass


def _read_first_csv_row(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        try:
            return next(csv.DictReader(handle))
        except StopIteration as exc:
            raise BatchError(f"CSV has no data row: {path}") from exc


def _speed_minute(column: str) -> int:
    hour, minute = column.removeprefix("spd_mph_").split(":", 1)
    return int(hour) * 60 + int(minute)


def discover_periods(scenario_dir: Path, names: Iterable[str]) -> list[PeriodSpec]:
    scenario_dir = scenario_dir.resolve()
    if not scenario_dir.is_dir():
        raise BatchError(f"scenario directory does not exist: {scenario_dir}")

    requested = list(dict.fromkeys(name.lower() for name in names))
    if not requested:
        raise BatchError("at least one period folder is required")

    result: list[PeriodSpec] = []
    for name in requested:
        directory = scenario_dir / name
        required = [
            directory / "settings.csv",
            directory / "link.csv",
            directory / "link_performance.csv",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise BatchError(f"period {name} is missing required files: {missing}")

        settings = _read_first_csv_row(directory / "settings.csv")
        start_hour = float(settings["demand_period_starting_hours"])
        end_hour = float(settings["demand_period_ending_hours"])
        with (directory / "link_performance.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            fieldnames = csv.DictReader(handle).fieldnames or []
        speed_columns = tuple(
            column for column in fieldnames if column and column.startswith("spd_mph_")
        )
        minutes = tuple(_speed_minute(column) for column in speed_columns)
        expected = tuple(
            range(round(start_hour * 60), round(end_hour * 60), 5)
        )
        if minutes != expected:
            raise BatchError(
                f"{name} speed columns do not match settings: "
                f"expected {len(expected)}, found {len(minutes)}"
            )
        result.append(
            PeriodSpec(
                name=name,
                directory=directory,
                start_hour=start_hour,
                end_hour=end_hour,
                speed_columns=speed_columns,
                minutes=minutes,
            )
        )

    result.sort(key=lambda item: item.start_hour)
    for left, right in zip(result, result[1:]):
        if abs(left.end_hour - right.start_hour) > 1e-9:
            raise BatchError(
                f"selected periods must be adjacent for carried anchors: "
                f"{left.name} ends {left.end_hour}, {right.name} starts {right.start_hour}"
            )
    return result


def choose_worker_count(requested: str) -> tuple[int, dict[str, object]]:
    logical = os.cpu_count() or 1
    details: dict[str, object] = {
        "requested": requested,
        "logical_cpus": logical,
        "mode": "manual" if requested.lower() != "auto" else "auto",
    }
    if requested.lower() != "auto":
        try:
            workers = int(requested)
        except ValueError as exc:
            raise BatchError("--workers must be 'auto' or a positive integer") from exc
        if workers < 1:
            raise BatchError("--workers must be positive")
        workers = min(workers, logical, MAX_WORKERS)
        details["selected"] = workers
        return workers, details

    try:
        import psutil  # type: ignore

        utilization = psutil.cpu_percent(interval=1.0, percpu=True)
        idle_equivalents = math.floor(
            sum(max(0.0, 100.0 - value) / 100.0 for value in utilization)
        )
        workers = max(1, math.floor(idle_equivalents * 0.5))
        workers = min(workers, logical, MAX_WORKERS)
        details.update(
            {
                "per_cpu_utilization_percent": utilization,
                "idle_core_equivalents": idle_equivalents,
                "allocation_fraction": 0.5,
                "selected": workers,
            }
        )
        return workers, details
    except Exception as exc:
        details.update(
            {
                "selected": 1,
                "fallback_reason": f"automatic free-core detection unavailable: {exc}",
            }
        )
        return 1, details


def _open_stage(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=120.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _create_stage_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE inputs (
            period TEXT NOT NULL,
            link_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (period, link_id)
        ) WITHOUT ROWID;
        CREATE INDEX inputs_link_id_idx ON inputs(link_id);
        CREATE TABLE results (
            period TEXT NOT NULL,
            link_id TEXT NOT NULL,
            speeds BLOB NOT NULL,
            PRIMARY KEY (period, link_id)
        ) WITHOUT ROWID;
        CREATE TABLE errors (
            link_id TEXT PRIMARY KEY,
            message TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def _compact_link_rows(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "link_id" not in (reader.fieldnames or []):
            raise BatchError(f"link_id is missing from {path}")
        for row in reader:
            link_id = row.get("link_id", "")
            if not link_id:
                raise BatchError(f"blank link_id in {path}")
            if link_id in result:
                raise BatchError(f"duplicate link_id={link_id} in {path}")
            result[link_id] = {
                name: row.get(name, "") for name in LINK_INPUT_COLUMNS
            }
    return result


def stage_inputs(
    connection: sqlite3.Connection, specs: list[PeriodSpec]
) -> dict[str, object]:
    started = time.perf_counter()
    periods: dict[str, object] = {}
    total_rows = 0
    total_bytes = 0
    for spec in specs:
        link_path = spec.directory / "link.csv"
        performance_path = spec.directory / "link_performance.csv"
        settings = _read_first_csv_row(spec.directory / "settings.csv")
        links = _compact_link_rows(link_path)
        rows = 0
        inserts: list[tuple[str, str, str]] = []
        with performance_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"link_id", "volume", "speed_mph"}
            missing = sorted(required - set(reader.fieldnames or []))
            if missing:
                raise BatchError(f"{performance_path} is missing {missing}")
            for performance in reader:
                link_id = performance.get("link_id", "")
                link = links.get(link_id)
                if link is None:
                    raise BatchError(
                        f"{spec.name} link_performance link_id={link_id} "
                        "is absent from link.csv"
                    )
                compact_performance = {
                    "link_id": link_id,
                    "volume": performance.get("volume", ""),
                    "speed_mph": performance.get("speed_mph", ""),
                }
                inputs = inputs_from_rows(
                    spec.name, link, compact_performance, settings
                )
                inserts.append(
                    (
                        spec.name,
                        link_id,
                        json.dumps(asdict(inputs), separators=(",", ":")),
                    )
                )
                rows += 1
                if len(inserts) >= 1000:
                    try:
                        connection.executemany(
                            "INSERT INTO inputs(period, link_id, payload) VALUES (?, ?, ?)",
                            inserts,
                        )
                    except sqlite3.IntegrityError as exc:
                        raise BatchError(
                            f"duplicate link_id in {performance_path}"
                        ) from exc
                    inserts.clear()
        if inserts:
            connection.executemany(
                "INSERT INTO inputs(period, link_id, payload) VALUES (?, ?, ?)",
                inserts,
            )
        connection.commit()
        periods[spec.name] = {
            "performance_rows": rows,
            "link_rows": len(links),
            "link_csv_bytes": link_path.stat().st_size,
            "link_performance_csv_bytes": performance_path.stat().st_size,
        }
        total_rows += rows
        total_bytes += link_path.stat().st_size + performance_path.stat().st_size
        del links
        print(f"staged period={spec.name} rows={rows}", flush=True)

    unique_links = connection.execute(
        "SELECT COUNT(DISTINCT link_id) FROM inputs"
    ).fetchone()[0]
    return {
        "wall_seconds": time.perf_counter() - started,
        "periods": periods,
        "total_period_rows": total_rows,
        "unique_links": unique_links,
        "input_bytes_scanned": total_bytes,
    }


def _contiguous_runs(
    ordered: list[tuple[int, QvdfInputs]],
) -> list[list[QvdfInputs]]:
    runs: list[list[QvdfInputs]] = []
    prior_index: int | None = None
    for index, inputs in ordered:
        if prior_index is None or index != prior_index + 1:
            runs.append([])
        runs[-1].append(inputs)
        prior_index = index
    return runs


def _process_chunk(task: dict[str, object]) -> dict[str, object]:
    cpu_started = time.process_time()
    db_path = str(task["db_path"])
    link_ids = [str(value) for value in task["link_ids"]]
    period_order = [str(value) for value in task["period_order"]]
    period_minutes = {
        str(key): [int(value) for value in values]
        for key, values in dict(task["period_minutes"]).items()
    }
    limits = ConnectorLimits(**dict(task["limits"]))
    speed_decimals = int(task["speed_decimals"])
    placeholders = ",".join("?" for _ in link_ids)
    # Use an ordinary filesystem path. SQLite URI syntax interprets the server
    # component of a UNC path as a URI authority and rejects it on Windows.
    connection = sqlite3.connect(db_path, timeout=120.0)
    rows = connection.execute(
        f"SELECT link_id, period, payload FROM inputs WHERE link_id IN ({placeholders})",
        link_ids,
    ).fetchall()
    connection.close()

    grouped: dict[str, dict[str, QvdfInputs]] = {link_id: {} for link_id in link_ids}
    for link_id, period, payload in rows:
        grouped[link_id][period] = QvdfInputs(**json.loads(payload))

    results: list[dict[str, object]] = []
    errors: list[tuple[str, str]] = []
    for link_id in link_ids:
        try:
            present = grouped[link_id]
            ordered = [
                (index, present[period])
                for index, period in enumerate(period_order)
                if period in present
            ]
            if not ordered:
                raise BatchError("no staged period input")

            all_profiles: dict[str, list[float]] = {}
            adjusted_samples = 0
            smoothed_boundaries = 0
            metrics = {
                "max_five_minute_change_mph": 0.0,
                "max_rolling_average_abs_change_mph": 0.0,
                "max_acceleration_mph_per_hour2": 0.0,
            }
            serialized_metrics = {
                "max_five_minute_change_mph": 0.0,
                "max_rolling_average_abs_change_mph": 0.0,
                "max_acceleration_mph_per_hour2": 0.0,
            }
            for run in _contiguous_runs(ordered):
                profiles, diagnostics = build_enforced_period_profiles(run, limits)
                serialized_run: list[float] = []
                for period, profile in profiles.items():
                    actual_minutes = [minute for minute, _ in profile]
                    if actual_minutes != period_minutes[period]:
                        raise BatchError(
                            f"{period} generated {len(actual_minutes)} timestamps; "
                            f"expected {len(period_minutes[period])}"
                        )
                    all_profiles[period] = [speed for _, speed in profile]
                    serialized_run.extend(
                        float(f"{speed:.{speed_decimals}f}") for _, speed in profile
                    )
                adjusted_samples += sum(
                    int(summary["adjusted_samples"])
                    for summary in diagnostics["rate_limiter"].values()
                )
                smoothed_boundaries += len(diagnostics["boundary_smoother"])
                for name, value in diagnostics["final_constraint_metrics"].items():
                    metrics[name] = max(metrics[name], float(value))
                for name, value in _constraint_metrics(
                    serialized_run, limits
                ).items():
                    serialized_metrics[name] = max(
                        serialized_metrics[name], float(value)
                    )

            violation = (
                metrics["max_five_minute_change_mph"]
                > limits.max_five_minute_change_mph + 1e-8
                or metrics["max_rolling_average_abs_change_mph"]
                > limits.max_rolling_average_change_mph + 1e-8
                or metrics["max_acceleration_mph_per_hour2"]
                > limits.max_acceleration_mph_per_hour2 + 1e-6
            )
            serialized_violation = (
                serialized_metrics["max_five_minute_change_mph"]
                > limits.max_five_minute_change_mph + 1e-8
                or serialized_metrics["max_rolling_average_abs_change_mph"]
                > limits.max_rolling_average_change_mph + 1e-8
                or serialized_metrics["max_acceleration_mph_per_hour2"]
                > limits.max_acceleration_mph_per_hour2 + 1e-6
            )
            results.append(
                {
                    "link_id": link_id,
                    "profiles": all_profiles,
                    "adjusted_samples": adjusted_samples,
                    "smoothed_boundaries": smoothed_boundaries,
                    "metrics": metrics,
                    "constraint_violation": violation,
                    "serialized_metrics": serialized_metrics,
                    "serialized_constraint_violation": serialized_violation,
                }
            )
        except Exception as exc:
            errors.append((link_id, f"{type(exc).__name__}: {exc}"))

    return {
        "results": results,
        "errors": errors,
        "worker_cpu_seconds": time.process_time() - cpu_started,
    }


def _rss_with_children() -> int | None:
    try:
        import psutil  # type: ignore

        process = psutil.Process()
        return process.memory_info().rss + sum(
            child.memory_info().rss
            for child in process.children(recursive=True)
            if child.is_running()
        )
    except Exception:
        return None


def _store_chunk(
    connection: sqlite3.Connection,
    outcome: dict[str, object],
    aggregate: dict[str, object],
) -> None:
    inserts: list[tuple[str, str, bytes]] = []
    for result in outcome["results"]:
        link_id = str(result["link_id"])
        for period, speeds in result["profiles"].items():
            values = [float(value) for value in speeds]
            inserts.append(
                (
                    str(period),
                    link_id,
                    struct.pack(f"<{len(values)}d", *values),
                )
            )
        aggregate["processed_links"] += 1
        aggregate["period_profiles"] += len(result["profiles"])
        aggregate["rate_limiter_adjusted_samples"] += int(
            result["adjusted_samples"]
        )
        aggregate["smoothed_boundaries"] += int(result["smoothed_boundaries"])
        aggregate["constraint_violations"] += int(result["constraint_violation"])
        aggregate["serialized_constraint_violations"] += int(
            result["serialized_constraint_violation"]
        )
        for name, value in result["metrics"].items():
            aggregate["max_metrics"][name] = max(
                aggregate["max_metrics"][name], float(value)
            )
        for name, value in result["serialized_metrics"].items():
            aggregate["serialized_max_metrics"][name] = max(
                aggregate["serialized_max_metrics"][name], float(value)
            )
    if inserts:
        connection.executemany(
            "INSERT INTO results(period, link_id, speeds) VALUES (?, ?, ?)",
            inserts,
        )
    if outcome["errors"]:
        connection.executemany(
            "INSERT OR REPLACE INTO errors(link_id, message) VALUES (?, ?)",
            outcome["errors"],
        )
        aggregate["failed_links"] += len(outcome["errors"])
    aggregate["worker_cpu_seconds"] += float(outcome["worker_cpu_seconds"])


def compute_profiles(
    connection: sqlite3.Connection,
    db_path: Path,
    specs: list[PeriodSpec],
    limits: ConnectorLimits,
    workers: int,
    worker_details: dict[str, object],
    chunk_size: int,
    speed_decimals: int,
) -> dict[str, object]:
    link_ids = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT link_id FROM inputs ORDER BY link_id"
        )
    ]
    if not link_ids:
        raise BatchError("no links were staged")
    if chunk_size <= 0:
        chunk_size = min(
            900,
            max(50, math.ceil(len(link_ids) / max(1, workers * 8))),
        )
    if chunk_size > 900:
        raise BatchError("--chunk-size must be 900 or less")

    base_task = {
        "db_path": str(db_path),
        "period_order": [spec.name for spec in specs],
        "period_minutes": {spec.name: list(spec.minutes) for spec in specs},
        "limits": asdict(limits),
        "speed_decimals": speed_decimals,
    }
    tasks = [
        {**base_task, "link_ids": link_ids[index : index + chunk_size]}
        for index in range(0, len(link_ids), chunk_size)
    ]

    def new_aggregate() -> dict[str, object]:
        return {
            "processed_links": 0,
            "failed_links": 0,
            "period_profiles": 0,
            "rate_limiter_adjusted_samples": 0,
            "smoothed_boundaries": 0,
            "constraint_violations": 0,
            "serialized_constraint_violations": 0,
            "worker_cpu_seconds": 0.0,
            "max_metrics": {
                "max_five_minute_change_mph": 0.0,
                "max_rolling_average_abs_change_mph": 0.0,
                "max_acceleration_mph_per_hour2": 0.0,
            },
            "serialized_max_metrics": {
                "max_five_minute_change_mph": 0.0,
                "max_rolling_average_abs_change_mph": 0.0,
                "max_acceleration_mph_per_hour2": 0.0,
            },
        }

    def reset_output() -> None:
        connection.execute("DELETE FROM results")
        connection.execute("DELETE FROM errors")
        connection.commit()

    def consume(outcomes: Iterable[dict[str, object]]) -> dict[str, object]:
        aggregate = new_aggregate()
        peak_rss = _rss_with_children() or 0
        next_progress = 5000
        for outcome in outcomes:
            _store_chunk(connection, outcome, aggregate)
            connection.commit()
            current_rss = _rss_with_children()
            if current_rss is not None:
                peak_rss = max(peak_rss, current_rss)
            processed = aggregate["processed_links"] + aggregate["failed_links"]
            if processed >= next_progress or processed == len(link_ids):
                print(
                    f"computed links={processed}/{len(link_ids)} "
                    f"failed={aggregate['failed_links']}",
                    flush=True,
                )
                next_progress += 5000
        aggregate["peak_parent_and_children_rss_bytes"] = peak_rss or None
        return aggregate

    reset_output()
    started = time.perf_counter()
    used_workers = workers
    fallback_reason: str | None = None
    try:
        if workers == 1:
            aggregate = consume(map(_process_chunk, tasks))
        else:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                aggregate = consume(executor.map(_process_chunk, tasks, chunksize=1))
    except Exception as exc:
        if workers == 1:
            raise
        fallback_reason = f"multiprocessing unavailable at runtime: {type(exc).__name__}: {exc}"
        print(f"warning: {fallback_reason}; retrying with one process", flush=True)
        reset_output()
        used_workers = 1
        aggregate = consume(map(_process_chunk, tasks))

    wall_seconds = time.perf_counter() - started
    worker_details = dict(worker_details)
    worker_details["used"] = used_workers
    if fallback_reason:
        worker_details["fallback_reason"] = fallback_reason
    aggregate.update(
        {
            "wall_seconds": wall_seconds,
            "links_per_second": len(link_ids) / wall_seconds,
            "chunk_size": chunk_size,
            "chunks": len(tasks),
            "workers": worker_details,
            "parallel_cpu_utilization_percent": (
                100.0
                * aggregate["worker_cpu_seconds"]
                / max(1e-9, wall_seconds * used_workers)
            ),
        }
    )
    return aggregate


def _logical_row_hash_update(
    digest: "hashlib._Hash", row: dict[str | None, str | list[str] | None], fields: list[str]
) -> None:
    values = [row.get(field, "") for field in fields]
    digest.update(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    digest.update(b"\n")


def _unpack_speeds(blob: bytes, count: int) -> tuple[float, ...]:
    if len(blob) != count * 8:
        raise BatchError(
            f"profile blob has {len(blob)} bytes; expected {count * 8}"
        )
    return struct.unpack(f"<{count}d", blob)


def _write_period_temp(
    connection: sqlite3.Connection,
    spec: PeriodSpec,
    run_id: str,
    speed_decimals: int,
) -> dict[str, object]:
    source = spec.directory / "link_performance.csv"
    destination = spec.directory / f".link_performance.{run_id}.tmp.csv"
    profiles = {
        link_id: blob
        for link_id, blob in connection.execute(
            "SELECT link_id, speeds FROM results WHERE period = ?", (spec.name,)
        )
    }
    before_hash = hashlib.sha256()
    rows = 0
    used_profiles: set[str] = set()
    with source.open("r", encoding="utf-8-sig", newline="") as input_handle:
        reader = csv.DictReader(input_handle)
        fieldnames = reader.fieldnames or []
        if tuple(
            name for name in fieldnames if name and name.startswith("spd_mph_")
        ) != spec.speed_columns:
            raise BatchError(f"speed columns changed before write: {source}")
        non_speed_fields = [
            name for name in fieldnames if not (name and name.startswith("spd_mph_"))
        ]
        with destination.open(
            "w", encoding="utf-8-sig", newline=""
        ) as output_handle:
            writer = csv.DictWriter(
                output_handle,
                fieldnames=fieldnames,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in reader:
                link_id = str(row.get("link_id", ""))
                blob = profiles.get(link_id)
                if blob is None:
                    raise BatchError(
                        f"no computed {spec.name} profile for link_id={link_id}"
                    )
                speeds = _unpack_speeds(blob, len(spec.speed_columns))
                _logical_row_hash_update(before_hash, row, non_speed_fields)
                for column, value in zip(spec.speed_columns, speeds):
                    row[column] = f"{value:.{speed_decimals}f}"
                writer.writerow(row)
                used_profiles.add(link_id)
                rows += 1
            output_handle.flush()
            os.fsync(output_handle.fileno())

    if len(used_profiles) != len(profiles):
        raise BatchError(
            f"{spec.name} wrote {len(used_profiles)} profiles but computed {len(profiles)}"
        )

    after_hash = hashlib.sha256()
    verified_rows = 0
    with destination.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != fieldnames:
            raise BatchError(f"header changed in temporary output: {destination}")
        for row in reader:
            _logical_row_hash_update(after_hash, row, non_speed_fields)
            verified_rows += 1
    if rows != verified_rows:
        raise BatchError(
            f"row count changed for {spec.name}: {rows} vs {verified_rows}"
        )
    if before_hash.hexdigest() != after_hash.hexdigest():
        raise BatchError(f"non-speed fields changed for {spec.name}")

    return {
        "period": spec.name,
        "source": str(source),
        "temporary_output": str(destination),
        "rows": rows,
        "profiles_written": len(used_profiles),
        "speed_columns": len(spec.speed_columns),
        "speed_decimals": speed_decimals,
        "non_speed_logical_sha256": before_hash.hexdigest(),
        "output_bytes": destination.stat().st_size,
    }


def _atomic_install(
    write_results: list[dict[str, object]],
    stage_dir: Path,
    run_id: str,
    keep_backup: bool,
) -> list[str]:
    rollback_paths: dict[Path, Path] = {}
    user_backups: list[str] = []
    originals = [Path(str(result["source"])) for result in write_results]
    for original in originals:
        rollback = stage_dir / f"rollback-{original.parent.name}.csv"
        shutil.copy2(original, rollback)
        rollback_paths[original] = rollback
        if keep_backup:
            backup = original.with_name(
                f"link_performance.pre-qvdf-{run_id}.csv"
            )
            shutil.copy2(original, backup)
            user_backups.append(str(backup))

    installed: list[Path] = []
    try:
        for result in write_results:
            original = Path(str(result["source"]))
            temporary = Path(str(result["temporary_output"]))
            os.replace(temporary, original)
            installed.append(original)
    except Exception:
        for original in installed:
            shutil.copy2(rollback_paths[original], original)
        raise
    return user_backups


def _audit_profiles(
    connection: sqlite3.Connection,
    specs: list[PeriodSpec],
    link_ids: list[str],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for link_id in link_ids:
        periods: dict[str, object] = {}
        for spec in specs:
            row = connection.execute(
                "SELECT speeds FROM results WHERE period = ? AND link_id = ?",
                (spec.name, link_id),
            ).fetchone()
            if row is None:
                continue
            values = _unpack_speeds(row[0], len(spec.speed_columns))
            periods[spec.name] = {
                column.removeprefix("spd_mph_"): round(value, 9)
                for column, value in zip(spec.speed_columns, values)
            }
        result[link_id] = periods
    return result


def _verify_installed_audits(
    specs: list[PeriodSpec], audits: dict[str, object]
) -> dict[str, object]:
    result: dict[str, object] = {}
    wanted = set(audits)
    for spec in specs:
        found: dict[str, dict[str, str]] = {}
        with (spec.directory / "link_performance.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            for row in csv.DictReader(handle):
                link_id = str(row.get("link_id", ""))
                if link_id in wanted:
                    found[link_id] = row
                    if len(found) == len(wanted):
                        break
        for link_id, expected_periods in audits.items():
            expected = dict(expected_periods).get(spec.name)
            if expected is None:
                continue
            row = found.get(link_id)
            if row is None:
                raise BatchError(
                    f"audit link_id={link_id} missing after {spec.name} write"
                )
            maximum_error = max(
                abs(
                    float(row[f"spd_mph_{time_label}"])
                    - float(expected_value)
                )
                for time_label, expected_value in dict(expected).items()
            )
            result.setdefault(link_id, {})[spec.name] = {
                "max_abs_write_rounding_error_mph": maximum_error,
                "verified_samples": len(expected),
            }
    return result


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _is_unc_path(path: Path) -> bool:
    normalized = str(path).replace("/", "\\")
    return normalized.startswith("\\\\")


def _stage_parent(scenario_dir: Path) -> tuple[Path, str]:
    configured = os.environ.get("NVTA_QVDF_TEMP_DIR", "").strip()
    if configured:
        parent = Path(configured).expanduser()
        if not parent.is_absolute():
            raise BatchError("NVTA_QVDF_TEMP_DIR must be an absolute path")
        parent.mkdir(parents=True, exist_ok=True)
        return parent.resolve(), "NVTA_QVDF_TEMP_DIR"

    if _is_unc_path(scenario_dir):
        return Path(tempfile.gettempdir()).resolve(), "local system temp for UNC output"

    return scenario_dir, "scenario output"


def run_batch(args: argparse.Namespace) -> dict[str, object]:
    total_started = time.perf_counter()
    scenario_dir = args.scenario_dir.resolve()
    specs = discover_periods(scenario_dir, args.periods)
    limits = ConnectorLimits(
        max_five_minute_change_mph=args.max_five_minute_change,
        rolling_window_intervals=args.rolling_window_intervals,
        max_rolling_average_change_mph=args.max_rolling_average_change,
        max_acceleration_mph_per_hour2=args.max_acceleration,
    )
    speed_decimals = int(args.speed_decimals)
    if not 3 <= speed_decimals <= 15:
        raise BatchError("--speed-decimals must be between 3 and 15")
    workers, worker_details = choose_worker_count(str(args.workers))
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = (
        args.report.resolve()
        if args.report is not None
        else scenario_dir / "qvdf_batch_report.json"
    )
    stage_parent, stage_policy = _stage_parent(scenario_dir)
    stage_dir = Path(tempfile.mkdtemp(prefix=".qvdf-batch-", dir=stage_parent))
    print(
        f"temporary smoother database: policy={stage_policy} parent={stage_parent}",
        flush=True,
    )
    db_path = stage_dir / "batch.sqlite"
    connection = _open_stage(db_path)
    _create_stage_schema(connection)
    write_results: list[dict[str, object]] = []
    report: dict[str, object] = {
        "scenario_dir": str(scenario_dir),
        "run_id": run_id,
        "periods": [
            {
                "name": spec.name,
                "start_hour": spec.start_hour,
                "end_hour": spec.end_hour,
                "speed_columns": len(spec.speed_columns),
            }
            for spec in specs
        ],
        "limits": asdict(limits),
        "speed_decimals": speed_decimals,
        "write_back_requested": bool(args.write_back),
        "temporary_storage": {
            "policy": stage_policy,
            "parent": str(stage_parent),
            "persistent_cache": False,
            "cleaned_after_run": True,
        },
    }
    try:
        report["staging"] = stage_inputs(connection, specs)
        report["compute"] = compute_profiles(
            connection,
            db_path,
            specs,
            limits,
            workers,
            worker_details,
            args.chunk_size,
            speed_decimals,
        )
        errors = connection.execute(
            "SELECT link_id, message FROM errors ORDER BY link_id"
        ).fetchall()
        report["errors"] = [
            {"link_id": link_id, "message": message}
            for link_id, message in errors[:100]
        ]
        report["error_count"] = len(errors)
        if errors:
            raise BatchError(
                f"{len(errors)} links failed; write-back was not attempted"
            )
        if report["compute"]["constraint_violations"]:
            raise BatchError(
                f"{report['compute']['constraint_violations']} links violate final constraints"
            )
        if report["compute"]["serialized_constraint_violations"]:
            raise BatchError(
                f"{report['compute']['serialized_constraint_violations']} links "
                "would violate constraints after CSV serialization"
            )

        audit_links = list(dict.fromkeys(args.audit_link or []))
        report["audit_profiles"] = _audit_profiles(connection, specs, audit_links)

        write_started = time.perf_counter()
        if args.write_back:
            for spec in specs:
                result = _write_period_temp(
                    connection, spec, run_id, speed_decimals
                )
                write_results.append(result)
                print(
                    f"validated period={spec.name} rows={result['rows']} "
                    f"profiles={result['profiles_written']}",
                    flush=True,
                )
            backups = _atomic_install(
                write_results, stage_dir, run_id, bool(args.backup)
            )
            report["write_back"] = {
                "wall_seconds": time.perf_counter() - write_started,
                "periods": write_results,
                "backups": backups,
                "installed": True,
            }
            report["installed_audit_verification"] = _verify_installed_audits(
                specs, report["audit_profiles"]
            )
        else:
            report["write_back"] = {
                "wall_seconds": 0.0,
                "periods": [],
                "backups": [],
                "installed": False,
            }

        report["total_wall_seconds"] = time.perf_counter() - total_started
        report["status"] = "complete"
        _write_report(report_path, report)
        print(f"report={report_path}", flush=True)
        return report
    except Exception as exc:
        report["total_wall_seconds"] = time.perf_counter() - total_started
        report["status"] = "failed"
        report["failure"] = f"{type(exc).__name__}: {exc}"
        _write_report(report_path, report)
        raise
    finally:
        connection.close()
        for result in write_results:
            temporary = Path(str(result["temporary_output"]))
            if temporary.exists():
                temporary.unlink()
        shutil.rmtree(stage_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario-dir", type=Path, required=True)
    parser.add_argument("--periods", nargs="+", required=True)
    parser.add_argument("--workers", "--threads", default="auto")
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--write-back", action="store_true")
    parser.add_argument(
        "--backup", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--audit-link", action="append", default=[])
    parser.add_argument("--max-five-minute-change", type=float, default=8.0)
    parser.add_argument("--rolling-window-intervals", type=int, default=3)
    parser.add_argument("--max-rolling-average-change", type=float, default=4.0)
    parser.add_argument("--max-acceleration", type=float, default=576.0)
    parser.add_argument(
        "--speed-decimals",
        type=int,
        default=9,
        help="decimal places written to spd_mph_* values (default: 9)",
    )
    return parser


def main() -> None:
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    parser = build_parser()
    args = parser.parse_args()
    try:
        report = run_batch(args)
    except Exception as exc:
        print(f"error={type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        "summary="
        + json.dumps(
            {
                "status": report["status"],
                "unique_links": report["staging"]["unique_links"],
                "workers": report["compute"]["workers"],
                "compute_wall_seconds": report["compute"]["wall_seconds"],
                "links_per_second": report["compute"]["links_per_second"],
                "total_wall_seconds": report["total_wall_seconds"],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
