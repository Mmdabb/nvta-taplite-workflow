from __future__ import annotations

import csv
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .dtab import demand_binary_path, inspect_dtab
    from .file_utils import copy_files_parallel, resolved_workers
    from .settings.generate_dtalite_settings import normalize_dtalite_period_hours
except ImportError:
    from dtab import demand_binary_path, inspect_dtab
    from file_utils import copy_files_parallel, resolved_workers
    from settings.generate_dtalite_settings import normalize_dtalite_period_hours

logger = logging.getLogger(__name__)

VALID_KERNEL_SOURCES = {"pypi"}
DEFAULT_KERNEL_SOURCE = "pypi"

REQUIRED_INPUTS = ("node.csv", "link.csv", "settings.csv", "mode_type.csv")
ROUTE_OUTPUTS = (
    "route_assignment.csv",
)
REQUIRED_OUTPUTS = (
    "od_performance.csv",
    "link_performance.csv",
)
EXPECTED_OUTPUTS = ROUTE_OUTPUTS + REQUIRED_OUTPUTS + (
    "origin_accessibility.csv",
    "destination_accessibility.csv",
    "inaccessible_od.csv",
    "google_maps_od_distance.csv",
    "system_performance.csv",
    "summary_log_file.txt",
    "TAP_log.csv",
)
DEFAULT_SETTINGS_HEADER = (
    "number_of_iterations,number_of_processors,"
    "demand_period_starting_hours,demand_period_ending_hours,"
    "first_through_node_id,base_demand_mode,route_output,vehicle_output,"
    "log_file,odme_mode,odme_vmt,demand_format"
)


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    path = Path(path)
    if not path.exists():
        return "(missing)"

    digest = hashlib.md5()
    with path.open("rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def count_rows(path: Path) -> int:
    path = Path(path)
    if not path.exists():
        return -1

    with path.open("r", newline="", encoding="utf-8", errors="replace") as f:
        return max(sum(1 for _ in f) - 1, 0)


def fmt_size(n_bytes: int) -> str:
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024**2:
        return f"{n_bytes / 1024:.1f} KB"
    if n_bytes < 1024**3:
        return f"{n_bytes / 1024**2:.2f} MB"
    return f"{n_bytes / 1024**3:.2f} GB"


def fmt_count(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _read_header(path: Path) -> list[str]:
    with path.open("r", newline="", encoding="utf-8", errors="replace") as f:
        return next(csv.reader(f), [])


def _require_columns(file_name: str, columns: list[str], required: set[str]) -> None:
    missing = sorted(required - set(columns))
    if missing:
        raise ValueError(
            f"{file_name} is missing required column(s): {', '.join(missing)}. "
            f"Available columns include: {', '.join(columns[:12])}"
        )


def _settings_demand_format(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8", errors="replace") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return 0
    try:
        return int(float(rows[0].get("demand_format") or 0))
    except ValueError as exc:
        raise ValueError(f"Invalid demand_format in {path}") from exc


def preflight(src: Path) -> dict[str, Any]:
    src = Path(src).resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"DTALite source folder does not exist: {src}")

    info: dict[str, Any] = {"src": str(src), "files": {}}
    missing_files = [name for name in REQUIRED_INPUTS if not (src / name).is_file()]
    if missing_files:
        raise FileNotFoundError(
            f"DTALite preflight failed in {src}. Missing required input file(s): "
            f"{', '.join(missing_files)}"
        )

    for name in REQUIRED_INPUTS:
        path = src / name
        info["files"][name] = {
            "size": path.stat().st_size,
            "md5": md5_of(path),
            "rows": count_rows(path),
        }

    node_cols = _read_header(src / "node.csv")
    link_cols = _read_header(src / "link.csv")
    mode_type_cols = _read_header(src / "mode_type.csv")

    _require_columns("node.csv", node_cols, {"node_id"})
    _require_columns("link.csv", link_cols, {"link_id", "from_node_id", "to_node_id"})
    _require_columns("mode_type.csv", mode_type_cols, {"mode_type", "demand_file"})
    demand_files = _read_mode_type_demand_files(src / "mode_type.csv")
    demand_format = _settings_demand_format(src / "settings.csv")
    actual_demand_paths = {
        name: (
            demand_binary_path(src / name)
            if demand_format == 1
            else src / name
        )
        for name in demand_files
    }
    missing_demand_files = [
        path.name
        for path in actual_demand_paths.values()
        if not path.is_file()
    ]
    if missing_demand_files:
        raise FileNotFoundError(
            f"DTALite preflight failed in {src}. Missing demand file(s) referenced by mode_type.csv: "
            f"{', '.join(missing_demand_files)}"
        )
    if demand_format == 0:
        for demand_file in demand_files:
            _require_columns(
                demand_file,
                _read_header(src / demand_file),
                {"o_zone_id", "d_zone_id", "volume"},
            )

    info["files"]["node.csv"]["columns"] = node_cols
    info["files"]["link.csv"]["columns"] = link_cols
    info["files"]["mode_type.csv"]["columns"] = mode_type_cols
    for demand_file in demand_files:
        path = actual_demand_paths[demand_file]
        if demand_format == 1:
            dtab_info = inspect_dtab(path)
            info["files"][path.name] = {
                "size": path.stat().st_size,
                "md5": md5_of(path),
                "rows": dtab_info["records"],
                "format": "DTAB",
                "version": dtab_info["version"],
            }
        else:
            info["files"][demand_file] = {
                "size": path.stat().st_size,
                "md5": md5_of(path),
                "rows": count_rows(path),
                "columns": _read_header(path),
            }
    info["counts"] = {
        "node_rows": info["files"]["node.csv"]["rows"],
        "link_rows": info["files"]["link.csv"]["rows"],
        "mode_type_rows": info["files"]["mode_type.csv"]["rows"],
        "demand_files": len(demand_files),
    }
    logger.info(
        "DTALite preflight OK for %s: nodes=%s links=%s demand_files=%s",
        src,
        info["counts"]["node_rows"],
        info["counts"]["link_rows"],
        info["counts"]["demand_files"],
    )
    return info


def _read_mode_type_demand_files(path: Path) -> list[str]:
    with path.open("r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if "demand_file" not in (reader.fieldnames or []):
            return []
        return [row["demand_file"] for row in reader if row.get("demand_file")]


def stage_inputs(
    src: Path,
    work_dir: Path,
    iterations: int,
    processors: int,
    route_output: int,
    vehicle_output: int,
    period_start: int,
    period_end: int,
    metric_system: int,
    copy_workers: int = 1,
) -> Path:
    src = Path(src).resolve()
    work_dir = Path(work_dir).resolve()

    if work_dir != src and work_dir.exists():
        if work_dir in src.parents:
            raise ValueError(
                f"Refusing to clean work_dir because it contains the source folder: {work_dir}"
            )
        if work_dir.parent == work_dir:
            raise ValueError(f"Refusing to clean filesystem root as work_dir: {work_dir}")
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    copy_pairs = [
        (source, work_dir / source.name)
        for source in src.iterdir()
        if source.is_file()
    ]
    copied = copy_files_parallel(
        copy_pairs,
        workers=copy_workers,
        preserve_metadata=True,
    )
    for source, target in copied:
        logger.info(
            "Staged %s to %s (%s)",
            source.name,
            work_dir,
            fmt_size(target.stat().st_size),
        )
    if copied:
        logger.info(
            "Staged %s assignment input files with %s copy worker(s)",
            len(copied),
            resolved_workers(copy_workers, len(copy_pairs)),
        )

    settings_path = work_dir / "settings.csv"
    normalized_settings = _normalize_settings_rows(settings_path, route_output, vehicle_output)
    if normalized_settings is not None:
        with settings_path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(normalized_settings)
        logger.info(
            "Set route_output=%s and vehicle_output=%s in %s",
            route_output,
            vehicle_output,
            settings_path,
        )
    else:
        settings_path.write_text(
            _default_settings_csv(
                iterations,
                processors,
                route_output,
                vehicle_output,
                period_start,
                period_end,
                metric_system,
            ),
            encoding="utf-8",
        )
        logger.info(
            "Wrote default settings.csv with route_output=%s and vehicle_output=%s in %s",
            route_output,
            vehicle_output,
            settings_path,
        )

    return work_dir


def _normalize_settings_rows(settings_path: Path, route_output: int, vehicle_output: int) -> list[list[str]] | None:
    with settings_path.open("r", newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))

    required_columns = {
        "route_output",
        "vehicle_output",
        "demand_period_starting_hours",
        "demand_period_ending_hours",
    }
    if len(rows) < 2 or not required_columns.issubset(rows[0]):
        return None

    route_output_index = rows[0].index("route_output")
    vehicle_output_index = rows[0].index("vehicle_output")
    period_start_index = rows[0].index("demand_period_starting_hours")
    period_end_index = rows[0].index("demand_period_ending_hours")
    for row in rows[1:]:
        while len(row) <= max(route_output_index, vehicle_output_index, period_start_index, period_end_index):
            row.append("")
        row[route_output_index] = str(route_output)
        row[vehicle_output_index] = str(vehicle_output)
        start_hour = int(float(row[period_start_index]))
        end_hour = int(float(row[period_end_index]))
        normalized_start, normalized_end, crosses_midnight = normalize_dtalite_period_hours(start_hour, end_hour)
        if crosses_midnight:
            logger.warning(
                "DTALite settings period crosses midnight (%s -> %s). "
                "DTALite settings will temporarily use %s -> 24 only. "
                "The post-midnight portion is not assigned in this run.",
                start_hour,
                end_hour,
                start_hour,
            )
        row[period_start_index] = str(normalized_start)
        row[period_end_index] = str(normalized_end)
    return rows


def _default_settings_csv(
    iterations: int,
    processors: int,
    route_output: int,
    vehicle_output: int,
    period_start: int,
    period_end: int,
    metric_system: int,
) -> str:
    _ = metric_system
    normalized_start, normalized_end, crosses_midnight = normalize_dtalite_period_hours(period_start, period_end)
    if crosses_midnight:
        logger.warning(
            "DTALite settings period crosses midnight (%s -> %s). "
            "DTALite settings will temporarily use %s -> 24 only. "
            "The post-midnight portion is not assigned in this run.",
            period_start,
            period_end,
            period_start,
        )
    return (
        f"{DEFAULT_SETTINGS_HEADER}\n"
        f"{iterations},{processors},{normalized_start},{normalized_end},-1,0,{route_output},{vehicle_output},0,0,0,0\n"
    )

def _build_dtalite_command(kernel_source: str) -> list[str]:
    if kernel_source not in VALID_KERNEL_SOURCES:
        raise ValueError(
            f"kernel_source must be one of {sorted(VALID_KERNEL_SOURCES)}; got {kernel_source!r}"
        )

    # The pinned PyPI prerelease installs both the public namespace and its
    # pytaplite kernel driver. No alternate engine fallback is permitted.
    path_bootstrap = "pass"
    code = f"""
import os
import sys

{path_bootstrap}

import taplite4mpo
import pytaplite

print("taplite4mpo source:", taplite4mpo.__file__)
print("taplite4mpo version:", taplite4mpo.__version__)
result = pytaplite.assign(os.getcwd(), in_place=True)
print(result.log)
print("taplite4mpo assignment:", result.summary())
raise SystemExit(result.returncode)
"""
    return [sys.executable, "-c", code]


def run_dtalite(work_dir: Path, kernel_source: str = DEFAULT_KERNEL_SOURCE) -> tuple[float, str]:
    work_dir = Path(work_dir).resolve()
    kernel_source = (kernel_source or DEFAULT_KERNEL_SOURCE).strip().lower()
    command = _build_dtalite_command(kernel_source)
    log_path = work_dir / "dtalite_run.log"
    logger.info("Running DTALite in %s using kernel_source=%s", work_dir, kernel_source)
    started = time.time()
    process = subprocess.Popen(
        command,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
        bufsize=1,
    )
    log_lines: list[str] = []
    assert process.stdout is not None
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        for line in process.stdout:
            log_lines.append(line)
            log_file.write(line)
            log_file.flush()
            logger.info("[DTALite] %s", line.rstrip())

    return_code = process.wait()
    elapsed = time.time() - started
    log = "".join(log_lines)

    if return_code != 0:
        raise RuntimeError(
            f"DTALite failed in {work_dir} with return code {return_code}. "
            f"See {log_path}"
        )

    logger.info("DTALite completed in %.1fs; log written to %s", elapsed, log_path)
    return elapsed, log


def verify_outputs(work_dir: Path, route_output: int = 0) -> dict[str, Any]:
    work_dir = Path(work_dir).resolve()
    info: dict[str, Any] = {"outputs": {}}
    missing_or_empty = []
    required_outputs = ["od_performance.csv", "link_performance.csv"]
    if route_output:
        required_outputs.insert(0, "route_assignment.csv")

    for name in required_outputs:
        path = work_dir / name
        if not path.exists() or path.stat().st_size == 0:
            missing_or_empty.append(name)
            continue
        info["outputs"][name] = _file_info(path)

    if missing_or_empty:
        raise FileNotFoundError(
            "DTALite output verification failed. Missing or empty output file(s): "
            + ", ".join(missing_or_empty)
        )

    if route_output:
        route_assignment = work_dir / "route_assignment.csv"
        info["columns"] = {
            "file": str(route_assignment),
            "rows": info["outputs"]["route_assignment.csv"]["rows"],
            "unique_od_pairs": _count_unique_od(route_assignment),
        }

    for name in EXPECTED_OUTPUTS:
        if name in info["outputs"]:
            continue
        path = work_dir / name
        if path.exists():
            info["outputs"][name] = _file_info(path)

    return info


def _file_info(path: Path) -> dict[str, Any]:
    return {
        "size": path.stat().st_size,
        "md5": md5_of(path),
        "rows": count_rows(path),
    }


def _count_unique_od(route_assignment_csv: Path) -> int:
    route_assignment_csv = Path(route_assignment_csv)
    if not route_assignment_csv.exists():
        return -1

    seen: set[tuple[str, str]] = set()
    with route_assignment_csv.open("r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        try:
            origin_index = header.index("o_zone_id")
            destination_index = header.index("d_zone_id")
        except ValueError:
            return -1

        required_width = max(origin_index, destination_index)
        for row in reader:
            if len(row) > required_width:
                seen.add((row[origin_index], row[destination_index]))

    return len(seen)


def parse_convergence(log: str, work_dir: Path | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "iterations": [],
        "final_gap_pct": None,
        "final_iter": None,
        "cpu_time": None,
    }

    sources: list[str] = []
    if work_dir is not None:
        summary_log = Path(work_dir) / "summary_log_file.txt"
        if summary_log.exists():
            sources.append(summary_log.read_text(encoding="utf-8", errors="replace"))
    if log:
        sources.append(log)

    for source in sources:
        for line in source.splitlines():
            clean_line = line.strip()
            if clean_line.startswith("iter No"):
                info["iterations"].append(clean_line)
                try:
                    gap_text = clean_line.split("gap = ")[-1].strip().rstrip("%").strip()
                    iter_text = clean_line.split("iter No = ")[-1].split(",")[0]
                    info["final_gap_pct"] = float(gap_text)
                    info["final_iter"] = int(iter_text)
                except (IndexError, ValueError):
                    logger.debug("Unable to parse convergence line: %s", clean_line)
            elif clean_line.startswith("CPU running time"):
                info["cpu_time"] = clean_line.replace("CPU running time:", "").strip()

        if info["iterations"]:
            break

    return info


def write_run_card(
    work_dir: Path,
    src: Path,
    label: str,
    preflight_info: dict[str, Any],
    run_elapsed: float,
    convergence: dict[str, Any],
    verify_info: dict[str, Any],
    args_used: dict[str, Any],
) -> Path:
    work_dir = Path(work_dir).resolve()
    src = Path(src).resolve()
    lines: list[str] = []
    add = lines.append

    add("# DTALite RUN_CARD")
    add("")
    add(f"- Generated: `{datetime.now(timezone.utc).isoformat()}`")
    add(f"- Label: `{label}`")
    add(f"- Source: `{src}`")
    add(f"- Work dir: `{work_dir}`")
    add(f"- Python: `{sys.version.split()[0]}`")
    add(f"- Runtime: `{run_elapsed:.1f} s`")
    add("")

    add("## Inputs")
    add("")
    add("| file | rows | size | md5 |")
    add("|---|---:|---:|---|")
    for name in REQUIRED_INPUTS:
        meta = preflight_info["files"].get(name, {})
        add(
            f"| `{name}` | {meta.get('rows', -1):,} | "
            f"{fmt_size(meta.get('size', 0))} | `{meta.get('md5', '-')}` |"
        )
    add("")

    add("## Settings")
    add("")
    settings_path = work_dir / "settings.csv"
    if settings_path.exists():
        add("```csv")
        add(settings_path.read_text(encoding="utf-8", errors="replace").strip())
        add("```")
    else:
        add("`settings.csv` was not found in the run folder.")
    add("")

    add("## Convergence")
    add("")
    if convergence.get("iterations"):
        add(f"- Final iteration: `{convergence.get('final_iter', '?')}`")
        add(f"- Final gap percent: `{convergence.get('final_gap_pct', '?')}`")
        if convergence.get("cpu_time"):
            add(f"- DTALite CPU time: `{convergence['cpu_time']}`")
        add("")
        add("```text")
        for line in convergence["iterations"][-12:]:
            add(line)
        add("```")
    else:
        add("No convergence iteration log was found.")
    add("")

    add("## Outputs")
    add("")
    add("| file | rows | size | md5 |")
    add("|---|---:|---:|---|")
    for name in EXPECTED_OUTPUTS:
        meta = verify_info["outputs"].get(name)
        if meta:
            add(
                f"| `{name}` | {meta['rows']:,} | "
                f"{fmt_size(meta['size'])} | `{meta['md5']}` |"
            )
        else:
            add(f"| `{name}` | not produced | - | - |")
    add("")

    columns = verify_info.get("columns", {})
    add("## Route Assignment Summary")
    add("")
    add(f"- Route assignment rows: `{fmt_count(columns.get('rows', '?'))}`")
    add(f"- Unique OD pairs: `{fmt_count(columns.get('unique_od_pairs', '?'))}`")
    add("")

    add("## Reproduction Command")
    add("")
    add("```powershell")
    add(_reproduction_command(src, work_dir, label, args_used))
    add("```")
    add("")

    run_card_path = work_dir / "RUN_CARD.md"
    run_card_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote DTALite run card: %s", run_card_path)
    return run_card_path


def _reproduction_command(src: Path, work_dir: Path, label: str, args_used: dict[str, Any]) -> str:
    command = [
        "python",
        "scripts/run_dtalite_taplite.py",
        "--src",
        str(src),
        "--work-dir",
        str(work_dir),
        "--iterations",
        str(args_used.get("iterations")),
        "--processors",
        str(args_used.get("processors")),
        "--period-start",
        str(args_used.get("period_start")),
        "--period-end",
        str(args_used.get("period_end")),
        "--unit-system",
        str(args_used.get("unit_system", "imperial")),
    ]
    if label:
        command.extend(["--label", label])
    return " ".join(command)


