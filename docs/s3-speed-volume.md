# Incremental speed and approximate link volume

Postprocessing automatically writes `link_speed_volume.csv` and
`link_speed_volume_regional.csv` beside the combined performance files under
the resolved DTALite folder (`<Scenario>/Outputs/DTALite`, or the supplied
folder when that child does not exist). Both have matching `_audit.csv` files.
Their scopes are exactly those of the two existing combined files: roadway
links, with or without the established NVTA TAZ filter.

## Output contract

There is one row per directed `(from_node_id, to_node_id)` pair across all
configured periods. Opposite directions are separate. The exported `link_id`
comes from the first configured period's converted `link.csv`, rather than an
assignment-internal ID. A link absent from a period has blank cells for that
period. Duplicate or unmatched directed pairs raise a clear error.

Columns are ordered as follows:

1. `link_id`, `from_node_id`, `to_node_id`, `link_type`.
2. `free_speed` **in mph**, `capacity` **in whole-link vehicles/hour**, `lanes`.
3. All `spd_mph_HH:MM` columns, sorted by clock time.
4. All `approx_volume_HH:MM` columns in the same order, containing fractional
   vehicles during that interval (not vehicles/hour or vehicles/lane).

Static attributes describe the first configured period containing a link.
Effective attributes for every period are retained in the audit. For example,
AM with three lanes and MD with two lanes uses the corresponding capacity in
each period's FD, even though the daily row lists the AM static attributes.

## Sources and precedence

Read each period's `link.csv` and join it to the processed performance rows by
normalized directed node pair, independent of `link_id` changes.

| Quantity | Priority / calculation |
|---|---|
| Assigned period total | Period `link_performance.csv` column `volume`, retained in combined performance. Never divide it by lanes. |
| Speeds | Period-owned `spd_mph_HH:MM`; legacy `spd_HH:MM` assumes mph; `spd_kmph_HH:MM` is converted using 1.609344 km/h per mph. Prefer explicit mph when both exist. |
| Free speed vf | `link.csv:vdf_free_speed_mph`; performance `free_speed_mph`; `link.csv:free_speed / 1.609344` (generic converter field is km/h). If missing, derive from supplied vc and m. Finally use speed p95 clipped to 35-85 mph, or 35 mph when no valid speed is available. Known network free speeds are not clipped, including low-speed urban links. |
| Lanes | `link.csv:lanes`; assume one only if missing/invalid, and identify this in the audit. |
| Whole-link capacity C | Performance `link_capacity` already in veh/hour; else `link.csv:capacity * lanes`; else performance `lane_capacity * lanes`; else assume 1,800 veh/hour/lane times lanes. Explicit capacity is never multiplied by lanes twice. |
| Critical speed vc | Performance `speed_at_capacity_mph`, `vc_mph`, `fd_vc_mph`, then same named network fields, provided 0 < vc < vf. Otherwise derive from supplied positive `s3_shape_m` or `s3_m`. Otherwise use 0.70 vf. |
| S3 shape m | Supplied `s3_shape_m` or `s3_m`, performance then network; derive missing m as 2 ln(2) / ln(vf/vc). When supplied m and vc conflict, vc is authoritative and m is recomputed. |
| Critical density kc | C / vc, in vehicles/mile for the whole link. |

QVDF `cutoff_speed_mph`, `vdf_s`, `vdf_n` and `qvdf_start/end_speed_mph` are
not interpreted as S3 critical speed or S3 shape. Those parameters have
different meanings. No new FD calibration is claimed from speed data alone.

## Calculation, independently for each link and period

The reference is CBI `cbi/src/cbi/fundamental_diagram.py`, functions
`s3_speed`, `inverse_s3_flow`, and `synthetic_s3_context`, verified at commit
`1ce571f80e34efa2afdde7ca266740ba057d0523` (file blob
`80b2efedcda49293ffbd355aef277f7fa85ee72c`). Its 1,800 veh/hour/lane prior is
also used in `cbi/src/cbi/batch.py` for the speed-only context.

1. Select speed columns owned by the configured period. Periods include their
   start and exclude their end. NT 19:00-06:00 wraps across midnight.
2. Infer a regular time grid using the greatest common divisor of timestamp
   spacings and the final period boundary. A single available sample uses a
   five-minute grid (or the period duration if shorter). Require a sample at
   the period start. This normally gives five-minute intervals, with each
   sample representing its interval through the next grid timestamp.
   Missing timestamp columns are filled with the same link's assigned period
   average `speed_mph`; `imputed_speed_intervals` and `imputation_source`
   explicitly record this assumption. For example, NT source speeds ending
   at 23:55 require 72 average-speed intervals for 00:00-05:55. Available
   source speed cells are preserved. This provides 288 intervals for the
   normal full-day configuration without stretching the 23:55 sample over
   six hours. Each interval's duration is derived from this completed grid.
3. Resolve vf, vc, m, C and kc using the table above. With S3,
   `v(k) = vf / [1 + (k/kc)^m]^(2/m)` and `C = kc * vc`.
4. Invert speed using CBI safeguards: `v* = min(v, 0.99 vf)`,
   `r = max(vf / max(v*, 1 mph), 1.00001)`,
   `k = kc * max(r^(m/2) - 1, 1e-8)^(1/m)`,
   `q = min(v* * k, C)` in whole-link vehicles/hour. The implementation uses
   an equivalent log expression to avoid exponent overflow. Saved speed
   values themselves are not changed by this inversion clipping.
5. Compute raw incremental volume `u_i = q_i * dt_i_hours` and total `U`.
6. For assigned period volume V, calculate `R = V / U` and save
   `approx_volume_i = R * u_i`. Use the **same R** for every interval in that
   link/period. Apply a machine-precision summation correction to the largest
   interval; do not round volumes to integer vehicles.
7. Repeat separately for AM, MD, PM and NT, then combine the intervals into
   one row. There is no daily/global correction factor.

## Exceptional cases and limitations

- V = 0: every interval count is zero, regardless of speed.
- Positive V with U = 0 (all zero/invalid speed): distribute V proportional
  to interval duration; audit status `uniform_duration_fallback` and blank
  scale factor. This is an explicit fallback, not an S3-derived profile.
- Missing/nonfinite/negative speed: keep the supplied speed cell, use zero
  raw weight at that interval, and count it in `invalid_speed_intervals`.
  If other valid intervals have positive flow, allocate to those intervals.
  This rule concerns cells in existing timestamp columns; entirely missing
  timestamp columns use the explicitly audited period-average fill above.
- Nonfinite or negative assigned volume: fail rather than invent a total.
- Overlapping configured periods, missing period-start sample, duplicate or
  unmatched directed pairs: fail with a clear message.
- No incremental speed columns at all (legacy outputs): log a warning and
  skip the additional exports. Remove stale same-named speed/volume exports
  from a prior run. Existing combined files/statistics are still generated.
- A period present in combined performance with no owned speed columns fails
  clearly. Periods excluded by the existing preprocessing rules remain
  excluded; this export does not change that filtering.
- Rescaling can make final implied flow exceed C. Re-clipping would violate
  the required period totals, so final counts are not clipped; the audit
  counts `above_capacity_intervals`. These are estimated temporal allocations,
  not new assignment results or independently observed counts.
- Free speed and critical speed alone do not identify capacity. A capacity
  input or explicit assumption is necessary. C cancels algebraically from R
  when the same shape is normalized, but it is still resolved at link level
  for dimensional correctness, raw volumes and auditing.

The CBI `conserve_flow_from_speed` routine uses an additive capacity-constrained
projection. This export deliberately uses the requested multiplicative period
scaling instead, so every interval within a period shares one scale factor.

## Validation and reproduction

Run `python -m unittest tests.test_speed_volume -v` for equation, units,
whole-link capacity, independent period scaling, midnight, parameter fallback,
identity mismatch, audit and runner integration checks. To reprocess an
existing assignment in a separate test folder:

```powershell
python scripts/validate_speed_volume.py --source path/to/existing/run --output path/to/new/test/run
```

The validator stages input copies, executes normal postprocessing, rereads
both wide CSVs, checks every exported link/period total within absolute 1e-8
and relative 1e-12 tolerance, and writes `validation_summary.json` with source
SHA256 hashes, output counts, conservation errors, parameter-source counts,
and representative examples. It does not rerun assignment.

Reference: https://github.com/Mmdabb/CBI-TAPLite-Pipeline/blob/1ce571f80e34efa2afdde7ca266740ba057d0523/cbi/src/cbi/fundamental_diagram.py
