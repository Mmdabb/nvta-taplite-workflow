# Hourly speed and approximate link volume

Normal postprocessing creates exactly two additional CSVs beside combined
performance: `link_speed_volume.csv` (jurisdiction) and
`link_speed_volume_regional.csv` (regional). Each has one row per directed
`(from_node_id, to_node_id)` pair across the modeled periods. Scopes follow the
existing roadway/TAZ filtering. Opposite directions remain separate. Exported
`link_id` comes from converted `link.csv`, independent of assignment-internal IDs.

## Time range and hourly schema

Read `demand_period_starting_hours` and `demand_period_ending_hours` from each
period's `settings.csv` when present. These actual assignment bounds take
precedence over configured ranges; conflicting ranges produce a log warning.
When settings is absent, use the configured `HHMM_HHMM` range. Missing both,
malformed settings, overlapping periods and spans over 24 hours fail clearly.
`None` ranges are supported by the scenario postprocessor and exporter.

The hourly grid begins exactly at the first period's start and ends exactly
at the last period's end, in supplied period order. It is not padded to a
calendar day. Example: 06:30-08:45 gives 06:30-07:30, 07:30-08:30 and
08:30-08:45. Bins can cross period boundaries. Real overnight ranges use
increasing hour labels such as 23:00-24:00 and 24:00-25:00. Unmodeled gaps
have blank bins; a partially covered bin summarizes only modeled portions.

The existing true-shape run has settings AM 06-09, MD 09-15, PM 15-19, NT
19-24. The converter truncates its configured NT 19-06 at midnight. Its export
therefore contains 18 hourly bins from 06:00 through 24:00. It does not create
00:00-06:00 samples or volumes.

Column order:

1. `link_id`, `from_node_id`, `to_node_id`, `link_type`, `free_speed` (mph),
   `capacity` (whole-link vehicles/hour), `lanes`.
2. All `spd_mph_START_END` columns in modeled order.
3. All `approx_volume_START_END` columns in the same order, in vehicles during
   that bin, including fractional vehicles. A partial bin is not extrapolated.

The seven static fields use the first configured period containing the link.
Actual period lanes, capacity and S3 parameters are used for each calculation.
Detailed per-period diagnostics are returned in memory; production does not
write audit companion CSVs. The validation tool saves its own diagnostics.

## Inputs and assumptions

| Quantity | Input priority |
|---|---|
| Assignment target V | Period performance `volume`; do not divide by lanes. |
| Native speeds | `spd_mph_HH:MM`; legacy `spd_HH:MM` assumes mph; `spd_kmph_HH:MM` converts by 1.609344. Prefer explicit mph at duplicate timestamps. |
| Free speed vf | Network `vdf_free_speed_mph`; performance `free_speed_mph`; network generic `free_speed / 1.609344` (km/h). If missing, derive from vc and m; finally speed p95 clipped to 35-85 mph, or 35 mph if no valid observations. Known network vf is preserved. |
| Lanes | Network `lanes`; assume 1 only if unavailable/invalid and record the source. |
| Whole-link hourly C | Performance `link_capacity` already accounts for lanes; else network per-lane `capacity * lanes`; else performance `lane_capacity * lanes`; else assume 1,800 veh/hour/lane times lanes. |
| Critical speed vc | Existing explicit S3 inputs `speed_at_capacity_mph`, `vc_mph`, `fd_vc_mph`, performance then network, if 0 < vc < vf; otherwise derive from supplied `s3_shape_m` / `s3_m`; otherwise use **0.75 vf**, matching the kernel default ratio. |
| S3 m | Supplied S3 shape if consistent, otherwise derive `2 ln(2) / ln(vf/vc)`. At vc/vf = 0.75, m is approximately 4.81884. Valid supplied vc is authoritative when vc and m conflict. |
| Critical density kc | C / vc, in vehicles/mile for the whole directed link. |

This change only adjusts the fallback ratio to 0.75. It does not introduce
new cutoff-field priority or interpret QVDF `vdf_s`, `vdf_n` or boundary speed
anchors as S3 shape parameters.

## Calculation: native increments first, hourly aggregation last

1. Select native speeds in each actual half-open period range. Infer the native
   grid from timestamp spacings and period boundaries (normally five minutes).
   Require a period-start sample. Missing timestamp columns *within the actual
   period* use that link's assigned period-average `speed_mph`, explicitly
   counted in diagnostics. Existing supplied cells are preserved.
2. Invert S3 separately for every native interval using whole-link capacity:

   `v* = min(v, 0.99 vf)`

   `r = max(vf / max(v*, 1 mph), 1.00001)`

   `k = kc * max(r^(m/2) - 1, 1e-8)^(1/m)`

   `q = min(v* k, C)` in whole-link vehicles/hour.

3. Convert flow to count: `u_i = q_i * dt_i_hours`. Sum U within that link/period.
4. Apply one common factor `R = V/U` to every native count: `a_i = R*u_i`.
   Correct only floating-point summation residue in the largest count.
5. Form hourly counts **after** this correction: sum a_i. An interval crossing
   a bin boundary contributes in proportion to its overlap duration. Native
   counts are assumed uniform within their native interval for this split.
6. Calculate hourly speed using the **duration-weighted harmonic mean**:

   `v_hour = sum(dt_i) / sum(dt_i / v_i)`.

   For equal five-minute samples this is `n / sum(1/v_i)`. Use overlap
   durations for partial intervals/bins. This is a travel-time-based summary;
   it is not volume weighted. A zero supplied speed makes harmonic speed zero;
   any nonfinite/negative supplied speed makes that hourly speed blank.
7. Combine period contributions within shared hourly bins, preserving harmonic
   denominators and already-scaled counts. Each period's total remains
   conserved even if an hour crosses its boundary. Such a shared hour cannot
   be disaggregated back into periods from the final CSV alone; diagnostics
   retain each period's hourly-contribution total for verification.

The S3 inverse and safeguards follow CBI fundamental_diagram.py at commit
`1ce571f80e34efa2afdde7ca266740ba057d0523`. The inverse is evaluated in log
form for stability. The 1,800 per-lane prior is used by CBI batch.py. The
fallback 0.75 ratio follows TAPLite.cpp at commit
`2048cf0579d159337fca5add5a50990927ea9682`, lines 7319-7326. S3 volume shaping
remains separate from the kernel's QVDF profile generation.

## Exceptional cases and verification

- V = 0 gives zero counts. Positive V with zero total raw flow uses allocation
  proportional to native duration, recorded as `uniform_duration_fallback`.
- Invalid existing speed cells receive zero raw S3 flow weight and are counted
  in diagnostics. Invalid assigned totals fail clearly.
- Final scaled flow may exceed capacity; clipping it again would violate the
  required period totals. Diagnostics count these intervals.
- Duplicate/unmatched directed pairs fail. A link absent from a period has no
  contribution to that period; bins with no contribution remain blank.
- Legacy outputs with no incremental speeds log a warning and skip the new
  exports, removing stale same-named main exports from earlier runs.

`python -m unittest tests.test_speed_volume -v` tests hourly origin, partial
hours, boundary crossings, settings lookup, midnight order, harmonic speed,
whole-link units, period scaling and exactly two normal output files.

`python scripts/validate_speed_volume.py --source EXISTING_RUN --output NEW_RUN`
stages input copies, runs normal postprocessing, rereads both CSVs and checks
every link/period total where bins align with period boundaries. It also checks
native-to-hourly conservation in diagnostics. It writes validation_summary.json,
sample CSVs and a validation_diagnostics subfolder only in that test directory,
and verifies unchanged source-file SHA256 hashes.
