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
# The standalone transfer commands below remain available for maintainer diagnostics.
python -m nvta_taplite_workflow transfer --base-network "C:\path\to\2025.shp" `
  --future-network "C:\path\to\2050.shp" --method simple `
  --output-dir "C:\path\to\transfer-results"
python -m nvta_taplite_workflow transfer --base-network "C:\path\to\2025.shp" `
  --future-network "C:\path\to\2050.shp" --method framework `
  --corridor-root "C:\path\to\cbi\corridors" `
  --output-dir "C:\path\to\transfer-results"
python -m nvta_taplite_workflow postprocess "C:\path\to\scenario"
python -m nvta_taplite_workflow doctor
python -m nvta_taplite_workflow version
```

### Future-network QVDF transfer

Future-network transfer is automatic whenever assignment runs with network
conversion enabled (the default). The client only runs the normal assignment
command. The workflow selects the network shapefile, fingerprints its full
contents, and creates or reuses a matching transfer set under:

```text
<folder-containing-the-shapefile>\NVTA_Attribute_Transfer\sets\network-<fingerprint>
```

If any network content changes, a new set is created and retained beside the
older set. Assignment is then bound to the two dictionaries in the matching
set, so manually supplied stale dictionary paths cannot be used during network
conversion. `active_transfer.json` identifies the set used by the latest run.

The standalone `transfer` command remains available for maintainer diagnostics
and creates the same assignment-ready dictionaries and audit evidence.

- `--method simple` copies exact directed 2025 node pairs. Missing pairs are
  written to `missing_2025_node_pairs.csv` and receive QVDF values from their
  link type. No other attributes are inferred.
- `--method framework` adds constrained directed replacement-path recovery and
  high-confidence geometry recovery. Lower-confidence geometry is never used
  for link-level transfer. When `--corridor-root` is provided, the next layer
  uses only accepted corridor fits (`sensor_uid=all`, `status=ok`) and requires
  spatial confidence, direction alignment, and corridor matched-link-type
  compatibility. Street name is supporting evidence, not a sufficient match.
  The final layer remains the safe link-type fallback.

Each run writes the complete `future_qvdf_node_pair_overrides.npy`, a CSV and
GeoPackage crosswalk, the missing-pair log, and a JSON summary. It also creates
`expanded_node_pair_qvdf.csv` beside the log and reuses that run-local expansion
when all input fingerprints still match. Corridor fits supply `n`, `s`, `cp`,
`cd`, `alpha`, and `beta`; `PLF`/`QDF` and unavailable corridor periods retain
the link-type fallback. The separate `future_speed_t_overrides.npy` carries
period speed anchors and complete T0/T2/T3 triplets only through exact,
replacement-path, or accepted high-confidence geometry lineage. Corridor and
link-type fallback rows remain blank. Automatic assignment selects both files;
the client does not need to pass their paths.

The packaged base-year QVDF and speed/T resources are never modified or
expanded in place. They are treated as read-only, checked by hash before and
after processing, and cannot share paths with the generated future dictionaries.

#### Supported scope and future-network editing rules

The transfer command is not tied to the year 2050. It can process any
future-year network that has unique directed `A`/`B` node pairs, valid line
geometry and CRS information, and numeric `ATYPE`/`FTYPE` values. It produces
complete AM, MD, and PM QVDF coverage (`plf`, `qdf`, `n`, `s`, `cp`, `cd`,
`alpha`, and `beta`) for every future link. AM, MD, and PM speed anchors and
T0/T2/T3 are transferred only when an accepted base-link lineage exists. NT
retains the normal assignment workflow's mapped values. A complete QVDF output
dictionary does not mean that every new link received link-specific base
calibration: the crosswalk records whether each value came from a base link,
an accepted corridor fit, or the link-type fallback.

Do:

- Preserve stable node IDs and directed `A` to `B` orientation for physically
  unchanged links.
- When splitting a calibrated link, keep a connected directed path between
  the original endpoints, use new intermediate node IDs, and retain realistic
  geometry, street name, `ATYPE`, and `FTYPE` attributes.
- Give genuinely new or substantially realigned facilities new node pairs.
- Keep geometries spatially accurate and use a valid CRS. The 2025 NVTA base
  network is in US survey feet; spatial matching thresholds are evaluated in
  that base CRS after the future network is reprojected.
- Keep each scenario/year in its own network folder and inspect the active
  set's `attribute_transfer_crosswalk.csv`, `missing_2025_node_pairs.csv`, and
  every row with `review_flag=1` before relying on a new network.
- Keep the scenario folder writable so the workflow can maintain its local
  `NVTA_Attribute_Transfer` folder.

Don't:

- Do not reuse an existing directed node pair for a different physical link,
  corridor, or alignment. Exact `A`/`B` is intentionally the strongest match
  and will retain the old calibration.
- Do not reverse `A` and `B`, move a link to another corridor, or make a major
  realignment while expecting the old link-specific calibration to remain
  valid. Model it as a new directed pair and review its fallback instead.
- Do not merge multiple independently calibrated links into one link without
  manual review; the automated framework has explicit split-path recovery but
  no equivalent many-to-one calibration rule.
- Do not change `ATYPE`/`FTYPE` merely to force a match. They are both matching
  evidence and the source of the final safety fallback.
- Do not treat street name alone as corridor membership. Corridor recovery
  also requires spatial confidence, direction alignment, and compatible link
  types.
- Do not fill speed anchors or T0/T2/T3 from corridor or link type. Without
  accepted link-level lineage, blank values intentionally select TAPlite's
  analytical/free-speed behavior.
- Do not point transfer output at the packaged base dictionary, edit that base
  resource, or reuse a stale scenario cache after network/configuration edits.
  Use a new output directory or explicitly request a cache rebuild.

Postprocessing writes two daily link-performance files and two matching
statistics files under the DTALite output folder:

- `link_performance_combined_processed.csv` and `statistics_data.csv` retain
  the established NVTA jurisdiction/TAZ-range scope.
- `link_performance_combined_processed_regional.csv` and
  `statistics_data_regional.csv` cover all regional roadway links without the
  NVTA jurisdiction/TAZ-range filter.

Both postprocessed link-performance files include the source network's
`STREETNAME` and full WKT `geometry` fields.

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
