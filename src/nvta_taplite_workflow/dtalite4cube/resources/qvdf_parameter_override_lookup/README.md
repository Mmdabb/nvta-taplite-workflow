# Stable-OD auto-calibrated node-pair QVDF parameter overrides

`qvdf_node_pair_overrides.npy` is the refined-volume-floor stable-OD
auto-calibrated dictionary promoted on August 23, 2026. It contains 49,336
directed `(from_node_id, to_node_id)` keys and period-specific PLF, QDF, N, S,
CP, CD, alpha, and beta values for AM, MD, and PM. Its SHA-256 is
`CFC0913E508D429D9DE3A9F63B8545AB1639F4225B75C6DF6085C55A52AF313F`.

The workflow first completes its normal network mapping (including the stable
network QVDF, observed PLF, observed congestion timing, and speed anchors),
then overwrites these eight QVDF fields by directed node pair. The overlay
requires complete coverage, retains `vdf_type=2` and the requested
`qvdf_profile_mode`, backs up every pre-overlay `link.csv`, and writes an
auditable manifest before assignment begins.

`source_manifest.json` records the calibration source, coverage, guardrail
status, dictionary hash, and per-period parameter provenance.
