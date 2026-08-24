"""Filesystem helpers for DTALite workflow internals."""

from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def resolved_workers(requested_workers: int, task_count: int) -> int:
    """Resolve a bounded worker count for independent tasks."""
    if requested_workers < 0:
        raise ValueError("requested_workers must be zero (auto) or positive")
    if task_count <= 1:
        return 1
    if requested_workers > 0:
        return min(requested_workers, task_count)
    available = max(1, (os.cpu_count() or 2) - 1)
    return min(available, task_count)


def _copy_file(
    task: tuple[Path, Path],
    *,
    preserve_metadata: bool,
) -> tuple[Path, Path]:
    source, target = task
    target.parent.mkdir(parents=True, exist_ok=True)
    if preserve_metadata:
        shutil.copy2(source, target)
    else:
        shutil.copyfile(source, target)
    return source, target


def copy_files_parallel(
    file_pairs: Iterable[tuple[Path, Path]],
    *,
    workers: int = 1,
    preserve_metadata: bool = True,
) -> list[tuple[Path, Path]]:
    """Copy independent files with a bounded thread pool.

    Results preserve input ordering. Copies whose source and destination resolve
    to the same path are skipped.
    """
    tasks: list[tuple[Path, Path]] = []
    target_paths: set[Path] = set()
    for raw_source, raw_target in file_pairs:
        source = Path(raw_source)
        target = Path(raw_target)
        if source.resolve() == target.resolve():
            continue
        resolved_target = target.resolve()
        if resolved_target in target_paths:
            raise ValueError(f"Duplicate copy destination: {target}")
        target_paths.add(resolved_target)
        tasks.append((source, target))

    worker_count = resolved_workers(workers, len(tasks))
    if worker_count == 1:
        return [
            _copy_file(task, preserve_metadata=preserve_metadata)
            for task in tasks
        ]

    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="file-copy",
    ) as executor:
        return list(
            executor.map(
                partial(
                    _copy_file,
                    preserve_metadata=preserve_metadata,
                ),
                tasks,
            )
        )


def safe_rmtree(path: Path, *, attempts: int = 3, delay_seconds: float = 0.5) -> bool:
    """Remove a directory tree with short retries for Windows file handles."""
    path = Path(path)
    if not path.exists():
        return True

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)

    logger.warning("Unable to remove directory after %s attempts: %s (%s)", attempts, path, last_error)
    return False
