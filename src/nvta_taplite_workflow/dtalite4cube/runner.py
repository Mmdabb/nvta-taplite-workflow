from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import logging
import math
import shutil
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .file_utils import copy_files_parallel, resolved_workers
from .internal_config import RUN_GMNS_READINESS_CHECK
from .internal_config import WRITE_ASSIGNMENT_SUMMARY
from .readiness_check import ReadinessResult
from .readiness_check import run_gmns_readiness_check as run_period_readiness_check
from .qvdf_smoother import SmootherConfig, smooth_scenario
from .reproducible_run import (
    DEFAULT_KERNEL_SOURCE,
    VALID_KERNEL_SOURCES,
    parse_convergence,
    preflight,
    run_dtalite,
    stage_inputs,
    verify_outputs,
    write_run_card,
)
from .dtab import demand_binary_path, inspect_dtab
from .settings.dtalite_settings_config import SUPPORTED_MODE_TYPES, demand_file_name
from .settings.generate_dtalite_settings import (
    configure_time_period_hours,
    generate_dtalite_input_files,
    normalize_period_key,
)
from .unit_contract import (
    TAPLITE_GENERIC_LENGTH_UNIT,
    TAPLITE_GENERIC_SPEED_UNIT,
    TAPLITE_LINK_UNIT_CONTRACT,
)

logger = logging.getLogger(__name__)
MAX_GLOBAL_CORES = 20
DEFAULT_QVDF_OVERRIDE_DICTIONARY = (
    Path(__file__).resolve().parent
    / "resources"
    / "qvdf_parameter_override_lookup"
    / "qvdf_node_pair_overrides.npy"
)

NETWORK_QVDF_VALUE_FIELDS = (
    "vdf_alpha",
    "vdf_beta",
    "vdf_qdf",
    "vdf_plf",
    "vdf_cp",
    "vdf_cd",
    "vdf_n",
    "vdf_s",
)
NETWORK_MAPPING_SCHEMA_FIELDS = (
    *NETWORK_QVDF_VALUE_FIELDS,
    "qvdf_start_speed_mph",
    "qvdf_end_speed_mph",
    "t0_hour",
    "t2_hour",
    "t3_hour",
    "qvdf_profile_mode",
)

LEGACY_ASSIGNMENT_KEYS = {
    "dta_exe_name",
    "inplace",
    "length",
    "memory_blocks",
    "metric_system",
    "modes",
    "period_scale_factors",
    "route",
    "scenario_output_dir",
    "settings_overrides",
    "simu",
    "speed",
}

INTERNAL_ASSIGNMENT_KEYS = {
    "run_gmns_readiness_check",
    "write_assignment_summary",
}


def _taplite_provenance() -> dict[str, str | None]:
    distribution_name = "taplite4mpo-pre-release"
    workflow_distribution_name = "nvta-taplite-workflow"
    project_url = "https://pypi.org/project/taplite4mpo-pre-release/0.4.0rc1/"
    try:
        distribution = importlib.metadata.distribution(distribution_name)
        installed_version = distribution.version
        package_location = str(distribution.locate_file(""))
    except importlib.metadata.PackageNotFoundError:
        installed_version = None
        package_location = None
    try:
        workflow_version = importlib.metadata.version(workflow_distribution_name)
    except importlib.metadata.PackageNotFoundError:
        workflow_version = None
    return {
        "workflow_distribution": workflow_distribution_name,
        "workflow_version": workflow_version,
        "kernel_source": DEFAULT_KERNEL_SOURCE,
        "distribution": distribution_name,
        "installed_version": installed_version,
        "pypi_url": project_url,
        "package_location": package_location,
    }


def _write_pipeline_manifest(
    config: "AssignmentConfig",
    output_root: Path,
    *,
    status: str,
) -> Path:
    periods: list[dict[str, object]] = []
    for raw_period in config.active_time_periods:
        period = normalize_period_key(raw_period)
        period_root = output_root / period
        link_path = period_root / "link.csv"
        performance_path = period_root / "link_performance.csv"
        link_boundary_fields: list[str] = []
        performance_boundary_fields: list[str] = []
        if link_path.is_file():
            with link_path.open("r", encoding="utf-8-sig", newline="") as stream:
                link_header = next(csv.reader(stream), [])
            link_boundary_fields = [
                field
                for field in (
                    "t0_hour",
                    "t2_hour",
                    "t3_hour",
                    "qvdf_profile_mode",
                )
                if field in link_header
            ]
        if performance_path.is_file():
            with performance_path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                performance_header = next(csv.reader(stream), [])
            performance_boundary_fields = [
                field for field in ("t0", "t2", "t3") if field in performance_header
            ]
        periods.append(
            {
                "period": period,
                "range": config.period_times[len(periods)],
                "link_csv": str(link_path.resolve()) if link_path.is_file() else None,
                "link_performance_csv": (
                    str(performance_path.resolve())
                    if performance_path.is_file()
                    else None
                ),
                "converted_link_boundary_fields": link_boundary_fields,
                "performance_boundary_fields": performance_boundary_fields,
                "route_assignment_exists": (
                    period_root / "route_assignment.csv"
                ).is_file(),
            }
        )
    smoother_report_path = output_root / "qvdf_batch_report.json"
    override_manifest_path = output_root / "qvdf_override_audit" / "qvdf_override_manifest.json"
    smoother_status = None
    if config.qvdf_smoothing and smoother_report_path.is_file():
        try:
            smoother_status = json.loads(
                smoother_report_path.read_text(encoding="utf-8")
            ).get("status")
        except (OSError, json.JSONDecodeError):
            smoother_status = "unreadable"

    manifest = {
        "status": status,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": config.scenario_name or config.network_path.name,
        "scenario_input": str(config.network_path.resolve()),
        "output_root": str(output_root.resolve()),
        "taplite": _taplite_provenance(),
        "settings": {
            "iterations": config.iterations,
            "processors": config.processors,
            "route_output": config.route_output,
            "vehicle_output": config.vehicle_output,
            "legacy_unit_system_argument": config.unit_system,
            "link_input_units": TAPLITE_LINK_UNIT_CONTRACT,
            "vdf_type": config.vdf_type,
            "conversion_workers": config.conversion_workers,
            "conversion_reserve_cores": config.conversion_reserve_cores,
            "conversion_adaptive": config.conversion_adaptive,
            "conversion_cache": config.conversion_cache,
            "demand_output_format": config.demand_output_format,
            "qvdf_parameter_override": config.qvdf_parameter_override,
            "qvdf_override_dictionary": str(config.resolved_qvdf_override_dictionary),
            "qvdf_smoothing": config.qvdf_smoothing,
            "qvdf_smoother_workers": config.qvdf_smoother_workers,
            "qvdf_smoother_backup": config.qvdf_smoother_backup,
        },
        "qvdf_smoother": {
            "enabled": config.qvdf_smoothing,
            "status": smoother_status,
            "report": (
                str(smoother_report_path.resolve())
                if config.qvdf_smoothing and smoother_report_path.is_file()
                else None
            ),
        },
        "qvdf_parameter_override": {
            "enabled": config.qvdf_parameter_override,
            "dictionary": str(config.resolved_qvdf_override_dictionary),
            "manifest": (
                str(override_manifest_path.resolve())
                if override_manifest_path.is_file()
                else None
            ),
        },
        "periods": periods,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "RUN_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Assignment run manifest written: %s", manifest_path)
    return manifest_path


@dataclass
class AssignmentConfig:
    network_path: Path
    scenario_name: str | None = None
    ue_converge: float = 0.1
    dtalite_run_mode: str = "assignment"
    time_periods: list[str] = field(default_factory=lambda: ["am", "md", "pm", "nt"])
    period_times: list[str] = field(default_factory=lambda: ["0600_0900", "0900_1500", "1500_1900", "1900_0600"])
    output_files: list[str] = field(default_factory=lambda: [
        "log.txt",
        "summary_log.txt",
        "link_performance.csv",
    ])
    dtalite_assignment: bool = False
    network_conversion: bool = False
    demand_conversion: bool = False
    conversion_workers: int = 0
    conversion_reserve_cores: int = 1
    network_chunks: int = 0
    demand_chunks: int = 0
    conversion_adaptive: bool = True
    conversion_cache: bool = True
    conversion_cache_dir: Path | None = None
    qvdf_parameter_override: bool = True
    qvdf_override_dictionary: Path | None = None
    demand_output_format: str = "csv"
    work_dir: Path | None = None
    output_dir: Path | None = None
    iterations: int = 10
    processors: int = 20
    route_output: int = 0
    vehicle_output: int = 0
    period_start: int = 7
    period_end: int = 8
    unit_system: str = "metric"
    vdf_type: str = "qvdf"
    label: str | None = None
    kernel_source: str = DEFAULT_KERNEL_SOURCE
    inplace: bool = True
    dry_run: bool = False
    no_rename_columns: bool = False
    scenario_input_dir: Path | None = None
    demand_dir: Path | None = None
    write_legacy_outputs: bool = False
    qvdf_smoothing: bool = True
    qvdf_smoother_workers: str = "auto"
    qvdf_smoother_backup: bool = True
    run_gmns_readiness_check: bool = field(default=RUN_GMNS_READINESS_CHECK, init=False)
    write_assignment_summary: bool = field(default=WRITE_ASSIGNMENT_SUMMARY, init=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AssignmentConfig":
        parsed = data.copy()
        if "iteration" in parsed and "iterations" not in parsed:
            logger.warning("Using legacy assignment config key 'iteration' as 'iterations'.")
            parsed["iterations"] = parsed["iteration"]
        parsed.pop("iteration", None)

        if "metric_system" in parsed and "unit_system" not in parsed:
            logger.warning("Using legacy assignment config key 'metric_system' as 'unit_system'.")
            parsed["unit_system"] = "metric" if parsed["metric_system"] == 1 else "imperial"

        if "period_titles" in parsed and "time_periods" not in parsed:
            logger.warning("Using legacy assignment config key 'period_titles' as 'time_periods'.")
            parsed["time_periods"] = parsed["period_titles"]
        elif "period_titles" in parsed:
            logger.warning("Ignoring legacy assignment config key: period_titles")
        parsed.pop("period_titles", None)

        for legacy_key in LEGACY_ASSIGNMENT_KEYS:
            if legacy_key in parsed:
                logger.warning("Ignoring legacy assignment config key: %s", legacy_key)
                parsed.pop(legacy_key, None)

        for internal_key in INTERNAL_ASSIGNMENT_KEYS:
            if internal_key in parsed:
                logger.warning(
                    "Ignoring internal assignment config key: %s; controlled by src/dtalite4cube/internal_config.py",
                    internal_key,
                )
                parsed.pop(internal_key, None)

        known_fields = {field.name for field in dataclass_fields(cls)}
        unknown_keys = sorted(set(parsed) - known_fields)
        if unknown_keys:
            logger.warning("Ignoring unknown assignment config key(s): %s", ", ".join(unknown_keys))
            for key in unknown_keys:
                parsed.pop(key, None)

        parsed["network_path"] = Path(parsed["network_path"])
        for path_key in (
            "work_dir",
            "output_dir",
            "scenario_input_dir",
            "demand_dir",
            "conversion_cache_dir",
            "qvdf_override_dictionary",
        ):
            if parsed.get(path_key) is not None:
                parsed[path_key] = Path(parsed[path_key])
        return cls(**parsed)

    def validate(self) -> None:
        if not self.network_path.exists():
            raise FileNotFoundError(f"Scenario folder does not exist: {self.network_path}")

        if self.dtalite_run_mode not in {"assignment", "simulation"}:
            raise ValueError("dtalite_run_mode must be 'assignment' or 'simulation'.")

        if self.dtalite_run_mode == "simulation":
            raise NotImplementedError("dtalite_run_mode='simulation' is reserved but not implemented in this workflow.")

        if self.unit_system not in {"imperial", "metric"}:
            raise ValueError("unit_system compatibility argument must be 'imperial' or 'metric'.")
        if self.unit_system != "metric":
            logger.warning(
                "unit_system=%s is retained only for command compatibility and does not alter link.csv. "
                "TAPlite requires length/free_speed in meter/km/h plus vdf_length_mi/"
                "vdf_free_speed_mph in mile/mph.",
                self.unit_system,
            )

        if self.route_output not in {0, 1}:
            raise ValueError("route_output must be 0 or 1.")

        if self.vehicle_output not in {0, 1}:
            raise ValueError("vehicle_output must be 0 or 1.")

        if self.route_output or self.vehicle_output:
            logger.warning(
                "This NVTA package forces route_output=0 and vehicle_output=0."
            )
        self.route_output = 0
        self.vehicle_output = 0
        self.processors = min(max(1, int(self.processors)), MAX_GLOBAL_CORES)
        if self.conversion_workers > 0:
            self.conversion_workers = min(
                int(self.conversion_workers), MAX_GLOBAL_CORES
            )

        if self.vdf_type != "qvdf":
            raise ValueError("This NVTA package requires vdf_type='qvdf'.")

        if self.qvdf_parameter_override and self.network_conversion:
            if not self.resolved_qvdf_override_dictionary.is_file():
                raise FileNotFoundError(
                    "QVDF override dictionary does not exist: "
                    f"{self.resolved_qvdf_override_dictionary}"
                )

        if self.conversion_workers < 0:
            raise ValueError("conversion_workers must be zero (auto) or positive.")

        if self.conversion_reserve_cores < 0:
            raise ValueError("conversion_reserve_cores must be nonnegative.")

        if self.network_chunks < 0 or self.demand_chunks < 0:
            raise ValueError("network_chunks and demand_chunks must be zero (auto) or positive.")

        smoother_workers = str(self.qvdf_smoother_workers).strip().lower()
        if smoother_workers != "auto":
            try:
                worker_count = int(smoother_workers)
            except ValueError as exc:
                raise ValueError(
                    "qvdf_smoother_workers must be 'auto' or a positive integer."
                ) from exc
            if worker_count < 1:
                raise ValueError(
                    "qvdf_smoother_workers must be 'auto' or a positive integer."
                )
            smoother_workers = str(min(worker_count, MAX_GLOBAL_CORES))
        self.qvdf_smoother_workers = smoother_workers

        self.demand_output_format = self.demand_output_format.strip().lower()
        if self.demand_output_format not in {"csv", "binary", "both"}:
            raise ValueError("demand_output_format must be one of: csv, binary, both.")

        self.kernel_source = (self.kernel_source or DEFAULT_KERNEL_SOURCE).strip().lower()
        if self.kernel_source not in VALID_KERNEL_SOURCES:
            raise ValueError("kernel_source must be one of: " + ", ".join(sorted(VALID_KERNEL_SOURCES)))

        if len(self.active_time_periods) != len(self.period_times):
            raise ValueError("time_periods and period_times must have the same length.")

        if not self.active_time_periods:
            raise ValueError("time_periods cannot be empty.")

    @property
    def active_time_periods(self) -> list[str]:
        return [normalize_period_key(period) for period in self.time_periods]

    @property
    def resolved_qvdf_override_dictionary(self) -> Path:
        return self.qvdf_override_dictionary or DEFAULT_QVDF_OVERRIDE_DICTIONARY

    @property
    def length_unit(self) -> str:
        return TAPLITE_GENERIC_LENGTH_UNIT

    @property
    def speed_unit(self) -> str:
        return TAPLITE_GENERIC_SPEED_UNIT

    @property
    def metric_system(self) -> int:
        return 1


def run_network_conversion(config: AssignmentConfig) -> dict:
    from .cube2gmns import get_gmns_from_cube

    scenario_output_dir = resolve_scenario_output_dir(config)
    logger.info("Running network conversion into period folders under %s", scenario_output_dir)
    result = get_gmns_from_cube(
        str(config.network_path),
        config.active_time_periods,
        length_unit=config.length_unit,
        speed_unit=config.speed_unit,
        district_id_assignment=True,
        capacity_adjustment=False,
        vdf_type=config.vdf_type,
        output_dir=str(scenario_output_dir),
        period_folder_output=True,
        conversion_workers=config.conversion_workers,
        reserve_cores=config.conversion_reserve_cores,
        chunks_per_period=config.network_chunks,
        adaptive=config.conversion_adaptive,
        conversion_cache=config.conversion_cache,
        cache_dir=config.conversion_cache_dir,
    )
    logger.info(
        "Network conversion finished in %.3fs with %s worker(s)",
        result["elapsed_sec"],
        result["worker_plan"]["workers"],
    )
    result["mapping_validation"] = validate_network_mapping_outputs(
        result["outputs"]
    )
    logger.info(
        "Network mapping verified for %s links across %s period(s)",
        result["mapping_validation"]["qvdf_mapped_links"],
        len(result["mapping_validation"]["periods"]),
    )
    return result


def validate_network_mapping_outputs(
    outputs: list[dict[str, object]],
) -> dict[str, object]:
    """Verify that public network conversion emitted the complete mapping contract."""

    period_summaries: list[dict[str, object]] = []
    total_links = 0
    for output in outputs:
        period = str(output.get("period") or "(unknown)")
        output_path = Path(str(output.get("output") or ""))
        if not output_path.is_file():
            raise FileNotFoundError(
                f"Converted network output is missing for {period}: {output_path}"
            )

        with output_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = set(reader.fieldnames or [])
            missing_fields = sorted(set(NETWORK_MAPPING_SCHEMA_FIELDS) - fieldnames)
            if missing_fields:
                raise RuntimeError(
                    f"Network conversion mapping is incomplete for {period}; "
                    f"{output_path.name} is missing: {', '.join(missing_fields)}"
                )

            link_count = 0
            congestion_boundary_links = 0
            observed_speed_anchor_links = 0
            for row_number, row in enumerate(reader, start=2):
                link_count += 1
                link_id = row.get("link_id") or f"row {row_number}"
                for field in NETWORK_QVDF_VALUE_FIELDS:
                    text = str(row.get(field) or "").strip()
                    try:
                        value = float(text)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"Network conversion did not map numeric {field} for "
                            f"link {link_id} in {period}: {text or '(blank)'}"
                        ) from exc
                    if not math.isfinite(value):
                        raise RuntimeError(
                            f"Network conversion mapped non-finite {field} for "
                            f"link {link_id} in {period}: {text}"
                        )

                profile_mode = str(row.get("qvdf_profile_mode") or "").strip()
                try:
                    valid_profile_mode = int(float(profile_mode)) == 2
                except ValueError:
                    valid_profile_mode = False
                if not valid_profile_mode:
                    raise RuntimeError(
                        "Network conversion did not apply qvdf_profile_mode=2 for "
                        f"link {link_id} in {period}: {profile_mode or '(blank)'}"
                    )

                if any(str(row.get(field) or "").strip() for field in ("t0_hour", "t2_hour", "t3_hour")):
                    congestion_boundary_links += 1
                if any(
                    str(row.get(field) or "").strip()
                    for field in ("qvdf_start_speed_mph", "qvdf_end_speed_mph")
                ):
                    observed_speed_anchor_links += 1

        if link_count == 0:
            raise RuntimeError(
                f"Network conversion produced no links for {period}: {output_path}"
            )
        total_links += link_count
        period_summaries.append(
            {
                "period": period,
                "output": str(output_path),
                "links": link_count,
                "qvdf_mapped_links": link_count,
                "congestion_boundary_links": congestion_boundary_links,
                "observed_speed_anchor_links": observed_speed_anchor_links,
                "district_mapping_present": "district_id" in fieldnames,
            }
        )

    if not period_summaries:
        raise RuntimeError("Network conversion returned no period outputs to validate")
    return {
        "validated": True,
        "qvdf_mapped_links": total_links,
        "periods": period_summaries,
    }


def run_demand_conversion(config: AssignmentConfig) -> dict:
    from .omx2csv import get_gmns_demand_from_omx

    scenario_output_dir = resolve_scenario_output_dir(config)
    demand_dir = config.demand_dir or config.network_path
    logger.info("Running demand conversion into period folders under %s", scenario_output_dir)
    result = get_gmns_demand_from_omx(
        str(demand_dir),
        config.active_time_periods,
        output_base_dir=scenario_output_dir,
        period_folder_output=True,
        conversion_workers=config.conversion_workers,
        reserve_cores=config.conversion_reserve_cores,
        chunks_per_mode=config.demand_chunks,
        adaptive=config.conversion_adaptive,
        output_format=config.demand_output_format,
    )
    logger.info(
        "Demand conversion finished in %.3fs with %s worker(s)",
        result["elapsed_sec"],
        result["worker_plan"]["workers"],
    )
    return result


def resolve_scenario_input_dir(config: AssignmentConfig) -> Path:
    return config.scenario_input_dir or config.network_path


def resolve_scenario_output_dir(config: AssignmentConfig) -> Path:
    configured = config.output_dir
    if configured is not None:
        return configured if configured.is_absolute() else config.network_path / configured
    return config.network_path


def resolve_run_folder(config: AssignmentConfig, time_period: str | None = None) -> Path:
    configured_path = config.work_dir
    if configured_path is not None:
        base = configured_path if configured_path.is_absolute() else config.network_path / configured_path
    else:
        base = config.network_path / "dtalite_runs" / "latest"

    if time_period is not None:
        return base / time_period
    return base


def prepare_dtalite_period_folders(
    scenario_input_dir: Path,
    scenario_output_dir: Path,
    time_periods: list[str],
    *,
    demand_dir: Path | None = None,
    settings_overrides: dict | None = None,
    copy_workers: int = 1,
) -> dict[str, Path]:
    scenario_input_dir = Path(scenario_input_dir)
    scenario_output_dir = Path(scenario_output_dir)
    demand_source_dir = Path(demand_dir) if demand_dir is not None else scenario_input_dir
    binary_demand = bool((settings_overrides or {}).get("demand_format", 0))

    prepared_folders: dict[str, Path] = {}
    for raw_period in time_periods:
        period_key = normalize_period_key(raw_period)
        period_folder = scenario_output_dir / period_key
        period_folder.mkdir(parents=True, exist_ok=True)
        copy_pairs: list[tuple[Path, Path]] = []
        period_binary_paths: list[Path] = []

        period_node = period_folder / "node.csv"
        if not period_node.is_file():
            node_source = scenario_input_dir / "node.csv"
            if not node_source.is_file():
                raise FileNotFoundError(
                    f"Missing node.csv for {period_key}. Expected {period_node} "
                    f"or legacy source {node_source}"
                )
            copy_pairs.append((node_source, period_node))

        period_link = period_folder / "link.csv"
        if not period_link.is_file():
            link_source = scenario_input_dir / f"link_{period_key}.csv"
            direct_link_source = scenario_input_dir / period_key / "link.csv"
            if direct_link_source.is_file():
                link_source = direct_link_source
            elif not link_source.is_file():
                raise FileNotFoundError(
                    f"Missing period link file for {period_key}. Expected {period_link}, "
                    f"{direct_link_source}, or legacy source {link_source}"
                )
            copy_pairs.append((link_source, period_link))

        for mode in SUPPORTED_MODE_TYPES:
            demand_name = demand_file_name(mode, period_key)
            period_demand = period_folder / demand_name
            if binary_demand:
                period_binary = demand_binary_path(period_demand)
                if not period_binary.is_file():
                    direct_binary_source = demand_binary_path(
                        demand_source_dir / period_key / demand_name
                    )
                    root_binary_source = demand_binary_path(demand_source_dir / demand_name)
                    if direct_binary_source.is_file():
                        binary_source = direct_binary_source
                    elif root_binary_source.is_file():
                        binary_source = root_binary_source
                    else:
                        raise FileNotFoundError(
                            f"Missing binary demand for {period_key}: expected {period_binary}, "
                            f"{direct_binary_source}, or {root_binary_source}"
                        )
                    copy_pairs.append((binary_source, period_binary))
                period_binary_paths.append(period_binary)
            elif not period_demand.is_file():
                demand_source = demand_source_dir / demand_name
                direct_demand_source = demand_source_dir / period_key / demand_name
                if direct_demand_source.is_file():
                    demand_source = direct_demand_source
                elif not demand_source.is_file():
                    raise FileNotFoundError(
                        f"Missing demand file for {period_key}: expected {period_demand}, "
                        f"{direct_demand_source}, or legacy source {demand_source}"
                    )
                copy_pairs.append((demand_source, period_demand))

        copied = copy_files_parallel(
            copy_pairs,
            workers=copy_workers,
            preserve_metadata=True,
        )
        for source, target in copied:
            logger.info("Copied %s -> %s", source, target)
        if copied:
            logger.info(
                "Prepared %s period input copies with %s worker(s)",
                len(copied),
                resolved_workers(copy_workers, len(copy_pairs)),
            )
        for period_binary in period_binary_paths:
            inspect_dtab(period_binary)

        generate_dtalite_input_files(period_folder, period_key, overrides=settings_overrides)
        _validate_period_folder(period_folder)
        prepared_folders[period_key] = period_folder
        logger.info("Prepared DTALite period folder for %s: %s", period_key, period_folder)

    return prepared_folders


def remove_root_period_duplicates(scenario_output_dir: Path, time_periods: list[str]) -> list[Path]:
    scenario_output_dir = Path(scenario_output_dir)
    removed: list[Path] = []

    for file_name in ("link.csv", "demand.csv", "settings.csv", "mode_type.csv"):
        path = scenario_output_dir / file_name
        if path.is_file():
            path.unlink()
            removed.append(path)
    root_demand_binary = scenario_output_dir / "demand.bin"
    if root_demand_binary.is_file():
        root_demand_binary.unlink()
        removed.append(root_demand_binary)

    node_path = scenario_output_dir / "node.csv"
    if node_path.is_file() and all((scenario_output_dir / period / "node.csv").is_file() for period in time_periods):
        node_path.unlink()
        removed.append(node_path)

    for period in time_periods:
        period_folder = scenario_output_dir / period
        period_file_pairs = {
            f"link_{period}.csv": period_folder / "link.csv",
            f"settings_{period}.csv": period_folder / "settings.csv",
            f"mode_type_{period}.csv": period_folder / "mode_type.csv",
            f"demand_{period}.csv": period_folder / "demand.csv",
        }
        for mode in SUPPORTED_MODE_TYPES:
            demand_name = demand_file_name(mode, period)
            period_file_pairs[demand_name] = period_folder / demand_name
            period_file_pairs[demand_binary_path(demand_name).name] = demand_binary_path(
                period_folder / demand_name
            )

        for root_name, period_path in period_file_pairs.items():
            root_path = scenario_output_dir / root_name
            if root_path.is_file() and period_path.is_file():
                root_path.unlink()
                removed.append(root_path)

    for path in removed:
        logger.info("Removed root-level generated duplicate: %s", path)

    return removed


def _validate_period_folder(period_folder: Path) -> None:
    for required_file in ("node.csv", "link.csv", "settings.csv", "mode_type.csv"):
        path = period_folder / required_file
        if not path.is_file():
            raise FileNotFoundError(f"Prepared period folder is missing {required_file}: {period_folder}")

    with (period_folder / "settings.csv").open("r", newline="", encoding="utf-8") as f:
        settings_rows = list(csv.DictReader(f))
        if len(settings_rows) != 1:
            raise ValueError(f"settings.csv must have exactly 2 lines: {period_folder / 'settings.csv'}")
    binary_demand = int(float(settings_rows[0].get("demand_format") or 0)) == 1

    with (period_folder / "mode_type.csv").open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 6:
        raise ValueError(f"mode_type.csv must have exactly 6 mode rows: {period_folder / 'mode_type.csv'}")

    missing = []
    for row in rows:
        demand_path = period_folder / row["demand_file"]
        if binary_demand:
            binary_path = demand_binary_path(demand_path)
            if binary_path.is_file():
                inspect_dtab(binary_path)
                continue
        elif demand_path.is_file():
            continue
        missing.append(row["demand_file"])
    if missing:
        raise FileNotFoundError(
            f"mode_type.csv in {period_folder} references missing demand file(s): {', '.join(missing)}"
        )


def copy_route_assignment_to_columns(work_dir: Path) -> None:
    route_assignment = work_dir / "route_assignment.csv"
    columns = work_dir / "columns.csv"
    shutil.copy2(route_assignment, columns)
    logger.info("Copied %s to %s", route_assignment.name, columns.name)


def run_reproducible_dtalite(
    *,
    config: AssignmentConfig,
    source_network_path: Path,
    work_dir: Path,
    label: str,
    time_period: str | None = None,
    readiness_result: ReadinessResult | None = None,
) -> Path:
    logger.info(
        "Starting direct period-folder TAPlite run: source=%s work_dir=%s",
        source_network_path,
        work_dir,
    )
    preflight_info = preflight(source_network_path)
    run_folder = source_network_path if config.inplace else work_dir

    if config.dry_run:
        logger.info("DTALite dry_run=True; preflight passed and execution is skipped.")
        return run_folder

    try:
        stage_inputs(
            source_network_path,
            run_folder,
            iterations=config.iterations,
            processors=config.processors,
            route_output=config.route_output,
            vehicle_output=config.vehicle_output,
            period_start=config.period_start,
            period_end=config.period_end,
            metric_system=config.metric_system,
            copy_workers=config.conversion_workers,
        )
        elapsed, log = run_dtalite(run_folder, kernel_source=config.kernel_source)
        verify_info = verify_outputs(run_folder, route_output=config.route_output)
        convergence = parse_convergence(log, run_folder)
    except Exception as exc:
        _write_assignment_failure_metadata(
            run_folder / "RUN_FAILURE.json",
            period_dir=source_network_path,
            error=exc,
        )
        raise

    if config.route_output and not config.no_rename_columns:
        copy_route_assignment_to_columns(run_folder)

    args_used = {
        "iterations": config.iterations,
        "processors": config.processors,
        "route_output": config.route_output,
        "vehicle_output": config.vehicle_output,
        "period_start": config.period_start,
        "period_end": config.period_end,
        "unit_system": config.unit_system,
        "id_handling": "taplite_native_internal",
    }
    write_run_card(
        run_folder,
        source_network_path,
        label,
        preflight_info,
        elapsed,
        convergence,
        verify_info,
        args_used,
    )

    if config.write_assignment_summary and time_period is not None:
        _write_assignment_summary(
            run_folder / "RUN_SUMMARY.md",
            config=config,
            time_period=time_period,
            preflight_info=preflight_info,
            run_status="success",
            convergence=convergence,
            dtalite_log=run_folder / "dtalite_run.log",
            readiness_result=readiness_result,
        )
        logger.info("summary written: %s", run_folder / "RUN_SUMMARY.md")

    if time_period is not None and config.write_legacy_outputs:
        output_files = [
            *config.output_files,
            "od_performance.csv",
            "dtalite_run.log",
            "summary_log_file.txt",
            "RUN_CARD.md",
        ]
        if config.route_output:
            output_files.extend(["route_assignment.csv", "columns.csv"])

        save_period_outputs(
            network_path=config.network_path,
            source_dir=run_folder,
            time_period=time_period,
            output_files=output_files,
            extra_files=[],
            copy_workers=config.conversion_workers,
        )

    return run_folder


def _write_assignment_summary(
    path: Path,
    *,
    config: AssignmentConfig,
    time_period: str,
    preflight_info: dict[str, object],
    run_status: str,
    convergence: dict[str, object],
    dtalite_log: Path,
    readiness_result: ReadinessResult | None,
) -> None:
    counts = preflight_info.get("counts", {})
    readiness_log = readiness_result.log_path if readiness_result else None
    convergence_lines = [f"  - {key}: {value}" for key, value in convergence.items()] or ["  - not available"]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# DTALite Assignment Summary",
                "",
                f"- Scenario: {config.scenario_name or config.network_path.name}",
                f"- Period: {time_period}",
                f"- Node count: {counts.get('node_rows', 'not available')}",
                f"- Link count: {counts.get('link_rows', 'not available')}",
                f"- Demand file count: {counts.get('demand_files', 'not available')}",
                "- ID handling: TAPlite native internal renumbering",
                "- Output ID handling: TAPlite writes assignment outputs with source GMNS IDs",
                f"- DTALite run status: {run_status}",
                f"- Full DTALite log: {dtalite_log}",
                f"- GMNS readiness log: {readiness_log if readiness_log else 'not run'}",
                "",
                "## Convergence",
                *convergence_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_assignment_failure_metadata(
    path: Path,
    *,
    period_dir: Path,
    error: Exception,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "period_folder": str(period_dir),
        "run_folder": str(path.parent),
        "status": "failed",
        "error_type": type(error).__name__,
        "error": str(error),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.warning("Failure metadata written to: %s", path)
def save_period_outputs(
    *,
    network_path: Path,
    source_dir: Path,
    time_period: str,
    output_files: Iterable[str],
    extra_files: Iterable[str],
    copy_workers: int = 1,
) -> None:
    output_dir = network_path / "Outputs" / "DTALite"
    output_dir.mkdir(parents=True, exist_ok=True)

    period_suffix = f"_{time_period}"

    copy_pairs: list[tuple[Path, Path]] = []
    for file_name in output_files:
        source_path = source_dir / file_name
        if not source_path.exists():
            logger.warning("Expected output file not found: %s", source_path)
            continue

        new_name = f"{source_path.stem}{period_suffix}{source_path.suffix}"
        target_path = output_dir / new_name
        copy_pairs.append((source_path, target_path))

    for file_name in extra_files:
        source_path = network_path / file_name
        if not source_path.exists():
            logger.warning("Extra file not found: %s", source_path)
            continue

        target_path = output_dir / source_path.name
        copy_pairs.append((source_path, target_path))

    copied = copy_files_parallel(
        copy_pairs,
        workers=copy_workers,
        preserve_metadata=False,
    )
    for _, target_path in copied:
        logger.info("Saved %s", target_path)
    if copied:
        logger.info(
            "Saved %s output files with %s copy worker(s)",
            len(copied),
            resolved_workers(copy_workers, len(copy_pairs)),
        )


def run_assignment_pipeline(config: AssignmentConfig) -> bool:
    config.validate()
    configure_time_period_hours(config.active_time_periods, config.period_times)
    scenario_input_dir = resolve_scenario_input_dir(config)
    scenario_output_dir = resolve_scenario_output_dir(config)
    # logger.info("Running assignment pipeline for: %s", config.network_path)
    logger.info(
        "Running assignment pipeline for scenario=%s path=%s",
        config.scenario_name or "<unnamed>",
        config.network_path,
    )
    conversion_profile = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenario": config.scenario_name or config.network_path.name,
        "periods": config.active_time_periods,
        "conversion_workers": config.conversion_workers,
        "conversion_reserve_cores": config.conversion_reserve_cores,
        "network_chunks": config.network_chunks,
        "demand_chunks": config.demand_chunks,
        "demand_output_format": config.demand_output_format,
        "conversion_adaptive": config.conversion_adaptive,
        "conversion_cache": config.conversion_cache,
        "conversion_cache_dir": (
            str(config.conversion_cache_dir)
            if config.conversion_cache_dir is not None
            else None
        ),
        "stages": {},
    }

    if config.network_conversion:
        conversion_profile["stages"]["network"] = run_network_conversion(config)
        if config.qvdf_parameter_override:
            from .qvdf_overrides import apply_overrides

            override_audit_root = scenario_output_dir / "qvdf_override_audit"
            backup_root = override_audit_root / "pre-overlay-network"
            if backup_root.exists():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_root = override_audit_root / f"pre-overlay-network-{timestamp}"
            logger.info(
                "Applying calibrated node-pair QVDF overrides from %s",
                config.resolved_qvdf_override_dictionary,
            )
            override_result = apply_overrides(
                scenario_output_dir,
                config.resolved_qvdf_override_dictionary,
                backup_root,
                expected_profile_mode=2,
                periods=config.active_time_periods,
            )
            conversion_profile["stages"]["qvdf_parameter_override"] = override_result
            skipped_periods = override_result.get("skipped_periods", [])
            if skipped_periods:
                logger.info(
                    "QVDF override has no calibrated dictionary periods for %s; "
                    "normal mapped values were retained",
                    ", ".join(str(period) for period in skipped_periods),
                )

    if config.demand_conversion:
        conversion_profile["stages"]["demand"] = run_demand_conversion(config)

    if conversion_profile["stages"]:
        scenario_output_dir.mkdir(parents=True, exist_ok=True)
        profile_path = scenario_output_dir / "CONVERSION_PROFILE.json"
        profile_path.write_text(json.dumps(conversion_profile, indent=2) + "\n", encoding="utf-8")
        logger.info("Conversion profile written: %s", profile_path)

    if not config.dtalite_assignment and not (
        config.network_conversion and config.demand_conversion
    ):
        logger.info(
            "Partial conversion complete; skipping DTALite period preparation because "
            "assignment is disabled."
        )
        _write_pipeline_manifest(config, scenario_output_dir, status="PARTIAL_CONVERSION")
        return False

    prepared_period_folders = prepare_dtalite_period_folders(
        scenario_input_dir=scenario_input_dir,
        scenario_output_dir=scenario_output_dir,
        time_periods=config.active_time_periods,
        demand_dir=config.demand_dir,
        settings_overrides={
            "number_of_iterations": config.iterations,
            "number_of_processors": config.processors,
            "route_output": config.route_output,
            "vehicle_output": config.vehicle_output,
            "demand_format": 1 if config.demand_output_format in {"binary", "both"} else 0,
        },
        copy_workers=config.conversion_workers,
    )
    remove_root_period_duplicates(scenario_output_dir, config.active_time_periods)
    logger.info("conversion complete")

    readiness_results: dict[str, ReadinessResult] = {}
    if config.run_gmns_readiness_check:
        for time_period, period_source in prepared_period_folders.items():
            readiness_results[time_period] = run_period_readiness_check(period_source)
        logger.info("readiness check complete")

    if not config.dtalite_assignment:
        logger.info("DTALite assignment is disabled for: %s", config.network_path)
        _write_pipeline_manifest(config, scenario_output_dir, status="CONVERSION_COMPLETE")
        return False

    for time_period, period_source in prepared_period_folders.items():
        period_label = f"{config.label or config.scenario_name or 'scenario'}_{time_period}"
        run_reproducible_dtalite(
            config=config,
            source_network_path=period_source,
            work_dir=resolve_run_folder(config, time_period=time_period),
            label=period_label,
            time_period=time_period,
            readiness_result=readiness_results.get(time_period),
        )

    if config.dry_run:
        logger.info("QVDF smoothing skipped because this is a dry run.")
    elif config.qvdf_smoothing:
        logger.info(
            "Running QVDF speed smoother before link-performance aggregation: %s",
            scenario_output_dir,
        )
        smoother_report = smooth_scenario(
            SmootherConfig(
                scenario_dir=scenario_output_dir,
                periods=config.active_time_periods,
                workers=config.qvdf_smoother_workers,
                backup=config.qvdf_smoother_backup,
                report=scenario_output_dir / "qvdf_batch_report.json",
            )
        )
        if smoother_report.get("status") != "complete":
            raise RuntimeError(
                "QVDF smoother did not complete successfully; aggregation is blocked."
            )
        logger.info(
            "QVDF smoothing complete for %s unique links; report: %s",
            smoother_report.get("staging", {}).get("unique_links", "unknown"),
            scenario_output_dir / "qvdf_batch_report.json",
        )
    else:
        logger.warning(
            "QVDF smoothing is disabled; downstream aggregation will use raw "
            "TAPlite five-minute speeds."
        )

    logger.info("Finished DTALite assignment pipeline for: %s", config.network_path)
    _write_pipeline_manifest(config, scenario_output_dir, status="PASS")
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DTALite4Cube assignment pipeline for a scenario."
    )

    parser.add_argument("--network-path", required=True, help="Scenario folder path")
    parser.add_argument("--dtalite-run-mode", choices=["assignment", "simulation"], default="assignment")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--scenario-input-dir", type=Path)
    parser.add_argument("--demand-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--processors", type=int, default=20)
    parser.add_argument("--route-output", type=int, choices=[0], default=0)
    parser.add_argument("--vehicle-output", type=int, choices=[0], default=0)
    parser.add_argument("--period-start", type=int, default=7)
    parser.add_argument("--period-end", type=int, default=8)
    parser.add_argument(
        "--unit-system",
        choices=["imperial", "metric"],
        default="metric",
        help="Deprecated compatibility flag; TAPlite's fixed mixed-unit schema is always used.",
    )
    parser.add_argument(
        "--metric-system",
        type=int,
        choices=[0, 1],
        help="Legacy compatibility alias: 1=metric, 0=imperial; output units remain fixed.",
    )
    parser.add_argument("--vdf-type", choices=["qvdf"], default="qvdf")
    parser.add_argument("--label")
    parser.add_argument("--kernel-source", choices=sorted(VALID_KERNEL_SOURCES), default=DEFAULT_KERNEL_SOURCE)

    parser.add_argument("--network-conversion", action="store_true")
    parser.add_argument("--demand-conversion", action="store_true")
    parser.add_argument("--dtalite-assignment", action="store_true")
    parser.add_argument("--conversion-workers", type=int, default=0)
    parser.add_argument("--conversion-reserve-cores", type=int, default=1)
    parser.add_argument("--network-chunks", type=int, default=0)
    parser.add_argument("--demand-chunks", type=int, default=0)
    parser.add_argument("--demand-output-format", choices=["csv", "binary", "both"], default="csv")
    parser.add_argument("--conversion-adaptive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--conversion-cache", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--conversion-cache-dir", type=Path)
    parser.add_argument(
        "--qvdf-parameter-override",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--qvdf-override-dictionary", type=Path)
    parser.add_argument(
        "--qvdf-smoothing",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--qvdf-smoother-workers", default="auto")
    parser.add_argument(
        "--qvdf-smoother-backup",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--isolated-work-dir", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-rename-columns", action="store_true")
    parser.add_argument("--time-periods", nargs="+", default=["am", "md", "pm", "nt"])
    parser.add_argument(
        "--period-times",
        nargs="+",
        default=["0600_0900", "0900_1500", "1500_1900", "1900_0600"],
    )

    return parser


def main() -> None:
    install_root_log_capture("dtalite4cube_runner")
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    parser = build_arg_parser()
    args = parser.parse_args()

    config = AssignmentConfig(
        network_path=Path(args.network_path),
        dtalite_run_mode=args.dtalite_run_mode,
        time_periods=args.time_periods,
        period_times=args.period_times,
        dtalite_assignment=args.dtalite_assignment,
        network_conversion=args.network_conversion,
        demand_conversion=args.demand_conversion,
        conversion_workers=args.conversion_workers,
        conversion_reserve_cores=args.conversion_reserve_cores,
        network_chunks=args.network_chunks,
        demand_chunks=args.demand_chunks,
        demand_output_format=args.demand_output_format,
        conversion_adaptive=args.conversion_adaptive,
        conversion_cache=args.conversion_cache,
        conversion_cache_dir=args.conversion_cache_dir,
        qvdf_parameter_override=args.qvdf_parameter_override,
        qvdf_override_dictionary=args.qvdf_override_dictionary,
        qvdf_smoothing=args.qvdf_smoothing,
        qvdf_smoother_workers=args.qvdf_smoother_workers,
        qvdf_smoother_backup=args.qvdf_smoother_backup,
        work_dir=args.work_dir,
        output_dir=args.output_dir,
        scenario_input_dir=args.scenario_input_dir,
        demand_dir=args.demand_dir,
        iterations=args.iterations,
        processors=args.processors,
        route_output=args.route_output,
        vehicle_output=args.vehicle_output,
        period_start=args.period_start,
        period_end=args.period_end,
        unit_system=args.unit_system if args.metric_system is None else ("metric" if args.metric_system == 1 else "imperial"),
        vdf_type=args.vdf_type,
        label=args.label,
        kernel_source=args.kernel_source,
        inplace=not args.isolated_work_dir,
        dry_run=args.dry_run,
        no_rename_columns=args.no_rename_columns,
    )

    run_assignment_pipeline(config)


if __name__ == "__main__":
    main()

