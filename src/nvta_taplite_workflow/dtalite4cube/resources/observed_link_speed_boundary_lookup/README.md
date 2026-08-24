# Observed-link speed-boundary lookup

`observed_link_speed_boundaries.npy` is a sorted NumPy structured array keyed
by directed `(from_node_id, to_node_id)` pairs for the complete stable model
network.

For AM, MD, and PM it stores speed in mph at the exact assignment-period start
and end minute. Network conversion maps the selected pair to the optional
`qvdf_start_speed_mph` and `qvdf_end_speed_mph` link fields. Canonical TMC-link
winners use observed weekday-average speed; other links use stable-assignment
speed according to the hierarchy below. A boundary remains NaN only when the
corresponding period link is absent from the stable assignment, such as a
closed reversible direction.

The complete lookup uses this fixed hierarchy:

1. Existing post-QC CBI weekday-average anchors are retained for canonical
   node-pair winners already covered by the stable resource.
2. Remaining canonical winners use weekday-average INRIX speeds calculated
   directly from the shared regional 15-minute export at 06:00, 09:00, 15:00,
   and 19:00.
3. Every non-canonical network pair uses the stable assignment `speed_mph`:
   AM start is AM speed, the AM/MD boundary is their available-value mean, the
   MD/PM boundary is their available-value mean, and PM end is PM speed.

`generate_hybrid_speed_boundaries.py` rebuilds and validates this resource. It
does not run network conversion or TAPlite. When installation is requested, it
copies the previous lookup, completeness report, and metadata into the
timestamped output before replacing the live resource.

`boundary_completeness_report.csv` accompanies the lookup. For every selected
node pair and period it reports `both`, `start_only`, `end_only`, or `neither`
and identifies whether a blank came from a missing source row or a source speed
left blank by weekday-average QC.
