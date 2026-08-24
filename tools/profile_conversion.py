"""Profile serial and bounded-parallel NVTA conversion runs.

The profiler invokes the complete workflow with assignment disabled.  Each run
gets its own output folder, execution log, resource samples, and conversion
profile.  Output hashes are compared with the one-worker baseline.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil


WORKFLOW_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = WORKFLOW_ROOT.parent
DEFAULT_SCENARIO = (
    PACKAGE_ROOT
    / "input-data"
    / "nvta-taplite-workflow"
    / "dtalite-run-07162026"
)
DEFAULT_PERIODS = ["am", "md", "pm", "nt"]
DEFAULT_PERIOD_TIMES = ["0600_0900", "0900_1500", "1500_1900", "1900_0600"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare one-worker and bounded-parallel conversion with assignment disabled."
    )
    parser.add_argument("scenario", nargs="?", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--workers", default="1,4,8")
    parser.add_argument("--reserve-cores", type=int, default=1)
    parser.add_argument("--network-chunks", type=int, default=0)
    parser.add_argument("--demand-chunks", type=int, default=0)
    parser.add_argument(
        "--demand-output-format",
        choices=["csv", "binary", "both"],
        default="csv",
    )
    parser.add_argument(
        "--conversion-cache",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable prepared-network cache. Disabled by default so worker comparisons are fair.",
    )
    parser.add_argument(
        "--conversion-cache-dir",
        type=Path,
        default=None,
        help="Optional prepared-network cache directory.",
    )
    parser.add_argument("--periods", nargs="+", default=DEFAULT_PERIODS)
    parser.add_argument("--period-times", nargs="+", default=DEFAULT_PERIOD_TIMES)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument(
        "--cpu-affinity",
        default=None,
        help="Optional logical CPU list/range for profiled conversion, for example 0-15 or 0,2,4.",
    )
    parser.add_argument(
        "--low-priority",
        action="store_true",
        help="Run profiled conversion below normal Windows priority.",
    )
    parser.add_argument(
        "--legacy-workflow-root",
        type=Path,
        default=None,
        help=(
            "Profile the unchanged serial workflow at this path instead of "
            "nvta-taplite-workflow. Requires --workers 1 and CSV output."
        ),
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def parse_workers(raw: str) -> list[int]:
    workers = [int(value.strip()) for value in raw.split(",") if value.strip()]
    if not workers or any(value < 1 for value in workers):
        raise ValueError("--workers must contain positive integers")
    return list(dict.fromkeys(workers))


def parse_cpu_affinity(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    cpus = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if "-" in value:
            start_text, stop_text = value.split("-", 1)
            start, stop = int(start_text), int(stop_text)
            if stop < start:
                raise ValueError("CPU affinity ranges must be ascending")
            cpus.update(range(start, stop + 1))
        else:
            cpus.add(int(value))
    if not cpus or min(cpus) < 0:
        raise ValueError("--cpu-affinity must contain nonnegative CPU indices")
    return sorted(cpus)


def process_tree(root: psutil.Process) -> list[psutil.Process]:
    processes = [root]
    try:
        processes.extend(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return list({process.pid: process for process in processes}.values())


def sample_tree(root: psutil.Process, elapsed_sec: float) -> dict:
    rss = 0
    cpu_percent = 0.0
    process_count = 0
    for process in process_tree(root):
        try:
            rss += process.memory_info().rss
            cpu_percent += process.cpu_percent(None)
            process_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    memory = psutil.virtual_memory()
    return {
        "elapsed_sec": round(elapsed_sec, 6),
        "process_count": process_count,
        "rss_mb": round(rss / 1024**2, 3),
        "cpu_percent": round(cpu_percent, 3),
        "system_memory_percent": memory.percent,
        "system_available_mb": round(memory.available / 1024**2, 3),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def output_manifest(output_dir: Path, periods: list[str]) -> dict[str, dict]:
    manifest = {}
    for period in periods:
        period_dir = output_dir / period
        for path in sorted(
            [
                *period_dir.glob("*.csv"),
                *period_dir.glob("*.bin"),
            ]
        ):
            relative = path.relative_to(output_dir).as_posix()
            manifest[relative] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    return manifest


def stage_legacy_scenario_inputs(scenario: Path, target: Path) -> dict[str, int]:
    """Stage source files without copying large OMX inputs when possible."""

    hardlinks = 0
    copies = 0
    for source in scenario.iterdir():
        if not source.is_file():
            continue
        destination = target / source.name
        try:
            os.link(source, destination)
            hardlinks += 1
        except OSError:
            shutil.copy2(source, destination)
            copies += 1
    return {"hardlinks": hardlinks, "copies": copies}


def run_profile(
    *,
    scenario: Path,
    run_dir: Path,
    workers: int,
    reserve_cores: int,
    network_chunks: int,
    demand_chunks: int,
    conversion_cache: bool,
    conversion_cache_dir: Path | None,
    demand_output_format: str,
    periods: list[str],
    period_times: list[str],
    sample_interval: float,
    cpu_affinity: list[int] | None,
    low_priority: bool,
    legacy_workflow_root: Path | None,
) -> dict:
    output_dir = run_dir / "converted"
    output_dir.mkdir(parents=True)
    if legacy_workflow_root is None:
        command_cwd = WORKFLOW_ROOT
        command = [
            sys.executable,
            str(WORKFLOW_ROOT / "run_assignment.py"),
            str(scenario),
            "--network-conversion",
            "true",
            "--demand-conversion",
            "true",
            "--dtalite-assignment",
            "false",
            "--route-output",
            "0",
            "--vehicle-output",
            "0",
            "--conversion-workers",
            str(workers),
            "--conversion-reserve-cores",
            str(reserve_cores),
            "--network-chunks",
            str(network_chunks),
            "--demand-chunks",
            str(demand_chunks),
            "--conversion-adaptive",
            "false",
            "--conversion-cache",
            "true" if conversion_cache else "false",
            "--demand-output-format",
            demand_output_format,
            "--output-dir",
            str(output_dir),
            "--time-periods",
            *periods,
            "--period-times",
            *period_times,
        ]
        if conversion_cache_dir is not None:
            command.extend(["--conversion-cache-dir", str(conversion_cache_dir)])
    else:
        command_cwd = legacy_workflow_root
        staging = stage_legacy_scenario_inputs(scenario, output_dir)
        (run_dir / "legacy_staging.json").write_text(
            json.dumps(staging, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(legacy_workflow_root / "run_assignment.py"),
            str(output_dir),
            "--network-conversion",
            "true",
            "--demand-conversion",
            "true",
            "--dtalite-assignment",
            "false",
            "--route-output",
            "0",
            "--vehicle-output",
            "0",
            "--time-periods",
            *periods,
            "--period-times",
            *period_times,
        ]
    (run_dir / "command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )

    samples = []
    started = time.perf_counter()
    with (run_dir / "execution.log").open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            command,
            cwd=command_cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        root = psutil.Process(process.pid)
        if cpu_affinity is not None:
            root.cpu_affinity(cpu_affinity)
        if low_priority:
            root.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        for item in process_tree(root):
            try:
                item.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        while process.poll() is None:
            time.sleep(sample_interval)
            samples.append(sample_tree(root, time.perf_counter() - started))
        return_code = process.wait()

    elapsed = time.perf_counter() - started
    write_csv(run_dir / "resource_samples.csv", samples)
    conversion_profile_path = output_dir / "CONVERSION_PROFILE.json"
    conversion_profile = (
        json.loads(conversion_profile_path.read_text(encoding="utf-8"))
        if conversion_profile_path.exists()
        else None
    )
    summary = {
        "workers": workers,
        "workflow_root": str(command_cwd),
        "legacy_serial_workflow": legacy_workflow_root is not None,
        "return_code": return_code,
        "elapsed_sec": elapsed,
        "peak_rss_mb": max((row["rss_mb"] for row in samples), default=0),
        "peak_cpu_percent": max((row["cpu_percent"] for row in samples), default=0),
        "mean_cpu_percent": (
            sum(row["cpu_percent"] for row in samples) / len(samples) if samples else 0
        ),
        "minimum_system_available_mb": min(
            (row["system_available_mb"] for row in samples), default=0
        ),
        "conversion_profile": conversion_profile,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    if return_code != 0:
        raise RuntimeError(
            f"Conversion profile with workers={workers} failed; see {run_dir / 'execution.log'}"
        )
    return summary


def main() -> None:
    args = parse_args()
    scenario = args.scenario.resolve()
    workers = parse_workers(args.workers)
    cpu_affinity = parse_cpu_affinity(args.cpu_affinity)
    conversion_cache_dir = (
        args.conversion_cache_dir.resolve()
        if args.conversion_cache_dir is not None
        else None
    )
    legacy_workflow_root = (
        args.legacy_workflow_root.resolve()
        if args.legacy_workflow_root is not None
        else None
    )
    if legacy_workflow_root is not None:
        if workers != [1]:
            raise ValueError("Legacy workflow profiling requires --workers 1.")
        if args.demand_output_format != "csv":
            raise ValueError("Legacy workflow profiling supports CSV output only.")
        if not (legacy_workflow_root / "src" / "dtalite4cube" / "runner.py").is_file():
            raise FileNotFoundError(
                f"Legacy workflow runner not found under {legacy_workflow_root}"
            )
    if len(args.periods) != len(args.period_times):
        raise ValueError("--periods and --period-times must have equal lengths")

    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H-%M")
    output_root = (
        args.output.resolve()
        if args.output is not None
        else (
            PACKAGE_ROOT
            / "outputs"
            / "nvta-taplite-workflow"
            / f"nvta-taplite-workflow-{timestamp}"
            / "conversion-profiles"
        )
    )
    output_root.mkdir(parents=True, exist_ok=False)

    summaries = []
    manifests = {}
    for worker_count in workers:
        run_dir = output_root / f"workers_{worker_count}"
        run_dir.mkdir()
        print(f"Profiling conversion with {worker_count} worker(s): {run_dir}", flush=True)
        summary = run_profile(
            scenario=scenario,
            run_dir=run_dir,
            workers=worker_count,
            reserve_cores=args.reserve_cores,
            network_chunks=args.network_chunks,
            demand_chunks=args.demand_chunks,
            conversion_cache=args.conversion_cache,
            conversion_cache_dir=conversion_cache_dir,
            demand_output_format=args.demand_output_format,
            periods=args.periods,
            period_times=args.period_times,
            sample_interval=args.sample_interval,
            cpu_affinity=cpu_affinity,
            low_priority=args.low_priority,
            legacy_workflow_root=legacy_workflow_root,
        )
        summaries.append(summary)
        manifests[worker_count] = output_manifest(run_dir / "converted", args.periods)

    baseline_worker = workers[0]
    baseline = manifests[baseline_worker]
    comparisons = {}
    for worker_count in workers[1:]:
        candidate = manifests[worker_count]
        all_paths = sorted(set(baseline) | set(candidate))
        differences = [
            path
            for path in all_paths
            if baseline.get(path) != candidate.get(path)
        ]
        comparisons[str(worker_count)] = {
            "matches_worker": baseline_worker,
            "matching_files": len(all_paths) - len(differences),
            "different_files": differences,
        }

    report = {
        "scenario": str(scenario),
        "output_root": str(output_root),
        "workers": workers,
        "legacy_workflow_root": (
            str(legacy_workflow_root)
            if legacy_workflow_root is not None
            else None
        ),
        "summaries": summaries,
        "output_comparisons": comparisons,
    }
    (output_root / "profile_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
