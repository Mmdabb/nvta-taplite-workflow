from __future__ import annotations

import atexit
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, TextIO


class TeeTextIO:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary
        self.encoding = getattr(primary, "encoding", None)
        self.errors = getattr(primary, "errors", None)

    def write(self, text: str) -> int:
        written = self.primary.write(text)
        self.secondary.write(text)
        return written

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()

    def isatty(self) -> bool:
        return self.primary.isatty()

    def fileno(self) -> int:
        return self.primary.fileno()

    def writable(self) -> bool:
        return True


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "workflow").is_dir() and (candidate / "test_networks").is_dir():
            return candidate
    return current


def _unique_log_dirs(candidates: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = Path(candidate).expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def install_root_log_capture(
    name: str,
    *,
    root: Path | None = None,
    log_dirs: Iterable[Path] | None = None,
) -> Path:
    """Tee stdout/stderr to the first writable candidate log directory."""

    if log_dirs is None:
        project_root = root or find_project_root()
        candidates = [project_root / "logs"]
    else:
        candidates = [*log_dirs]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    last_error: OSError | None = None
    log_path: Path | None = None
    latest_path: Path | None = None
    log_file: TextIO | None = None
    latest_file: TextIO | None = None
    selected_index = 0
    unique_candidates = _unique_log_dirs(candidates)
    for selected_index, log_dir in enumerate(unique_candidates):
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{name}_{timestamp}.log"
            latest_path = log_dir / f"{name}_latest.log"
            log_file = log_path.open("w", encoding="utf-8", errors="replace")
            try:
                latest_file = latest_path.open("w", encoding="utf-8", errors="replace")
            except OSError:
                log_file.close()
                raise
            break
        except OSError as exc:
            last_error = exc
            log_path = None
            latest_path = None
            log_file = None
            latest_file = None
    else:
        attempted = ", ".join(str(path) for path in unique_candidates)
        raise OSError(f"Could not create a workflow log in: {attempted}") from last_error

    assert log_path is not None
    assert latest_path is not None
    assert log_file is not None
    assert latest_file is not None

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeTextIO(original_stdout, log_file)  # type: ignore[assignment]
    sys.stderr = TeeTextIO(TeeTextIO(original_stderr, log_file), latest_file)  # type: ignore[assignment]
    sys.stdout = TeeTextIO(sys.stdout, latest_file)  # type: ignore[assignment]

    print(f"[INFO] Root workflow log: {log_path}")
    if selected_index > 0:
        print(
            "[WARN] Preferred run log location was not writable; "
            f"using workflow-package fallback: {log_path.parent}"
        )

    def close_log_files() -> None:
        print(f"[INFO] Root workflow log saved: {log_path}")
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_file.close()
        latest_file.close()

    atexit.register(close_log_files)
    return log_path
