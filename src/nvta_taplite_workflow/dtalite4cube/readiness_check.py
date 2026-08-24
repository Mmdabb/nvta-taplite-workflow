"""Optional GMNS readiness checks for generated DTALite period folders."""

from __future__ import annotations

import contextlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    log_path: Path
    summary_path: Path
    errors: int | None = None
    warnings: int | None = None
    passed: int | None = None


def _count_from_log(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _parse_counts(log_text: str) -> tuple[int | None, int | None, int | None]:
    errors = _count_from_log(r"errors?\D+(\d+)", log_text)
    warnings = _count_from_log(r"warnings?\D+(\d+)", log_text)
    passed = _count_from_log(r"(?:pass|passed)\D+(\d+)", log_text)
    return errors, warnings, passed


def run_gmns_readiness_check(period_dir: Path) -> ReadinessResult:
    period_dir = Path(period_dir)
    log_path = period_dir / "gmns_readiness.log"
    summary_path = period_dir / "gmns_readiness_summary.md"
    buffer = io.StringIO()
    status = "complete"

    previous_cwd = Path.cwd()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            import gmns_ready as gr  # type: ignore[import-not-found]

            print(f"Running gmns_ready.quick_check() in {period_dir}")
            os.chdir(period_dir)
            gr.quick_check()
        except ImportError as exc:
            status = "not_installed"
            print("gmns_ready is not installed; skipping GMNS readiness check.")
            print(str(exc))
        except Exception as exc:  # pragma: no cover - depends on external checker behavior
            status = "failed"
            print("gmns_ready.quick_check() failed.")
            print(str(exc))
        finally:
            os.chdir(previous_cwd)

    log_text = buffer.getvalue()
    errors, warnings, passed = _parse_counts(log_text)
    log_path.write_text(log_text, encoding="utf-8")
    summary_path.write_text(
        "\n".join(
            [
                "# GMNS Readiness Summary",
                "",
                f"- Status: {status}",
                f"- Errors: {errors if errors is not None else 'not reported'}",
                f"- Warnings: {warnings if warnings is not None else 'not reported'}",
                f"- Passed: {passed if passed is not None else 'not reported'}",
                f"- Full log: {log_path.name}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return ReadinessResult(status, log_path, summary_path, errors, warnings, passed)
