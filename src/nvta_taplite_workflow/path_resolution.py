from __future__ import annotations

from pathlib import Path


DTALITE_RELATIVE_DIR = Path("Outputs") / "DTALite"


def is_dtalite_workdir(path: Path) -> bool:
    """Return whether *path* is the conventional ``Outputs/DTALite`` folder."""

    return path.name.casefold() == "dtalite" and path.parent.name.casefold() == "outputs"


def resolve_dtalite_workdir(raw_path: str | Path) -> Path:
    """Resolve a scenario path to its effective DTALite working directory.

    Client applications provide the scenario root while keeping all DTALite
    inputs and outputs under ``<Scenario>/Outputs/DTALite``. Maintainer test
    folders may place those files directly in the supplied directory, so that
    directory remains the fallback when the conventional child does not exist.
    """

    path = Path(raw_path).expanduser().resolve()
    if is_dtalite_workdir(path):
        return path

    candidate = path / DTALITE_RELATIVE_DIR
    if candidate.is_dir():
        return candidate
    return path


def resolve_scenario_and_dtalite_paths(raw_path: str | Path) -> tuple[Path, Path]:
    """Return the scenario root and its effective DTALite working directory."""

    path = Path(raw_path).expanduser().resolve()
    if is_dtalite_workdir(path):
        return path.parent.parent, path
    return path, resolve_dtalite_workdir(path)


__all__ = [
    "DTALITE_RELATIVE_DIR",
    "is_dtalite_workdir",
    "resolve_dtalite_workdir",
    "resolve_scenario_and_dtalite_paths",
]
