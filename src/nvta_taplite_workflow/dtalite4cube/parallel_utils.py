"""Bounded process-planning helpers for Cube/OMX conversion.

The conversion pipeline has several independent dimensions (period, mode, and
chunk), but it deliberately uses one flat process pool.  Nested pools multiply
process counts and memory use, especially on Windows where every worker is
spawned.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass


MAX_GLOBAL_CORES = 20


@dataclass(frozen=True)
class WorkerPlan:
    workers: int
    requested_workers: int
    logical_cores: int
    physical_cores: int
    reserve_cores: int
    idle_physical_cores: int | None
    adaptive: bool
    reason: str

    def as_dict(self) -> dict[str, int | bool | str | None]:
        return asdict(self)


def _cpu_counts() -> tuple[int, int]:
    logical = max(1, os.cpu_count() or 1)
    physical = 0
    try:
        import psutil

        physical = int(psutil.cpu_count(logical=False) or 0)
    except (ImportError, OSError, ValueError):
        pass

    if physical < 1:
        # A conservative fallback for the common SMT/Hyper-Threading case.
        physical = max(1, logical // 2) if logical >= 4 else logical
    return logical, physical


def _idle_physical_cores(logical: int, physical: int) -> int | None:
    try:
        import psutil

        utilization = psutil.cpu_percent(interval=0.25, percpu=True)
    except (ImportError, OSError, ValueError):
        return None

    if not utilization:
        return None

    idle_logical = sum(1 for value in utilization if value < 35.0)
    threads_per_core = max(1.0, logical / max(1, physical))
    return max(0, int(idle_logical / threads_per_core))


def choose_worker_plan(
    *,
    requested_workers: int,
    reserve_cores: int,
    task_count: int,
    work_items: int,
    min_work_items_per_worker: int,
    adaptive: bool,
    logical_cores: int | None = None,
    physical_cores: int | None = None,
    idle_physical_cores: int | None = None,
) -> WorkerPlan:
    """Choose a safe global process count, falling back to serial when needed."""

    if requested_workers < 0:
        raise ValueError("requested_workers must be zero (auto) or positive")
    if reserve_cores < 0:
        raise ValueError("reserve_cores must be nonnegative")
    if task_count < 1:
        raise ValueError("task_count must be positive")
    if min_work_items_per_worker < 1:
        raise ValueError("min_work_items_per_worker must be positive")

    detected_logical, detected_physical = _cpu_counts()
    logical = max(1, logical_cores or detected_logical)
    physical = max(1, physical_cores or detected_physical)
    available = min(MAX_GLOBAL_CORES, max(1, physical - reserve_cores))

    sampled_idle = idle_physical_cores
    if adaptive and sampled_idle is None:
        sampled_idle = _idle_physical_cores(logical, physical)
    if adaptive and sampled_idle is not None:
        available = min(available, max(1, sampled_idle))

    requested_cap = available if requested_workers == 0 else min(requested_workers, available)
    useful_by_work = max(1, work_items // min_work_items_per_worker)
    workers = min(task_count, requested_cap, useful_by_work)

    if task_count < 2:
        workers = 1
        reason = "only one conversion task"
    elif work_items < min_work_items_per_worker * 2:
        workers = 1
        reason = "workload is below the parallel threshold"
    elif available < 2:
        workers = 1
        reason = "fewer than two safe physical cores are available"
    elif workers < 2:
        workers = 1
        reason = "parallel overhead would exceed the useful work"
    else:
        limit = "automatic" if requested_workers == 0 else f"requested={requested_workers}"
        if adaptive and sampled_idle is not None:
            reason = f"bounded flat pool ({limit}, sampled_idle_physical={sampled_idle})"
        else:
            reason = f"bounded flat pool ({limit})"

    return WorkerPlan(
        workers=workers,
        requested_workers=requested_workers,
        logical_cores=logical,
        physical_cores=physical,
        reserve_cores=reserve_cores,
        idle_physical_cores=sampled_idle,
        adaptive=adaptive,
        reason=reason,
    )


def choose_chunks_per_group(
    *,
    items_per_group: int,
    group_count: int,
    workers: int,
    requested_chunks: int,
    min_chunk_items: int,
) -> int:
    """Choose enough chunks to feed the pool without creating tiny tasks."""

    if items_per_group < 1 or group_count < 1:
        return 1
    if workers < 1:
        raise ValueError("workers must be positive")
    if requested_chunks < 0:
        raise ValueError("requested_chunks must be zero (auto) or positive")
    if min_chunk_items < 1:
        raise ValueError("min_chunk_items must be positive")

    maximum_chunks = max(1, math.ceil(items_per_group / min_chunk_items))
    if requested_chunks > 0:
        return min(requested_chunks, maximum_chunks)

    # Two tasks per worker gives reasonable load balancing for uneven chunks.
    target_tasks = max(group_count, workers * 2)
    automatic_chunks = max(1, math.ceil(target_tasks / group_count))
    return min(automatic_chunks, maximum_chunks)


def chunk_ranges(total_items: int, chunk_count: int) -> list[tuple[int, int]]:
    if total_items < 0:
        raise ValueError("total_items must be nonnegative")
    if chunk_count < 1:
        raise ValueError("chunk_count must be positive")
    if total_items == 0:
        return [(0, 0)]

    size = math.ceil(total_items / chunk_count)
    return [
        (start, min(total_items, start + size))
        for start in range(0, total_items, size)
    ]
