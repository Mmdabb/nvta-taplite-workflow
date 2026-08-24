from __future__ import annotations

import atexit
import csv
import os
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import openmatrix as omx

try:
    from .dtab import (
        demand_binary_path,
        merge_dtab_parts,
        write_dtab_file,
        write_dtab_record_part,
    )
    from .parallel_utils import (
        choose_chunks_per_group,
        choose_worker_plan,
        chunk_ranges,
    )
    from .settings.dtalite_settings_config import DEMAND_LANE_USES, demand_file_name
except ImportError:
    from dtab import (
        demand_binary_path,
        merge_dtab_parts,
        write_dtab_file,
        write_dtab_record_part,
    )
    from parallel_utils import choose_chunks_per_group, choose_worker_plan, chunk_ranges
    from settings.dtalite_settings_config import DEMAND_LANE_USES, demand_file_name


DEMAND_HEADER = b"o_zone_id,d_zone_id,volume\r\n"
VALID_DEMAND_OUTPUT_FORMATS = {"csv", "binary", "both"}
_WORKER_OMX_FILES: dict[str, object] = {}


def _close_worker_omx_files() -> None:
    for matrix_file in _WORKER_OMX_FILES.values():
        try:
            matrix_file.close()
        except Exception:
            pass
    _WORKER_OMX_FILES.clear()


def _initialize_demand_worker() -> None:
    # These tasks use NumPy indexing/nonzero, not BLAS. Keep third-party numeric
    # runtimes from creating a nested thread pool in every process.
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
    _close_worker_omx_files()
    atexit.register(_close_worker_omx_files)


def _worker_omx_file(path: str):
    matrix_file = _WORKER_OMX_FILES.get(path)
    if matrix_file is None:
        matrix_file = omx.open_file(path, "r")
        _WORKER_OMX_FILES[path] = matrix_file
    return matrix_file


def export_matrix_data(
    output_dir,
    time_period,
    lane_uses,
    matrix_file,
    *,
    output_format="csv",
):
    """Original serial exporter retained as the low-overhead fallback."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for lu in lane_uses:
        matrix_name = f"{time_period}_{lu.upper()}s"
        arr = np.asarray(matrix_file[matrix_name])

        output_file_name = demand_file_name(lu, time_period)
        csv_output = output_dir / output_file_name
        binary_output = demand_binary_path(csv_output)

        positive_mask = arr > 0
        o_idx, d_idx = np.nonzero(positive_mask)
        volumes = arr[o_idx, d_idx]

        if output_format in {"csv", "both"}:
            with csv_output.open("w", newline="", encoding="utf-8") as df:
                f_csv = csv.writer(df)
                f_csv.writerow(["o_zone_id", "d_zone_id", "volume"])
                f_csv.writerows(zip(o_idx + 1, d_idx + 1, volumes))
        if output_format in {"binary", "both"}:
            write_dtab_file(binary_output, o_idx + 1, d_idx + 1, volumes)

        emitted = []
        if output_format in {"csv", "both"}:
            emitted.append(csv_output.name)
        if output_format in {"binary", "both"}:
            emitted.append(binary_output.name)
        print(f"Wrote {len(volumes):,} rows to {', '.join(emitted)}")
        results.append(
            {
                "mode": lu,
                "matrix": matrix_name,
                "output": str(csv_output if output_format != "binary" else binary_output),
                "csv_output": str(csv_output) if output_format in {"csv", "both"} else None,
                "binary_output": (
                    str(binary_output)
                    if output_format in {"binary", "both"}
                    else None
                ),
                "rows": int(len(volumes)),
            }
        )
    return results


def _discover_period_files(demand_path: Path, time_period_list: list[str]) -> list[dict]:
    period_keys = [period.upper() for period in time_period_list]
    discovered = []
    for omx_path in sorted(demand_path.iterdir()):
        file_name_lower = omx_path.name.lower()
        if omx_path.suffix.lower() != ".omx" or "transit" in file_name_lower:
            continue
        matching_periods = [period for period in period_keys if period in omx_path.stem.upper()]
        for period in matching_periods:
            with omx.open_file(str(omx_path)) as matrix_file:
                matrix_names = set(matrix_file.list_matrices())
                shape = tuple(int(value) for value in matrix_file.shape())
            required = [f"{period}_{mode.upper()}s" for mode in DEMAND_LANE_USES]
            missing = [name for name in required if name not in matrix_names]
            if missing:
                raise KeyError(
                    f"{omx_path} is missing expected {period} matrix/matrices: {', '.join(missing)}"
                )
            discovered.append(
                {
                    "period": period,
                    "path": omx_path,
                    "shape": shape,
                    "matrix_names": required,
                }
            )
    return discovered


def _export_matrix_chunk(task: dict) -> dict:
    """Read one OMX row slice and write one headerless CSV part."""

    started = time.perf_counter()
    matrix_file = _worker_omx_file(task["omx_path"])
    arr = np.asarray(
        matrix_file[task["matrix_name"]][task["row_start"] : task["row_stop"], :]
    )

    positive_mask = arr > 0
    local_o_idx, d_idx = np.nonzero(positive_mask)
    volumes = arr[local_o_idx, d_idx]
    o_idx = local_o_idx + task["row_start"] + 1

    csv_part_path = (
        Path(task["csv_part_path"])
        if task.get("csv_part_path")
        else None
    )
    binary_part_path = (
        Path(task["binary_part_path"])
        if task.get("binary_part_path")
        else None
    )
    if csv_part_path is not None:
        with csv_part_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerows(zip(o_idx, d_idx + 1, volumes))
    if binary_part_path is not None:
        write_dtab_record_part(
            binary_part_path,
            o_idx,
            d_idx + 1,
            volumes,
        )

    return {
        "period": task["period"],
        "mode": task["mode"],
        "matrix_name": task["matrix_name"],
        "output_path": task["output_path"],
        "csv_part_path": str(csv_part_path) if csv_part_path is not None else None,
        "binary_part_path": (
            str(binary_part_path)
            if binary_part_path is not None
            else None
        ),
        "row_start": task["row_start"],
        "row_stop": task["row_stop"],
        "rows": int(len(volumes)),
        "elapsed_sec": time.perf_counter() - started,
    }


def _merge_demand_parts(output_path: Path, parts: list[dict]) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(parts, key=lambda item: item["row_start"])
    with output_path.open("wb") as target:
        target.write(DEMAND_HEADER)
        for part in ordered:
            with Path(part["csv_part_path"]).open("rb") as source:
                shutil.copyfileobj(source, target, length=1 << 20)
    return sum(int(part["rows"]) for part in ordered)


def get_gmns_demand_from_omx(
    demand_dir,
    time_period_list,
    output_base_dir=None,
    period_folder_output=True,
    *,
    conversion_workers=0,
    reserve_cores=1,
    chunks_per_mode=0,
    adaptive=True,
    output_format="csv",
):
    """Convert OMX demand with a bounded flat period/mode/chunk process pool."""

    started = time.perf_counter()
    demand_path = Path(demand_dir)
    output_root = Path(output_base_dir) if output_base_dir is not None else demand_path
    output_root.mkdir(parents=True, exist_ok=True)
    output_format = str(output_format).strip().lower()
    if output_format not in VALID_DEMAND_OUTPUT_FORMATS:
        raise ValueError(
            "output_format must be one of: "
            + ", ".join(sorted(VALID_DEMAND_OUTPUT_FORMATS))
        )

    lane_uses = list(DEMAND_LANE_USES)
    discovered = _discover_period_files(demand_path, list(time_period_list))
    if not discovered:
        raise FileNotFoundError(
            f"No non-transit OMX files matched requested periods {list(time_period_list)} in {demand_path}"
        )

    matrix_rows = max(item["shape"][0] for item in discovered)
    matrix_cells = sum(
        item["shape"][0] * item["shape"][1] * len(lane_uses) for item in discovered
    )
    group_count = len(discovered) * len(lane_uses)
    max_chunks = max(1, (matrix_rows + 127) // 128)
    potential_tasks = group_count * max_chunks
    plan = choose_worker_plan(
        requested_workers=conversion_workers,
        reserve_cores=reserve_cores,
        task_count=potential_tasks,
        work_items=matrix_cells,
        min_work_items_per_worker=1_000_000,
        adaptive=adaptive,
    )
    chunks = choose_chunks_per_group(
        items_per_group=matrix_rows,
        group_count=group_count,
        workers=plan.workers,
        requested_chunks=chunks_per_mode,
        min_chunk_items=128,
    )

    print(
        "Demand conversion plan: "
        f"periods={len(discovered)}, modes={len(lane_uses)}, chunks_per_mode={chunks}, "
        f"tasks={group_count * chunks}, workers={plan.workers}; {plan.reason}"
    )

    # Avoid process startup and repeated HDF5 opens when the scheduler decides
    # there is not enough useful parallel capacity.
    if plan.workers == 1:
        outputs = []
        for item in discovered:
            period = item["period"]
            output_dir = output_root / period.lower() if period_folder_output else output_root
            print(f"Processing file: {item['path'].name} for time period: {period}")
            period_started = time.perf_counter()
            with omx.open_file(str(item["path"])) as matrix_file:
                print("Shape:", matrix_file.shape())
                print("Number of tables:", len(matrix_file))
                print("Table names:", matrix_file.list_matrices())
                outputs.extend(
                    export_matrix_data(
                        output_dir,
                        period,
                        lane_uses,
                        matrix_file,
                        output_format=output_format,
                    )
                )
            print(
                f"Total wall time for {period}: "
                f"{time.perf_counter() - period_started:.3f} seconds"
            )
        return {
            "stage": "demand",
            "parallel": False,
            "worker_plan": plan.as_dict(),
            "output_format": output_format,
            "chunks_per_mode": 1,
            "task_count": group_count,
            "outputs": outputs,
            "elapsed_sec": time.perf_counter() - started,
        }

    tasks = []
    output_groups: dict[str, list[dict]] = {}
    with tempfile.TemporaryDirectory(prefix=".demand_parts_", dir=output_root) as temp_dir:
        temp_root = Path(temp_dir)
        task_number = 0
        for item in discovered:
            period = item["period"]
            output_dir = output_root / period.lower() if period_folder_output else output_root
            ranges = chunk_ranges(item["shape"][0], chunks)
            for mode in lane_uses:
                matrix_name = f"{period}_{mode.upper()}s"
                output_path = output_dir / demand_file_name(mode, period)
                for chunk_number, (row_start, row_stop) in enumerate(ranges):
                    csv_part_path = (
                        temp_root / f"{period}_{mode}_{chunk_number:04d}.csv"
                        if output_format in {"csv", "both"}
                        else None
                    )
                    binary_part_path = (
                        temp_root / f"{period}_{mode}_{chunk_number:04d}.binpart"
                        if output_format in {"binary", "both"}
                        else None
                    )
                    tasks.append(
                        {
                            "period": period,
                            "mode": mode,
                            "matrix_name": matrix_name,
                            "omx_path": str(item["path"]),
                            "output_path": str(output_path),
                            "csv_part_path": (
                                str(csv_part_path)
                                if csv_part_path is not None
                                else None
                            ),
                            "binary_part_path": (
                                str(binary_part_path)
                                if binary_part_path is not None
                                else None
                            ),
                            "row_start": row_start,
                            "row_stop": row_stop,
                        }
                    )
                    task_number += 1

        completed = 0
        with ProcessPoolExecutor(
            max_workers=plan.workers,
            initializer=_initialize_demand_worker,
        ) as executor:
            futures = [executor.submit(_export_matrix_chunk, task) for task in tasks]
            for future in as_completed(futures):
                result = future.result()
                output_groups.setdefault(result["output_path"], []).append(result)
                completed += 1
                print(
                    f"Demand chunk {completed}/{len(tasks)} complete: "
                    f"{result['period']} {result['mode']} rows "
                    f"{result['row_start']}:{result['row_stop']} -> {result['rows']:,} records"
                )

        outputs = []
        for output_path_text, parts in sorted(output_groups.items()):
            csv_output_path = Path(output_path_text)
            binary_output_path = demand_binary_path(csv_output_path)
            row_count = sum(int(part["rows"]) for part in parts)
            if output_format in {"csv", "both"}:
                row_count = _merge_demand_parts(csv_output_path, parts)
            if output_format in {"binary", "both"}:
                binary_count = merge_dtab_parts(binary_output_path, parts)
                if binary_count != row_count:
                    raise RuntimeError(
                        f"CSV/DTAB row-count mismatch for {csv_output_path.name}: "
                        f"{row_count} vs {binary_count}"
                    )
            first = parts[0]
            emitted = []
            if output_format in {"csv", "both"}:
                emitted.append(csv_output_path.name)
            if output_format in {"binary", "both"}:
                emitted.append(binary_output_path.name)
            print(f"Wrote {row_count:,} rows to {', '.join(emitted)}")
            outputs.append(
                {
                    "period": first["period"],
                    "mode": first["mode"],
                    "matrix": first["matrix_name"],
                    "output": str(
                        csv_output_path
                        if output_format != "binary"
                        else binary_output_path
                    ),
                    "csv_output": (
                        str(csv_output_path)
                        if output_format in {"csv", "both"}
                        else None
                    ),
                    "binary_output": (
                        str(binary_output_path)
                        if output_format in {"binary", "both"}
                        else None
                    ),
                    "rows": row_count,
                }
            )

    return {
        "stage": "demand",
        "parallel": True,
        "worker_plan": plan.as_dict(),
        "output_format": output_format,
        "chunks_per_mode": chunks,
        "task_count": len(tasks),
        "outputs": outputs,
        "elapsed_sec": time.perf_counter() - started,
    }
