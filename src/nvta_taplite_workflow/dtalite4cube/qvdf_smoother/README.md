# Integrated QVDF Speed Smoother 1.0.0

This subpackage is the NVTA workflow integration of the TAPLite QVDF time-dependent speed
construction, rate enforcers, boundary smoothing, multiprocessing, and atomic
`link_performance.csv` write-back.

Normal users do not run it separately. `run_assignment.py` calls it after all
requested TAPLite periods succeed and before downstream postprocessing combines
the period link-performance files.

## Requirements

- Python 3.11
- Windows, Linux, or macOS
- `psutil` is installed by `setup_environment.bat` for automatic free-core
  allocation and memory reporting. Manual worker selection and the
  single-process fallback do not require it.

## Expected scenario structure

The scenario directory must contain one folder for every requested period.
Period names are not hard-coded, but the selected periods must be adjacent.

```text
assignment/
  am/
    settings.csv
    link.csv
    link_performance.csv
  md/
    settings.csv
    link.csv
    link_performance.csv
  pm/
    settings.csv
    link.csv
    link_performance.csv
  nt/
    settings.csv
    link.csv
    link_performance.csv
```

The actual start and end hours are read from each period's `settings.csv`.

## Normal production use

```powershell
python run_assignment.py `
  --scenario-dir "C:\path\to\scenario" `
  --output-dir "C:\path\to\assignment" `
  --qvdf-smoothing true `
  --qvdf-smoother-workers auto `
  --qvdf-smoother-backup true
```

The batch launcher accepts the same options. For exactly six smoother workers,
replace `auto` with `6`. Both automatic and manual selection are capped at 20
workers by the NVTA workflow.

For developer-only manual use from an installed environment:

```powershell
python -m nvta_taplite_workflow.dtalite4cube.qvdf_smoother `
  --scenario-dir "C:\path\to\assignment" `
  --periods am md pm nt `
  --workers auto `
  --write-back
```

Omit `--write-back` in the developer command for a computation-only validation run. The JSON report is
written to `qvdf_batch_report.json` inside the scenario directory unless a
different path is supplied with `--report`.

## Safety and output behavior

- Only `spd_mph_*` columns are replaced. `speed_mph`, `volume`, and all other
  logical CSV values are preserved and hashed before installation.
- Output speed values use nine decimal places by default so CSV rounding does
  not defeat the acceleration constraint.
- The program refuses write-back if any link fails or either the computed or
  serialized profile violates a constraint.
- All period outputs are validated before atomic installation.
- Backups are enabled by default and are named
  `link_performance.pre-qvdf-<timestamp>.csv`. Use `--no-backup` only when the
  complete scenario is already a disposable copy.
- If multiprocessing is unavailable, processing falls back to one worker and
  records the reason in the report.
- A UNC scenario uses an automatically cleaned database in the local system
  temporary folder; final results and the report stay on the scenario share.
  `NVTA_QVDF_TEMP_DIR` may select an approved absolute local scratch folder.

## Default enforcers

- Maximum five-minute speed change: 8 mph
- Rolling window: 3 five-minute intervals
- Maximum rolling average absolute change: 4 mph per interval
- Maximum acceleration/secant-slope change: 576 mph/hour²
- Monotone cubic Hermite smoothing across adjacent period boundaries
- One-sample feasibility look-ahead to prevent a legal increment from making
  the next increment impossible

Use `python -m nvta_taplite_workflow.dtalite4cube.qvdf_smoother --help` for all
developer CLI overrides and options.
