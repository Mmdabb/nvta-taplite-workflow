# NVTA TAPLite workflow

This project distributes the complete NVTA traffic-assignment workflow as one
versioned Python package. It includes network and demand conversion, calibrated
resources, QVDF parameter overrides, the TAPLite assignment sequence, QVDF
speed smoothing, aggregation, postprocessing, and run logging.

The project-specific orchestration is separate from the native assignment
engine. This package depends on the verified
`taplite4mpo-pre-release==0.4.0rc1` wheel from PyPI.

## Installation

The supported client setup is `client\setup_environment.bat`. Every setup run
recreates the `dtalite_pipeline` Miniforge environment and installs or upgrades
this package from PyPI. For a quick in-place package update, use
`client\update_environment.bat`.

During the release-candidate phase, a direct update is:

```powershell
python -m pip install --upgrade --pre --only-binary=:all: nvta-taplite-workflow
```

Rerunning `setup_environment.bat` is the clean-refresh path: it removes and
recreates `dtalite_pipeline`. `update_environment.bat` only updates the
installed package in the existing environment.

## Permanent client launchers

Keep the small `client` folder with the project. The Python launchers are shown
first because they are the clearest interface; they require either an active
`dtalite_pipeline` environment or `conda run` as shown below.

### Python launchers

```powershell
conda run --no-capture-output -n dtalite_pipeline python run_assignment.py `
  --scenario-dir "C:\path\to\scenario"

conda run --no-capture-output -n dtalite_pipeline python run_postprocessing.py `
  "C:\path\to\scenario"
```

If `dtalite_pipeline` is already active, omit the `conda run ...` prefix:

```powershell
python run_assignment.py --scenario-dir "C:\path\to\scenario"
python run_postprocessing.py "C:\path\to\scenario"
```

### Batch launchers

The batch launchers locate Miniforge themselves and do not require `conda
activate` or `conda init`:

```bat
run_assignment.bat --scenario-dir "C:\path\to\scenario"
run_postprocessing.bat "C:\path\to\scenario"
```

Assignment defaults to network conversion, demand conversion, all four daily
periods, TAPLite assignment, calibrated QVDF override, QVDF smoothing, and
aggregation. Disable a stage explicitly only for a diagnostic run, for example
`--network-conversion false --demand-conversion false --dtalite-assignment
false`.

## Commands

```powershell
python -m nvta_taplite_workflow assignment --scenario-dir "C:\path\to\scenario"
python -m nvta_taplite_workflow postprocess "C:\path\to\scenario"
python -m nvta_taplite_workflow doctor
python -m nvta_taplite_workflow version
```

The files in `client` are permanent thin launchers. They contain no workflow
logic and do not need to change when conversion, assignment, smoothing, or
postprocessing internals change.

## Python API

```python
from nvta_taplite_workflow import (
    AssignmentConfig,
    PostprocessingConfig,
    run_assignment,
    run_postprocessing,
)
```

The command-line interface is the preferred compatibility boundary. New
internal stages use package defaults, so existing client commands keep working.

## Resources and output safety

All lookup tables and dictionaries are immutable package data. Runtime outputs,
logs, conversion caches, QVDF backups, and audit manifests are written only to
the selected scenario or output location.

The QVDF smoother normally stages its transient SQLite database beside local
outputs. When the output resolves to a UNC share, the database is staged in the
user's local temporary directory because SQLite URI authorities and network
filesystem locking are unreliable. It is deleted after the run. Final results,
backups, reports, and logs remain on the selected scenario share. Set
`NVTA_QVDF_TEMP_DIR` to an approved local scratch folder when company policy
requires a specific temporary location.

Every run records the installed workflow version, TAPLite version, and resource
provenance so an environment can be reproduced or rolled back.

## Development

```powershell
python -m pip install -e .
python -m unittest discover -s tests -p "test_*.py"
python -m build
python -m twine check dist\*
```

Release details are in `RELEASING.md`.
